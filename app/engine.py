import asyncio
import threading
from pathlib import Path
from PySide6.QtCore import QThread, Signal
from playwright.async_api import async_playwright
from .ai import AI, get_key
from .browser import Browser, UncertainSend, SkippedCustomer
from .classifier import classify_comments

DEFAULTS = dict(days=7,ai_concurrency=5,poll_seconds=300,scroll_limit=80,base_url='',model='',thinking_mode='default',thinking_effort='high',
    filter_prompt='识别评论中是否明确表达了对我提供的产品或服务的购买或咨询需求。产品和目标客户：请填写。不要把普通互动识别为需求。',
    message_prompt='根据客户评论写一条自然、简短的咨询回复。我的产品或服务：请填写。尊重对方，不夸大，不使用虚假承诺。',
    comment_panel='[data-e2e="comment-list"]',message_editor='[contenteditable="true"]')


class Engine(QThread):
    log = Signal(str)
    status = Signal(str,str)
    changed = Signal()

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
                        if ident in self.contexts:
                            await self.contexts.pop(ident).context.close()
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
                        self.status.emit(ident,'浏览器已打开 · 请本人登录')
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
            for b in self.contexts.values():
                await b.context.close()
        self.loop = None

    def settings(self):
        return {k:self.db.setting(k,v) for k,v in DEFAULTS.items()}

    async def open(self, account):
        ident = account['id']
        if ident not in self.contexts:
            context = await self.pw.chromium.launch_persistent_context(str(self.data/'profiles'/ident),channel='chrome',headless=False,viewport={'width':1280,'height':900},locale='zh-CN',chromium_sandbox=True)
            b = Browser(context,self.settings(),self.data/'diagnostics'/ident,log=lambda text:self.log.emit(account['name']+'：'+text))
            self.contexts[ident] = b
            try:
                await b.init()
            except BaseException:
                self.contexts.pop(ident,None)
                await context.close()
                raise
        return self.contexts[ident]

    async def stop_job(self, ident):
        task = self.jobs.pop(ident,None)
        if task:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        with self.lock:
            self.running_accounts.discard(ident)
        self.status.emit(ident,'已停止')

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
                    bloggers = self.db.rows('SELECT * FROM bloggers')
                    if not bloggers:
                        raise ValueError('请先添加需要监控的博主')
                    for blogger in bloggers:
                        self.status.emit(ident,'采集：'+blogger['name'])
                        videos = await b.videos(blogger['id'],s['days'])
                        for video in videos:
                            comments = await b.comments(video)
                            await classify_comments(self.db,ai,comments,s['ai_concurrency'],
                                lambda text:self.log.emit(account['name']+'：'+text),self.changed.emit)
                        self.log.emit(blogger['name']+' 本轮检查视频 '+str(len(videos))+' 个')
                        for warning in b.warnings:
                            self.log.emit(warning)
                        b.warnings.clear()
                    self.status.emit(ident,'等待下一轮')
                    await asyncio.sleep(s['poll_seconds'])
                else:
                    customer = self.db.claim(ident)
                    if not customer:
                        self.status.emit(ident,'等待客户')
                        await asyncio.sleep(5)
                        continue
                    self.changed.emit()
                    self.status.emit(ident,'处理：'+customer['name'])
                    clicked = False
                    try:
                        await b.check_reachable(customer)
                        message = await ai.message(customer)
                        self.db.finish(customer['uid'],ident,'processing',message)
                        def before_click():
                            nonlocal clicked
                            self.db.finish(customer['uid'],ident,'failed',message,'准备提交发送，结果不明时请勿重复联系')
                            clicked = True
                        await b.send(customer,message,before_click)
                        self.db.finish(customer['uid'],ident,'sent',message)
                    except SkippedCustomer as exc:
                        self.db.finish(customer['uid'],ident,'skipped',message,str(exc))
                        self.log.emit(account['name']+'：客户 '+customer['name']+' 已跳过：'+str(exc))
                    except UncertainSend as exc:
                        self.db.finish(customer['uid'],ident,'failed',message,str(exc))
                        self.log.emit(account['name']+'：'+str(exc))
                    self.changed.emit()
                    customer = None
                    interval = self.db.rows('SELECT interval FROM accounts WHERE id=?',(ident,))[0]['interval']
                    self.status.emit(ident,'间隔等待')
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
