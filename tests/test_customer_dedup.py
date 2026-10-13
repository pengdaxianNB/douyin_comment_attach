import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.classifier import classify_comments
from app.db import DB


UID = 'MS4wLjABAAAAexample_customer_123456'


def comment(cid, created, text=None):
    return dict(id=cid,uid=UID,name='客户 '+cid,text=text or cid,
                comment_time=created,video_id='video-'+cid,
                video_title='作品 '+cid,blogger='博主 '+cid)


def sent_customer(db):
    db.import_customers([dict(uid=UID)])
    db.claim('sender')
    db.start_send(UID,'sender','已发送文案')
    assert db.finish(UID,'sender','sent','已发送文案')


@pytest.mark.parametrize('late_status',['failed','pending','processing','skipped','sent'])
def test_late_outcome_cannot_change_success_or_create_event(tmp_path,late_status):
    db = DB(tmp_path/'test.sqlite3')
    sent_customer(db)
    before = db.rows('SELECT * FROM customers')[0]
    events = db.rows('SELECT * FROM events')
    assert not db.finish(UID,'sender',late_status,'错误文案','迟到回调')
    assert db.rows('SELECT * FROM customers')[0]==before
    assert db.rows('SELECT * FROM events')==events
    assert db.requeue_failed()==0
    assert db.claim('other') is None


def test_concurrent_late_callbacks_preserve_success(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    sent_customer(db)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _:db.finish(UID,'sender','failed',error='停止'),range(16)))
    assert not any(results)
    assert db.rows('SELECT status,error FROM customers')==[dict(status='sent',error='')]
    assert len(db.rows("SELECT * FROM events WHERE kind='sent'"))==1
    assert not db.rows("SELECT * FROM events WHERE kind='failed'")


def test_finish_by_wrong_owner_does_not_publish_false_success(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    db.import_customers([dict(uid=UID)])
    db.claim('sender')
    assert not db.finish(UID,'other','sent','错误账号文案')
    assert db.rows('SELECT status,account_id FROM customers')==[dict(status='processing',account_id='sender')]
    assert not db.rows('SELECT * FROM events')
    assert db.finish(UID,'sender','sent','正确账号文案')


@pytest.mark.parametrize('owner',['sender','other'])
def test_start_send_requires_active_unsent_owner(tmp_path,owner):
    db = DB(tmp_path/'test.sqlite3')
    sent_customer(db)
    events = db.rows('SELECT * FROM events')
    with pytest.raises(ValueError,match='阻止提交发送'):
        db.start_send(UID,owner,'禁止重复发送')
    assert db.rows('SELECT * FROM events')==events
    assert db.rows('SELECT status,message FROM customers')==[dict(status='sent',message='已发送文案')]


def legacy_success(db, status, evidence):
    db.import_customers([dict(uid=UID,status='failed',message='历史文案')])
    if evidence=='sent_at':
        db.execute("UPDATE customers SET sent_at='2026-10-01 01:00:00' WHERE uid=?",(UID,))
    else:
        db.execute("INSERT INTO events(uid,kind,at) VALUES (?,'sent','2026-10-01 01:00:00')",(UID,))
    db.execute("UPDATE customers SET status=?,error='旧版本错误状态' WHERE uid=?",(status,UID))


@pytest.mark.parametrize('evidence',['sent_at','history'])
@pytest.mark.parametrize('status',['pending','processing','failed','uncertain','skipped'])
def test_startup_repairs_legacy_success(tmp_path,evidence,status):
    db = DB(tmp_path/'test.sqlite3')
    legacy_success(db,status,evidence)
    db = DB(db.path)
    db.recover()
    assert db.rows('SELECT status,sent_at,error,message FROM customers')==[
        dict(status='sent',sent_at='2026-10-01 01:00:00',error='',message='历史文案')]
    assert db.claim('other') is None


@pytest.mark.parametrize('evidence',['sent_at','history'])
def test_bulk_requeue_excludes_legacy_success_and_keeps_real_failures(tmp_path,evidence):
    db = DB(tmp_path/'test.sqlite3')
    legacy_success(db,'failed',evidence)
    retry = UID+'retry'
    db.import_customers([dict(uid=retry,status='failed',message='保留草稿')])
    # import also repairs legacy rows; downgrade again to exercise requeue itself.
    db.execute("UPDATE customers SET status='failed' WHERE uid=?",(UID,))
    assert db.failed_requeue_count()==1
    assert db.requeue_failed()==1
    rows = {r['uid']:r for r in db.rows('SELECT uid,status,message,sent_at FROM customers')}
    assert rows[UID]['status']=='sent' and rows[UID]['sent_at']
    assert rows[retry]['status']=='pending' and rows[retry]['message']=='保留草稿'
    assert db.claim('other')['uid']==retry
    assert not db.rows("SELECT * FROM events WHERE uid=? AND kind='pending'",(UID,))


@pytest.mark.parametrize('evidence',['sent_at','history'])
def test_claim_and_manual_requeue_cannot_bypass_success(tmp_path,evidence):
    db = DB(tmp_path/'test.sqlite3')
    legacy_success(db,'pending',evidence)
    assert db.claim('other') is None
    for status in ('pending','skipped'):
        with pytest.raises(ValueError,match='成功私信记录'):
            db.mark_customer(UID,status)
    assert not db.rows("SELECT * FROM events WHERE kind='pending'")


@pytest.mark.parametrize('rediscover',['import','comment'])
def test_deleted_customer_with_retained_success_history_is_not_requeued(tmp_path,rediscover):
    db = DB(tmp_path/'test.sqlite3')
    sent_customer(db)
    db.delete_data('customers',[UID])
    if rediscover=='import':
        assert db.import_customers([dict(uid=UID,status='pending')])==1
    else:
        db.ingest(comment('new','2026-10-13T06:00:00Z'),True,'需求')
    assert db.rows('SELECT status FROM customers')==[dict(status='sent')]
    assert db.claim('other') is None


def test_manual_mark_success_is_terminal_and_ordinary_failure_can_retry(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    db.import_customers([dict(uid=UID,status='failed')])
    db.mark_customer(UID,'pending')
    db.mark_customer(UID,'sent')
    with pytest.raises(ValueError,match='成功私信记录'):
        db.mark_customer(UID,'pending')
    assert db.claim('other') is None


def test_import_null_send_time_is_not_success_evidence(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    assert db.import_customers([dict(uid=UID,sent_at=None,status='failed')])==1
    assert db.requeue_failed()==1
    assert db.claim('sender')['uid']==UID


@pytest.mark.parametrize('old_time',[
    '2026-10-12T06:00:00Z', '2026-10-13T06:00:00Z',
    '2026-10-13T14:00:00+08:00', '', 'bad time', None,
])
def test_older_equal_or_invalid_time_cannot_replace_newer_material(tmp_path,old_time):
    db = DB(tmp_path/'test.sqlite3')
    db.ingest(comment('new','2026-10-13T06:00:00+00:00'),True,'需求')
    before = db.rows('SELECT * FROM customers')[0]
    assert db.ingest(comment('old',old_time),True,'需求')
    assert db.rows('SELECT * FROM customers')[0]==before
    assert len(db.rows('SELECT * FROM comments'))==2


@pytest.mark.parametrize('existing_time,new_time',[
    ('2026-10-13T06:00:00Z','2026-10-13T14:01:00+08:00'),
    ('2026-10-13 14:00:00','2026-10-13T06:01:00Z'),
    ('2026-10-13T06:00:00Z','2026-10-13 14:01:00'),
    ('','2026-10-13T06:00:00Z'),
    ('bad time','2026-10-13T06:00:00Z'),
])
def test_newer_material_updates_as_one_bundle(tmp_path,existing_time,new_time):
    db = DB(tmp_path/'test.sqlite3')
    db.ingest(comment('old',existing_time),True,'需求')
    db.ingest(comment('new',new_time),True,'需求')
    row = db.rows('SELECT * FROM customers')[0]
    assert (row['name'],row['text'],row['comment_time'],row['video_title'],row['blogger'])==(
        '客户 new','new',new_time,'作品 new','博主 new')
    assert row['status']=='pending'


def test_naive_beijing_time_does_not_outrank_later_utc_time(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    db.ingest(comment('new','2026-10-13T06:00:00Z'),True,'需求')
    db.ingest(comment('old','2026-10-13 13:59:00'),True,'需求')
    assert db.rows('SELECT text FROM customers')==[dict(text='new')]


def test_concurrent_ai_finishing_old_comment_last_keeps_newest(tmp_path):
    async def run():
        db = DB(tmp_path/'test.sqlite3')
        new_done = asyncio.Event()
        async def classify(c):
            if c['id']=='old':
                await new_done.wait()
            return True,'需求'
        def changed():
            if db.rows("SELECT 1 FROM comments WHERE id='new'"):
                new_done.set()
        data = [comment('old','2026-10-12T06:00:00Z'),comment('new','2026-10-13T06:00:00Z')]
        await classify_comments(db,SimpleNamespace(classify=classify),data,2,lambda _:None,changed)
        assert db.rows('SELECT text FROM customers')==[dict(text='new')]
        assert len(db.rows('SELECT * FROM comments'))==2
    asyncio.run(run())


@pytest.mark.parametrize('status',['pending','processing','sent','failed','skipped'])
def test_startup_repairs_only_pending_material_from_target_comments(tmp_path,status):
    db = DB(tmp_path/'test.sqlite3')
    db.ingest(comment('old','2026-10-12T06:00:00Z'),True,'需求')
    db.ingest(comment('new','2026-10-13T06:00:00Z'),True,'需求')
    db.ingest(comment('non-target','2026-10-13T07:00:00Z'),False,'无需求')
    db.execute("""UPDATE customers SET status=?,text='旧评论',comment_time='2026-10-12T06:00:00Z',
        updated=123,message='保留草稿' WHERE uid=?""",(status,UID))
    db = DB(db.path)
    row = db.rows('SELECT * FROM customers')[0]
    assert row['text']==('new' if status=='pending' else '旧评论')
    if status=='pending':
        assert (row['name'],row['video_title'],row['blogger'])==('客户 new','作品 new','博主 new')
    assert row['updated']==123 and row['message']=='保留草稿'


@pytest.mark.parametrize('stored_time',['2026-10-13T06:00:00Z','bad time',''])
def test_startup_does_not_replace_newer_imported_material(tmp_path,stored_time):
    db = DB(tmp_path/'test.sqlite3')
    db.ingest(comment('old',stored_time),True,'需求')
    db.execute("UPDATE customers SET text='更新的导入素材',comment_time='2026-10-13 14:01:00' WHERE uid=?",(UID,))
    db = DB(db.path)
    assert db.rows('SELECT text FROM customers')==[dict(text='更新的导入素材')]
