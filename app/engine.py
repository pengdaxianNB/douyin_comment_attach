import asyncio
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from PySide6.QtCore import QThread, Signal
from playwright.async_api import async_playwright, Error as PlaywrightError
from playwright._impl._async_base import mapping as async_api_mapping
from .ai import AI, get_key
from .browser import Browser, AccountNeedsAttention, CommentPageUnavailable, NeedsAttention, UncertainSend, SkippedCustomer, PrivateAccount
from .classifier import classify_comments
from .embedded_browser import MARKER_PREFIX, page_id, unregister_page

DEFAULTS = dict(days=7,comment_days=7,video_list_reset_hours=24,ai_concurrency=5,poll_seconds=300,scroll_limit=80,base_url='',model='',thinking_mode='default',thinking_effort='high',
    filter_prompt='识别评论中是否明确表达了对我提供的产品或服务的购买或咨询需求。产品和目标客户：请填写。不要把普通互动识别为需求。',
    message_prompt='根据客户评论写一条自然、简短的咨询回复。我的产品或服务：请填写。尊重对方，不夸大，不使用虚假承诺。',
    comment_panel='[data-e2e="comment-list"]',message_editor='[contenteditable="true"]')


def recent_comments(comments, days, now=None):
    """Keep comments inside the rolling UTC time window, including its boundary."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    cutoff = current - timedelta(days=max(1,int(days)))
    result = []
    for comment in comments:
        try:
            created = datetime.fromisoformat(str(comment.get('comment_time','')).replace('Z','+00:00'))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            else:
                created = created.astimezone(timezone.utc)
        except (TypeError,ValueError):
            continue
        if cutoff <= created <= current:
            result.append(comment)
    return result


class Engine(QThread):
    log = Signal(str)
    status = Signal(str,str)
    changed = Signal()
    browser_reset = Signal(str)

    def __init__(self, db, data):
        super().__init__()
        self.db,self.data = db,Path(data)
        self.loop = None
        self.commands = None
        self.running_accounts = set()
        self.lock = threading.Lock()
        self.ready = threading.Event()

    def command(self, op, account=None):
        if self.loop and self.commands:
            if op == 'start':
                with self.lock:
                    if account['id'] in self.running_accounts:
                        return
                    self.running_accounts.add(account['id'])
            self.loop.call_soon_threadsafe(self.commands.put_nowait,(op,account))

    def busy(self):
        with self.lock:
            return bool(self.running_accounts)

    def run(self):
        asyncio.run(self.main())

    async def main(self):
        self.loop = asyncio.get_running_loop()
        self.commands = asyncio.Queue()
        self.contexts,self.jobs = {},{}
        self.cdp_browser = None
        async with async_playwright() as pw:
            self.pw = pw
            self.ready.set()
            while True:
                op,account = await self.commands.get()
                try:
                    if op == 'shutdown':
                        break
                    ident = account['id']
                    if op == 'stop':
                        await self.stop_job(ident)
                    elif op == 'close':
                        await self.stop_job(ident)
                        self.contexts.pop(ident,None)
                        self.status.emit(ident,'已关闭')
                    elif op == 'diagnose':
                        if ident in self.jobs:
                            raise ValueError('请先停止该账号任务，再采集发送诊断')
                        if ident not in self.contexts:
                            raise ValueError('请先打开该账号的私信窗口')
                        path = await self.contexts[ident].diagnose_sender()
                        self.log.emit(account['name']+'：发送区域诊断已保存：'+path)
                    elif op == 'login':
                        b = await self.open(account)
                        await b.page.bring_to_front()
                        self.status.emit(ident,'内嵌浏览器已打开 · 请本人登录')
                    elif op == 'start' and ident not in self.jobs:
                        with self.lock:
                            self.running_accounts.add(ident)
                        self.jobs[ident] = asyncio.create_task(self.work(account))
                except Exception as exc:
                    if account:
                        with self.lock:
                            self.running_accounts.discard(account['id'])
                    self.log.emit('账号操作失败：'+str(exc))
            for ident in list(self.jobs):
                await self.stop_job(ident)
            self.contexts.clear()
        self.loop = None

    def settings(self):
        return {k:self.db.setting(k,v) for k,v in DEFAULTS.items()}

    async def open(self, account):
        ident = account['id']
        if ident not in self.contexts:
            page = await self.embedded_page(ident)
            b = Browser(page.context,self.settings(),self.data/'diagnostics'/ident,
                log=lambda text:self.log.emit(account['name']+'：'+text),page=page)
            self.contexts[ident] = b
            try:
                await b.init()
            except BaseException:
                self.contexts.pop(ident,None)
                raise
        return self.contexts[ident]

    async def rebuild_browser(self, account):
        """让 GUI 重建单个账号页面，然后重新绑定 Playwright。"""
        ident = account['id']
        contexts = getattr(self,'contexts',None)
        if contexts is not None:
            contexts.pop(ident,None)
        # Prevent embedded_page() from immediately rediscovering the stale
        # target before the queued GUI reset signal is handled.
        unregister_page(ident)
        self.status.emit(ident,'浏览器无响应，正在自动重建')
        self.log.emit(account['name']+'：检测到页面超时或内核异常，正在重建该账号浏览器')
        self.browser_reset.emit(ident)
        try:
            browser = await self.open(account)
        except Exception as exc:
            raise AccountNeedsAttention('浏览器自动重建失败，请停止该账号后点击“重启浏览器”：'+str(exc)) from exc
        self.log.emit(account['name']+'：浏览器已重建，将继续处理队列')
        return browser

    def cdp_endpoint(self):
        value = os.environ.get('QTWEBENGINE_REMOTE_DEBUGGING','').strip()
        if not value:
            raise ValueError('内嵌浏览器调试通道未配置，请重启应用')
        if '://' not in value:
            value = 'http://'+value
        return value

    async def connect_embedded_browser(self):
        if self.cdp_browser and self.cdp_browser.is_connected():
            return self.cdp_browser
        last_error = None
        for _ in range(40):
            try:
                # QtWebEngine intentionally omits browser-context download
                # management. Playwright's internal noDefaults option skips that
                # one unsupported command while retaining its normal Page API.
                impl = await self.pw.chromium._impl_obj.connect_over_cdp(
                    self.cdp_endpoint(),timeout=2000,noDefaults=True)
                self.cdp_browser = async_api_mapping.from_impl(impl)
                return self.cdp_browser
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.25)
        raise ValueError('无法连接应用内嵌浏览器：'+str(last_error))

    async def embedded_page(self, ident):
        browser = await self.connect_embedded_browser()
        expected = MARKER_PREFIX+ident
        for _ in range(60):
            expected_target = page_id(ident)
            for context in browser.contexts:
                for page in context.pages:
                    try:
                        session = await context.new_cdp_session(page)
                        info = await session.send('Target.getTargetInfo')
                        await session.detach()
                        target = (info.get('targetInfo') or {}).get('targetId')
                        if (expected_target and target == expected_target) or await page.title() == expected:
                            return page
                    except Exception:
                        pass
            await asyncio.sleep(0.25)
        raise ValueError('未找到该账号的内嵌页面，请重新点击“登录 / 查看内嵌浏览器”')

    async def stop_job(self, ident):
        task = self.jobs.pop(ident,None)
        if task:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        with self.lock:
            self.running_accounts.discard(ident)
        self.status.emit(ident,'已停止')

    async def scan_videos(self, account, browser, ai, videos, settings):
        """跳过暂时未就绪的单视频，不让慢加载终止整个监控任务。"""
        consecutive_failures = 0
        failed_videos = 0
        expired_comments = 0
        for video in videos:
            video_id = str(video.get('aweme_id') or video.get('id') or '')
            if (video_id and hasattr(self.db,'video_tasks')
                    and not self.db.rows('SELECT 1 FROM videos WHERE id=?',(video_id,))):
                self.log.emit(account['name']+'：检测到待分析视频列表已清空，结束当前旧列表扫描')
                break
            if 'aweme_id' not in video:
                video = dict(video,aweme_id=video_id,desc=video.get('title',''),
                             author={'nickname':video.get('author','')})
            tracking = bool(video_id and hasattr(self.db,'set_video_status'))
            if tracking:
                self.db.set_video_status(video_id,'scanning')
                self.changed.emit()
            try:
                comments = await browser.comments(video)
            except CommentPageUnavailable as exc:
                consecutive_failures += 1
                failed_videos += 1
                video_id = str(video.get('aweme_id','未知'))
                title = ' '.join(str(video.get('desc','')).split())[:40]
                detail = f' · {title}' if title else ''
                if tracking:
                    self.db.set_video_status(video_id,'failed',str(exc))
                    self.changed.emit()
                self.log.emit(f'{account["name"]}：已跳过视频 {video_id}{detail}：{exc}')
                if consecutive_failures == 3:
                    self.log.emit(f'{account["name"]}：连续 3 个视频加载失败，暂停 10 秒后继续；本轮跳过的视频下轮会自动重试')
                    await asyncio.sleep(10)
                continue
            except asyncio.CancelledError:
                if tracking:
                    self.db.set_video_status(video_id,'pending')
                    self.changed.emit()
                raise
            except Exception as exc:
                if tracking:
                    self.db.set_video_status(video_id,'failed',str(exc))
                    self.changed.emit()
                raise
            consecutive_failures = 0
            eligible = recent_comments(comments,settings.get('comment_days',DEFAULTS['comment_days']))
            expired_comments += len(comments)-len(eligible)
            try:
                await classify_comments(self.db,ai,eligible,settings['ai_concurrency'],
                    lambda text:self.log.emit(account['name']+'：'+text),self.changed.emit)
            except asyncio.CancelledError:
                if tracking:
                    self.db.set_video_status(video_id,'pending')
                    self.changed.emit()
                raise
            except Exception as exc:
                if tracking:
                    self.db.set_video_status(video_id,'failed',str(exc))
                    self.changed.emit()
                raise
            if tracking:
                self.db.set_video_status(video_id,'done')
                self.changed.emit()
        if expired_comments:
            days = settings.get('comment_days',DEFAULTS['comment_days'])
            self.log.emit(f'{account["name"]}：本轮已过滤 {expired_comments} 条超过最近 {days} 天的评论，未提交 AI 分析')
        if failed_videos:
            self.log.emit(f'{account["name"]}：本轮有 {failed_videos} 个视频评论区未就绪，已跳过并保持任务运行，下轮自动重试')

    async def rebuild_video_list(self, account, browser, settings):
        """Discover videos from every configured source before comment scans."""
        bloggers = self.db.rows('SELECT * FROM bloggers ORDER BY name,id')
        keywords = self.db.rows('SELECT * FROM keywords ORDER BY id')
        if not bloggers and not keywords:
            raise ValueError('请先添加监控博主或找视频关键词')
        for row in keywords:
            keyword = row['keyword']
            self.status.emit(account['id'],'关键词找视频：'+keyword)
            videos = await browser.keyword_videos(keyword,7)
            for video in videos:
                self.db.add_video(video,'keyword',keyword,keyword)
            self.log.emit('关键词“'+keyword+'”：近 7 天已加入待分析视频 '+str(len(videos))+' 个')
            self.changed.emit()
        for blogger in bloggers:
            self.status.emit(account['id'],'整理视频：'+blogger['name'])
            videos = await browser.videos(blogger['id'],settings['days'])
            for video in videos:
                self.db.add_video(video,'blogger',blogger['name'],blogger['id'])
            self.log.emit(blogger['name']+'：已加入待分析视频 '+str(len(videos))+' 个')
            self.changed.emit()

    async def work(self, account):
        ident = account['id']
        customer = None
        message = ''
        clicked = False
        try:
            s = self.settings()
            ai = AI(s, log=lambda text: self.log.emit(account["name"]+"："+text))
            if not s['base_url'] or not s['model'] or not get_key():
                raise ValueError('请先配置 AI 接口、模型及 API Key')
            b = await self.open(account)
            b.s = s
            self.status.emit(ident,'运行中')
            while True:
                if account['role']=='main':
                    rebuild = self.db.prepare_video_scan(s['video_list_reset_hours'])
                    if rebuild:
                        self.status.emit(ident,'正在重建待分析视频列表')
                        try:
                            await self.rebuild_video_list(account,b,s)
                        except BaseException:
                            # A partial list must not be treated as fresh for a
                            # full reset interval. The next run rebuilds it.
                            self.db.save_settings({'video_list_built_at':0})
                            raise
                    else:
                        self.log.emit(account['name']+'：视频列表未到清空时间，已重置逐视频扫描状态')
                        self.changed.emit()
                    videos = self.db.video_tasks('pending')
                    self.log.emit(account['name']+'：本轮待分析视频共 '+str(len(videos))+' 个')
                    if not videos:
                        self.status.emit(ident,'本轮没有找到待分析视频')
                        await asyncio.sleep(s['poll_seconds'])
                        continue
                    await self.scan_videos(account,b,ai,videos,s)
                    for warning in b.warnings:
                        self.log.emit(warning)
                    b.warnings.clear()
                    if not self.db.video_tasks():
                        self.status.emit(ident,'视频列表为空，立即开始新一轮找视频')
                        self.log.emit(account['name']+'：待分析视频列表为空，立即重新从全部来源找视频')
                        continue
                    self.status.emit(ident,'等待下一轮')
                    await asyncio.sleep(s['poll_seconds'])
                else:
                    customer = self.db.claim(ident)
                    if not customer:
                        quota = self.db.sender_quota(ident)
                        if quota['limit'] and quota['used']>=quota['limit']:
                            self.status.emit(ident,f'今日已达上限 {quota["used"]}/{quota["limit"]} · 0 点自动恢复')
                        else:
                            self.status.emit(ident,'等待客户')
                        await asyncio.sleep(5)
                        continue
                    self.changed.emit()
                    self.status.emit(ident,'处理：'+customer['name'])
                    clicked = False
                    message = str(customer.get('message','') or '')
                    skip_cooldown = False
                    try:
                        await asyncio.wait_for(b.check_reachable(customer),timeout=45)
                        if message.strip():
                            self.status.emit(ident,'复用已有文案：'+customer['name'])
                        else:
                            self.status.emit(ident,'生成文案：'+customer['name'])
                            message = await ai.message(customer)
                        self.db.finish(customer['uid'],ident,'processing',message)
                        def before_click():
                            nonlocal clicked
                            self.db.start_send(customer['uid'],ident,message)
                            clicked = True
                        self.status.emit(ident,'打开会话并发送：'+customer['name'])
                        await asyncio.wait_for(b.send(customer,message,before_click),timeout=90)
                        self.db.finish(customer['uid'],ident,'sent',message)
                    except SkippedCustomer as exc:
                        self.db.finish(customer['uid'],ident,'skipped',message,str(exc))
                        self.log.emit(account['name']+'：客户 '+customer['name']+' 已跳过：'+str(exc))
                        skip_cooldown = isinstance(exc,PrivateAccount)
                    except UncertainSend as exc:
                        self.db.finish(customer['uid'],ident,'failed',message,str(exc))
                        self.log.emit(account['name']+'：'+str(exc))
                    except AccountNeedsAttention:
                        # Verification/login/rate-limit prompts affect the whole
                        # account and still require an operator before continuing.
                        raise
                    except NeedsAttention as exc:
                        # Profile/chat DOM failures happen before the send click.
                        # Fail only this customer instead of stopping the sender.
                        self.db.finish(customer['uid'],ident,'failed',message,str(exc))
                        self.log.emit(account['name']+'：客户 '+customer['name']+
                                      ' 处理失败，从号将继续运行：'+str(exc))
                    except (PlaywrightError, asyncio.TimeoutError) as exc:
                        # A stalled locator must not end the whole sender job.
                        # Never requeue a customer once submission has started.
                        detail = ('发送结果不明，禁止自动重发' if clicked else
                                  '页面操作超时或失败，尚未提交发送')+'：'+(str(exc) or '超过处理时限')
                        self.db.finish(customer['uid'],ident,'failed',message,detail)
                        self.log.emit(account['name']+'：'+detail)
                        b = await self.rebuild_browser(account)
                    self.changed.emit()
                    customer = None
                    if skip_cooldown:
                        self.status.emit(ident,'私密账号已跳过，继续处理')
                        continue
                    interval = self.db.rows('SELECT interval FROM accounts WHERE id=?',(ident,))[0]['interval']
                    self.status.emit(ident,f'间隔等待 {interval} 秒')
                    await asyncio.sleep(interval)
        except asyncio.CancelledError:
            if customer:
                self.db.finish(customer['uid'],ident,'failed' if clicked else 'pending',message,'任务已停止；发送结果不明，请勿重复联系' if clicked else '任务已停止')
            raise
        except Exception as exc:
            if customer:
                self.db.finish(customer['uid'],ident,'failed',message,str(exc))
            self.status.emit(ident,'需处理：'+str(exc))
            self.log.emit(account['name']+'：'+str(exc))
        finally:
            with self.lock:
                self.running_accounts.discard(ident)
            self.jobs.pop(ident,None)
            self.changed.emit()
