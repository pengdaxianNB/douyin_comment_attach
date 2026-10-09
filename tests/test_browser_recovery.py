"""Recovery regressions: all browser and sender operations stay local/mocked."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.browser import AccountNeedsAttention, Browser, SearchPageUnavailable
from app.db import DB
from app.engine import Engine, BROWSER_REBUILD_ATTEMPTS, PAGE_RECYCLE_OPERATIONS
from playwright.async_api import Error as PlaywrightError


@pytest.mark.parametrize('error',[SearchPageUnavailable('搜索持续加载'),
                                  PlaywrightError('页面超时'),asyncio.TimeoutError()])
def test_search_rebuild_retries_current_keyword_before_next(tmp_path,error):
    db=DB(tmp_path/'test.sqlite3')
    db.execute("INSERT INTO search_keywords(keyword) VALUES ('当前词'),('下一个词')")
    account={'id':'main','name':'主号'}
    settings={'days':7}
    good={'aweme_id':'1234567890123456789','desc':'结果','author':{}}
    frozen=SimpleNamespace(search_videos=AsyncMock(side_effect=error),warnings=[])
    fresh=SimpleNamespace(search_videos=AsyncMock(side_effect=[[good],[]]),
                          release_page_memory=AsyncMock(),warnings=[],s=None)
    engine=Engine(db,tmp_path)
    engine.rebuild_browser=AsyncMock(return_value=fresh)
    logs=[];engine.log.connect(logs.append)

    assert asyncio.run(engine.rebuild_video_list(account,frozen,settings)) is True

    engine.rebuild_browser.assert_awaited_once_with(account)
    assert [call.args for call in fresh.search_videos.await_args_list] == [('当前词',),('下一个词',)]
    assert fresh.s is settings
    assert fresh.release_page_memory.await_count == 2
    assert [row['id'] for row in db.video_tasks()] == [good['aweme_id']]
    assert any('重试当前词' in line for line in logs)


def test_repeated_search_loading_failure_is_bounded_and_next_source_runs(tmp_path):
    db=DB(tmp_path/'test.sqlite3')
    db.execute("INSERT INTO search_keywords(keyword) VALUES ('坏词'),('好词')")
    frozen=SimpleNamespace(search_videos=AsyncMock(side_effect=SearchPageUnavailable('卡住')))
    good={'aweme_id':'1234567890123456789','desc':'结果','author':{}}
    fresh=SimpleNamespace(search_videos=AsyncMock(side_effect=[SearchPageUnavailable('仍卡住'),[good]]),
                          release_page_memory=AsyncMock(),s=None,warnings=[])
    engine=Engine(db,tmp_path)
    engine.rebuild_browser=AsyncMock(return_value=fresh)

    assert asyncio.run(engine.rebuild_video_list({'id':'main','name':'主号'},frozen,{'days':7})) is False

    engine.rebuild_browser.assert_awaited_once()
    assert frozen.search_videos.await_count == 1
    assert [call.args[0] for call in fresh.search_videos.await_args_list] == ['坏词','好词']
    assert [row['id'] for row in db.video_tasks()] == [good['aweme_id']]
    assert fresh.release_page_memory.await_count == 2


@pytest.mark.parametrize('error',[AccountNeedsAttention('请完成安全验证'),asyncio.CancelledError()])
def test_search_discovery_does_not_rebuild_on_account_prompt_or_user_stop(tmp_path,error):
    db=DB(tmp_path/'test.sqlite3')
    db.execute("INSERT INTO search_keywords(keyword) VALUES ('测试词')")
    browser=SimpleNamespace(search_videos=AsyncMock(side_effect=error),release_page_memory=AsyncMock())
    engine=Engine(db,tmp_path)
    engine.rebuild_browser=AsyncMock()

    with pytest.raises(type(error)):
        asyncio.run(engine.rebuild_video_list({'id':'main','name':'主号'},browser,{'days':7}))

    engine.rebuild_browser.assert_not_awaited()
    browser.release_page_memory.assert_not_awaited()


def test_search_batch_recycles_before_next_keyword_and_uses_new_browser(tmp_path):
    db=DB(tmp_path/'test.sqlite3')
    keywords=[f'词{i}' for i in range(PAGE_RECYCLE_OPERATIONS+2)]
    for keyword in keywords:
        db.execute('INSERT INTO search_keywords(keyword) VALUES (?)',(keyword,))
    frozen=SimpleNamespace(search_videos=AsyncMock(return_value=[]),
                           release_page_memory=AsyncMock(),warnings=[])
    fresh=SimpleNamespace(search_videos=AsyncMock(return_value=[]),
                          release_page_memory=AsyncMock(),warnings=[],s=None)
    engine=Engine(db,tmp_path)
    engine.contexts={'main':frozen}
    boundaries=[]
    async def rebuild(account,memory_cleanup=False):
        boundaries.append((frozen.search_videos.await_count,memory_cleanup))
        engine.page_operations[account['id']]=0
        engine.contexts[account['id']]=fresh
        return fresh
    engine.rebuild_browser=AsyncMock(side_effect=rebuild)

    assert asyncio.run(engine.rebuild_video_list({'id':'main','name':'主号'},frozen,{'days':7})) is True

    assert boundaries == [(PAGE_RECYCLE_OPERATIONS,True)]
    assert frozen.release_page_memory.await_count == PAGE_RECYCLE_OPERATIONS-1
    assert [call.args[0] for call in fresh.search_videos.await_args_list] == keywords[-2:]
    assert engine.contexts['main'] is fresh


def test_partial_search_results_remain_usable_but_list_requires_retry(tmp_path):
    db=DB(tmp_path/'test.sqlite3')
    db.execute("INSERT INTO search_keywords(keyword) VALUES ('测试词')")
    good={'aweme_id':'1234567890123456789','desc':'结果','author':{}}
    browser=SimpleNamespace(warnings=[],release_page_memory=AsyncMock())
    async def search(keyword):
        browser.warnings.append('搜索未完成，本轮覆盖可能不完整')
        return [good]
    browser.search_videos=search
    engine=Engine(db,tmp_path)

    assert asyncio.run(engine.rebuild_video_list({'id':'main','name':'主号'},browser,{'days':7})) is False
    assert [row['id'] for row in db.video_tasks()] == [good['aweme_id']]


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
    ident = db.account('从号')
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
    ident = db.account('从号')
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
