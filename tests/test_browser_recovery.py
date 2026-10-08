"""Recovery regressions: all browser and sender operations stay local/mocked."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.browser import AccountNeedsAttention, Browser
from app.db import DB
from app.engine import Engine, BROWSER_REBUILD_ATTEMPTS, PAGE_RECYCLE_OPERATIONS


def test_hung_cleanup_is_cancelled_and_browser_rebuilt(tmp_path,monkeypatch):
    engine = Engine(SimpleNamespace(),tmp_path)
    cancelled = []

    async def hang():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    browser = SimpleNamespace(release_page_memory=hang)
    fresh = SimpleNamespace()
    engine.rebuild_browser = AsyncMock(return_value=fresh)
    monkeypatch.setattr('app.engine.PAGE_CLEANUP_TIMEOUT',0.01)
    account = {'id':'sender','name':'从号'}

    result = asyncio.run(engine.maintain_browser_memory(account,browser))

    assert cancelled == [True]
    assert result is fresh
    engine.rebuild_browser.assert_awaited_once_with(account)


def test_hung_heap_collection_and_session_detach_cannot_block_recovery(tmp_path,monkeypatch):
    engine = Engine(SimpleNamespace(),tmp_path)
    page = SimpleNamespace(goto=AsyncMock())

    async def hang(*args):
        await asyncio.Event().wait()

    session = SimpleNamespace(send=AsyncMock(side_effect=hang),detach=AsyncMock(side_effect=hang))
    context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))
    browser = Browser(context,{},page=page)
    fresh = object()
    engine.rebuild_browser = AsyncMock(return_value=fresh)
    monkeypatch.setattr('app.engine.PAGE_CLEANUP_TIMEOUT',0.01)

    async def run():
        return await asyncio.wait_for(engine.maintain_browser_memory(
            {'id':'sender','name':'从号'},browser),timeout=2)

    assert asyncio.run(run()) is fresh
    page.goto.assert_awaited_once_with('about:blank',wait_until='commit',timeout=10000)
    session.detach.assert_awaited_once()
    engine.rebuild_browser.assert_awaited_once()


def test_user_stop_during_cleanup_does_not_trigger_rebuild(tmp_path):
    engine = Engine(SimpleNamespace(),tmp_path)
    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.Event().wait()

    engine.rebuild_browser = AsyncMock()

    async def run():
        task = asyncio.create_task(engine.maintain_browser_memory(
            {'id':'sender','name':'从号'},SimpleNamespace(release_page_memory=hang)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    engine.rebuild_browser.assert_not_awaited()


def test_rebuild_retries_temporary_failure_and_rebinds_fresh_browser(tmp_path):
    engine = Engine(SimpleNamespace(),tmp_path)
    engine.contexts = {'sender':object()}
    fresh = object()
    engine.open = AsyncMock(side_effect=[RuntimeError('暂时未找到页面'),fresh])
    resets = []
    engine.browser_reset.connect(resets.append)
    with patch('app.engine.asyncio.sleep',new=AsyncMock()) as sleep:
        result = asyncio.run(engine.rebuild_browser({'id':'sender','name':'从号'}))

    assert result is fresh
    assert resets == ['sender','sender']
    sleep.assert_awaited_once_with(5)
    assert 'sender' not in engine.contexts


def test_hung_rebuild_has_deadline_and_limited_attempts(tmp_path,monkeypatch):
    engine = Engine(SimpleNamespace(),tmp_path)
    attempts = []

    async def hang(account):
        attempts.append(account['id'])
        await asyncio.Event().wait()

    engine.open = hang
    monkeypatch.setattr('app.engine.BROWSER_OPEN_TIMEOUT',0.01)
    with patch('app.engine.asyncio.sleep',new=AsyncMock()):
        with pytest.raises(AccountNeedsAttention,match='连续 3 次自动重建失败'):
            asyncio.run(engine.rebuild_browser({'id':'sender','name':'从号'}))
    assert len(attempts) == BROWSER_REBUILD_ATTEMPTS


def test_rebuild_does_not_retry_account_verification(tmp_path):
    engine = Engine(SimpleNamespace(),tmp_path)
    engine.open = AsyncMock(side_effect=AccountNeedsAttention('请完成安全验证'))
    with patch('app.engine.asyncio.sleep',new=AsyncMock()) as sleep:
        with pytest.raises(AccountNeedsAttention,match='安全验证'):
            asyncio.run(engine.rebuild_browser({'id':'sender','name':'从号'}))
    engine.open.assert_awaited_once()
    sleep.assert_not_awaited()


def test_page_binding_does_not_read_other_accounts_renderer_title(tmp_path):
    engine = Engine(SimpleNamespace(),tmp_path)
    wrong = SimpleNamespace(title=AsyncMock(side_effect=AssertionError('other renderer')))
    correct = SimpleNamespace(title=AsyncMock(side_effect=AssertionError('title unnecessary')))
    sessions = [SimpleNamespace(send=AsyncMock(return_value={'targetInfo':{'targetId':target}}),
                                detach=AsyncMock()) for target in ('other','sender-target')]
    context = SimpleNamespace(pages=[wrong,correct],new_cdp_session=AsyncMock(side_effect=sessions))
    engine.connect_embedded_browser = AsyncMock(return_value=SimpleNamespace(contexts=[context]))
    with patch('app.engine.page_id',return_value='sender-target'):
        assert asyncio.run(engine.embedded_page('sender')) is correct
    wrong.title.assert_not_awaited()
    correct.title.assert_not_awaited()
    for session in sessions:
        session.detach.assert_awaited_once()


def test_page_binding_waits_for_gui_to_register_new_target(tmp_path):
    engine = Engine(SimpleNamespace(),tmp_path)
    page = SimpleNamespace(title=AsyncMock())
    session = SimpleNamespace(send=AsyncMock(return_value={'targetInfo':{'targetId':'new-target'}}),
                              detach=AsyncMock())
    context = SimpleNamespace(pages=[page],new_cdp_session=AsyncMock(return_value=session))
    engine.connect_embedded_browser = AsyncMock(return_value=SimpleNamespace(contexts=[context]))
    with patch('app.engine.page_id',side_effect=[None,'new-target','new-target']), \
         patch('app.engine.asyncio.sleep',new=AsyncMock()) as sleep:
        assert asyncio.run(engine.embedded_page('sender')) is page
    sleep.assert_awaited_once_with(0.25)
    context.new_cdp_session.assert_awaited_once()


@pytest.mark.parametrize('recovery_error',[AccountNeedsAttention('重建失败'),asyncio.CancelledError()])
def test_completed_send_is_preserved_when_periodic_rebuild_fails(tmp_path,recovery_error):
    db = DB(tmp_path/'test.sqlite3')
    ident = db.account('sender','从号')
    uid = 'MS4wLjABAAAArecovery_customer_123456'
    db.save_settings({'base_url':'https://example.com/v1','model':'test'})
    db.import_customers([{'uid':uid,'name':'客户'}])
    engine = Engine(db,tmp_path)
    engine.jobs = {}
    engine.page_operations[ident] = PAGE_RECYCLE_OPERATIONS-1
    browser = AsyncMock()

    async def send(customer,message,before_click):
        before_click()

    browser.send.side_effect = send
    engine.open = AsyncMock(return_value=browser)
    engine.rebuild_browser = AsyncMock(side_effect=recovery_error)
    with patch('app.engine.get_key',return_value='key'),patch('app.engine.AI') as ai:
        ai.return_value.message = AsyncMock(return_value='你好')
        if isinstance(recovery_error,asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(engine.work({'id':ident,'role':'sender','name':'从号'}))
        else:
            asyncio.run(engine.work({'id':ident,'role':'sender','name':'从号'}))

    assert db.rows('SELECT status,message,error FROM customers')[0] == {
        'status':'sent','message':'你好','error':''}
    assert [row['kind'] for row in db.rows("SELECT kind FROM events WHERE kind IN ('sent','failed','pending')")] == ['sent']
    browser.send.assert_awaited_once()


def test_sender_processes_next_customer_after_cleanup_recovery(tmp_path,monkeypatch):
    db = DB(tmp_path/'test.sqlite3')
    ident = db.account('sender','从号')
    uids = ['MS4wLjABAAAArecovery_customer_123456','MS4wLjABAAAAnext_customer_1234567890']
    db.save_settings({'base_url':'https://example.com/v1','model':'test'})
    db.import_customers([{'uid':uid,'name':str(index)} for index,uid in enumerate(uids)])
    for index,uid in enumerate(uids):
        db.execute('UPDATE customers SET updated=? WHERE uid=?',(index,uid))
    engine = Engine(db,tmp_path)
    engine.jobs = {}
    frozen = AsyncMock()
    fresh = AsyncMock()

    async def hang():
        await asyncio.Event().wait()

    async def send(customer,message,before_click):
        before_click()

    frozen.release_page_memory.side_effect = hang
    frozen.send.side_effect = send
    fresh.send.side_effect = send
    engine.open = AsyncMock(return_value=frozen)
    engine.rebuild_browser = AsyncMock(return_value=fresh)
    monkeypatch.setattr('app.engine.PAGE_CLEANUP_TIMEOUT',0.01)
    with patch('app.engine.get_key',return_value='key'),patch('app.engine.AI') as ai, \
         patch('app.engine.asyncio.sleep',side_effect=[None,asyncio.CancelledError]):
        ai.return_value.message = AsyncMock(return_value='你好')
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(engine.work({'id':ident,'role':'sender','name':'从号'}))

    assert {row['uid']:row['status'] for row in db.rows('SELECT uid,status FROM customers')} == {
        uid:'sent' for uid in uids}
    assert frozen.send.await_args.args[0]['uid'] == uids[0]
    assert fresh.send.await_args.args[0]['uid'] == uids[1]
    assert len(db.rows("SELECT 1 FROM events WHERE kind='send_attempt'")) == 2
    engine.rebuild_browser.assert_awaited_once()
