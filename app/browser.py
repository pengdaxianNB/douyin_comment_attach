"""Site adapter. Response schemas/selectors require verification against a logged-in site."""
import asyncio
import json
from pathlib import Path
import random
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urljoin, urlparse
from playwright.async_api import Error as PlaywrightError
from .db import parse_work_url, work_url


class NeedsAttention(RuntimeError):
    pass


class AccountNeedsAttention(NeedsAttention):
    """账号级异常，需要停止当前账号等待人工处理。"""
    pass


class SearchPageUnavailable(NeedsAttention):
    """搜索结果未就绪；重建当前账号页面后可重试当前词。"""
    pass


class CommentPageUnavailable(NeedsAttention):
    """单个作品页暂时无法定位评论区。"""
    pass


class VideoUnavailable(CommentPageUnavailable):
    """页面已明确提示作品不存在或已失效。"""
    pass


class UncertainSend(RuntimeError):
    pass


class SkippedCustomer(RuntimeError):
    pass


class PrivateAccount(SkippedCustomer):
    """客户主页明确标记为私密账号。"""
    pass


class MissingAccount(SkippedCustomer):
    """客户主页明确提示用户不存在或已注销。"""
    pass


async def detach_cdp_session(session):
    """Best-effort disposal must not prolong cancellation of a frozen page."""
    try:
        await asyncio.wait_for(session.detach(),timeout=1)
    except Exception:
        pass


def customer_skip(reason):
    if reason.startswith('私密账号'):
        error = PrivateAccount
    elif reason.startswith('用户不存在'):
        error = MissingAccount
    else:
        error = SkippedCustomer
    raise error(reason)


def is_douyin_host(hostname):
    """Accept Douyin's first-party API subdomains without matching lookalikes."""
    hostname = (hostname or '').lower().rstrip('.')
    return hostname == 'douyin.com' or hostname.endswith('.douyin.com')


SEC_UID = re.compile(r'MS4[\w-]{10,}')


def parse_comments(payload, video):
    result = []
    for c in payload.get('comments') or []:
        if not isinstance(c,dict):
            continue
        if str(c.get('reply_id', '0')) not in ('0', '', 'None'):
            continue
        u = c.get('user') or {}
        # One malformed record must not abort the whole video: the database
        # rejects a non-standard sec_uid, so such a comment is skipped here.
        if (not c.get('cid') or not isinstance(u,dict) or not SEC_UID.fullmatch(str(u.get('sec_uid') or ''))
                or not isinstance(c.get('text'),str) or not c['text'].strip()):
            continue
        try:
            created = datetime.fromtimestamp(int(c.get('create_time') or 0),timezone.utc)
        except (TypeError,ValueError,OverflowError,OSError):
            created = datetime.fromtimestamp(0,timezone.utc)
        result.append(dict(id=str(c['cid']),uid=u['sec_uid'],name=str(u.get('nickname') or ''),text=c['text'],
            comment_time=created.isoformat(),
            video_id=str(video['aweme_id']),video_title=video.get('desc',''),blogger=(video.get('author') or {}).get('nickname','')))
    return result


def is_note_post(item):
    """Image-text posts (aweme_type 68 / an images array) live under /note/."""
    if not isinstance(item,dict):
        return False
    return str(item.get('aweme_type','')) == '68' or bool(item.get('images'))


def comment_page_url(video):
    """Page that hosts a work's comments; notes are served under /note/."""
    ident = str(video['aweme_id'])
    try:
        found,kind = parse_work_url(video.get('url'))
        if found == ident:
            return work_url(ident,kind)
    except ValueError:
        pass
    return work_url(ident,'note' if video.get('kind')=='note' else 'video')


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


def creator_search_videos(payload):
    """Extract video-shaped records from creator-center search JSON.

    Creator Center has changed the nesting and camel/snake-case of this
    response more than once.  Requiring both a numeric work ID and a title
    keeps the tolerant traversal from mistaking unrelated analytics IDs for
    videos.
    """
    result = {}

    def visit(value):
        if isinstance(value,list):
            for item in value:
                visit(item)
            return
        if not isinstance(value,dict):
            return
        nested = next((value.get(key) for key in
                       ('aweme_info','awemeInfo','video_info','videoInfo','item_info','itemInfo')
                       if isinstance(value.get(key),dict)),{})
        candidates = (value,nested)
        ident = next((item.get(key) for item in candidates for key in
                      ('aweme_id','awemeId','item_id','itemId','video_id','videoId')
                      if item.get(key) is not None),None)
        title = next((item.get(key) for item in candidates for key in
                      ('desc','title','video_title','videoTitle','item_title','itemTitle','video_name')
                      if item.get(key)), '')
        if re.fullmatch(r'\d{8,}',str(ident or '')) and str(title).strip():
            author = next((item.get(key) for item in candidates for key in
                           ('author','user','creator') if item.get(key)),{})
            if not isinstance(author,dict):
                author = {'nickname':str(author)}
            if not author:
                author = {'nickname':str(value.get('nickname') or value.get('author_name') or value.get('authorName') or '')}
            created = next((item.get(key) for item in candidates for key in
                            ('create_time','createTime','publish_time','publishTime')
                            if item.get(key) is not None),0)
            try:
                created = int(created or 0)
                if created > 10_000_000_000:
                    created //= 1000
            except (TypeError,ValueError):
                try:
                    created = int(datetime.fromisoformat(str(created)).replace(
                        tzinfo=timezone(timedelta(hours=8))).timestamp())
                except ValueError:
                    created = 0
            result[str(ident)] = dict(aweme_id=str(ident),desc=str(title),author=author,
                                      create_time=created)
        for child in value.values():
            if isinstance(child,(dict,list)):
                visit(child)

    visit(payload)
    return list(result.values())


SEARCH_END_TEXT = '暂时没有更多了'
# Safety cap only: a search normally ends at the "no more" marker long before.
SEARCH_MAX_SCROLLS = 600
SEARCH_STALE_LIMIT = 6
SEARCH_READY_CHECKS = 30
SEARCH_READY_TIMEOUT = 45
SEARCH_STEP_TIMEOUT = 60
SEARCH_RESPONSE_TIMEOUT = 8
# Every so often take a longer break, like someone stepping away for a moment.
SEARCH_BREAK_EVERY = (12,25)
SEARCH_BREAK_SECONDS = (8,20)


def search_result_videos(payload):
    """Extract works from direct results and wrapped comprehensive-search cards.

    Text/medical advice cards can accompany nested work cards, sometimes with
    JSON-encoded data. Follow only result/card containers, not arbitrary
    metadata or related-user widgets, and require an explicit numeric aweme_id.
    """
    result = {}

    def visit(value, depth=0):
        if depth > 12:
            return
        if isinstance(value,str):
            try:
                value = json.loads(value)
            except (ValueError,RecursionError):
                return
        if isinstance(value,list):
            for item in value:
                visit(item,depth+1)
            return
        if not isinstance(value,dict):
            return
        ident = str(value.get('aweme_id') or '')
        if re.fullmatch(r'\d{8,}',ident):
            try:
                created = int(value.get('create_time') or 0)
            except (TypeError,ValueError):
                created = 0
            author = value.get('author')
            result[ident] = dict(aweme_id=ident,desc=str(value.get('desc') or ''),
                                 author=author if isinstance(author,dict) else {},
                                 create_time=created,kind='note' if is_note_post(value) else 'video')
            return
        for key in ('data','business_data','aweme_info','aweme_list','sub_card_list',
                    'card_info','raw_data','items','item_list'):
            visit(value.get(key),depth+1)

    if isinstance(payload,dict):
        for key in ('data','business_data','aweme_list'):
            visit(payload.get(key))
    return list(result.values())


def search_work_link(href):
    """Read direct work links and Douyin's in-place modal work links."""
    try:
        parsed = urlparse(urljoin('https://www.douyin.com/',str(href or '')))
    except ValueError:
        return None
    if parsed.scheme not in ('http','https') or not is_douyin_host(parsed.hostname):
        return None
    match = re.fullmatch(r'/(video|note)/(\d{8,})/?',parsed.path)
    if match:
        return match.group(2),match.group(1)
    ids = parse_qs(parsed.query).get('modal_id',[])
    if len(ids)==1 and re.fullmatch(r'\d{8,}',ids[0]):
        return ids[0],'video'
    return None


async def human_scroll(page, rng=random):
    """Scroll a results feed the way a person browsing it would.

    A fixed wheel delta on a fixed timer is an easy automation signature.
    Each call varies the pointer position, splits the distance into uneven
    wheel ticks with short gaps, sometimes drifts back up a little, and ends
    with a "reading" pause of random length (occasionally a long one).
    """
    size = getattr(page,'viewport_size',None) or {}
    width = size.get('width') or 1200
    height = size.get('height') or 800
    await page.mouse.move(rng.uniform(width*.3,width*.7),rng.uniform(height*.35,height*.7),
                          steps=rng.randint(4,14))
    remaining = rng.randint(600,1800)
    while remaining > 0:
        step = min(remaining,rng.randint(90,420))
        await page.mouse.wheel(0,step)
        remaining -= step
        await asyncio.sleep(rng.uniform(.06,.4))
    if rng.random() < .12:
        await asyncio.sleep(rng.uniform(.4,1.2))
        await page.mouse.wheel(0,-rng.randint(80,320))
        await asyncio.sleep(rng.uniform(.4,1.0))
    # Give the next batch time to load and the "reader" time to look at it.
    pause = rng.uniform(1.5,4.0)
    if rng.random() < .12:
        pause += rng.uniform(4,10)
    await asyncio.sleep(pause)


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


# Image-text notes keep the comment list hidden behind a "评论(N)" tab that sits
# beside "相关推荐"; video pages show the list directly and have no such tab.
COMMENT_TAB = re.compile(r'^评论\s*(?:[（(][^)）]*[)）])?$')
COMMENT_TAB_CLICKS = 3
COMMENT_READY_CHECKS = 20
COMMENT_READY_INTERVAL = 0.5
COMMENT_CAPTURE_ATTEMPTS = 3
PROFILE_CLICK_TIMEOUT = 4000
PROFILE_MESSAGE_CLICK_TIMEOUT = 10000
MESSAGE_EDITOR_ATTEMPTS = 30
MESSAGE_EDITOR_INTERVAL = 0.5
PROFILE_READY_ATTEMPTS = 30


MESSAGE_PANEL_CLOSE = r"""title => {
    const visible = el => {
        const r=el.getBoundingClientRect(),s=getComputedStyle(el);
        return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)>0;
    };
    if(!visible(title) || title.textContent.trim()!=='消息')return {panel:false,point:null};
    const tr=title.getBoundingClientRect();
    let panel=null;
    for(let el=title;el&&el!==document.body;el=el.parentElement){
        const r=el.getBoundingClientRect();
        // Header geometry alone also matches normal chats and top navigation.
        // Require the list's search area in the SAME tall drawer container.
        if(!(r.width>=260&&r.width<=Math.min(900,innerWidth*.8)&&r.height>=180&&
                tr.left-r.left>=0&&tr.left-r.left<=100&&tr.top-r.top>=0&&tr.top-r.top<=100))continue;
        const editors=[...el.querySelectorAll('[contenteditable="true"],textarea')].filter(visible);
        if(editors.some(e=>!e.matches('[role="searchbox"],input[type="search"]')&&
                !/搜索/.test((e.getAttribute('placeholder')||'')+(e.getAttribute('aria-label')||''))))
            return {panel:false,point:null,reason:'conversation'};
        const search=[...el.querySelectorAll('input,[role="searchbox"],span,div')].some(e=>
            visible(e)&&(/搜索/.test((e.getAttribute('placeholder')||'')+(e.getAttribute('aria-label')||''))||
                e.getAttribute('role')==='searchbox'||(e.children.length===0&&e.textContent.trim()==='搜索')));
        if(search){panel=el;break;}
    }
    if(!panel)return {panel:false,point:null,reason:'not-message-list'};
    const hr=panel.getBoundingClientRect(),candidates=[];
    // Use the full drawer width: title and close controls may be siblings
    // in separate portal/header wrappers. SVGs need not expose a cursor.
    for(const target of panel.querySelectorAll('*')){
        if(!visible(target))continue;
        const r=target.getBoundingClientRect(),s=getComputedStyle(target);
        const clickable=target.matches('button,a,[role="button"],svg')||typeof target.onclick==='function'||s.cursor==='pointer';
        if(!clickable||r.width<12||r.width>72||r.height<12||r.height>72)continue;
        if(r.left<tr.right+30||r.right>hr.right+2||r.top<hr.top-2||r.bottom>hr.bottom+2)continue;
        const x=r.left+r.width/2,y=r.top+r.height/2;
        if(Math.abs(y-(tr.top+tr.height/2))>24)continue;
        const hit=document.elementFromPoint(x,y);
        if(!hit||!(target===hit||target.contains(hit)))continue;
        candidates.push({x,y,right:r.right});
    }
    if(!candidates.length)return {panel:true,point:null,reason:'no-header-control'};
    // The drawer also has a pop-out icon; only accept the rightmost
    // control near the edge of its header, aligned with the title.
    candidates.sort((a,b)=>b.right-a.right);
    const target=candidates[0];
    if(hr.right-target.right>48)return {panel:true,point:null,reason:'no-right-edge-control'};
    return {panel:true,point:{x:target.x,y:target.y}};
}"""


EDITOR_UNOBSCURED = r"""el => {
    const r=el.getBoundingClientRect();
    if(r.width<=0||r.height<=0)return false;
    const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
    return !!hit&&(hit===el||el.contains(hit));
}"""


PROFILE_MESSAGE_HIT_TARGET = r"""el => {
    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    const info={rect:{x:r.x,y:r.y,width:r.width,height:r.height}};
    if(!el.isConnected)return {...info,reason:'按钮已从页面移除'};
    if(el.disabled||el.getAttribute('aria-disabled')==='true')return {...info,reason:'按钮已禁用'};
    if(r.width<=0||r.height<=0||s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0)
        return {...info,reason:'按钮不可见'};
    const x=r.x+r.width/2,y=r.y+r.height/2;
    if(x<0||y<0||x>=innerWidth||y>=innerHeight)return {...info,reason:'按钮中心位于可视区域外'};
    const hit=document.elementFromPoint(x,y);
    if(!hit||!(hit===el||el.contains(hit))){
        const blocker=hit?hit.tagName.toLowerCase()+'.'+[...hit.classList].join('.').slice(0,160):'未知元素';
        return {...info,reason:'按钮被遮挡：'+blocker};
    }
    return {...info,point:{x,y},reason:'按钮中心可点击'};
}"""


def page_action_error(exc):
    """Keep the call log that distinguishes overlays, animation and stale pages."""
    return type(exc).__name__+'：'+(' '.join(str(exc).split())[:1800] or '页面操作异常')


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


async def smallest_visible_text_control(page, pattern):
    """Find the tightest visible control/text node matching a flexible label."""
    candidates = []
    # Byted's Select renders the selected label as an <input value="…">,
    # not as a text node. get_by_text() therefore only sees the hidden option
    # template. Match the live value first.
    for item in await page.locator('input[placeholder="请选择"]').all():
        if not await item.is_visible():
            continue
        try:
            value = await item.input_value()
        except PlaywrightError:
            continue
        matched = bool(pattern.search(value)) if hasattr(pattern,'search') else str(pattern) in value
        if not matched:
            continue
        box = await item.bounding_box()
        if box:
            candidates.append((box['width']*box['height'],item))
    # Creator Center currently uses Semi Design selects. Accessible roles are
    # preferred, while the Byted/Semi class fallbacks tolerate page versions
    # without a combobox role.
    for selector in ('[role="combobox"]','.byted-select','.semi-select','[class*="select"]'):
        for item in await page.locator(selector).filter(has_text=pattern).all():
            if not await item.is_visible():
                continue
            box = await item.bounding_box()
            if box:
                candidates.append((box['width']*box['height'],item))
    if not candidates:
        for item in await page.get_by_text(pattern).all():
            if not await item.is_visible():
                continue
            box = await item.bounding_box()
            if box:
                candidates.append((box['width']*box['height'],item))
    return min(candidates,key=lambda candidate:candidate[0])[1] if candidates else None


async def smallest_visible_text(page, pattern):
    """Return the smallest visible text match, useful for portal menu items."""
    candidates = []
    for item in await page.get_by_text(pattern).all():
        if not await item.is_visible():
            continue
        box = await item.bounding_box()
        if box:
            candidates.append((box['width']*box['height'],item))
    return min(candidates,key=lambda candidate:candidate[0])[1] if candidates else None


async def visible_in_page_frames(page, pattern, controls=True):
    """Find text in the main document or any Creator Center child frame."""
    finder = smallest_visible_text_control if controls else smallest_visible_text
    candidates = []
    for frame in page.frames:
        try:
            item = await finder(frame,pattern)
            if item is None:
                continue
            box = await item.bounding_box()
            if box:
                candidates.append((box['width']*box['height'],frame,item))
        except PlaywrightError:
            # A frame can be replaced while the SPA is still mounting.
            continue
    if not candidates:
        return None,None
    _,frame,item = min(candidates,key=lambda candidate:candidate[0])
    return frame,item


async def click_visible_control_at_center(page, item):
    """Click a Byted select without Playwright scrolling it under the header."""
    target = item
    trigger = item.locator(
        'xpath=ancestor::*[contains(concat(" ",normalize-space(@class)," ")," byted-select ")][1]')
    if await trigger.count() and await trigger.is_visible():
        target = trigger
    box = await target.bounding_box()
    if not box:
        raise NeedsAttention('发布时间下拉框当前不可见')
    await page.mouse.click(box['x']+box['width']/2,box['y']+box['height']/2)


async def close_open_message_panel(page, guard, log, strict=True):
    """Close Douyin's floating message drawer when it covers the profile."""
    panel_found = False
    for title in await page.get_by_text('消息',exact=True).all():
        if not await title.is_visible():
            continue
        try:
            result = await title.evaluate(MESSAGE_PANEL_CLOSE)
        except Exception as exc:
            raise NeedsAttention('消息侧栏关闭失败，尚未发送私信') from exc
        panel_found = panel_found or result.get('panel',False)
        if result.get('point'):
            await guard()
            point = result['point']
            await page.mouse.click(point['x'],point['y'])
            log('检测到消息侧栏，已点击关闭按钮')
            for _ in range(10):
                await guard()
                if not await title.is_visible():
                    return True
                await asyncio.sleep(0.2)
            raise NeedsAttention('已点击消息侧栏关闭按钮，但侧栏未关闭，尚未发送私信')
    if panel_found and strict:
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


async def profile_message_buttons(page):
    """Return visible profile message buttons, never their nested text spans."""
    controls = page.get_by_role('button',name='私信',exact=True)
    matches = [item for item in await controls.all()
               if await item.is_visible() and await item.is_enabled()]
    if matches:
        return matches
    # Older page variants do not expose an accessible button name. Lift the
    # exact text match to its button so Playwright does not click a moving span.
    controls = page.locator('button').filter(
        has=page.get_by_text('私信',exact=True))
    return [item for item in await controls.all()
            if await item.is_visible() and await item.is_enabled()]


async def click_profile_message_button(page, guard, log):
    await close_open_message_panel(page,guard,log)
    buttons = await profile_message_buttons(page)
    if len(buttons)!=1:
        raise NeedsAttention('客户主页未找到唯一的私信按钮')
    await guard()
    log(f'正在打开当前客户私信窗口（点击等待最长 {PROFILE_MESSAGE_CLICK_TIMEOUT//1000} 秒）')
    try:
        await buttons[0].click(timeout=PROFILE_MESSAGE_CLICK_TIMEOUT)
    except Exception as exc:
        log('私信按钮首次点击异常：'+page_action_error(exc))
        if not click_blocked_before_dispatch(exc):
            raise NeedsAttention('客户主页私信按钮点击失败，尚未发送私信：'+page_action_error(exc)) from exc
        await close_open_message_panel(page,guard,log)
        buttons = await profile_message_buttons(page)
        if len(buttons)!=1:
            raise NeedsAttention('私信按钮状态已变化，无法安全重试') from exc
        await guard()
        log(f'私信按钮常规点击受阻，正在重试（点击等待最长 {PROFILE_MESSAGE_CLICK_TIMEOUT//1000} 秒）')
        try:
            await buttons[0].click(timeout=PROFILE_MESSAGE_CLICK_TIMEOUT)
        except Exception as retry_error:
            log('私信按钮常规重试异常：'+page_action_error(retry_error))
            if not click_blocked_before_dispatch(retry_error):
                raise NeedsAttention('私信按钮重试失败，尚未发送私信：'+page_action_error(retry_error)) from retry_error
            # A second identical locator.click() cannot recover an endlessly
            # animating Qt/SPA layout. Use a trusted mouse click only when the
            # current unique button itself occupies the live hit-test point.
            await close_open_message_panel(page,guard,log)
            buttons = await profile_message_buttons(page)
            if len(buttons)!=1:
                raise NeedsAttention('私信按钮重试失败，按钮状态已变化，尚未发送私信：'+page_action_error(retry_error)) from retry_error
            await guard()
            try:
                hit = await buttons[0].evaluate(PROFILE_MESSAGE_HIT_TARGET)
            except Exception as hit_error:
                raise NeedsAttention('私信按钮重试失败，无法检查按钮位置，尚未发送私信：'+page_action_error(hit_error)) from hit_error
            if not hit.get('point'):
                raise NeedsAttention('私信按钮重试失败，'+hit.get('reason','无法确认点击位置')+
                                     '，尚未发送私信：'+page_action_error(retry_error)) from retry_error
            log('私信按钮常规点击连续受阻，已确认按钮中心无遮挡，正在按当前位置点击')
            point = hit['point']
            try:
                await page.mouse.click(point['x'],point['y'])
            except Exception as mouse_error:
                raise NeedsAttention('私信按钮当前位置点击失败，尚未发送私信：'+page_action_error(mouse_error)) from mouse_error


async def visible_message_editor(page, selector):
    visible = [item for item in await page.locator(selector).all()
               if await item.is_visible()]
    if len(visible)==1:
        return visible[0]
    # Prefer the known Douyin composer if another contenteditable (for
    # example a search widget) is also visible.
    preferred = []
    for item in visible:
        classes = (await item.get_attribute('class')) or ''
        if 'messageEditorinputArea' in classes.split():
            preferred.append(item)
    return preferred[0] if len(preferred)==1 else None


async def wait_for_message_editor(page, selector, guard, log=None):
    for attempt in range(MESSAGE_EDITOR_ATTEMPTS):
        await guard()
        editor = await visible_message_editor(page,selector)
        if editor is not None and await editor.evaluate(EDITOR_UNOBSCURED):
            return editor
        if attempt==0 and log is not None:
            log('正在等待私信输入框加载（最长 15 秒）')
        # The chat is often labelled "消息" too. Accept a usable composer
        # first; an editor covered by the generic list is not usable.
        # Give the freshly opened conversation time to render its composer.
        # A missing close control is not proof that opening the chat failed.
        if attempt>=2 and log is not None and await close_open_message_panel(page,guard,log,strict=False):
            return None
        if attempt + 1 < MESSAGE_EDITOR_ATTEMPTS:
            await asyncio.sleep(MESSAGE_EDITOR_INTERVAL)
    return None


def click_blocked_before_dispatch(exc):
    """Recognize Playwright failures where no click was delivered."""
    detail = str(exc).lower()
    # Every normal click log starts with the actionability check. Its presence
    # alone does not prove the click was never dispatched: a later navigation
    # timeout can include that same line. Avoid clicking a toggling entry again.
    if any(marker in detail for marker in (
            'performing click action','click action done','waiting for scheduled navigations')):
        return False
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
        self.background_prepared = False

    async def prepare_background_execution(self):
        """Keep a CDP target responsive while its Qt account tab is hidden."""
        session = await self.context.new_cdp_session(self.page)
        errors = []
        try:
            for method,params in (
                    ('Page.setWebLifecycleState',{'state':'active'}),
                    ('Emulation.setFocusEmulationEnabled',{'enabled':True})):
                try:
                    await session.send(method,params)
                except Exception as exc:
                    errors.append(type(exc).__name__)
        finally:
            await detach_cdp_session(session)
        if not self.background_prepared:
            if errors:
                self.log('后台页面保活部分不受当前内核支持，已启用兼容模式')
            else:
                self.log('后台页面已保持活跃，无需切换到当前账号标签')
            self.background_prepared = True

    async def release_page_memory(self):
        """Unload the completed site page and ask Chromium to reclaim its heap.

        Douyin is a long-lived SPA. Repeated profile/video/chat navigations can
        otherwise leave detached DOM trees and back-forward entries resident in
        the renderer for a long time. Cookies and local storage live in the
        profile, so unloading the document does not log the account out.
        """
        if self.page is None:
            return
        await self.page.goto('about:blank',wait_until='commit',timeout=10000)
        session = await self.context.new_cdp_session(self.page)
        try:
            # QtWebEngine versions differ in which memory-related CDP domains
            # they expose. Navigation is the essential cleanup; these commands
            # are best-effort additions.
            for method in ('Page.resetNavigationHistory','HeapProfiler.collectGarbage'):
                try:
                    await session.send(method)
                except Exception:
                    pass
        finally:
            await detach_cdp_session(session)

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

    async def diagnose_page(self, prefix):
        if self.diagnostics_dir is None:
            raise NeedsAttention('未配置诊断目录')
        self.diagnostics_dir.mkdir(parents=True,exist_ok=True)
        path = self.diagnostics_dir / (prefix+'-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.png')
        await self.page.screenshot(path=str(path))
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
        await self.page.goto('https://www.douyin.com/',wait_until='domcontentloaded',timeout=20000)
        await self.prepare_background_execution()

    async def guard(self):
        for text in ('请完成下列验证','请完成安全验证','操作过于频繁','访问过于频繁','扫码登录'):
            if await self.page.get_by_text(text,exact=True).count() and await self.page.get_by_text(text,exact=True).first.is_visible():
                raise AccountNeedsAttention('页面需要人工处理：'+text)

    async def open_comment_tab(self):
        """Click the note page's 评论(N) tab; True when a click was delivered."""
        get_by_text = getattr(self.page,'get_by_text',None)
        if get_by_text is None:
            return False
        try:
            tabs = [item for item in await get_by_text(COMMENT_TAB).all()
                    if await item.is_visible()]
            # Never guess between several "评论" texts.
            if len(tabs) != 1:
                return False
            await self.guard()
            await tabs[0].click(timeout=PROFILE_CLICK_TIMEOUT)
        except PlaywrightError:
            return False
        self.log('评论区在标签页内，已点击“评论”标签')
        return True

    async def wait_for_comment_ready(self, packets, tasks):
        """Give a slow page time to expose either its API data or comment panel."""
        selector = self.s.get('comment_panel','')
        panel_locator = self.page.locator(selector) if selector else None
        tab_clicks = 0
        for check in range(COMMENT_READY_CHECKS):
            await self.guard()
            if tasks:
                await asyncio.gather(*list(tasks))
            if packets:
                return await unique_visible(panel_locator) if panel_locator is not None else None
            # Deleted/removed works never expose a comment API or
            # panel.  Detect the site's terminal empty-state instead of
            # spending all comment retries waiting on a panel that cannot
            # appear.
            get_by_text = getattr(self.page,'get_by_text',None)
            if get_by_text is not None:
                for text in ('你要观看的视频不存在','视频不存在','作品不存在',
                             '视频已删除','作品已删除','视频已下架','作品已下架',
                             '你要观看的笔记不存在','笔记不存在','笔记已删除','笔记已下架'):
                    notice = get_by_text(text,exact=True)
                    if await notice.count() and await notice.first.is_visible():
                        raise VideoUnavailable('视频已失效：页面提示“'+text+'”')
            panel = await unique_visible(panel_locator) if panel_locator is not None else None
            if panel is not None:
                return panel
            if tab_clicks < COMMENT_TAB_CLICKS and await self.open_comment_tab():
                tab_clicks += 1
            if check + 1 < COMMENT_READY_CHECKS:
                await asyncio.sleep(COMMENT_READY_INTERVAL)
        return None

    async def capture(self, url, path, key, limit, stop_when=None):
        packets = []
        stopped_at_boundary = False
        errors = []
        recovered_errors = []
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
                    # Keep only the collection used by the caller. Douyin API
                    # responses contain large side payloads which needlessly
                    # inflate the peak heap across dozens of scroll requests.
                    packets.append({key:p.get(key) or [],
                                    'has_more':bool(p.get('has_more',False))})
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
                    if panel is None and await self.open_comment_tab():
                        await asyncio.sleep(1)
                        panel = await unique_visible(panel_locator) if panel_locator is not None else None
                    if panel is not None:
                        scroll = await panel.evaluate(COMMENT_PANEL_SCROLL)
                        if not scroll.get('found'):
                            # A short list fits without a scrollbar, and a note's
                            # list only requests its data once the tab is open.
                            # Give that response a moment before judging.
                            for _ in range(10):
                                if packets:
                                    break
                                await asyncio.sleep(0.5)
                                if tasks:
                                    await asyncio.gather(*list(tasks))
                            if packets:
                                if packets[-1].get('has_more',False):
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
            if not packets:
                # A long comment list can occasionally leave Douyin's SPA in
                # an empty feed state. Re-open the intended URL once so that a
                # transient black page does not stop the main account task.
                recovered_errors.extend(errors)
                errors.clear()
                detail = '（'+recovered_errors[0]+'）' if recovered_errors else ''
                self.log('没有捕获到页面数据'+detail+'，正在重新打开当前页面重试')
                await self.page.goto(url,wait_until='domcontentloaded')
                await asyncio.sleep(3)
                await self.guard()
                if tasks:
                    await asyncio.gather(*list(tasks))
                if not packets:
                    if errors or recovered_errors:
                        raise NeedsAttention((errors or recovered_errors)[0])
                    error = '刷新重试后仍没有捕获到页面数据'
                    if key == 'comments':
                        raise CommentPageUnavailable(error)
                    raise NeedsAttention(error+'，请检查登录、页面结构及站点适配')
            ignored_errors = recovered_errors + errors
            if ignored_errors:
                self.warnings.append(f'已忽略 {len(ignored_errors)} 个异常页面响应，有效数据已正常采集')
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
                try:
                    created = int(v.get('create_time') or 0)
                    ident = str(v['aweme_id'])
                except (AttributeError,KeyError,TypeError,ValueError):
                    continue
                if created >= cutoff:
                    items[ident] = {
                        'aweme_id':ident,
                        'desc':str(v.get('desc','')),
                        'author':v.get('author') or {},
                        'create_time':created,
                        'kind':'note' if is_note_post(v) else 'video',
                    }
        return list(items.values())

    async def keyword_videos(self, keyword, days=7):
        """Collect recent videos from Creator Center's keyword search page."""
        keyword = str(keyword).strip()
        if not keyword:
            return []
        url = ('https://creator.douyin.com/creator-micro/creator-count/'
               'arithmetic-index/videosearch?query='+quote(keyword)+'&source=creator')
        items = {}
        tasks = set()

        async def consume(response):
            parsed = urlparse(response.url)
            # Do not ingest unrelated Creator Center widgets (for example the
            # signed-in creator's own recent works) that may load in parallel.
            is_video_query = parsed.path.rstrip('/') == '/api/v2/index/itemQuery'
            if (not is_douyin_host(parsed.hostname)
                    or (not is_video_query
                        and 'search' not in (parsed.path+'?'+parsed.query).lower())):
                return
            try:
                payload = await response.json()
            except Exception:
                return
            for video in creator_search_videos(payload):
                items[str(video['aweme_id'])] = video

        def receive(response):
            task = asyncio.create_task(consume(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        self.page.on('response',receive)
        try:
            await self.page.goto(url,wait_until='domcontentloaded')
            unlimited_pattern = re.compile(r'发布\s*时间\s*不限')
            seven_day_pattern = re.compile(r'近\s*7\s*天')
            search_frame = None
            publish_filter = current_seven_day = None
            # DOMContentLoaded is much earlier than the creator SPA's search
            # controls. Wait up to 20 seconds instead of sampling once at 2s.
            for _ in range(40):
                await self.guard()
                search_frame,publish_filter = await visible_in_page_frames(
                    self.page,unlimited_pattern,controls=True)
                if publish_filter is not None:
                    break
                search_frame,current_seven_day = await visible_in_page_frames(
                    self.page,seven_day_pattern,controls=True)
                if current_seven_day is not None:
                    break
                await asyncio.sleep(0.5)
            if publish_filter is None and current_seven_day is None:
                try:
                    path = await self.diagnose_page('keyword-video-filter')
                    self.log('找视频筛选定位诊断已保存：'+path)
                except Exception:
                    pass
                raise NeedsAttention('找视频页面等待 20 秒仍未出现发布时间筛选；请确认创作者中心已登录，或查看诊断截图')
            if publish_filter is not None:
                # The displayed value is an input with pointer-events:none.
                # A normal locator click auto-scrolls it under the fixed page
                # header. Click the outer Byted Select at its current position.
                await click_visible_control_at_center(self.page,publish_filter)
                seven_day = None
                for _ in range(20):
                    await self.guard()
                    option_frame,seven_day = await visible_in_page_frames(
                        self.page,seven_day_pattern,controls=False)
                    if seven_day is not None:
                        search_frame = option_frame
                        break
                    await asyncio.sleep(0.25)
                if seven_day is None:
                    raise NeedsAttention('已打开发布时间筛选，但等待 5 秒仍未找到“近7天”选项')
                await click_visible_control_at_center(self.page,seven_day)
                # Confirm the dropdown applied before collecting any video.
                applied = None
                for _ in range(20):
                    search_frame,applied = await visible_in_page_frames(
                        self.page,seven_day_pattern,controls=True)
                    if applied is not None:
                        break
                    await asyncio.sleep(0.25)
                if applied is None:
                    raise NeedsAttention('已点击“近7天”，但页面未确认发布时间筛选')
            await asyncio.sleep(2)

            stale = 0
            for _ in range(self.s['scroll_limit']):
                await self.guard()
                if tasks:
                    await asyncio.gather(*list(tasks))
                before = len(items)
                # Some page versions expose the final Douyin URL directly in
                # the card/detail DOM. Merge those links with response data.
                for frame in self.page.frames:
                    try:
                        anchors = await frame.locator(
                        'a[href*="douyin.com/video/"], a[href*="douyin.com/note/"]').all()
                    except PlaywrightError:
                        continue
                    for anchor in anchors:
                        href = await anchor.get_attribute('href') or ''
                        match = re.search(r'/(video|note)/(\d{8,})',href)
                        if not match:
                            continue
                        ident = match.group(2)
                        title = ' '.join((await anchor.inner_text()).split())
                        items.setdefault(ident,dict(aweme_id=ident,desc=title,author={},create_time=0,
                                                    kind='note' if match.group(1)=='note' else 'video'))
                await self.page.mouse.move(700,600)
                await self.page.mouse.wheel(0,1000)
                await asyncio.sleep(2)
                stale = stale+1 if len(items)==before else 0
                if stale >= 4:
                    break
            if tasks:
                await asyncio.gather(*list(tasks))
        finally:
            self.page.remove_listener('response',receive)
            if tasks:
                await asyncio.gather(*list(tasks),return_exceptions=True)

        cutoff = time.time()-max(1,int(days))*86400
        # The site-side “近7天” filter is authoritative when a response omits
        # publish time. Known timestamps receive the same local safety filter.
        return [video for video in items.values()
                if not video.get('create_time') or int(video['create_time']) >= cutoff]

    async def search_videos(self, keyword):
        """Collect every work listed by douyin.com's own search page.

        The result feed loads more works as the page is scrolled and shows
        "暂时没有更多了" once it is exhausted; scrolling stops there.
        """
        keyword = str(keyword).strip()
        if not keyword:
            return []
        url = 'https://www.douyin.com/search/'+quote(keyword)
        items = {}
        tasks = set()
        response_state = '未收到搜索接口响应'

        async def consume(response):
            nonlocal response_state
            parsed = urlparse(response.url)
            if not is_douyin_host(parsed.hostname) or '/aweme/v1/web/' not in parsed.path or 'search' not in parsed.path:
                return
            status = getattr(response,'status',200)
            if status >= 400:
                response_state = f'搜索接口 HTTP {status}'
                return
            try:
                payload = await asyncio.wait_for(response.json(),timeout=SEARCH_RESPONSE_TIMEOUT)
            except Exception as exc:
                response_state = '搜索接口响应读取失败：'+type(exc).__name__
                return
            if isinstance(payload,dict) and payload.get('status_code',0) not in (0,'0',None):
                response_state = '搜索接口状态异常：'+str(payload['status_code'])
                return
            videos = search_result_videos(payload)
            response_state = '搜索接口已响应，未提取到作品' if not videos else '搜索接口已返回作品'
            for video in videos:
                items[str(video['aweme_id'])] = video

        def receive(response):
            task = asyncio.create_task(consume(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        async def collect_links():
            # The rendered cards link to the final work page; merge them with
            # response data so a changed API shape still yields the videos.
            for frame in self.page.frames:
                try:
                    anchors = await frame.locator(
                        'a[href*="douyin.com/video/"], a[href*="douyin.com/note/"], '
                        'a[href^="/video/"], a[href^="/note/"], a[href*="modal_id="]').all()
                except PlaywrightError:
                    continue
                for anchor in anchors:
                    try:
                        href = await anchor.get_attribute('href') or ''
                    except PlaywrightError:
                        continue
                    work = search_work_link(href)
                    if not work:
                        continue
                    ident,kind = work
                    if ident not in items:
                        items[ident] = dict(aweme_id=ident,desc='',author={},create_time=0,
                                            kind=kind)

        async def reached_end():
            try:
                marker = self.page.get_by_text(SEARCH_END_TEXT).first
                return await marker.is_visible()
            except PlaywrightError:
                return False

        async def drain_responses():
            if tasks:
                await asyncio.gather(*list(tasks))

        async def snapshot_results():
            await self.guard()
            await drain_responses()
            await collect_links()
            return await reached_end()

        async def wait_for_results():
            # DOMContentLoaded only means the SPA shell is ready. Do not
            # scroll an empty/loading feed or treat it as an exhausted query.
            for check in range(SEARCH_READY_CHECKS):
                ended = await snapshot_results()
                if items or ended:
                    return ended
                if check < SEARCH_READY_CHECKS-1:
                    await asyncio.sleep(1)
            raise SearchPageUnavailable('抖音搜索结果持续未就绪（'+response_state+'）')

        async def scroll_results():
            if await snapshot_results():
                return True
            await human_scroll(self.page)
            # Count API results delivered during this scroll as progress.
            # Previously they were drained BEFORE the baseline was recorded,
            # so an API-only feed could be declared stale while still growing.
            return await snapshot_results()

        self.page.on('response',receive)
        try:
            await asyncio.wait_for(self.page.goto(url,wait_until='domcontentloaded',timeout=30000),timeout=35)
            try:
                finished = await asyncio.wait_for(wait_for_results(),timeout=SEARCH_READY_TIMEOUT)
            except asyncio.TimeoutError as exc:
                raise SearchPageUnavailable('抖音搜索结果等待超时（'+response_state+'）') from exc
            stale = 0
            until_break = random.randint(*SEARCH_BREAK_EVERY)
            for _ in range(SEARCH_MAX_SCROLLS):
                if finished:
                    break
                before = len(items)
                finished = await asyncio.wait_for(scroll_results(),timeout=SEARCH_STEP_TIMEOUT)
                if finished:
                    break
                until_break -= 1
                if until_break <= 0:
                    await asyncio.sleep(random.uniform(*SEARCH_BREAK_SECONDS))
                    until_break = random.randint(*SEARCH_BREAK_EVERY)
                stale = stale+1 if len(items)==before else 0
                if stale >= SEARCH_STALE_LIMIT:
                    break
            # Keep the last batch even if its response arrived alongside the
            # terminal marker or during the final reading break.
            ended = await asyncio.wait_for(snapshot_results(),timeout=SEARCH_STEP_TIMEOUT)
            finished = finished or ended
        finally:
            self.page.remove_listener('response',receive)
            # A pending JSON body must not keep a stopped/rebuilt page alive.
            pending = list(tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending,return_exceptions=True)
        if not items:
            if finished:
                return []
            raise SearchPageUnavailable('抖音搜索页没有捕获到任何作品（'+response_state+'）')
        if not finished:
            self.warnings.append(f'搜索“{keyword}”未出现“{SEARCH_END_TEXT}”就停止加载，本轮覆盖可能不完整')
        return list(items.values())

    async def comments(self, video):
        video_id = str(video['aweme_id'])
        for attempt in range(COMMENT_CAPTURE_ATTEMPTS):
            try:
                packets = await self.capture(comment_page_url(video),'/aweme/v1/web/comment/list/','comments',self.s['scroll_limit'])
                break
            except VideoUnavailable:
                # This is a terminal page state, so reopening the same URL
                # cannot make its comments available.
                raise
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
            ('用户不存在','用户不存在，无法私信'),
            ('该用户不存在','用户不存在，无法私信'),
            ('你访问的用户不存在','用户不存在，无法私信'),
            ('用户不存在或已注销','用户不存在或已注销，无法私信'),
            ('用户已注销','用户不存在：页面提示用户已注销，无法私信'),
            ('该用户已注销','用户不存在：页面提示该用户已注销，无法私信'),
            ('该账号已注销','用户不存在：页面提示该账号已注销，无法私信'),
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
        await self.prepare_background_execution()
        # AI generation and sending share the already loaded customer profile.
        # Compare path/host, allowing query strings and trailing slashes.
        current = urlparse(self.page.url) if isinstance(self.page.url,str) else None
        target = urlparse(customer['url'])
        if not current or (current.hostname,current.path.rstrip('/')) != (target.hostname,target.path.rstrip('/')):
            self.log('正在进入客户主页')
            try:
                await self.page.goto(customer['url'],wait_until='domcontentloaded',timeout=20000)
            except PlaywrightError as exc:
                await self.guard()
                raise NeedsAttention('客户主页加载失败，尚未发送私信：'+str(exc)) from exc
        for attempt in range(PROFILE_READY_ATTEMPTS):
            await self.guard()
            reason = await self.private_account_reason()
            if reason:
                customer_skip(reason)
            await close_open_message_panel(self.page,self.guard,self.log)
            if len(await profile_follow_matches(self.page))==1:
                return
            if attempt==0:
                self.log('正在等待客户主页操作区加载')
            await asyncio.sleep(0.5)
        raise NeedsAttention('客户主页操作区在 15 秒内未就绪，尚未发送私信')

    async def send(self, customer, message, before_click):
        try:
            await self._send(customer,message,before_click)
        except AccountNeedsAttention:
            raise
        except NeedsAttention:
            if self.diagnostics_dir is not None:
                try:
                    path = await asyncio.wait_for(self.diagnose_page('chat-failure'),timeout=3)
                    self.log('私信失败现场截图：'+path)
                except Exception:
                    self.log('私信失败现场截图未能保存')
            raise

    async def _send(self, customer, message, before_click):
        await self.check_reachable(customer)
        await follow_profile_button(self.page,self.guard,self.log)
        await click_profile_message_button(self.page,self.guard,self.log)
        reason = await self.private_account_reason()
        if reason:
            customer_skip(reason)
        editor = await wait_for_message_editor(
            self.page,self.s['message_editor'],self.guard,self.log)
        if editor is None:
            # Occasionally Douyin opens only the generic message list or drops
            # the first profile-button click. Close that drawer and retry once.
            await close_open_message_panel(self.page,self.guard,self.log)
            reason = await self.private_account_reason()
            if reason:
                customer_skip(reason)
            self.log('私信输入框未出现，正在重新打开私信窗口')
            await click_profile_message_button(self.page,self.guard,self.log)
            editor = await wait_for_message_editor(
                self.page,self.s['message_editor'],self.guard,self.log)
        if editor is None:
            try:
                diagnostic = '；页面截图已保存：'+await self.diagnose_page('open-chat')
            except Exception:
                diagnostic = '；页面截图保存失败'
            raise NeedsAttention('私信窗口已尝试打开两次，但输入框仍未出现'+diagnostic)
        await editor.fill(message,timeout=PROFILE_CLICK_TIMEOUT)
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
            await buttons[0].click(timeout=PROFILE_CLICK_TIMEOUT)
            await asyncio.sleep(3)
            await self.guard()
        except AccountNeedsAttention:
            raise
        except Exception as exc:
            raise UncertainSend('发送后结果不明确，禁止自动重试') from exc
