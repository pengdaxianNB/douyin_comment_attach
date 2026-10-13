"""No site traffic: exercise the coordinator with local, deterministic senders."""
import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.browser import AccountNeedsAttention
from app.db import DB
from app.engine import Engine


def pool(tmp_path, count=10, customers=20):
    db = DB(tmp_path/'pool.sqlite3')
    for i in range(count):
        db.account(str(i))
    db.save_settings({'base_url':'https://example.com/v1','model':'test'})
    db.import_customers([{'uid':f'MS4wLjABAAAAcustomer_{i:06d}','name':str(i)}
                         for i in range(customers)])
    engine = Engine(db,tmp_path)
    engine.contexts = {}; engine.jobs = {}
    accounts = engine.cluster_accounts('messages')
    engine.running_accounts.update(a['id'] for a in accounts)
    return db,engine,accounts


def test_ten_accounts_share_three_slots_and_keep_pages_during_cooldown(tmp_path):
    db,engine,accounts = pool(tmp_path)
    active=set(); peak=0; sent=[]; turns=Counter(); browsers={}
    real_sleep=asyncio.sleep
    async def opened(account):
        ident=account['id']
        if ident in browsers:
            return browsers[ident]
        async def check(customer):
            nonlocal peak
            active.add(ident); peak=max(peak,len(active))
            await real_sleep(0)
        async def send(customer,message,before_click):
            before_click(); await real_sleep(0)
            sent.append(customer['uid']); turns[ident]+=1
        async def idle():
            active.discard(ident)
            await real_sleep(0)
        browser=SimpleNamespace(s=None,check_reachable=check,send=send,go_idle=AsyncMock(side_effect=idle))
        browsers[ident]=engine.contexts[ident]=browser
        return browser
    async def cooldown(seconds):
        if seconds==120:
            ident=next(i for i,t in engine.jobs.items() if t is asyncio.current_task())
            assert ident not in active and engine.contexts[ident] is browsers[ident]
            if turns[ident]>=2:
                raise asyncio.CancelledError
        await real_sleep(0)
    engine.change_browser_state=AsyncMock(); engine.open=opened
    engine.rebuild_browser=AsyncMock()
    with patch('app.engine.get_key',return_value='key'),patch('app.engine.AI') as ai, \
         patch('app.engine.asyncio.sleep',side_effect=cooldown):
        ai.return_value.message=AsyncMock(return_value='本地测试')
        asyncio.run(asyncio.wait_for(engine.run_cluster(accounts,'messages'),timeout=3))
    assert peak==3 and not active
    assert set(turns)=={a['id'] for a in accounts} and set(turns.values())=={2}
    assert len(sent)==len(set(sent))==20
    assert len(engine.contexts)==10 and not engine.jobs and not engine.running_accounts
    assert all(b.go_idle.await_count==2 for b in browsers.values())
    assert all(r['status']=='sent' for r in db.rows('SELECT status FROM customers'))
    engine.rebuild_browser.assert_not_awaited()


@pytest.mark.parametrize('limited',[False,True])
def test_waiting_for_customers_or_quota_never_opens_page(tmp_path,limited):
    db,engine,accounts = pool(tmp_path,count=1,customers=int(limited))
    if limited:
        db.execute('UPDATE accounts SET daily_limit=1')
        db.execute("INSERT INTO events(account_id,uid,kind) VALUES (?,?,'sent')",
                   (accounts[0]['id'],'other'))
    engine.open = AsyncMock()
    engine.change_browser_state = AsyncMock()
    with patch('app.engine.get_key',return_value='key'),patch('app.engine.AI'), \
         patch('app.engine.asyncio.sleep',side_effect=asyncio.CancelledError):
        asyncio.run(engine.run_cluster(accounts,'messages'))
    engine.open.assert_not_awaited()
    engine.change_browser_state.assert_not_awaited()
    assert not engine.jobs


@pytest.mark.parametrize('submitted',[False,True])
def test_cancel_active_and_queued_accounts_preserves_pages_and_send_state(tmp_path,submitted):
    db,engine,accounts = pool(tmp_path,count=4,customers=4)
    db.save_settings({'message_concurrency':1})
    live = set(); entered = asyncio.Event()
    async def control(ident,operation):
        if operation=='wake': live.add(ident)
        else: live.discard(ident)
    async def send(customer,message,before_click):
        if submitted: before_click()
        entered.set(); await asyncio.Future()
    engine.change_browser_state = control
    browser=SimpleNamespace(s=None,check_reachable=AsyncMock(),send=send,go_idle=AsyncMock())
    async def opened(account):
        engine.contexts[account['id']]=browser
        return browser
    engine.open=AsyncMock(side_effect=opened)
    async def run():
        engine.cluster_mode = 'messages'
        engine.cluster_task = asyncio.create_task(engine.run_cluster(accounts,'messages'))
        await entered.wait()
        assert len(live)==1
        assert len(db.rows("SELECT 1 FROM customers WHERE status='processing'"))==1
        await engine.stop_cluster()
    with patch('app.engine.get_key',return_value='key'),patch('app.engine.AI') as ai:
        ai.return_value.message = AsyncMock(return_value='本地测试')
        asyncio.run(asyncio.wait_for(run(),timeout=3))
    states = Counter(r['status'] for r in db.rows('SELECT status FROM customers'))
    assert states == ({'failed':1,'pending':3} if submitted else {'pending':4})
    assert len(live)==1 and not engine.jobs and not engine.busy()
    assert len(engine.contexts)==1
    browser.go_idle.assert_awaited_once()
    engine.open.assert_awaited_once()


def test_failed_initial_connection_rebuilds_and_saves_completed_send(tmp_path):
    db,engine,accounts = pool(tmp_path,count=1,customers=1)
    browser = SimpleNamespace(s=None,check_reachable=AsyncMock(),send=AsyncMock())
    engine.change_browser_state = AsyncMock()
    engine.open = AsyncMock(side_effect=[ValueError('未找到该账号的内嵌页面'),browser])
    with patch('app.engine.get_key',return_value='key'),patch('app.engine.AI') as ai, \
         patch('app.engine.asyncio.sleep',side_effect=asyncio.CancelledError):
        ai.return_value.message = AsyncMock(return_value='本地测试')
        asyncio.run(engine.run_cluster(accounts,'messages'))
    assert [c.args[1] for c in engine.change_browser_state.await_args_list]==['wake','reset']
    assert db.rows('SELECT status FROM customers')==[{'status':'sent'}]


def test_unrecoverable_open_returns_customer_without_sending(tmp_path):
    db,engine,accounts = pool(tmp_path,count=1,customers=1)
    engine.change_browser_state = AsyncMock()
    engine.open = AsyncMock(side_effect=ValueError('未找到页面'))
    with patch('app.engine.get_key',return_value='key'),patch('app.engine.AI') as ai, \
         patch('app.engine.asyncio.sleep',new=AsyncMock()):
        asyncio.run(engine.run_cluster(accounts,'messages'))
    assert engine.open.await_count==4
    ai.return_value.message.assert_not_called()
    assert db.rows('SELECT status FROM customers')==[{'status':'pending'}]
    assert not db.rows("SELECT 1 FROM events WHERE kind='send_attempt'")


def test_failed_idle_navigation_does_not_block_other_senders(tmp_path):
    db,engine,accounts=pool(tmp_path,count=2,customers=2)
    db.save_settings({'message_concurrency':1})
    browsers={}
    async def opened(account):
        browser=SimpleNamespace(s=None,check_reachable=AsyncMock(),send=AsyncMock(),
                                go_idle=AsyncMock(side_effect=RuntimeError('个人页暂时不可用')))
        browsers[account['id']]=engine.contexts[account['id']]=browser
        return browser
    engine.change_browser_state=AsyncMock(); engine.open=AsyncMock(side_effect=opened)
    engine.rebuild_browser=AsyncMock()
    with patch('app.engine.get_key',return_value='key'),patch('app.engine.AI') as ai, \
         patch('app.engine.asyncio.sleep',side_effect=asyncio.CancelledError):
        ai.return_value.message=AsyncMock(return_value='本地测试')
        asyncio.run(asyncio.wait_for(engine.run_cluster(accounts,'messages'),timeout=3))
    assert engine.open.await_count==2 and len(engine.contexts)==2
    assert all(b.go_idle.await_count==1 for b in browsers.values())
    assert Counter(r['status'] for r in db.rows('SELECT status FROM customers'))=={'sent':2}
    engine.rebuild_browser.assert_not_awaited()


def test_account_verification_is_preserved_instead_of_returning_to_idle(tmp_path):
    _,engine,accounts=pool(tmp_path,count=1,customers=0)
    engine.sender_slots=asyncio.Semaphore(1)
    browser=SimpleNamespace(go_idle=AsyncMock())
    engine.contexts[accounts[0]['id']]=browser
    async def run():
        async with engine.sender_turn(accounts[0]):
            raise AccountNeedsAttention('请完成验证')
    with pytest.raises(AccountNeedsAttention):
        asyncio.run(run())
    assert engine.contexts[accounts[0]['id']] is browser
    browser.go_idle.assert_not_awaited()
    assert not engine.sender_slots.locked()


def test_gui_acknowledgement_times_out_and_ignores_late_result(tmp_path,monkeypatch):
    _,engine,_ = pool(tmp_path,count=1,customers=0)
    requests=[]
    engine.browser_state.connect(lambda ident,op,request:requests.append(request))
    monkeypatch.setattr('app.engine.PAGE_CLEANUP_TIMEOUT',.01)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(engine.change_browser_state('sender','wake'))
    assert not engine.browser_requests
    engine.complete_browser_request(requests[0])
