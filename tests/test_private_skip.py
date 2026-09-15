import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
from app.browser import Browser,SkippedCustomer
from app.db import DB

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
