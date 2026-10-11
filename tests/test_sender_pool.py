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


def test_ten_accounts_share_three_slots_and_release_before_cooldown(tmp_path):
    db,engine,accounts = pool(tmp_path)
    live = set(); peak = 0; sent = []; turns = Counter()
    real_sleep = asyncio.sleep
    async def control(ident,operation):
        nonlocal peak
        if operation in ('wake','reset'):
            live.add(ident); peak = max(peak,len(live))
        else:
            live.discard(ident)
        await real_sleep(0)
    async def opened(account):
        async def send(customer,message,before_click):
            before_click(); await real_sleep(0)
            sent.append(customer['uid']); turns[account['id']] += 1
        return SimpleNamespace(s=None,check_reachable=AsyncMock(),send=send)
    async def cooldown(seconds):
        # Awaited by the worker only after its page is released.
        if seconds == 120:
            current = asyncio.current_task()
            ident = next(i for i,t in engine.jobs.items() if t is current)
            assert ident not in live and ident not in engine.contexts
            if turns[ident] >= 2:
                raise asyncio.CancelledError
        await real_sleep(0)
    engine.change_browser_state = control; engine.open = opened
    with patch('app.engine.get_key',return_value='key'),patch('app.engine.AI') as ai, \
         patch('app.engine.asyncio.sleep',side_effect=cooldown):
        ai.return_value.message = AsyncMock(return_value='本地测试')
        asyncio.run(asyncio.wait_for(engine.run_cluster(accounts,'messages'),timeout=3))
    assert peak == 3 and not live
    assert set(turns) == {a['id'] for a in accounts}
    assert set(turns.values()) == {2}
    assert len(sent) == len(set(sent)) == 20
    assert not engine.contexts and not engine.jobs and not engine.running_accounts
    assert all(r['status']=='sent' for r in db.rows('SELECT status FROM customers'))


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
    assert all(c.args[1]=='park' for c in engine.change_browser_state.await_args_list)
    assert not engine.jobs


@pytest.mark.parametrize('submitted',[False,True])
def test_cancel_active_and_queued_accounts_releases_pages_and_preserves_send_state(tmp_path,submitted):
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
    engine.open = AsyncMock(return_value=SimpleNamespace(s=None,check_reachable=AsyncMock(),send=send))
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
    assert not live and not engine.jobs and not engine.busy()
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
    assert [c.args[1] for c in engine.change_browser_state.await_args_list]==['wake','reset','park']
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


def test_unconfirmed_release_blocks_new_page_launches(tmp_path):
    db,engine,accounts = pool(tmp_path,count=2,customers=2)
    db.save_settings({'message_concurrency':1})
    live = set()
    async def control(ident,operation):
        if operation=='wake': live.add(ident)
        else: raise RuntimeError('GUI 未完成释放')
    engine.change_browser_state = control
    engine.open = AsyncMock(return_value=SimpleNamespace(s=None,check_reachable=AsyncMock(),send=AsyncMock()))
    with patch('app.engine.get_key',return_value='key'),patch('app.engine.AI') as ai, \
         patch('app.engine.asyncio.sleep',new=AsyncMock()):
        ai.return_value.message = AsyncMock(return_value='本地测试')
        asyncio.run(asyncio.wait_for(engine.run_cluster(accounts,'messages'),timeout=3))
    assert len(live)==1
    engine.open.assert_awaited_once()
    assert Counter(r['status'] for r in db.rows('SELECT status FROM customers'))=={'sent':1,'pending':1}


def test_gui_acknowledgement_times_out_and_ignores_late_result(tmp_path,monkeypatch):
    _,engine,_ = pool(tmp_path,count=1,customers=0)
    requests=[]
    engine.browser_state.connect(lambda ident,op,request:requests.append(request))
    monkeypatch.setattr('app.engine.PAGE_CLEANUP_TIMEOUT',.01)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(engine.change_browser_state('sender','wake'))
    assert not engine.browser_requests
    engine.complete_browser_request(requests[0])
