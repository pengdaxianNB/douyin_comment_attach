import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from app.db import DB
from app.engine import Engine
from app.browser import AccountNeedsAttention


def cluster(tmp_path,count=3):
    db=DB(tmp_path/'test.sqlite3')
    for i in range(count): db.account(f'账号{i}')
    db.save_settings({'base_url':'https://example.com/v1','model':'test','collection_accounts':count})
    engine=Engine(db,tmp_path)
    engine.jobs={}; engine.contexts={}
    accounts=engine.cluster_accounts('comments')
    engine.running_accounts.update(a['id'] for a in accounts)
    sessions={a['id']:[a,SimpleNamespace(warnings=[]),None] for a in accounts}
    return db,engine,accounts,sessions


def test_one_hundred_sources_shared_without_duplicate_claims(tmp_path):
    db,engine,accounts,sessions=cluster(tmp_path)
    calls=[]; busy=set(); peak=0
    async def discover(account,browser,settings,sources=None):
        nonlocal peak
        busy.add(account['id']); peak=max(peak,len(busy))
        calls.append((account['id'],sources[0][1]['keyword']))
        await asyncio.sleep(0)
        busy.remove(account['id'])
        return True
    engine.rebuild_video_list=discover
    queue=deque(('keyword',{'keyword':str(i)}) for i in range(100))
    asyncio.run(engine.collection_stage(sessions,queue,engine.settings(),True,[True]))
    assert len(calls)==100
    assert len({word for ident,word in calls})==100
    assert {ident for ident,word in calls}=={a['id'] for a in accounts}
    assert peak==3 and not queue


def test_account_verification_returns_source_to_remaining_accounts(tmp_path):
    db,engine,accounts,sessions=cluster(tmp_path,2)
    failed=accounts[0]['id']; completed=[]
    async def discover(account,browser,settings,sources=None):
        await asyncio.sleep(0)
        if account['id']==failed: raise AccountNeedsAttention('请本人完成验证')
        completed.append(sources[0][1]['keyword'])
        return True
    engine.rebuild_video_list=discover
    queue=deque(('keyword',{'keyword':str(i)}) for i in range(10))
    asyncio.run(engine.collection_stage(sessions,queue,engine.settings(),True,[True]))
    assert sorted(completed)==[str(i) for i in range(10)]
    assert failed not in sessions and failed not in engine.running_accounts
    assert not queue


def test_deduplicated_videos_scanned_by_one_account_each(tmp_path):
    db,engine,accounts,sessions=cluster(tmp_path)
    for i in range(20):
        video={'aweme_id':str(i),'desc':str(i)}
        db.add_video(video,'keyword','关键词一')
        db.add_video(video,'search','搜索词二')
    scanned=[]
    async def scan(account,browser,ai,videos,settings):
        assert len(videos)==1
        scanned.append(videos[0]['id'])
        await asyncio.sleep(0)
        db.set_video_status(videos[0]['id'],'done')
        return browser
    engine.scan_videos=scan
    asyncio.run(engine.collection_stage(sessions,deque(db.video_tasks('pending')),engine.settings(),False,[True]))
    assert len(scanned)==len(set(scanned))==20
    assert not db.video_tasks('pending')


def test_cancelled_discovery_preserves_partial_list_for_resume(tmp_path):
    db,engine,accounts,sessions=cluster(tmp_path,2)
    db.import_keywords(['一','二','三'])
    engine.open=AsyncMock(side_effect=lambda a:SimpleNamespace(warnings=[],s=None))
    entered=asyncio.Event()
    async def discover(account,browser,settings,sources=None):
        db.add_video({'aweme_id':'1','desc':'已发现'},'keyword',sources[0][1]['keyword'])
        entered.set()
        await asyncio.Future()
    engine.rebuild_video_list=discover
    async def run():
        engine.cluster_mode='comments'
        engine.cluster_task=asyncio.create_task(engine.run_cluster(accounts,'comments'))
        await entered.wait()
        await engine.stop_cluster()
        assert not engine.jobs and not engine.running_accounts and engine.cluster_mode is None
    with patch('app.engine.get_key',return_value='key'),patch('app.engine.AI'):
        asyncio.run(run())
    assert db.setting('video_list_built_at')==0
    assert db.video_tasks()[0]['status']=='pending'


def test_prepares_shared_list_once_per_round_and_waits_for_discovery(tmp_path):
    db,engine,accounts,sessions=cluster(tmp_path,2)
    db.import_keywords(['一','二','三'])
    engine.open=AsyncMock(side_effect=lambda a:SimpleNamespace(warnings=[],s=None))
    discovered=[]; scanned=[]; prepares=[]
    prepare=db.prepare_video_scan
    def track(hours):
        prepares.append(1); return prepare(hours)
    db.prepare_video_scan=track
    async def discover(account,browser,settings,sources=None):
        await asyncio.sleep(0)
        word=sources[0][1]['keyword']; discovered.append(word)
        db.add_video({'aweme_id':word,'desc':word},'keyword',word)
        return True
    async def scan(account,browser,ai,videos,settings):
        assert len(discovered)==3
        scanned.append(videos[0]['id'])
        db.set_video_status(videos[0]['id'],'done')
        await asyncio.sleep(0)
        return browser
    engine.rebuild_video_list=discover; engine.scan_videos=scan
    real_sleep=asyncio.sleep
    async def sleep(seconds):
        if seconds: raise asyncio.CancelledError
        await real_sleep(0)
    with patch('app.engine.get_key',return_value='key'),patch('app.engine.AI'),patch('app.engine.asyncio.sleep',side_effect=sleep):
        with pytest.raises(asyncio.CancelledError): asyncio.run(engine.run_cluster(accounts,'comments'))
    assert len(prepares)==1 and len(scanned)==3


def test_engine_reserves_mode_before_dispatch_and_clamps_stale_count(tmp_path):
    db,engine,accounts,sessions=cluster(tmp_path,2)
    engine.running_accounts.clear()
    db.save_settings({'collection_accounts':999})
    assert len(engine.cluster_accounts('comments'))==2
    async def run():
        engine.loop=asyncio.get_running_loop(); engine.commands=asyncio.Queue()
        engine.command('start_cluster',{'mode':'comments'})
        with pytest.raises(ValueError,match='停止当前集群'): engine.command('start_cluster',{'mode':'messages'})
        await asyncio.sleep(0)
        op,payload=engine.commands.get_nowait()
        assert op=='start_cluster' and len(payload['accounts'])==2
    asyncio.run(run())


def test_stop_one_collector_leaves_other_collectors_running(tmp_path):
    db,engine,accounts,sessions=cluster(tmp_path,2)
    stopped=accounts[0]['id']; entered=asyncio.Event(); completed=[]
    async def discover(account,browser,settings,sources=None):
        if account['id']==stopped:
            entered.set()
            await asyncio.Future()
        await asyncio.sleep(0)
        completed.append(sources[0][1]['keyword'])
        return True
    engine.rebuild_video_list=discover
    async def run():
        queue=deque(('keyword',{'keyword':str(i)}) for i in range(10))
        stage=asyncio.create_task(engine.collection_stage(sessions,queue,engine.settings(),True,[True]))
        await entered.wait()
        await engine.stop_job(stopped)
        await stage
        assert not queue
        assert set(sessions)=={accounts[1]['id']}
        assert accounts[1]['id'] in engine.running_accounts
    asyncio.run(run())
    assert len(completed)==len(set(completed))==10


def test_stop_one_sender_does_not_release_mode_until_other_sender_stops(tmp_path):
    db,engine,accounts,sessions=cluster(tmp_path,2)
    entered=asyncio.Event(); started=[]
    async def work(account,mode):
        assert mode=='messages'
        started.append(account['id'])
        if len(started)==2: entered.set()
        try: await asyncio.Future()
        finally:
            engine.running_accounts.discard(account['id'])
            engine.jobs.pop(account['id'],None)
    engine.work=work
    async def run():
        engine.cluster_mode='messages'
        engine.cluster_task=asyncio.create_task(engine.run_cluster(accounts,'messages'))
        await entered.wait()
        await engine.stop_job(accounts[0]['id'])
        await asyncio.sleep(0)
        assert engine.cluster_mode=='messages' and engine.busy()
        assert accounts[1]['id'] in engine.jobs
        await engine.stop_cluster()
        assert not engine.busy() and not engine.jobs
    asyncio.run(run())


def test_mode_cannot_restart_during_final_cancellation_cleanup(tmp_path):
    db,engine,accounts,sessions=cluster(tmp_path,1)
    entered=asyncio.Event(); guarded=[]
    async def work(account,mode):
        entered.set()
        try: await asyncio.Future()
        finally:
            engine.running_accounts.discard(account['id'])
            engine.jobs.pop(account['id'],None)
    engine.work=work
    def changed():
        if engine.cluster_stopping and engine.cluster_task is None:
            assert engine.busy()
            with pytest.raises(ValueError,match='停止当前集群'):
                engine.command('start_cluster',{'mode':'comments'})
            guarded.append(True)
    engine.changed.connect(changed)
    async def run():
        engine.loop=asyncio.get_running_loop(); engine.commands=asyncio.Queue()
        engine.cluster_mode='messages'
        engine.cluster_task=asyncio.create_task(engine.run_cluster(accounts,'messages'))
        await entered.wait()
        await engine.stop_cluster()
        assert not engine.busy() and not engine.cluster_stopping
    asyncio.run(run())
    assert guarded==[True]
