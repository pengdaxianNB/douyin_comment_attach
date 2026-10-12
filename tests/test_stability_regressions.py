"""Failure injection for shutdown, migration and stalled response cleanup."""
import asyncio
import sqlite3
import subprocess
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.browser import Browser, NeedsAttention
from app.db import DB
from app.engine import Engine


@pytest.mark.parametrize('raw',['{broken','null','[]','"five"','true'])
def test_invalid_saved_setting_uses_default_without_overwriting(tmp_path,raw,caplog):
    db = DB(tmp_path/'test.sqlite3')
    db.execute('INSERT INTO settings VALUES (?,?)',('ai_concurrency',raw))
    assert db.setting('ai_concurrency',5) == 5
    assert db.rows('SELECT value FROM settings')[0]['value'] == raw
    assert 'ai_concurrency' in caplog.text


def test_numeric_settings_are_bounded_before_reaching_qt_or_worker(tmp_path):
    db = DB(tmp_path/'settings.sqlite3')
    db.save_settings(dict(message_concurrency=10**100,ai_concurrency=-5,
                          poll_seconds=0,keyword_pause_seconds=-1,days=10**100))
    settings = Engine(db,tmp_path).settings()
    assert settings['message_concurrency']==20 and settings['ai_concurrency']==1
    assert settings['poll_seconds']==1 and settings['keyword_pause_seconds']==0
    assert settings['days']==1000


def test_logging_is_idempotent_and_native_fault_capture_is_enabled(tmp_path):
    # A child process owns its exception hooks and faulthandler descriptor.
    result = subprocess.run([sys.executable,'-c',
        'import faulthandler, logging, sys; from pathlib import Path; '
        'from main import configure_logging; '
        'configure_logging(Path(sys.argv[1])); configure_logging(Path(sys.argv[1])); '
        'logging.getLogger("panda").info("single log record"); '
        'assert faulthandler.is_enabled()',str(tmp_path)],
        text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert (tmp_path/'logs'/'app.log').read_text().count('single log record')==1
    assert (tmp_path/'logs'/'native-crash.log').exists()


def test_second_instance_does_not_open_or_rotate_active_logs(tmp_path,monkeypatch):
    import main
    from PySide6.QtCore import QLockFile
    lock = QLockFile(str(tmp_path/'app.lock'))
    assert lock.tryLock(100)
    try:
        monkeypatch.setenv('DOUYIN_DATA_DIR',str(tmp_path))
        monkeypatch.setattr(main,'configure_embedded_browser',Mock())
        logging_setup = Mock()
        monkeypatch.setattr(main,'configure_logging',logging_setup)
        monkeypatch.setattr(main,'QApplication',Mock())
        warning = Mock()
        monkeypatch.setattr(main.QMessageBox,'warning',warning)
        assert main.main()==1
        logging_setup.assert_not_called()
        warning.assert_called_once()
    finally:
        lock.unlock()


def test_video_copy_failure_rolls_back_both_legacy_migrations(tmp_path):
    path = tmp_path/'legacy.sqlite3'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE accounts(id TEXT PRIMARY KEY,role TEXT,name TEXT,interval INTEGER,daily_limit INTEGER)')
        c.execute("INSERT INTO accounts VALUES ('old','main','旧账号',120,10)")
        c.execute("CREATE UNIQUE INDEX one_main ON accounts(role) WHERE role='main'")
        c.execute('''CREATE TABLE videos(
            id TEXT PRIMARY KEY,title TEXT,author TEXT,
            source_type TEXT CHECK(source_type IN ('blogger','keyword','legacy')),
            source_name TEXT,source_ref TEXT,create_time INTEGER,url TEXT,
            status TEXT,error TEXT,discovered_at REAL,scanned_at REAL)''')
        c.execute("INSERT INTO videos VALUES ('1','旧视频','','legacy','','',0,'https://www.douyin.com/video/1','pending','',1,NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        DB(path)
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT role,name FROM accounts').fetchall() == [('main','旧账号')]
        assert c.execute('SELECT title,source_type FROM videos').fetchall() == [('旧视频','legacy')]
        assert c.execute("SELECT name FROM sqlite_master WHERE name LIKE '%before%'").fetchall() == []
        assert c.execute("SELECT name FROM sqlite_master WHERE name='one_main'").fetchone()


def test_shutdown_during_login_cancels_command_and_workers_before_playwright(tmp_path,monkeypatch):
    engine = Engine(DB(tmp_path/'test.sqlite3'),tmp_path)
    entered = asyncio.Event()
    order = []

    @asynccontextmanager
    async def fake_playwright():
        try:
            yield SimpleNamespace()
        finally:
            order.append('playwright_closed')
    monkeypatch.setattr('app.engine.async_playwright',fake_playwright)

    async def login(account):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            order.append('login_cancelled')
    engine.open = login

    async def worker():
        try:
            await asyncio.Event().wait()
        finally:
            order.append('worker_cleaned')

    async def run():
        task = asyncio.create_task(engine.main())
        await asyncio.sleep(0)
        engine.jobs['account'] = asyncio.create_task(worker())
        engine.command('login',dict(id='account',name='账号'))
        await entered.wait()
        engine.command('shutdown')
        engine.command('shutdown')  # Repeated close must not interrupt cleanup.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,0.5)
        assert not engine.jobs and not engine.ready.is_set()
    asyncio.run(run())
    assert order == ['login_cancelled','worker_cleaned','playwright_closed']


def test_close_before_engine_ready_is_remembered(tmp_path,monkeypatch):
    engine = Engine(DB(tmp_path/'test.sqlite3'),tmp_path)
    opened = []
    @asynccontextmanager
    async def fake_playwright():
        opened.append(True)
        yield SimpleNamespace()
    monkeypatch.setattr('app.engine.async_playwright',fake_playwright)
    engine.command('shutdown')
    engine.run()
    assert opened and engine.loop is None and engine.commands is None
    assert not engine.ready.is_set() and not engine.busy()


def test_dead_engine_rejects_command_and_late_gui_acknowledgement(tmp_path):
    engine = Engine(DB(tmp_path/'test.sqlite3'),tmp_path)
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    engine.browser_requests['late'] = (loop,future)
    loop.close()
    engine.complete_browser_request('late')
    engine.loop = loop
    engine.commands = asyncio.Queue()
    with pytest.raises(ValueError,match='后台'):
        engine.command('start_cluster',dict(mode='comments'))
    assert not engine.busy()


@pytest.mark.parametrize('method',['capture','keyword_videos'])
def test_cancelled_collection_does_not_wait_for_response_body(tmp_path,method):
    async def run():
        started,cleaned = asyncio.Event(),asyncio.Event()
        listeners = {}
        async def read_json():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        url = ('https://www.douyin.com/aweme/v1/web/comment/list/' if method=='capture'
               else 'https://creator.douyin.com/api/v2/index/itemQuery')
        response = SimpleNamespace(url=url,json=read_json)
        async def goto(*args,**kwargs):
            listeners['response'](response)
            await started.wait()
            await asyncio.Event().wait()
        page = Mock(goto=goto)
        page.on = lambda event,callback:listeners.__setitem__(event,callback)
        page.remove_listener = lambda event,callback:listeners.pop(event)
        browser = Browser(Mock(),{},page=page)
        operation = (browser.capture('https://www.douyin.com/video/1',
                     '/aweme/v1/web/comment/list/','comments',1)
                     if method=='capture' else browser.keyword_videos('测试词'))
        task = asyncio.create_task(operation)
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,0.5)
        assert cleaned.is_set() and not listeners
    asyncio.run(run())


def test_stalled_comment_response_times_out_instead_of_hanging(monkeypatch):
    async def run():
        listeners = {}
        cancelled = []
        real_sleep = asyncio.sleep
        async def no_delay(seconds):
            await real_sleep(0)
        monkeypatch.setattr('app.browser.asyncio.sleep',no_delay)
        monkeypatch.setattr('app.browser.SEARCH_RESPONSE_TIMEOUT',0.01)
        async def read_json():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        response = SimpleNamespace(url='https://www.douyin.com/aweme/v1/web/aweme/post/',json=read_json)
        async def goto(*args,**kwargs):
            listeners['response'](response)
        page = Mock(goto=goto)
        page.on = lambda event,callback:listeners.__setitem__(event,callback)
        page.remove_listener = lambda event,callback:listeners.pop(event)
        browser = Browser(Mock(),{},page=page)
        browser.guard = AsyncMock()
        with pytest.raises(NeedsAttention,match='响应无法解析'):
            await asyncio.wait_for(browser.capture('https://www.douyin.com/user/test',
                '/aweme/v1/web/aweme/post/','aweme_list',0),0.5)
        assert len(cancelled)==2 and not listeners
    asyncio.run(run())
