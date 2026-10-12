"""Per-account search pacing, renderer release and discovery checkpoints."""
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
            release_page_memory=AsyncMock())
        engine.contexts[account['id']] = browser
        sessions[account['id']] = [account,browser,None]
    settings = dict(engine.settings(),keyword_pause_seconds=seconds)
    return db,engine,accounts,sessions,settings


@pytest.mark.parametrize('kind',['keyword','search'])
def test_search_releases_before_wait_and_wakes_only_for_more_work(tmp_path,kind):
    db,engine,accounts,sessions,settings = collector(tmp_path)
    ident = accounts[0]['id']; original = sessions[ident][1]
    sources = [(kind,{'keyword':word}) for word in ('一','二')]
    db.prepare_video_scan(24,sources=sources)
    events = []
    async def control(account_id,operation):
        events.append(operation)
        if operation == 'park':
            assert account_id not in engine.contexts
            assert sessions[account_id][1] is None
    async def open_page(account):
        events.append('open')
        browser = SimpleNamespace(warnings=[],s=None,
            search_videos=AsyncMock(return_value=[]),keyword_videos=AsyncMock(return_value=[]))
        engine.contexts[account['id']] = browser
        return browser
    async def sleep(seconds):
        assert seconds == 12
        assert events[-1] == 'park'
        assert db.rows("SELECT count(*) n FROM discovery_sources WHERE status='done'")[0]['n'] == events.count('park')
        events.append('sleep')
    engine.change_browser_state = control; engine.open = AsyncMock(side_effect=open_page)
    with patch('app.engine.asyncio.sleep',side_effect=sleep):
        asyncio.run(engine.collection_stage(sessions,deque(sources),settings,True,[True]))
    assert events == ['park','sleep','wake','open','park','sleep']
    assert sessions[ident][1] is None and ident not in engine.contexts
    original.release_page_memory.assert_not_awaited()
    assert db.pending_discovery_sources(sources) == []


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
        assert sessions[ident][1] is None and ident not in engine.contexts
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
    engine.change_browser_state.assert_awaited_once_with(ident,'park')


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


def test_zero_keeps_existing_cleanup_and_never_parks(tmp_path):
    db,engine,accounts,sessions,settings = collector(tmp_path,seconds=0)
    browser = sessions[accounts[0]['id']][1]
    engine.change_browser_state = AsyncMock()
    with patch('app.engine.asyncio.sleep',new=AsyncMock()) as sleep:
        asyncio.run(engine.collection_stage(sessions,deque([('search',{'keyword':'测试'})]),settings,True,[True]))
    browser.release_page_memory.assert_awaited_once()
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


def test_release_failure_stops_account_without_repeating_completed_word(tmp_path):
    db,engine,accounts,sessions,settings = collector(tmp_path)
    sources = [('search',{'keyword':'完成词'}),('search',{'keyword':'下个词'})]
    db.prepare_video_scan(24,sources=sources)
    engine.change_browser_state = AsyncMock(side_effect=RuntimeError('释放未确认'))
    queue = deque(sources)
    with patch('app.engine.asyncio.sleep',new=AsyncMock()) as sleep:
        asyncio.run(engine.collection_stage(sessions,queue,settings,True,[True]))
    assert not sessions and list(queue) == sources[1:]
    assert db.pending_discovery_sources(sources) == sources[1:]
    sleep.assert_not_awaited()


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


def test_standalone_monitor_pauses_and_keeps_page_parked_when_no_videos(tmp_path):
    db,engine,accounts,sessions,settings = collector(tmp_path)
    db.import_keywords(['测试'])
    ident = accounts[0]['id']; browser = sessions[ident][1]
    engine.change_browser_state = AsyncMock(); engine.open = AsyncMock()
    with patch('app.engine.asyncio.sleep',new=AsyncMock()) as sleep:
        result = asyncio.run(engine.monitor_round(accounts[0],browser,None,settings))
    assert result is None and ident not in engine.contexts
    assert [call.args[0] for call in sleep.await_args_list] == [12,settings['poll_seconds']]
    engine.open.assert_not_awaited()
    assert db.pending_discovery_sources(engine.collection_sources()) == []
