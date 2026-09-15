import asyncio
from unittest.mock import AsyncMock,Mock
import pytest
from app.browser import follow_in_panel,NeedsAttention


def scenario(initial=False,success=True):
    state={'followed':initial}
    async def click():state['followed']=success
    btn=Mock(is_visible=AsyncMock(return_value=True),is_enabled=AsyncMock(return_value=True),click=AsyncMock(side_effect=click))
    panel=Mock()
    def query(text,exact):
        async def all_items():
            return [btn] if (text=='已关注' and state['followed']) or (text=='关注' and not state['followed']) else []
        return Mock(all=all_items)
    panel.get_by_text=query
    return panel,btn


def test_follow_then_verify():
    panel,btn=scenario();logs=[]
    asyncio.run(follow_in_panel(panel,AsyncMock(),logs.append))
    btn.click.assert_awaited_once()
    assert '关注成功' in logs[-1]


def test_existing_follow_is_not_toggled():
    panel,btn=scenario(initial=True)
    asyncio.run(follow_in_panel(panel,AsyncMock(),lambda _:None))
    btn.click.assert_not_awaited()


def test_unconfirmed_follow_stops(monkeypatch):
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    panel,btn=scenario(success=False)
    with pytest.raises(NeedsAttention,match='未确认已关注'):
        asyncio.run(follow_in_panel(panel,AsyncMock(),lambda _:None))
    btn.click.assert_awaited_once()


def test_header_bounds_allow_wide_chat_and_exclude_profile():
    from app.browser import in_chat_header
    editor={'x':700,'y':900,'width':750,'height':100}
    send={'x':1500,'y':950,'width':32,'height':32}
    assert in_chat_header({'x':1400,'y':100,'width':45,'height':25},editor,send)
    assert not in_chat_header({'x':500,'y':100,'width':100,'height':40},editor,send)
    assert not in_chat_header({'x':1400,'y':920,'width':45,'height':25},editor,send)


def test_unrelated_follow_state_is_not_accepted(monkeypatch):
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    panel,btn=scenario(initial=True)
    with pytest.raises(NeedsAttention,match='唯一的关注按钮'):
        asyncio.run(follow_in_panel(panel,AsyncMock(),lambda _:None,AsyncMock(return_value=False)))
    btn.click.assert_not_awaited()


def test_profile_fallback_checks_state_after_click():
    from app.browser import follow_profile_button
    state={'text':'关注'}
    async def click(): state['text']='已关注'
    item=Mock(is_visible=AsyncMock(return_value=True),is_enabled=AsyncMock(return_value=True),
              inner_text=AsyncMock(side_effect=lambda:state['text']),click=AsyncMock(side_effect=click))
    page=Mock();page.locator.return_value.filter.return_value.all=AsyncMock(return_value=[item])
    logs=[]
    asyncio.run(follow_profile_button(page,AsyncMock(),logs.append))
    item.click.assert_awaited_once()
    assert '主页关注成功' in logs[-1]


def test_profile_fallback_does_not_unfollow():
    from app.browser import follow_profile_button
    item=Mock(is_visible=AsyncMock(return_value=True),is_enabled=AsyncMock(return_value=True),
              inner_text=AsyncMock(return_value='已关注'),click=AsyncMock())
    page=Mock();page.locator.return_value.filter.return_value.all=AsyncMock(return_value=[item])
    asyncio.run(follow_profile_button(page,AsyncMock(),lambda _:None))
    item.click.assert_not_awaited()


def test_follow_finishes_before_opening_chat(monkeypatch):
    from app.browser import Browser
    from types import SimpleNamespace
    events=[]
    async def follow(*args):events.append('follow_confirmed')
    async def click():events.append('open_chat')
    monkeypatch.setattr('app.browser.follow_profile_button',follow)
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    trigger=Mock(is_visible=AsyncMock(return_value=True),click=AsyncMock(side_effect=click))
    page=Mock(goto=AsyncMock())
    page.get_by_text.return_value.all=AsyncMock(return_value=[trigger])
    page.locator.return_value.wait_for=AsyncMock(side_effect=RuntimeError('stop at editor'))
    browser=Browser(SimpleNamespace(pages=[page]),{'message_editor':'[contenteditable=true]'})
    browser.guard=AsyncMock()
    browser.private_account_reason=AsyncMock(return_value=None)
    with pytest.raises(RuntimeError,match='stop at editor'):
        asyncio.run(browser.send({'url':'https://www.douyin.com/user/test'},'test',lambda:None))
    assert events==['follow_confirmed','open_chat']


def test_failed_follow_does_not_open_chat(monkeypatch):
    from app.browser import Browser
    from types import SimpleNamespace
    monkeypatch.setattr('app.browser.follow_profile_button',AsyncMock(side_effect=NeedsAttention('关注未成功')))
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    page=Mock(goto=AsyncMock())
    browser=Browser(SimpleNamespace(pages=[page]),{})
    browser.guard=AsyncMock()
    browser.private_account_reason=AsyncMock(return_value=None)
    with pytest.raises(NeedsAttention):
        asyncio.run(browser.send({'url':'https://www.douyin.com/user/test'},'test',lambda:None))
    page.get_by_text.assert_not_called()
