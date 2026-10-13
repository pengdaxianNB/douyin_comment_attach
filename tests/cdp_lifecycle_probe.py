"""Run in a fresh Qt process so DevTools flags precede WebEngine startup."""
import asyncio
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ['QT_QPA_PLATFORM']='offscreen'
from main import configure_embedded_browser
configure_embedded_browser()
from PySide6.QtWidgets import QApplication
from app.db import DB
from app.ui import Window
from app.embedded_browser import page_id
from app.browser import Browser
import app.browser as browser_adapter
import app.embedded_browser as embedded_browser
from playwright.async_api import async_playwright


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b'<!doctype html><title>local account test</title>')

    def log_message(self,*args):
        pass


def main():
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}/'
    # Use a loopback personal page; this probe never visits Douyin.
    browser_adapter.IDLE_URL=embedded_browser.IDLE_URL=url+'idle'
    app=QApplication([])
    with tempfile.TemporaryDirectory() as folder:
        db=DB(Path(folder)/'db.sqlite3')
        for i in range(10): db.account(f'local-{i}')
        w=Window(db,folder,start_engine=False);w.timer.stop()
        engine=w.engine
        engine.contexts={};engine.jobs={};engine.cdp_browser=None
        accounts=engine.cluster_accounts('messages')
        for account in accounts: w.ensure_browser(account,False,activate=False)
        errors=[];targets={}; peak=0;active=0
        async def probe():
            nonlocal peak,active
            engine.loop=asyncio.get_running_loop()
            engine.sender_slots=asyncio.Semaphore(3)
            async with async_playwright() as pw:
                engine.pw=pw
                async def account_turn(account,round_number):
                    nonlocal peak,active
                    ident=account['id']
                    async with engine.sender_turn(account):
                        active+=1; peak=max(peak,active)
                        try:
                            await engine.change_browser_state(ident,'wake')
                            page=await engine.embedded_page(ident)
                            target=page_id(ident)
                            if round_number:
                                assert target==targets[ident]
                                assert engine.contexts[ident].page is page
                            else:
                                targets[ident]=target
                                browser=Browser(page.context,{},page=page)
                                await browser.prepare_background_execution()
                                engine.contexts[ident]=browser
                            await page.goto(url+'task')
                            if round_number:
                                assert await page.evaluate('document.cookie')=='account='+ident
                                assert await page.evaluate("localStorage.getItem('account')")==ident
                            else:
                                assert await page.evaluate('document.cookie')==''
                                await page.evaluate("id => {document.cookie='account='+id+'; path=/';localStorage.setItem('account',id)}",ident)
                        finally:
                            active-=1
                    assert page_id(ident)==targets[ident]
                    assert page.url==url+'idle'
                    # The GUI parking operation also retains the same target.
                    await engine.change_browser_state(ident,'park')
                    assert page_id(ident)==targets[ident]
                for round_number in range(2):
                    await asyncio.gather(*(account_turn(a,round_number) for a in accounts))
                assert sum(page_id(a['id']) is not None for a in accounts)==10
                assert len(engine.contexts)==10
                await engine.cdp_browser.close()
        def run():
            try: asyncio.run(probe())
            except BaseException as exc: errors.append(exc)
        thread=threading.Thread(target=run,daemon=True);thread.start()
        deadline=time.monotonic()+45
        while thread.is_alive() and time.monotonic()<deadline:
            app.processEvents();time.sleep(.005)
        assert not thread.is_alive(),'local lifecycle probe timed out'
        w.close();app.processEvents()
        server.shutdown();server.server_close()
        if errors: raise errors[0]
        assert peak==3
        print('PASS: 10 retained pages, 3 concurrent tasks, 20 turns, stable targets, cookies and storage preserved')


if __name__=='__main__':
    main()
