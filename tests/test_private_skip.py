import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
import pytest
from app.browser import Browser,SkippedCustomer,PrivateAccount
from app.db import DB
from app.engine import Engine

UID='MS4wLjABAAAABRJKRY1YxfLwD6uDMCQo8O9la_PxcmCqMppnILWojlA'


def page_with_marker(marker=None):
    page=Mock(goto=AsyncMock())
    item=Mock(is_visible=AsyncMock(return_value=True))
    def get_by_text(text,exact=True):
        return Mock(all=AsyncMock(return_value=[item] if text==marker else []))
    page.get_by_text=get_by_text
    return page


def test_private_is_skipped_before_follow(monkeypatch):
    page=page_with_marker('私密账号')
    browser=Browser(SimpleNamespace(pages=[page]),{})
    browser.guard=AsyncMock()
    follow=AsyncMock()
    monkeypatch.setattr('app.browser.follow_profile_button',follow)
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    try:
        asyncio.run(browser.send({'url':'https://www.douyin.com/user/'+UID},'你好',lambda:None))
    except SkippedCustomer as exc:
        assert '私密账号' in str(exc)
    else: raise AssertionError('should skip')
    follow.assert_not_awaited()
    page.locator.assert_not_called()


def test_private_check_needs_explicit_marker():
    page=page_with_marker(None)
    browser=Browser(SimpleNamespace(pages=[page]),{})
    assert asyncio.run(browser.private_account_reason()) is None


def test_skipped_customer_not_reclaimed(tmp_path):
    db=DB(tmp_path/'db.sqlite3')
    db.import_customers([{'uid':UID}]); db.claim('sender')
    db.finish(UID,'sender','skipped',error='私密账号，无法私信')
    assert db.claim('other') is None
    assert db.rows('SELECT status,error FROM customers')==[{'status':'skipped','error':'私密账号，无法私信'}]
    assert db.import_customers([{'uid':UID,'status':'pending'}])==0
    assert db.rows('SELECT status FROM customers')[0]['status']=='skipped'


def test_reachability_check_before_ai(monkeypatch):
    page=page_with_marker('私密账号')
    browser=Browser(SimpleNamespace(pages=[page]),{})
    browser.guard=AsyncMock()
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    try:
        asyncio.run(browser.check_reachable({'url':'https://www.douyin.com/user/'+UID}))
    except SkippedCustomer:
        pass
    else: raise AssertionError('should skip before AI')
    page.goto.assert_awaited_once()


def test_private_account_immediately_processes_next_customer():
    second_uid='MS4wLjABAAAAsecond_customer_123456'
    with TemporaryDirectory() as folder:
        db=DB(Path(folder)/'db.sqlite3')
        account_id=db.account('sender','从号')
        db.save_settings({'base_url':'https://example.com/v1','model':'test'})
        for index,(uid,name) in enumerate(((UID,'私密客户'),(second_uid,'普通客户')),1):
            db.ingest(dict(id=str(index),uid=uid,name=name,text='咨询',comment_time='',
                           video_id='v1',video_title='',blogger=''),True,'')
        db.execute('UPDATE customers SET updated=1 WHERE uid=?',(UID,))
        db.execute('UPDATE customers SET updated=2 WHERE uid=?',(second_uid,))

        engine=Engine(db,folder)
        engine.jobs={}
        browser=AsyncMock()
        browser.check_reachable.side_effect=[PrivateAccount('私密账号，无法私信'),None]
        engine.open=AsyncMock(return_value=browser)

        with patch('app.engine.get_key',return_value='key'), \
             patch('app.engine.AI') as ai, \
             patch('app.engine.asyncio.sleep',side_effect=asyncio.CancelledError):
            ai.return_value.message=AsyncMock(return_value='你好')
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(engine.work(dict(id=account_id,role='sender',name='从号')))

        rows={row['uid']:row['status'] for row in db.rows('SELECT uid,status FROM customers')}
        assert rows=={UID:'skipped',second_uid:'sent'}
        assert browser.check_reachable.await_count==2
        browser.send.assert_awaited_once()
