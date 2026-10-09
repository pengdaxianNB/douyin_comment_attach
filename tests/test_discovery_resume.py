import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.db import DB
from app.engine import Engine


SOURCES = [('search',{'keyword':'已完成'}),('keyword',{'keyword':'进行中'}),
           ('blogger',{'id':'博主','name':'博主'})]


def test_upgrade_legacy_partial_list_rediscovers_without_losing_results(tmp_path):
    db = DB(tmp_path/'db.sqlite3')
    db.save_settings({'video_list_built_at':1000})
    db.add_video({'aweme_id':'1','desc':'旧版闪退前已入库'},'search','已完成')
    assert db.prepare_video_scan(24,now=1001,sources=SOURCES)
    assert db.pending_discovery_sources(SOURCES)==SOURCES
    assert db.video_tasks()[0]['id']=='1'
    assert db.setting('video_list_built_at')==0
    assert not db.finish_video_discovery(now=1002)


def test_crash_resumes_sources_and_preserves_results_even_after_expiry(tmp_path):
    path = tmp_path/'db.sqlite3'
    db = DB(path)
    assert db.prepare_video_scan(24,now=1000,sources=SOURCES)
    db.add_video({'aweme_id':'1','desc':'已发现'},'search','已完成')
    db.set_discovery_status(SOURCES[0],'done')
    db.set_discovery_status(SOURCES[1],'running')
    # Reopen without graceful cancellation: model an abruptly killed process.
    db = DB(path)
    db.recover()
    assert db.prepare_video_scan(24,now=1000+48*3600,sources=SOURCES)
    assert db.pending_discovery_sources(SOURCES)==SOURCES[1:]
    assert db.video_tasks()[0]['id']=='1'
    assert db.setting('video_list_built_at')==0
    assert not db.finish_video_discovery(now=2000)
    for source in SOURCES[1:]:
        db.set_discovery_status(source,'done')
    assert db.finish_video_discovery(now=2000)
    assert db.setting('video_list_built_at')==2000
    assert not db.prepare_video_scan(24,now=2001,sources=SOURCES)
    assert db.prepare_video_scan(24,now=2000+24*3600,sources=SOURCES)
    assert db.pending_discovery_sources(SOURCES)==SOURCES
    assert db.video_tasks()==[]


def test_resume_reconciles_added_removed_and_same_text_source_types(tmp_path):
    db = DB(tmp_path/'db.sqlite3')
    sources = [('search',{'keyword':'相同词'}),('keyword',{'keyword':'相同词'})]
    db.prepare_video_scan(24,sources=sources)
    db.set_discovery_status(sources[0],'done')
    added = ('blogger',{'id':'新博主'})
    db.prepare_video_scan(24,sources=[sources[0],added])
    assert db.pending_discovery_sources([sources[0],added])==[added]
    assert len(db.rows('SELECT * FROM discovery_sources'))==2
    db.clear_videos()
    assert db.rows('SELECT * FROM discovery_sources')==[]
    assert db.setting('video_discovery_active') is None


@pytest.mark.parametrize('incomplete',[False,True])
def test_cluster_restart_skips_completed_source_and_waits_before_comments(tmp_path,incomplete):
    db = DB(tmp_path/'db.sqlite3')
    for i in range(5):
        db.account(f'账号{i}')
    db.import_keywords(['完成词','中断词'])
    db.save_settings({'base_url':'https://example.com','model':'test','collection_accounts':5})
    engine = Engine(db,tmp_path)
    engine.jobs = {}; engine.contexts = {}
    sources = engine.collection_sources()
    db.prepare_video_scan(24,sources=sources)
    db.set_discovery_status(sources[0],'done')
    db.set_discovery_status(sources[1],'running')
    db.add_video({'aweme_id':'1'},'keyword','完成词')
    engine.db = db = DB(db.path)
    accounts = engine.cluster_accounts('comments')
    engine.running_accounts.update(row['id'] for row in accounts)
    engine.open = AsyncMock(side_effect=lambda a:SimpleNamespace(warnings=[],s=None))
    calls = []; scans = []

    async def discover(account,browser,settings,sources=None):
        calls.append(sources[0][1]['keyword'])
        db.add_video({'aweme_id':'2'},'keyword','中断词')
        return not incomplete

    async def scan(account,browser,ai,videos,settings):
        assert not db.setting('video_discovery_active')
        scans.append(videos[0]['id'])
        db.set_video_status(videos[0]['id'],'done')
        return browser

    engine.rebuild_video_list = discover
    engine.scan_videos = scan
    with patch('app.engine.get_key',return_value='key'),patch('app.engine.AI'), \
         patch('app.engine.asyncio.sleep',side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(engine.run_cluster(accounts,'comments'))
    assert calls==['中断词']
    assert {r['id'] for r in db.video_tasks()}=={'1','2'}
    assert set(scans)==(set() if incomplete else {'1','2'})
    assert db.pending_discovery_sources(sources)==([sources[1]] if incomplete else [])


def test_bulk_delete_keeps_comments_and_blocks_rediscovery_until_new_cycle(tmp_path):
    db = DB(tmp_path/'db.sqlite3')
    db.prepare_video_scan(24,now=1000,sources=SOURCES)
    manual = db.add_manual_video('https://www.douyin.com/video/1234567890123456789')
    db.add_video({'aweme_id':'2'},'search','词')
    db.execute("INSERT INTO comments(id,uid,name,text) VALUES ('comment','customer','客户','保留评论')")
    assert db.delete_videos([manual,'2','2'])==2
    assert db.video_tasks()==[] and db.rows('SELECT * FROM manual_videos')==[]
    assert not db.add_video({'aweme_id':'2'},'blogger','另一来源')
    # Resuming unfinished discovery must also respect the explicit deletion.
    db.prepare_video_scan(24,now=1001,sources=SOURCES)
    assert not db.add_video({'aweme_id':'2'},'search','词')
    for source in SOURCES:
        db.set_discovery_status(source,'done')
    assert db.finish_video_discovery(now=1001)
    assert not db.prepare_video_scan(24,now=1002,sources=SOURCES)
    assert db.prepare_video_scan(24,now=1001+24*3600,sources=SOURCES)
    assert db.add_video({'aweme_id':'2'},'search','词')
    assert db.rows('SELECT * FROM comments')[0]['text']=='保留评论'


def test_bulk_delete_is_atomic_if_selected_work_is_scanning(tmp_path):
    db = DB(tmp_path/'db.sqlite3')
    for ident in ('1','2'):
        db.add_video({'aweme_id':ident},'search','词')
    db.set_video_status('2','scanning')
    with pytest.raises(ValueError,match='停止任务'):
        db.delete_videos(['1','2'])
    assert len(db.video_tasks())==2
    assert db.rows('SELECT * FROM video_exclusions')==[]
