"""Per-account search pacing, retained pages and discovery checkpoints."""
import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.browser import AccountNeedsAttention, NeedsAttention
from app.db import DB
from app.engine import Engine


def collector(tmp_path, count=1, seconds=12):
    db = DB(tmp_path/'pause.sqlite3')
    for index in range(count):
        db.account(str(index))
    engine = Engine(db,tmp_path)
    engine.contexts = {}; engine.jobs = {}
    accounts = db.rows('SELECT * FROM accounts ORDER BY rowid')
    engine.running_accounts.update(a['id'] for a in accounts)
    sessions = {}
    for account in accounts:
        browser = SimpleNamespace(warnings=[],s=None,
            search_videos=AsyncMock(return_value=[]),keyword_videos=AsyncMock(return_value=[]),
            go_idle=AsyncMock())
        engine.contexts[account['id']] = browser
        sessions[account['id']] = [account,browser,None]
    settings = dict(engine.settings(),keyword_pause_seconds=seconds)
    return db,engine,accounts,sessions,settings


@pytest.mark.parametrize('kind',['keyword','search'])
def test_search_returns_to_personal_page_before_wait_and_reuses_browser(tmp_path,kind):
    db,engine,accounts,sessions,settings = collector(tmp_path)
    ident = accounts[0]['id']; browser = sessions[ident][1]
    sources = [(kind,{'keyword':word}) for word in ('一','二')]
    db.prepare_video_scan(24,sources=sources)
    events = []
    async def idle():
        assert engine.contexts[ident] is browser and sessions[ident][1] is browser
        events.append('idle')
    async def sleep(seconds):
        assert seconds == 12 and events[-1] == 'idle'
        assert db.rows("SELECT count(*) n FROM discovery_sources WHERE status='done'")[0]['n'] == events.count('idle')
        events.append('sleep')
    browser.go_idle.side_effect=idle
    engine.change_browser_state=AsyncMock(); engine.open=AsyncMock()
    with patch('app.engine.asyncio.sleep',side_effect=sleep):
        asyncio.run(engine.collection_stage(sessions,deque(sources),settings,True,[True]))
    assert events==['idle','sleep','idle','sleep']
    assert sessions[ident][1] is browser and engine.contexts[ident] is browser
    engine.change_browser_state.assert_not_awaited(); engine.open.assert_not_awaited()
    assert db.pending_discovery_sources(sources)==[]


def test_paused_account_does_not_block_other_account(tmp_path):
    db,engine,accounts,sessions,settings = collector(tmp_path,count=2)
    first,second = [a['id'] for a in accounts]
    paused = asyncio.Event(); completed = []
    real_sleep = asyncio.sleep
    async def discover(account,browser,s,sources=None):
        if account['id'] == second:
            await paused.wait()
        completed.append((account['id'],sources[0][1]['keyword']))
        return True
    async def sleep(seconds):
        ident = next(i for i,t in engine.jobs.items() if t is asyncio.current_task())
        assert sessions[ident][1] is engine.contexts[ident]
        if ident == first:
            paused.set()
            while len(completed) < 3:
                await real_sleep(0)
        await real_sleep(0)
    async def opened(account):
        browser = SimpleNamespace(warnings=[],s=None)
        engine.contexts[account['id']] = browser
        return browser
    engine.rebuild_video_list = discover
    engine.change_browser_state = AsyncMock(); engine.open = opened
    sources = [('search',{'keyword':str(i)}) for i in range(3)]
    with patch('app.engine.asyncio.sleep',side_effect=sleep):
        asyncio.run(asyncio.wait_for(engine.collection_stage(sessions,deque(sources),settings,True,[True]),2))
    assert completed == [(first,'0'),(second,'1'),(second,'2')]


def test_stop_during_pause_keeps_done_source_and_does_not_wake(tmp_path):
    db,engine,accounts,sessions,settings = collector(tmp_path)
    ident = accounts[0]['id']
    sources = [('search',{'keyword':'完成词'}),('search',{'keyword':'未开始'})]
    db.prepare_video_scan(24,sources=sources)
    queue = deque(sources); paused = asyncio.Event()
    sessions_browser = sessions[ident][1]
    async def sleep(seconds):
        paused.set()
        await asyncio.Future()
    engine.change_browser_state = AsyncMock(); engine.open = AsyncMock()
    async def run():
        task = asyncio.create_task(engine.collection_stage(sessions,queue,settings,True,[True]))
        await paused.wait()
        await engine.stop_job(ident)
        await task
    with patch('app.engine.asyncio.sleep',side_effect=sleep):
        asyncio.run(run())
    assert db.pending_discovery_sources(sources) == sources[1:]
    assert list(queue) == sources[1:]
    engine.open.assert_not_awaited()
    engine.change_browser_state.assert_not_awaited()
    sessions_browser.go_idle.assert_awaited_once()


@pytest.mark.parametrize('error',[NeedsAttention('没有作品'),None])
def test_empty_or_failed_search_also_pauses(tmp_path,error):
    db,engine,accounts,sessions,settings = collector(tmp_path)
    ident = accounts[0]['id']
    if error:
        sessions[ident][1].search_videos.side_effect = error
    source = ('search',{'keyword':'测试'})
    db.prepare_video_scan(24,sources=[source])
    engine.change_browser_state = AsyncMock()
    complete = [True]
    with patch('app.engine.asyncio.sleep',new=AsyncMock()) as sleep:
        asyncio.run(engine.collection_stage(sessions,deque([source]),settings,True,complete))
    sleep.assert_awaited_once_with(12)
    assert complete == [not bool(error)]
    assert db.pending_discovery_sources([source]) == ([source] if error else [])


def test_zero_returns_to_personal_page_without_search_pause(tmp_path):
    db,engine,accounts,sessions,settings = collector(tmp_path,seconds=0)
    browser = sessions[accounts[0]['id']][1]
    engine.change_browser_state = AsyncMock()
    with patch('app.engine.asyncio.sleep',new=AsyncMock()) as sleep:
        asyncio.run(engine.collection_stage(sessions,deque([('search',{'keyword':'测试'})]),settings,True,[True]))
    browser.go_idle.assert_awaited_once()
    engine.change_browser_state.assert_not_awaited(); sleep.assert_not_awaited()


def test_verification_stops_account_without_pause_or_rebuild(tmp_path):
    db,engine,accounts,sessions,settings = collector(tmp_path)
    ident = accounts[0]['id']
    sessions[ident][1].search_videos.side_effect = AccountNeedsAttention('请完成验证')
    engine.change_browser_state = AsyncMock(); engine.rebuild_browser = AsyncMock()
    queue = deque([('search',{'keyword':'验证词'})])
    with patch('app.engine.asyncio.sleep',new=AsyncMock()) as sleep:
        asyncio.run(engine.collection_stage(sessions,queue,settings,True,[True]))
    assert len(queue) == 1 and not sessions
    engine.change_browser_state.assert_not_awaited(); engine.rebuild_browser.assert_not_awaited()
    sleep.assert_not_awaited()


def test_failed_idle_navigation_keeps_account_and_completed_checkpoints(tmp_path):
    db,engine,accounts,sessions,settings = collector(tmp_path)
    ident=accounts[0]['id']; browser=sessions[ident][1]
    sources=[('search',{'keyword':'完成词'}),('search',{'keyword':'下个词'})]
    db.prepare_video_scan(24,sources=sources)
    browser.go_idle.side_effect=RuntimeError('个人页导航失败')
    engine.rebuild_browser=AsyncMock()
    queue=deque(sources)
    with patch('app.engine.asyncio.sleep',new=AsyncMock()) as sleep:
        asyncio.run(engine.collection_stage(sessions,queue,settings,True,[True]))
    assert sessions[ident][1] is browser and engine.contexts[ident] is browser
    assert not queue and db.pending_discovery_sources(sources)==[]
    assert sleep.await_count==2
    engine.rebuild_browser.assert_not_awaited()


def test_comment_stage_wakes_parked_account_with_task_settings(tmp_path):
    db,engine,accounts,sessions,settings = collector(tmp_path)
    ident = accounts[0]['id']; browser = sessions[ident][1]
    sessions[ident][1] = None; engine.contexts.clear()
    engine.change_browser_state = AsyncMock(); engine.open = AsyncMock(return_value=browser)
    engine.scan_videos = AsyncMock(return_value=browser)
    video = {'id':'1','title':'视频'}
    asyncio.run(engine.collection_stage(sessions,deque([video]),settings,False,[True]))
    engine.change_browser_state.assert_awaited_once_with(ident,'wake')
    engine.scan_videos.assert_awaited_once_with(accounts[0],browser,None,[video],settings)
    assert browser.s is settings


def test_standalone_monitor_keeps_page_when_no_videos(tmp_path):
    db,engine,accounts,sessions,settings = collector(tmp_path)
    db.import_keywords(['测试'])
    ident = accounts[0]['id']; browser = sessions[ident][1]
    engine.change_browser_state = AsyncMock(); engine.open = AsyncMock()
    with patch('app.engine.asyncio.sleep',new=AsyncMock()) as sleep:
        result = asyncio.run(engine.monitor_round(accounts[0],browser,None,settings))
    assert result is browser and engine.contexts[ident] is browser
    assert [call.args[0] for call in sleep.await_args_list] == [12,settings['poll_seconds']]
    engine.open.assert_not_awaited()
    assert db.pending_discovery_sources(engine.collection_sources()) == []
