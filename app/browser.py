"""Site adapter. Response schemas/selectors require verification against a logged-in site."""
import asyncio
import json
from pathlib import Path
import time
from datetime import datetime, timezone
from urllib.parse import urlparse


class NeedsAttention(RuntimeError):
    pass


class CommentPageUnavailable(NeedsAttention):
    """单个作品页暂时无法定位评论区。"""
    pass


class UncertainSend(RuntimeError):
    pass


class SkippedCustomer(RuntimeError):
    pass


def is_douyin_host(hostname):
    """Accept Douyin's first-party API subdomains without matching lookalikes."""
    hostname = (hostname or '').lower().rstrip('.')
    return hostname == 'douyin.com' or hostname.endswith('.douyin.com')


def parse_comments(payload, video):
    result = []
    for c in payload.get('comments') or []:
        if str(c.get('reply_id', '0')) not in ('0', '', 'None'):
            continue
        u = c.get('user') or {}
        if not c.get('cid') or not u.get('sec_uid') or not c.get('text'):
            continue
        result.append(dict(id=str(c['cid']),uid=u['sec_uid'],name=u.get('nickname',''),text=c['text'],
            comment_time=datetime.fromtimestamp(int(c.get('create_time',0)),timezone.utc).isoformat(),
            video_id=str(video['aweme_id']),video_title=video.get('desc',''),blogger=(video.get('author') or {}).get('nickname','')))
    return result


def reached_video_cutoff(packets, cutoff):
    """Stop at the end of a date-ordered batch; pinned items cannot set the boundary."""
    if not packets:
        return False
    dates = []
    for video in packets[-1].get('aweme_list') or []:
        if str(video.get('is_top', 0)).lower() in ('1', 'true'):
            continue
        try:
            timestamp = int(video.get('create_time', 0))
        except (TypeError, ValueError):
            return False
        if timestamp <= 0:
            return False
        dates.append(timestamp)
    # A batch with an old pinned item before recent videos must not stop scanning.
    return bool(dates) and dates[-1] < cutoff and all(a >= b for a,b in zip(dates,dates[1:]))


def is_send_icon(candidate, editor_rect):
    """Conservative fallback for the pink icon beside the message composer."""
    r = candidate['rect']
    rgb = candidate['rgb']
    return (candidate['enabled'] and candidate['clickable'] and candidate['has_icon']
        and 20 <= r['width'] <= 64 and 20 <= r['height'] <= 64
        and rgb is not None and rgb[0] >= 220 and rgb[1] <= 100 and 45 <= rgb[2] <= 180
        and (len(rgb)<4 or rgb[3]>=0.9)
        and abs(r['width']-r['height']) <= 12
        and editor_rect['right']-85 <= r['x'] <= editor_rect['right']+35
        and editor_rect['bottom']-85 <= r['y'] <= editor_rect['bottom']+45)


ICON_CANDIDATES = r"""editor => {
    const rect = el => {const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom}};
    const path = el => {
        const parts=[];
        while(el && el.nodeType===1){
            let index=1; for(let sib=el.previousElementSibling;sib;sib=sib.previousElementSibling)index++;
            parts.unshift(el.tagName.toLowerCase()+':nth-child('+index+')');el=el.parentElement;
        }
        return parts.join(' > ');
    };
    const nodes = new Set();
    for(const icon of document.querySelectorAll('svg')){
        let el=icon;
        for(let i=0;el && i<4;i++,el=el.parentElement)nodes.add(el);
    }
    const candidates=[];
    for(const el of nodes){
        const style=getComputedStyle(el),r=rect(el);
        if(style.visibility!=='visible'||style.display==='none'||Number(style.opacity)<0.9||r.width<20||r.width>64||r.height<20||r.height>64)continue;
        const match=style.backgroundColor.match(/[\d.]+/g);
        const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
        if(!hit || !(el===hit || el.contains(hit)))continue;
        candidates.push({selector:path(el),rect:r,rgb:match?match.map(Number):null,
            enabled:!el.matches(':disabled,[aria-disabled="true"]'),
            clickable:el.matches('button,[role="button"]')||style.cursor==='pointer',
            has_icon:el.tagName.toLowerCase()==='svg'||!!el.querySelector('svg')});
    }
    return {editor:rect(editor),candidates};
}"""


SEND_DIAGNOSTICS = r"""editor => {
    const e=editor.getBoundingClientRect();
    const nearby=[];
    const followControls=[...document.querySelectorAll('body *')].filter(el=>
        ['关注','已关注','互相关注','相互关注'].includes(el.textContent.trim()) &&
        ![...el.children].some(c=>c.textContent.trim()===el.textContent.trim())).map(el=>{
            const r=el.getBoundingClientRect();return {text:el.textContent.trim(),tag:el.tagName,classes:el.getAttribute('class'),
                rect:{x:r.x,y:r.y,width:r.width,height:r.height},visibility:getComputedStyle(el).visibility};
        }).filter(el=>el.rect.width>0&&el.rect.height>0);
    for(const el of document.querySelectorAll('body *')){
        const r=el.getBoundingClientRect();
        if(r.width<8||r.width>150||r.height<8||r.height>150)continue;
        if(r.right<e.left||r.left>e.right+100||r.bottom<e.top-30||r.top>e.bottom+150)continue;
        const s=getComputedStyle(el);
        if(s.display==='none'||s.visibility==='hidden')continue;
        nearby.push({tag:el.tagName,classes:el.getAttribute('class'),role:el.getAttribute('role'),
            ariaLabel:el.getAttribute('aria-label'),title:el.getAttribute('title'),
            rect:{x:r.x,y:r.y,width:r.width,height:r.height},background:s.backgroundColor,
            backgroundImage:s.backgroundImage.startsWith('url(')?'[image]':s.backgroundImage,
            fill:s.fill,color:s.color,cursor:s.cursor,
            before:{content:getComputedStyle(el,'::before').content,background:getComputedStyle(el,'::before').backgroundColor},
            disabled:el.matches(':disabled,[aria-disabled="true"]')});
        if(nearby.length>=150)break;
    }
    return {editor:{tag:editor.tagName,classes:editor.getAttribute('class'),rect:{x:e.x,y:e.y,width:e.width,height:e.height}},
        viewport:{width:innerWidth,height:innerHeight},nearby,followControls};
}"""


COMMENT_PANEL_SCROLL = r"""panel => {
    // Douyin renders the list inside one or more wrappers. Scroll the nearest
    // real overflow container instead of sending a wheel event to the page:
    // once a long list reaches its end, wheel events can bubble to the video
    // feed behind the panel and leave the tab on a blank/black feed item.
    let scroller=panel;
    while(scroller && scroller!==document.documentElement){
        const style=getComputedStyle(scroller);
        const overflow=style.overflowY;
        if(['auto','scroll','overlay'].includes(overflow) &&
                scroller.scrollHeight>scroller.clientHeight+1)break;
        scroller=scroller.parentElement;
    }
    if(!scroller || scroller===document.documentElement){
        return {found:false,moved:false};
    }
    const before=scroller.scrollTop;
    const distance=Math.max(400,Math.floor(scroller.clientHeight*0.8));
    scroller.scrollTop=Math.min(before+distance,scroller.scrollHeight-scroller.clientHeight);
    return {found:true,moved:scroller.scrollTop>before};
}"""


COMMENT_READY_CHECKS = 20
COMMENT_READY_INTERVAL = 0.5
COMMENT_CAPTURE_ATTEMPTS = 3
PROFILE_CLICK_TIMEOUT = 4000


MESSAGE_PANEL_CLOSE = r"""title => {
    const visible = el => {
        const r=el.getBoundingClientRect(),s=getComputedStyle(el);
        return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)>0;
    };
    if(!visible(title) || title.textContent.trim()!=='消息')return {panel:false,clicked:false};
    const tr=title.getBoundingClientRect();
    let header=null;
    for(let el=title;el&&el!==document.body;el=el.parentElement){
        const r=el.getBoundingClientRect();
        // The open message drawer has a wide header whose title is close to
        // its left edge. This excludes the identically named top-nav item.
        if(r.width>=260&&r.width<=Math.min(900,innerWidth*.8)&&r.height>=40&&r.height<=160&&
                tr.left-r.left>=0&&tr.left-r.left<=100){header=el;break;}
    }
    if(!header)return {panel:false,clicked:false};
    const hr=header.getBoundingClientRect(),seen=new Set(),candidates=[];
    for(const node of header.querySelectorAll('button,a,[role="button"],svg')){
        let target=node;
        while(target.parentElement&&target.parentElement!==header){
            const s=getComputedStyle(target);
            if(target.matches('button,a,[role="button"]')||typeof target.onclick==='function'||s.cursor==='pointer')break;
            target=target.parentElement;
        }
        if(seen.has(target)||!visible(target))continue;
        seen.add(target);
        const r=target.getBoundingClientRect(),s=getComputedStyle(target);
        const clickable=target.matches('button,a,[role="button"]')||typeof target.onclick==='function'||s.cursor==='pointer';
        if(!clickable||r.width<12||r.width>72||r.height<12||r.height>72)continue;
        if(r.left<title.getBoundingClientRect().right+30||r.right>hr.right+2||r.top<hr.top-2||r.bottom>hr.bottom+2)continue;
        candidates.push({target,x:r.right});
    }
    if(!candidates.length)return {panel:true,clicked:false};
    // The drawer header also has a pop-out icon; close is the rightmost icon.
    candidates.sort((a,b)=>b.x-a.x);
    candidates[0].target.click();
    return {panel:true,clicked:true};
}"""


async def unique_visible(locator):
    """Return the only visible match, tolerating hidden duplicate SPA nodes."""
    count = await locator.count()
    visible = []
    for index in range(count):
        item = locator if count == 1 else locator.nth(index)
        if await item.is_visible():
            visible.append(item)
            if len(visible) > 1:
                return None
    return visible[0] if visible else None


async def close_open_message_panel(page, guard, log):
    """Close Douyin's floating message drawer when it covers the profile."""
    panel_found = False
    for title in await page.get_by_text('消息',exact=True).all():
        if not await title.is_visible():
            continue
        result = await title.evaluate(MESSAGE_PANEL_CLOSE)
        panel_found = panel_found or result.get('panel',False)
        if result.get('clicked'):
            log('检测到消息侧栏，已点击关闭按钮')
            for _ in range(10):
                await guard()
                if not await title.is_visible():
                    return True
                await asyncio.sleep(0.2)
            raise NeedsAttention('已点击消息侧栏关闭按钮，但侧栏未关闭，尚未发送私信')
    if panel_found:
        raise NeedsAttention('消息侧栏遮挡客户主页，但未找到关闭按钮，尚未发送私信')
    return False


async def profile_follow_matches(page, require_enabled=False):
    # Prefer the stable e2e hook observed on the profile action. The fallback
    # preserves compatibility with older Douyin markup.
    controls = page.locator(
        'button[data-e2e="user-info-follow-btn"], button:has(.semi-button-content)')
    matches = []
    for item in await controls.all():
        if not await item.is_visible():
            continue
        text = (await item.inner_text()).strip()
        if text not in ('关注','已关注','互相关注','相互关注'):
            continue
        if require_enabled and not await item.is_enabled():
            continue
        matches.append((item,text))
    return matches


def click_blocked_before_dispatch(exc):
    """Recognize Playwright failures where no click was delivered."""
    detail = str(exc).lower()
    return ('waiting for element to be visible, enabled and stable' in detail
            or 'element is not stable' in detail
            or 'intercepts pointer events' in detail
            or 'element is outside of the viewport' in detail)


async def composer_send_buttons(editor):
    # Scope to the nearest container shared by this editor and its send control.
    container = editor.locator('xpath=ancestor::*[.//*[contains(concat(" ", normalize-space(@class), " "), " e2e-send-msg-btn ")]][1]')
    candidates = container.locator('.e2e-send-msg-btn')
    visible = [item for item in await candidates.all() if await item.is_visible()]
    enabled = [item for item in visible if await item.is_enabled()
               and await item.get_attribute('aria-disabled') != 'true']
    return visible, enabled


def in_chat_header(rect, editor, send):
    if not rect or not editor or not send:
        return False
    # Header controls sit above the composer, within the same horizontal column.
    left = min(editor['x'],send['x'])
    right = max(editor['x']+editor['width'],send['x']+send['width'])
    return (rect['width']>0 and rect['height']>0 and rect['y']>=0
            and rect['y']+rect['height']<=editor['y']
            and rect['x']>=left and rect['x']+rect['width']<=right)


async def follow_in_panel(panel, guard, log, accept=None):
    async def eligible(item):
        return await item.is_visible() and (accept is None or await accept(item))
    async def followed():
        for text in ('已关注','互相关注','相互关注'):
            for item in await panel.get_by_text(text,exact=True).all():
                if await eligible(item):
                    return True
        return False

    if await followed():
        log('发送前关注检查：已关注，跳过')
        return
    buttons = [item for item in await panel.get_by_text('关注',exact=True).all()
               if await eligible(item) and await item.is_enabled()]
    if len(buttons)!=1:
        raise NeedsAttention('未找到私信窗口中唯一的关注按钮，尚未发送私信')
    await guard()
    log('发送前关注检查：正在关注用户')
    await buttons[0].click()
    for _ in range(20):
        await guard()
        if await followed():
            log('关注成功，继续发送私信')
            return
        await asyncio.sleep(0.5)
    raise NeedsAttention('已点击关注，但未确认已关注状态，尚未发送私信；请检查关注结果')


async def follow_profile_button(page, guard, log):
    await close_open_message_panel(page,guard,log)
    matches = await profile_follow_matches(page,require_enabled=True)
    if len(matches)!=1:
        raise NeedsAttention('主页关注按钮不唯一或不可见，尚未发送私信')
    item,text = matches[0]
    if text!='关注':
        log('主页关注检查：已关注，继续发送私信')
        return
    await guard()
    log('发送前关注检查：正在点击当前客户主页关注按钮')
    try:
        await item.click(timeout=PROFILE_CLICK_TIMEOUT)
    except Exception as exc:
        # A normal Playwright click can wait forever for a profile whose layout
        # is animating. Only retry when its call log proves the click was never
        # dispatched; this avoids accidentally toggling an already-followed user.
        current = await profile_follow_matches(page)
        if len(current)==1 and current[0][1] in ('已关注','互相关注','相互关注'):
            log('主页关注成功，继续发送私信')
            return
        if not click_blocked_before_dispatch(exc):
            raise NeedsAttention('主页关注按钮点击失败，尚未发送私信') from exc
        await close_open_message_panel(page,guard,log)
        current = await profile_follow_matches(page,require_enabled=True)
        if len(current)!=1 or current[0][1]!='关注':
            raise NeedsAttention('主页关注按钮状态已变化，无法安全重试，尚未发送私信') from exc
        await guard()
        log('主页关注按钮常规点击受阻，正在重试')
        try:
            await current[0][0].evaluate('el => el.click()')
        except Exception as retry_error:
            raise NeedsAttention('主页关注按钮重试失败，尚未发送私信') from retry_error
    for _ in range(20):
        await guard()
        current = await profile_follow_matches(page)
        if len(current)==1 and current[0][1] in ('已关注','互相关注','相互关注'):
            log('主页关注成功，继续发送私信')
            return
        await asyncio.sleep(0.5)
    raise NeedsAttention('已点击主页关注，但未确认成功，尚未发送私信')


class Browser:
    def __init__(self, context, settings, diagnostics_dir=None, log=None, page=None):
        self.log = log or (lambda text: None)
        self.diagnostics_dir = Path(diagnostics_dir) if diagnostics_dir else None
        self.context = context
        self.s = settings
        self.page = page if page is not None else (context.pages[0] if context.pages else None)
        self.warnings = []

    async def diagnose_sender(self):
        if self.diagnostics_dir is None:
            raise NeedsAttention('未配置诊断目录')
        editor = self.page.locator(self.s['message_editor'])
        if await editor.count()!=1:
            raise NeedsAttention('请先打开保留草稿的私信窗口，再采集诊断')
        info = await editor.evaluate(SEND_DIAGNOSTICS)
        self.diagnostics_dir.mkdir(parents=True,exist_ok=True)
        path = self.diagnostics_dir / (datetime.now().strftime('send-%Y%m%d-%H%M%S-%f')+'.json')
        path.write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
        return str(path)

    async def follow_before_send(self, editor):
        # The chat header may be rendered in a different portal than the editor.
        # Do not impose a fixed ancestor width on responsive layouts.
        visible,enabled = await composer_send_buttons(editor)
        if len(visible)!=1:
            raise NeedsAttention('无法确定当前私信发送区域，尚未关注或发送')
        editor_bounds = await editor.bounding_box()
        send_bounds = await visible[0].bounding_box()
        if not editor_bounds or not send_bounds:
            raise NeedsAttention('当前私信区域不可见，尚未关注或发送')
        async def accept(item):
            return in_chat_header(await item.bounding_box(),editor_bounds,send_bounds)
        try:
            await follow_in_panel(self.page,self.guard,self.log,accept)
        except NeedsAttention as exc:
            # Only fall back when no header click was attempted; never retry a
            # follow whose result is uncertain.
            if str(exc)=='未找到私信窗口中唯一的关注按钮，尚未发送私信':
                try:
                    await follow_profile_button(self.page,self.guard,self.log)
                    return
                except NeedsAttention as profile_error:
                    exc = profile_error
            try:
                path = await self.diagnose_sender()
                self.log('关注定位诊断已保存：'+path)
            except Exception:
                self.log('关注定位诊断未能保存')
            raise exc

    async def init(self):
        if self.page is None:
            self.page = await self.context.new_page()
        await self.page.goto('https://www.douyin.com/',wait_until='domcontentloaded')

    async def guard(self):
        for text in ('请完成下列验证','请完成安全验证','操作过于频繁','访问过于频繁','扫码登录'):
            if await self.page.get_by_text(text,exact=True).count() and await self.page.get_by_text(text,exact=True).first.is_visible():
                raise NeedsAttention('页面需要人工处理：'+text)

    async def wait_for_comment_ready(self, packets, tasks):
        """Give a slow page time to expose either its API data or comment panel."""
        selector = self.s.get('comment_panel','')
        panel_locator = self.page.locator(selector) if selector else None
        for check in range(COMMENT_READY_CHECKS):
            await self.guard()
            if tasks:
                await asyncio.gather(*list(tasks))
            if packets:
                return await unique_visible(panel_locator) if panel_locator is not None else None
            panel = await unique_visible(panel_locator) if panel_locator is not None else None
            if panel is not None:
                return panel
            if check + 1 < COMMENT_READY_CHECKS:
                await asyncio.sleep(COMMENT_READY_INTERVAL)
        return None

    async def capture(self, url, path, key, limit, stop_when=None):
        packets = []
        stopped_at_boundary = False
        errors = []
        tasks = set()
        async def consume(response):
            parsed = urlparse(response.url)
            if not is_douyin_host(parsed.hostname) or parsed.path != path:
                return
            try:
                p = await response.json()
                if p.get('status_code',0) != 0:
                    errors.append('接口状态异常 '+str(p.get('status_code')))
                elif key in p:
                    packets.append(p)
            except Exception:
                errors.append('页面响应无法解析')
        def receive(response):
            task = asyncio.create_task(consume(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        self.page.on('response',receive)
        try:
            await self.page.goto(url,wait_until='domcontentloaded')
            if key == 'comments':
                # DOMContentLoaded fires well before the comment drawer/API is
                # ready on a busy machine. Poll both signals instead of relying
                # on one short fixed delay.
                await self.wait_for_comment_ready(packets,tasks)
            else:
                await asyncio.sleep(3)
            stale = 0
            for _ in range(limit):
                await self.guard()
                if tasks:
                    await asyncio.gather(*list(tasks))
                if stop_when and stop_when(packets):
                    stopped_at_boundary = True
                    break
                before = len(packets)
                if packets and not packets[-1].get('has_more',False):
                    break
                if key == 'comments':
                    selector = self.s.get('comment_panel','')
                    panel_locator = self.page.locator(selector) if selector else None
                    panel = await unique_visible(panel_locator) if panel_locator is not None else None
                    if panel is not None:
                        scroll = await panel.evaluate(COMMENT_PANEL_SCROLL)
                        if not scroll.get('found'):
                            if packets:
                                self.warnings.append('仅采集已加载评论：未找到评论区的可滚动容器，请校准 CSS')
                                break
                            raise CommentPageUnavailable('未找到评论区的可滚动容器')
                    else:
                        if packets:
                            self.warnings.append('仅采集已加载评论：未找到唯一评论区域，请校准 CSS')
                            break
                        raise CommentPageUnavailable('未找到唯一且可见的评论区')
                else:
                    await self.page.mouse.move(700,600)
                    await self.page.mouse.wheel(0,1000)
                await asyncio.sleep(2)
                stale = stale+1 if len(packets)==before else 0
                if stale >= 4:
                    break
            if tasks:
                await asyncio.gather(*list(tasks))
            if errors:
                raise NeedsAttention(errors[0])
            if not packets:
                # A long comment list can occasionally leave Douyin's SPA in
                # an empty feed state. Re-open the intended URL once so that a
                # transient black page does not stop the main account task.
                self.log('没有捕获到页面数据，正在重新打开当前页面重试')
                await self.page.goto(url,wait_until='domcontentloaded')
                await asyncio.sleep(3)
                await self.guard()
                if tasks:
                    await asyncio.gather(*list(tasks))
                if errors:
                    raise NeedsAttention(errors[0])
                if not packets:
                    error = '刷新重试后仍没有捕获到页面数据'
                    if key == 'comments':
                        raise CommentPageUnavailable(error)
                    raise NeedsAttention(error+'，请检查登录、页面结构及站点适配')
            stopped_at_boundary = stopped_at_boundary or bool(stop_when and stop_when(packets))
            if stopped_at_boundary:
                self.warnings.append('作品扫描已到设定日期边界，停止加载更早作品')
            elif packets[-1].get('has_more',False):
                self.warnings.append('页面仍有更多数据：已达到滚动限制或未触发加载，本轮覆盖不完整')
            return packets
        finally:
            self.page.remove_listener('response',receive)
            if tasks:
                await asyncio.gather(*list(tasks),return_exceptions=True)

    async def videos(self, uid, days):
        cutoff = time.time()-days*86400
        packets = await self.capture('https://www.douyin.com/user/'+uid,'/aweme/v1/web/aweme/post/','aweme_list',self.s['scroll_limit'],stop_when=lambda packets: reached_video_cutoff(packets,cutoff))
        items = {}
        for p in packets:
            for v in p.get('aweme_list') or []:
                if int(v.get('create_time',0)) >= cutoff:
                    items[str(v['aweme_id'])] = v
        return list(items.values())

    async def comments(self, video):
        video_id = str(video['aweme_id'])
        for attempt in range(COMMENT_CAPTURE_ATTEMPTS):
            try:
                packets = await self.capture('https://www.douyin.com/video/'+video_id,'/aweme/v1/web/comment/list/','comments',self.s['scroll_limit'])
                break
            except CommentPageUnavailable:
                if attempt + 1 >= COMMENT_CAPTURE_ATTEMPTS:
                    raise
                delay = 2 * (attempt + 1)
                self.log(f'视频 {video_id} 评论区未就绪，{delay} 秒后重新打开'
                         f'（第 {attempt + 2}/{COMMENT_CAPTURE_ATTEMPTS} 次）')
                await asyncio.sleep(delay)
        items = {}
        for p in packets:
            for c in parse_comments(p,video):
                items[c['id']] = c
        return list(items.values())

    async def private_account_reason(self):
        # Only explicit site signals can mark a customer as unreachable.
        checks = (
            ('私密账号','私密账号，无法私信'),
            ('该账号为私密账号','私密账号，无法私信'),
            ('该用户设置为私密账号','私密账号，无法私信'),
            ('该用户为私密账号','私密账号，无法私信'),
            ('无法私信','页面提示无法私信'),
            ('暂不支持私信','页面提示暂不支持私信'),
            ('暂无法私信','页面提示暂无法私信'),
        )
        for marker, reason in checks:
            for item in await self.page.get_by_text(marker,exact=True).all():
                if await item.is_visible():
                    return reason
        return None

    async def check_reachable(self, customer):
        await self.page.goto(customer['url'],wait_until='domcontentloaded')
        await asyncio.sleep(2)
        await self.guard()
        reason = await self.private_account_reason()
        if reason:
            raise SkippedCustomer(reason)

    async def send(self, customer, message, before_click):
        await self.page.goto(customer['url'],wait_until='domcontentloaded')
        await asyncio.sleep(2)
        await self.guard()
        reason = await self.private_account_reason()
        if reason:
            raise SkippedCustomer(reason)
        await follow_profile_button(self.page,self.guard,self.log)
        trigger = self.page.get_by_text('私信',exact=True)
        visible = [item for item in await trigger.all() if await item.is_visible()]
        if len(visible)!=1:
            reason = await self.private_account_reason()
            if reason:
                raise SkippedCustomer(reason)
            raise NeedsAttention('客户主页未找到唯一的私信按钮')
        await visible[0].click()
        reason = await self.private_account_reason()
        if reason:
            raise SkippedCustomer(reason)
        editor = self.page.locator(self.s['message_editor'])
        await editor.wait_for(state='visible',timeout=10000)
        if await editor.count()!=1:
            raise NeedsAttention('私信输入框不唯一，请校准 CSS')
        await editor.fill(message)
        await self.guard()
        # Prefer the chat dialog containing this editor over unrelated page controls.
        dialogs = self.page.get_by_role('dialog').filter(has=editor)
        scope = dialogs if await dialogs.count()==1 else self.page
        send = scope.get_by_role('button',name='发送',exact=True).or_(
            scope.get_by_text('发送',exact=True))
        buttons = []
        visible_count = 0
        for attempt in range(10):
            marked_visible, marked_enabled = await composer_send_buttons(editor)
            if marked_visible:
                visible_count = len(marked_visible)
                buttons = marked_enabled
                if len(marked_visible)==1 and len(buttons)==1:
                    break
                # An ambiguous/disabled known control must not fall back to guessing.
                buttons = []
                await asyncio.sleep(0.5)
                continue
            visible = [item for item in await send.all() if await item.is_visible()]
            # Text inside a button and the button itself are one action, not two.
            buttons = []
            for item in visible:
                nested = await item.evaluate("el => !!el.parentElement?.closest('button, [role=button]')")
                if nested:
                    continue
                if await item.is_enabled():
                    buttons.append(item)
            visible_count = len(visible)
            if not buttons and visible_count==0:
                # Inspect live DOM geometry/colors; no fixed screen coordinates.
                evidence = await editor.evaluate(ICON_CANDIDATES)
                icons = [c for c in evidence['candidates'] if is_send_icon(c,evidence['editor'])]
                if len(icons)==1:
                    buttons = [self.page.locator(icons[0]['selector'])]
            if len(buttons)==1:
                break
            await asyncio.sleep(0.5)
        if len(buttons)!=1:
            try:
                diagnostic = '；页面诊断已保存：'+await self.diagnose_sender()
            except Exception:
                diagnostic = '；页面诊断采集失败'
            raise NeedsAttention(f'发送按钮定位失败：文字匹配 {visible_count} 个，可用候选 {len(buttons)} 个；已保留草稿'+diagnostic)
        # Persist before an external side effect; a crash cannot make this pending again.
        before_click()
        try:
            await buttons[0].click()
            await asyncio.sleep(3)
            await self.guard()
        except Exception as exc:
            raise UncertainSend('发送后结果不明确，禁止自动重试') from exc
