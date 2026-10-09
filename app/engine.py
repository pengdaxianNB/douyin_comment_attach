import asyncio
import os
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from PySide6.QtCore import QThread, Signal
from playwright.async_api import async_playwright, Error as PlaywrightError
from playwright._impl._async_base import mapping as async_api_mapping
from .ai import AI, get_key
from .browser import Browser, AccountNeedsAttention, SearchPageUnavailable, CommentPageUnavailable, VideoUnavailable, NeedsAttention, UncertainSend, SkippedCustomer, PrivateAccount, MissingAccount, detach_cdp_session
from .classifier import classify_comments
from .embedded_browser import MARKER_PREFIX, page_id, unregister_page

DEFAULTS = dict(collection_accounts=1,days=7,comment_days=7,video_list_reset_hours=24,ai_concurrency=5,poll_seconds=300,scroll_limit=80,base_url='',model='',thinking_mode='default',thinking_effort='high',
    filter_prompt='识别评论中是否明确表达了对我提供的产品或服务的购买或咨询需求。产品和目标客户：请填写。不要把普通互动识别为需求。',
    message_prompt='根据客户评论写一条自然、简短的咨询回复。我的产品或服务：请填写。尊重对方，不夸大，不使用虚假承诺。',
    comment_panel='[data-e2e="comment-list"]',message_editor='[contenteditable="true"]')

# A soft unload happens after every completed page workflow. Replacing the
# whole QWebEnginePage periodically also releases renderer/process state that a
# normal navigation cannot reach, while preserving the account profile/login.
PAGE_RECYCLE_OPERATIONS = 20
PAGE_CLEANUP_TIMEOUT = 15
BROWSER_OPEN_TIMEOUT = 45
BROWSER_REBUILD_ATTEMPTS = 3

# Unattended stability: transient failures back off and retry instead of ending
# the job. After this many consecutive failures the account is stopped so an
# operator can look at it.
MAX_TRANSIENT_FAILURES = 5


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
        self.cluster_mode = None
        self.cluster_task = None
        self.cluster_stopping = False
        self.page_operations = {}
        self.lock = threading.Lock()
        self.ready = threading.Event()

    def command(self, op, account=None):
        if self.loop and self.commands:
            if op == 'start_cluster':
                mode = account['mode']
                accounts = self.cluster_accounts(mode)
                with self.lock:
                    if self.cluster_mode or self.cluster_stopping or self.running_accounts:
                        raise ValueError('请先停止当前集群任务，再切换模式')
                    self.cluster_mode = mode
                    self.running_accounts.update(row['id'] for row in accounts)
                account = dict(mode=mode,accounts=accounts)
            self.loop.call_soon_threadsafe(self.commands.put_nowait,(op,account))

    def cluster_accounts(self, mode):
        if mode not in ('comments','messages'):
            raise ValueError('不支持的集群模式')
        accounts = self.db.rows('SELECT * FROM accounts ORDER BY rowid')
        if not accounts:
            raise ValueError('请先添加账号')
        if mode == 'comments':
            count = max(1,min(len(accounts),int(self.db.setting('collection_accounts',1))))
            accounts = accounts[:count]
        return accounts

    def busy(self):
        with self.lock:
            return bool(self.cluster_mode or self.cluster_stopping or self.running_accounts)

    def run(self):
        try:
            asyncio.run(self.main())
        except Exception as exc:
            # Without this the worker thread would die silently and every later
            # button press would be dropped by command().
            self.loop = None
            self.log.emit('后台引擎异常退出，请重启应用：'+type(exc).__name__+'：'+str(exc))
            with self.lock:
                self.running_accounts.clear()
                self.cluster_mode = None
                self.cluster_stopping = False
            self.changed.emit()

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
                    if op == 'start_cluster':
                        self.cluster_task = asyncio.create_task(self.run_cluster(account['accounts'],account['mode']))
                        continue
                    if op == 'stop_cluster':
                        await self.stop_cluster()
                        continue
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
                except Exception as exc:
                    self.log.emit('账号操作失败：'+str(exc))
            await self.stop_cluster()
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
                await asyncio.wait_for(b.init(),timeout=BROWSER_OPEN_TIMEOUT)
            except BaseException:
                self.contexts.pop(ident,None)
                raise
        return self.contexts[ident]

    async def rebuild_browser(self, account, memory_cleanup=False):
        """让 GUI 重建单个账号页面，然后重新绑定 Playwright。"""
        ident = account['id']
        self.page_operations[ident] = 0
        contexts = getattr(self,'contexts',None)
        if memory_cleanup:
            self.status.emit(ident,'正在定期释放浏览器内存')
            self.log.emit(account['name']+'：达到页面操作回收周期，正在重建该账号浏览器')
        else:
            self.status.emit(ident,'浏览器无响应，正在自动重建')
            self.log.emit(account['name']+'：检测到页面超时或内核异常，正在重建该账号浏览器')
        for attempt in range(1,BROWSER_REBUILD_ATTEMPTS+1):
            if contexts is not None:
                contexts.pop(ident,None)
            unregister_page(ident)
            self.browser_reset.emit(ident)
            try:
                browser = await asyncio.wait_for(self.open(account),timeout=BROWSER_OPEN_TIMEOUT)
            except AccountNeedsAttention:
                raise
            except Exception as exc:
                reason = str(exc) or '连接或初始化超过处理时限'
                if attempt == BROWSER_REBUILD_ATTEMPTS:
                    raise AccountNeedsAttention(
                        f'浏览器连续 {attempt} 次自动重建失败，请停止该账号后点击“重启浏览器”：'+reason) from exc
                delay = 5*attempt
                self.status.emit(ident,f'浏览器重建失败，{delay} 秒后重试（{attempt}/{BROWSER_REBUILD_ATTEMPTS}）')
                self.log.emit(account['name']+f'：浏览器重建暂时失败，{delay} 秒后自动重试：'+reason)
                await asyncio.sleep(delay)
                continue
            self.log.emit(account['name']+'：浏览器已重建，将继续处理队列')
            return browser

    async def maintain_browser_memory(self, account, browser):
        """Bound per-account Chromium growth between completed operations."""
        ident = account.get('id',account['name'])
        operations = self.page_operations.get(ident,0) + 1
        self.page_operations[ident] = operations
        if operations >= PAGE_RECYCLE_OPERATIONS:
            self.log.emit(account['name']+'：已完成多轮页面操作，正在释放浏览器内存')
            warnings = list(getattr(browser,'warnings',[]))
            rebuilt = await self.rebuild_browser(account,memory_cleanup=True)
            if warnings and hasattr(rebuilt,'warnings'):
                rebuilt.warnings[:0] = warnings
            return rebuilt
        release = getattr(browser,'release_page_memory',None)
        if release is not None:
            self.status.emit(ident,f'正在释放页面内存（最多 {PAGE_CLEANUP_TIMEOUT} 秒）')
            try:
                await asyncio.wait_for(release(),timeout=PAGE_CLEANUP_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # CDP heap collection has no built-in timeout. If it hangs
                # after unloading to about:blank, rebuild instead of leaving
                # the sender stuck between two customers indefinitely.
                self.log.emit(account['name']+'：页面内存清理超时或失败，正在自动重建：'+(str(exc) or '超过处理时限'))
                return await self.rebuild_browser(account)
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
        deadline = asyncio.get_running_loop().time()+15

        async def matches(context,page,expected_target):
            session = None
            try:
                session = await context.new_cdp_session(page)
                info = await session.send('Target.getTargetInfo')
                target = (info.get('targetInfo') or {}).get('targetId')
                # Known target IDs don't require touching another account's
                # renderer, which may be frozen and never answer title().
                if expected_target:
                    return target == expected_target
                return await page.title() == expected
            finally:
                if session is not None:
                    await detach_cdp_session(session)

        while asyncio.get_running_loop().time() < deadline:
            expected_target = page_id(ident)
            if expected_target is None:
                # A GUI reset is pending. Do not bind an old marker page while
                # the new target has not been registered yet.
                await asyncio.sleep(0.25)
                continue
            for context in browser.contexts:
                for page in context.pages:
                    remaining = deadline-asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        if await asyncio.wait_for(matches(context,page,expected_target),timeout=min(2,remaining)):
                            if page_id(ident) == expected_target:
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
            empty = not self.running_accounts
        self.status.emit(ident,'已停止')
        if empty and self.cluster_task and asyncio.current_task() is not self.cluster_task:
            await self.stop_cluster()

    async def stop_cluster(self):
        with self.lock:
            self.cluster_stopping = True
            stopped = set(self.running_accounts)
        task = self.cluster_task
        if task:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        for ident in list(self.jobs):
            await self.stop_job(ident)
        with self.lock:
            self.running_accounts.clear()
            self.cluster_mode = None
            self.cluster_stopping = False
        self.cluster_task = None
        for ident in stopped:
            self.status.emit(ident,'已停止')
        self.changed.emit()

    async def run_cluster(self, accounts, mode):
        """Own the mode until every account has stopped, including cancellation."""
        try:
            if mode == 'messages':
                tasks = []
                for account in accounts:
                    task = asyncio.create_task(self.work(account,mode))
                    self.jobs[account['id']] = task
                    tasks.append(task)
                await asyncio.gather(*tasks,return_exceptions=True)
            else:
                await self.collect_cluster(accounts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.emit('集群任务已停止：'+str(exc))
            for account in accounts:
                self.status.emit(account['id'],'需处理：'+str(exc))
        finally:
            # Gather cancellation alone is insufficient when the coordinator
            # is between stages. Await all page work before releasing the mode.
            for ident in list(self.jobs):
                await self.stop_job(ident)
            with self.lock:
                self.running_accounts.clear()
                if not self.cluster_stopping:
                    self.cluster_mode = None
            self.cluster_task = None
            self.changed.emit()

    def collection_sources(self):
        return [(kind,row) for kind,table,order in (
            ('keyword','keywords','id'),('search','search_keywords','id'),
            ('blogger','bloggers','name,id'),('manual','manual_videos','added_at,id'))
            for row in self.db.rows(f'SELECT * FROM {table} ORDER BY {order}')]

    async def collect_cluster(self, accounts):
        s = self.settings()
        if not s['base_url'] or not s['model'] or not get_key():
            raise ValueError('请先配置 AI 接口、模型及 API Key')
        sessions = {}

        async def initialize(account):
            ident = account['id']
            try:
                browser = await self.open(account)
                browser.s = s
                ai = AI(s,log=lambda text:self.log.emit(account['name']+'：'+text))
                sessions[ident] = [account,browser,ai]
                self.status.emit(ident,'集群采集已就绪')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with self.lock:
                    self.running_accounts.discard(ident)
                self.status.emit(ident,'需处理：'+str(exc))
                self.log.emit(account['name']+'：'+str(exc))

        tasks = [asyncio.create_task(initialize(account)) for account in accounts]
        self.jobs.update({account['id']:task for account,task in zip(accounts,tasks)})
        await asyncio.gather(*tasks,return_exceptions=True)
        self.jobs.clear()
        while sessions:
            with self.lock:
                active = set(self.running_accounts)
            sessions = {ident:session for ident,session in sessions.items() if ident in active}
            if not sessions:
                break
            sources = self.collection_sources()
            if not sources and not self.db.video_tasks():
                raise ValueError('请先添加关键词、抖音搜索词、监控博主或手动视频链接')
            rebuild = self.db.prepare_video_scan(s['video_list_reset_hours'],sources=sources)
            if rebuild:
                pending_sources = self.db.pending_discovery_sources(sources)
                self.log.emit(f'集群：找视频进度 {len(sources)-len(pending_sources)}/{len(sources)}，继续未完成来源 {len(pending_sources)} 个')
                queue = deque(pending_sources)
                complete = [True]
                await self.collection_stage(sessions,queue,s,True,complete)
                if not self.db.finish_video_discovery():
                    self.log.emit('集群：找视频尚未完成，已保存来源断点，下轮继续找视频后再分析评论')
                    for ident in sessions:
                        self.status.emit(ident,'找视频未完成，等待断点续跑')
                    if sessions:
                        await asyncio.sleep(s['poll_seconds'])
                    continue
                self.log.emit('集群：全部来源找视频已完成，开始分析评论')
            videos = deque(self.db.video_tasks('pending'))
            self.log.emit(f'集群：本轮待分析视频 {len(videos)} 个，采集账号 {len(sessions)} 个')
            await self.collection_stage(sessions,videos,s,False,[True])
            self.changed.emit()
            for ident in sessions:
                self.status.emit(ident,'等待下一轮集群采集')
            if not sessions:
                break
            await asyncio.sleep(s['poll_seconds'])

    async def discover_source(self, account, browser, settings, source):
        """Checkpoint only completed sources; partial results safely deduplicate."""
        self.db.set_discovery_status(source,'running')
        completed = False
        try:
            completed = await self.rebuild_video_list(account,browser,settings,sources=[source])
            return completed
        finally:
            self.db.set_discovery_status(source,'done' if completed else 'pending')

    async def collection_stage(self, sessions, queue, settings, discovery, complete):
        """Claim each source/video once on the engine loop; no account owns a list."""
        async def worker(ident, session):
            account,browser,ai = session
            failures = 0
            try:
                while queue:
                    item = queue.popleft()
                    try:
                        if discovery:
                            result = await self.discover_source(account,browser,settings,item)
                            complete[0] = complete[0] and result
                            browser = self.contexts.get(ident,browser)
                        else:
                            self.status.emit(ident,'集群扫描评论：'+item['title'])
                            browser = await self.scan_videos(account,browser,ai,[item],settings)
                        session[1] = browser
                        for warning in browser.warnings:
                            self.log.emit(account['name']+'：'+warning)
                        browser.warnings.clear()
                        failures = 0
                    except (asyncio.CancelledError,AccountNeedsAttention,ValueError):
                        queue.appendleft(item)
                        raise
                    except Exception as exc:
                        if discovery:
                            queue.append(item)
                        failures += 1
                        if failures >= MAX_TRANSIENT_FAILURES:
                            raise RuntimeError(f'连续 {failures} 次采集异常：{exc}') from exc
                        delay = min(120,15*failures)
                        self.log.emit(account['name']+f'：采集异常，{delay} 秒后继续：{exc}')
                        if isinstance(exc,(PlaywrightError,asyncio.TimeoutError)):
                            browser = await self.rebuild_browser(account)
                            browser.s = settings
                            session[1] = browser
                        await asyncio.sleep(delay)
            except asyncio.CancelledError:
                sessions.pop(ident,None)
                raise
            except Exception as exc:
                sessions.pop(ident,None)
                with self.lock:
                    self.running_accounts.discard(ident)
                self.status.emit(ident,'需处理：'+str(exc))
                self.log.emit(account['name']+'：已停止该账号，其余账号继续领取任务：'+str(exc))
            finally:
                self.jobs.pop(ident,None)
                self.changed.emit()

        # A failing account can return its item after other workers emptied the
        # queue. Run survivors again so that item is not stranded until tomorrow.
        while queue and sessions:
            tasks = []
            for ident,session in list(sessions.items()):
                task = asyncio.create_task(worker(ident,session))
                self.jobs[ident] = task
                tasks.append(task)
            await asyncio.gather(*tasks,return_exceptions=True)

    async def scan_videos(self, account, browser, ai, videos, settings):
        """跳过单视频异常，页面卡死时重建浏览器后继续队列。"""
        consecutive_failures = 0
        failed_videos = 0
        browser_failed_videos = 0
        unavailable_videos = 0
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
            browser_restarts = 0
            while True:
                if tracking:
                    self.db.set_video_status(video_id,'scanning')
                    self.changed.emit()
                try:
                    comments = await browser.comments(video)
                    break
                except VideoUnavailable as exc:
                    unavailable_videos += 1
                    consecutive_failures = 0
                    title = ' '.join(str(video.get('desc','')).split())[:40]
                    detail = f' · {title}' if title else ''
                    if tracking:
                        self.db.set_video_status(video_id,'failed',str(exc))
                        self.changed.emit()
                    self.log.emit(f'{account["name"]}：视频 {video_id or "未知"}{detail} 已失效，已标记并继续下一个：{exc}')
                    comments = None
                    break
                except AccountNeedsAttention:
                    # Login/verification prompts are not the video's fault; keep
                    # it queued for when the operator resumes the account.
                    if tracking:
                        self.db.set_video_status(video_id,'pending')
                        self.changed.emit()
                    raise
                except NeedsAttention as exc:
                    # Includes CommentPageUnavailable and abnormal API replies:
                    # skip this one work and keep the whole monitor running.
                    consecutive_failures += 1
                    failed_videos += 1
                    title = ' '.join(str(video.get('desc','')).split())[:40]
                    detail = f' · {title}' if title else ''
                    if tracking:
                        self.db.set_video_status(video_id,'failed',str(exc))
                        self.changed.emit()
                    self.log.emit(f'{account["name"]}：已跳过视频 {video_id or "未知"}{detail}：{exc}')
                    if consecutive_failures == 3:
                        self.log.emit(f'{account["name"]}：连续 3 个视频加载失败，暂停 10 秒后继续；本轮跳过的视频下轮会自动重试')
                        await asyncio.sleep(10)
                    comments = None
                    break
                except asyncio.CancelledError:
                    if tracking:
                        self.db.set_video_status(video_id,'pending')
                        self.changed.emit()
                    raise
                except (PlaywrightError, asyncio.TimeoutError) as exc:
                    # A frozen renderer makes subsequent navigations fail as
                    # well. Rebuild the account page and retry this exact
                    # video once; after a second failure rebuild once more for
                    # the next video and leave this item failed.
                    browser_restarts += 1
                    if tracking:
                        self.db.set_video_status(video_id,'pending')
                        self.changed.emit()
                    reason = str(exc) or '页面操作超时'
                    self.log.emit(f'{account["name"]}：视频 {video_id or "未知"} 扫描时浏览器无响应：{reason}')
                    browser = await self.rebuild_browser(account)
                    browser.s = settings
                    if browser_restarts == 1:
                        self.log.emit(f'{account["name"]}：已自动重建浏览器，重试当前视频 {video_id or "未知"}')
                        continue
                    browser_failed_videos += 1
                    consecutive_failures = 0
                    if tracking:
                        self.db.set_video_status(video_id,'failed','浏览器重建后重试仍失败：'+reason)
                        self.changed.emit()
                    self.log.emit(f'{account["name"]}：视频 {video_id or "未知"} 重试仍超时，已跳过并继续队列')
                    comments = None
                    break
                except Exception as exc:
                    if tracking:
                        self.db.set_video_status(video_id,'failed',str(exc))
                        self.changed.emit()
                    raise
            if comments is not None:
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
            browser = await self.maintain_browser_memory(account,browser)
        if expired_comments:
            days = settings.get('comment_days',DEFAULTS['comment_days'])
            self.log.emit(f'{account["name"]}：本轮已过滤 {expired_comments} 条超过最近 {days} 天的评论，未提交 AI 分析')
        if failed_videos:
            self.log.emit(f'{account["name"]}：本轮有 {failed_videos} 个视频评论区未就绪，已跳过并保持任务运行，下轮自动重试')
        if browser_failed_videos:
            self.log.emit(f'{account["name"]}：本轮有 {browser_failed_videos} 个视频在浏览器重建后仍超时，已跳过并继续运行')
        if unavailable_videos:
            self.log.emit(f'{account["name"]}：本轮发现 {unavailable_videos} 个已失效视频，当前列表不再重试')
        return browser

    async def rebuild_video_list(self, account, browser, settings, sources=None):
        """Discover videos from every configured source before comment scans."""
        bloggers = self.db.rows('SELECT * FROM bloggers ORDER BY name,id') if sources is None else [r for kind,r in sources if kind=='blogger']
        keywords = self.db.rows('SELECT * FROM keywords ORDER BY id') if sources is None else [r for kind,r in sources if kind=='keyword']
        searches = self.db.rows('SELECT * FROM search_keywords ORDER BY id') if sources is None else [r for kind,r in sources if kind=='search']
        manual_videos = self.db.rows('SELECT * FROM manual_videos ORDER BY added_at,id') if sources is None else [r for kind,r in sources if kind=='manual']
        if not bloggers and not keywords and not searches and not manual_videos:
            raise ValueError('请先添加关键词、抖音搜索词、监控博主或手动视频链接')
        complete = True
        for row in keywords:
            keyword = row['keyword']
            self.status.emit(account['id'],'抖音指数找视频：'+keyword)
            try:
                videos = await browser.keyword_videos(keyword,7)
            except AccountNeedsAttention:
                raise
            except NeedsAttention as exc:
                complete = False
                self.log.emit(account['name']+f'：关键词“{keyword}”视频获取失败，已跳过并继续其他来源：{exc}')
                browser = await self.maintain_browser_memory(account,browser)
                browser.s = settings
                continue
            except (PlaywrightError, asyncio.TimeoutError) as exc:
                complete = False
                self.log.emit(account['name']+f'：关键词“{keyword}”页面操作超时，正在重建浏览器并继续其他来源：{exc or "超时"}')
                browser = await self.rebuild_browser(account)
                browser.s = settings
                continue
            for video in videos:
                self.db.add_video(video,'keyword',keyword,keyword)
            self.log.emit('关键词“'+keyword+'”：近 7 天已加入待分析视频 '+str(len(videos))+' 个')
            self.changed.emit()
            browser = await self.maintain_browser_memory(account,browser)
            browser.s = settings
        for row in searches:
            keyword = row['keyword']
            videos = None
            for attempt in range(2):
                self.status.emit(account['id'],'抖音搜索找视频：'+keyword+('（重建后重试）' if attempt else ''))
                warning_count = len(getattr(browser,'warnings',[]))
                try:
                    videos = await browser.search_videos(keyword)
                except AccountNeedsAttention:
                    raise
                except (SearchPageUnavailable, PlaywrightError, asyncio.TimeoutError) as exc:
                    reason = str(exc) or '页面操作超时'
                    if attempt == 0:
                        self.log.emit(account['name']+f'：抖音搜索“{keyword}”加载异常，正在重建浏览器并重试当前词：{reason}')
                        browser = await self.rebuild_browser(account)
                        browser.s = settings
                        continue
                    complete = False
                    self.log.emit(account['name']+f'：抖音搜索“{keyword}”重建后重试仍失败，已保留其他结果，下一轮重新获取：{reason}')
                except NeedsAttention as exc:
                    complete = False
                    self.log.emit(account['name']+f'：抖音搜索“{keyword}”视频获取失败，已跳过并继续其他来源：{exc}')
                else:
                    # Partial feeds are useful now, but must not mark the
                    # source list as fresh for the entire reset interval.
                    if len(getattr(browser,'warnings',[])) > warning_count:
                        complete = False
                break
            if videos is not None:
                for video in videos:
                    self.db.add_video(video,'search',keyword,keyword)
                self.log.emit('抖音搜索“'+keyword+'”：已加入待分析视频 '+str(len(videos))+' 个')
                self.changed.emit()
            # Each query is a full SPA workflow. Counting only the whole batch
            # allowed hundreds of searches to bypass the renderer recycle.
            browser = await self.maintain_browser_memory(account,browser)
            browser.s = settings
        for blogger in bloggers:
            self.status.emit(account['id'],'整理视频：'+blogger['name'])
            try:
                videos = await browser.videos(blogger['id'],settings['days'])
            except AccountNeedsAttention:
                raise
            except NeedsAttention as exc:
                complete = False
                self.log.emit(account['name']+f'：博主“{blogger["name"]}”视频获取失败，已跳过并继续其他来源：{exc}')
                browser = await self.maintain_browser_memory(account,browser)
                browser.s = settings
                continue
            except (PlaywrightError, asyncio.TimeoutError) as exc:
                complete = False
                self.log.emit(account['name']+f'：博主“{blogger["name"]}”页面操作超时，正在重建浏览器并继续其他来源：{exc or "超时"}')
                browser = await self.rebuild_browser(account)
                browser.s = settings
                continue
            for video in videos:
                self.db.add_video(video,'blogger',blogger['name'],blogger['id'])
            self.log.emit(blogger['name']+'：已加入待分析视频 '+str(len(videos))+' 个')
            self.changed.emit()
            browser = await self.maintain_browser_memory(account,browser)
            browser.s = settings
        for item in manual_videos:
            video = {'aweme_id':item['id'],'desc':item['title'],'author':{},'create_time':0,'url':item['url']}
            self.db.add_video(video,'manual','手动录入',item['url'])
        if manual_videos:
            self.log.emit('手动录入：已加入待分析视频 '+str(len(manual_videos))+' 个')
            self.changed.emit()
        return complete

    async def monitor_round(self, account, b, ai, s):
        """Standalone monitoring pass, also useful for browser recovery checks."""
        ident = account['id']
        resume = bool(self.db.video_tasks('pending') or
                      self.db.rows("SELECT 1 FROM videos WHERE status='scanning' LIMIT 1"))
        sources = self.collection_sources()
        rebuild = self.db.prepare_video_scan(s['video_list_reset_hours'],sources=sources)
        if rebuild:
            pending_sources = self.db.pending_discovery_sources(sources)
            self.status.emit(ident,f'断点续跑找视频：剩余 {len(pending_sources)} 个来源')
            for source in pending_sources:
                await self.discover_source(account,b,s,source)
                b = getattr(self,'contexts',{}).get(ident,b)
            if not self.db.finish_video_discovery():
                self.log.emit(account['name']+'：找视频未完成，已保存来源断点，下轮继续后再分析评论')
                await asyncio.sleep(s['poll_seconds'])
                return b
        else:
            if resume:
                self.log.emit(account['name']+'：视频列表未到重建时间，从上次断点继续扫描未完成视频')
            else:
                self.log.emit(account['name']+'：上一轮视频已全部处理，开始新一轮评论扫描')
            self.changed.emit()
        videos = self.db.video_tasks('pending')
        self.log.emit(account['name']+'：本轮待分析视频共 '+str(len(videos))+' 个')
        if not videos:
            self.status.emit(ident,'本轮没有找到待分析视频')
            await asyncio.sleep(s['poll_seconds'])
            return b
        b = await self.scan_videos(account,b,ai,videos,s)
        for warning in b.warnings:
            self.log.emit(warning)
        b.warnings.clear()
        if not self.db.video_tasks():
            self.status.emit(ident,'视频列表为空，立即开始新一轮找视频')
            self.log.emit(account['name']+'：待分析视频列表为空，立即重新从全部来源找视频')
            return b
        self.status.emit(ident,'等待下一轮')
        await asyncio.sleep(s['poll_seconds'])
        return b

    async def work(self, account, mode='messages'):
        ident = account['id']
        customer = None
        message = ''
        clicked = False
        monitor_failures = 0
        ai_failures = 0
        try:
            s = self.settings()
            ai = AI(s, log=lambda text: self.log.emit(account["name"]+"："+text))
            if not s['base_url'] or not s['model'] or not get_key():
                raise ValueError('请先配置 AI 接口、模型及 API Key')
            b = await self.open(account)
            b.s = s
            self.status.emit(ident,'运行中')
            while True:
                if mode=='comments':
                    try:
                        b = await self.monitor_round(account,b,ai,s)
                        monitor_failures = 0
                    except (AccountNeedsAttention, asyncio.CancelledError, ValueError):
                        # Account prompts and configuration problems need a person.
                        raise
                    except Exception as exc:
                        # An unexpected page/DB hiccup must not end an unattended
                        # monitor; back off, refresh the page, and try again.
                        monitor_failures += 1
                        reason = type(exc).__name__+('：'+str(exc) if str(exc) else '')
                        if monitor_failures >= MAX_TRANSIENT_FAILURES:
                            raise RuntimeError(f'连续 {monitor_failures} 轮监控异常，已停止：{reason}') from exc
                        delay = min(120,15*monitor_failures)
                        self.log.emit(f'{account["name"]}：监控本轮异常（{monitor_failures}/{MAX_TRANSIENT_FAILURES}），{delay} 秒后自动重试：{reason}')
                        self.status.emit(ident,f'异常，{delay} 秒后重试')
                        if isinstance(exc,(PlaywrightError,asyncio.TimeoutError)):
                            b = await self.rebuild_browser(account)
                            b.s = s
                        await asyncio.sleep(delay)
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
                            try:
                                message = await ai.message(customer)
                            except ValueError as exc:
                                # AI outages/rate limits happen before any page
                                # side effect: put the customer back in the queue
                                # and back off instead of stopping the sender.
                                ai_failures += 1
                                self.db.finish(customer['uid'],ident,'pending','','AI 文案生成失败，已放回队列：'+str(exc))
                                customer = None
                                self.changed.emit()
                                if ai_failures >= MAX_TRANSIENT_FAILURES:
                                    raise ValueError(f'AI 连续 {ai_failures} 次生成文案失败，已停止：{exc}') from exc
                                delay = min(120,15*ai_failures)
                                self.log.emit(f'{account["name"]}：AI 文案生成失败（{ai_failures}/{MAX_TRANSIENT_FAILURES}），客户已放回队列，{delay} 秒后重试：{exc}')
                                self.status.emit(ident,f'AI 异常，{delay} 秒后重试')
                                await asyncio.sleep(delay)
                                continue
                            ai_failures = 0
                        self.db.finish(customer['uid'],ident,'processing',message)
                        def before_click():
                            nonlocal clicked
                            self.db.start_send(customer['uid'],ident,message)
                            clicked = True
                        self.status.emit(ident,'打开会话并发送：'+customer['name'])
                        await asyncio.wait_for(b.send(customer,message,before_click),timeout=120)
                        self.db.finish(customer['uid'],ident,'sent',message)
                    except SkippedCustomer as exc:
                        self.db.finish(customer['uid'],ident,'skipped',message,str(exc))
                        self.log.emit(account['name']+'：客户 '+customer['name']+' 已跳过：'+str(exc))
                        skip_cooldown = isinstance(exc,(PrivateAccount,MissingAccount))
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
                                      ' 处理失败，账号将继续运行：'+str(exc))
                    except (PlaywrightError, asyncio.TimeoutError) as exc:
                        # A stalled locator must not end the whole sender job.
                        # Never requeue a customer once submission has started.
                        detail = ('发送结果不明，禁止自动重发' if clicked else
                                  '页面操作超时或失败，尚未提交发送')+'：'+(str(exc) or '超过处理时限')
                        self.db.finish(customer['uid'],ident,'failed',message,detail)
                        self.log.emit(account['name']+'：'+detail)
                        customer = None
                        b = await self.rebuild_browser(account)
                    self.changed.emit()
                    # The customer's outcome is final before maintenance. A
                    # reset failure/cancellation must not overwrite it.
                    customer = None
                    b = await self.maintain_browser_memory(account,b)
                    if skip_cooldown:
                        self.status.emit(ident,'客户已跳过，继续处理')
                        continue
                    row = self.db.rows('SELECT interval FROM accounts WHERE id=?',(ident,))
                    if not row:
                        raise ValueError('账号已被删除，任务已停止')
                    interval = row[0]['interval']
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
