import asyncio
from unittest.mock import AsyncMock,Mock
import pytest
from app.browser import close_open_message_panel,follow_in_panel,MESSAGE_PANEL_CLOSE,NeedsAttention


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
    async def click(**kwargs): state['text']='已关注'
    item=Mock(is_visible=AsyncMock(return_value=True),is_enabled=AsyncMock(return_value=True),
              inner_text=AsyncMock(side_effect=lambda:state['text']),click=AsyncMock(side_effect=click))
    page=Mock();page.locator.return_value.all=AsyncMock(return_value=[item])
    page.get_by_text.return_value.all=AsyncMock(return_value=[])
    logs=[]
    asyncio.run(follow_profile_button(page,AsyncMock(),logs.append))
    item.click.assert_awaited_once_with(timeout=4000)
    assert '主页关注成功' in logs[-1]


def test_profile_fallback_does_not_unfollow():
    from app.browser import follow_profile_button
    item=Mock(is_visible=AsyncMock(return_value=True),is_enabled=AsyncMock(return_value=True),
              inner_text=AsyncMock(return_value='已关注'),click=AsyncMock())
    page=Mock();page.locator.return_value.all=AsyncMock(return_value=[item])
    page.get_by_text.return_value.all=AsyncMock(return_value=[])
    asyncio.run(follow_profile_button(page,AsyncMock(),lambda _:None))
    item.click.assert_not_awaited()


def test_open_message_panel_is_closed_before_follow():
    from app.browser import follow_profile_button
    events=[]
    title=Mock(is_visible=AsyncMock(side_effect=[True,False]),
               evaluate=AsyncMock(return_value={'panel':True,'point':{'x':900,'y':100}}))
    item=Mock(is_visible=AsyncMock(return_value=True),is_enabled=AsyncMock(return_value=True),
              inner_text=AsyncMock(return_value='已关注'),click=AsyncMock())
    page=Mock();page.get_by_text.return_value.all=AsyncMock(return_value=[title])
    page.mouse.click=AsyncMock(side_effect=lambda *args: events.append('close'))
    page.locator.return_value.all=AsyncMock(side_effect=lambda: events.append('locate') or [item])
    asyncio.run(follow_profile_button(page,AsyncMock(),lambda _:None))
    assert events==['close','locate']


def test_message_panel_script_error_is_a_customer_interaction_failure():
    title=Mock(is_visible=AsyncMock(return_value=True),
               evaluate=AsyncMock(side_effect=TypeError('target.click is not a function')))
    page=Mock();page.get_by_text.return_value.all=AsyncMock(return_value=[title])
    with pytest.raises(NeedsAttention,match='消息侧栏关闭失败'):
        asyncio.run(close_open_message_panel(page,AsyncMock(),lambda _:None))


def test_profile_click_actionability_timeout_uses_safe_retry():
    from app.browser import follow_profile_button
    state={'text':'关注'}
    async def retry(script): state['text']='已关注'
    item=Mock(is_visible=AsyncMock(return_value=True),is_enabled=AsyncMock(return_value=True),
              inner_text=AsyncMock(side_effect=lambda:state['text']),
              click=AsyncMock(side_effect=TimeoutError('waiting for element to be visible, enabled and stable')),
              evaluate=AsyncMock(side_effect=retry))
    page=Mock();page.locator.return_value.all=AsyncMock(return_value=[item])
    page.get_by_text.return_value.all=AsyncMock(return_value=[])
    logs=[]
    asyncio.run(follow_profile_button(page,AsyncMock(),logs.append))
    item.evaluate.assert_awaited_once()
    assert any('正在重试' in line for line in logs)
    assert '主页关注成功' in logs[-1]


def test_message_button_uses_button_and_safe_retry():
    from app.browser import click_profile_message_button
    button=Mock(is_visible=AsyncMock(return_value=True),is_enabled=AsyncMock(return_value=True),
                click=AsyncMock(side_effect=[TimeoutError('waiting for element to be visible, enabled and stable'),None]),
                evaluate=AsyncMock())
    role=Mock(all=AsyncMock(return_value=[button]))
    page=Mock();page.get_by_role.return_value=role
    page.get_by_text.return_value.all=AsyncMock(return_value=[])
    logs=[]
    asyncio.run(click_profile_message_button(page,AsyncMock(),logs.append))
    assert button.click.await_count==2
    button.evaluate.assert_not_awaited()
    page.locator.assert_not_called()
    assert any('正在重试' in line for line in logs)


def test_message_button_uses_live_mouse_point_after_two_preclick_timeouts():
    from app.browser import click_profile_message_button
    button=Mock(is_visible=AsyncMock(return_value=True),is_enabled=AsyncMock(return_value=True),
                click=AsyncMock(side_effect=TimeoutError('element is not stable')),
                evaluate=AsyncMock(return_value={'point':{'x':140,'y':85},'reason':'按钮中心可点击'}))
    page=Mock()
    page.get_by_role.return_value.all=AsyncMock(return_value=[button])
    page.get_by_text.return_value.all=AsyncMock(return_value=[])
    page.mouse.click=AsyncMock()
    logs=[]

    asyncio.run(click_profile_message_button(page,AsyncMock(),logs.append))

    assert button.click.await_count==2
    page.mouse.click.assert_awaited_once_with(140,85)
    assert any('element is not stable' in line for line in logs)


def test_message_button_never_clicks_through_an_unknown_overlay():
    from app.browser import click_profile_message_button
    button=Mock(is_visible=AsyncMock(return_value=True),is_enabled=AsyncMock(return_value=True),
                click=AsyncMock(side_effect=TimeoutError('div.modal intercepts pointer events')),
                evaluate=AsyncMock(return_value={'reason':'按钮被遮挡：div.modal'}))
    page=Mock()
    page.get_by_role.return_value.all=AsyncMock(return_value=[button])
    page.get_by_text.return_value.all=AsyncMock(return_value=[])
    page.mouse.click=AsyncMock()

    with pytest.raises(NeedsAttention,match='按钮被遮挡：div.modal') as error:
        asyncio.run(click_profile_message_button(page,AsyncMock(),lambda _:None))

    assert 'intercepts pointer events' in str(error.value)
    page.mouse.click.assert_not_awaited()


def test_message_button_does_not_repeat_a_click_followed_by_navigation_timeout():
    from app.browser import click_profile_message_button,click_blocked_before_dispatch
    error=TimeoutError('waiting for element to be visible, enabled and stable\n'
                       'performing click action\nclick action done\n'
                       'waiting for scheduled navigations to finish')
    button=Mock(is_visible=AsyncMock(return_value=True),is_enabled=AsyncMock(return_value=True),
                click=AsyncMock(side_effect=error),evaluate=AsyncMock())
    page=Mock()
    page.get_by_role.return_value.all=AsyncMock(return_value=[button])
    page.get_by_text.return_value.all=AsyncMock(return_value=[])
    page.mouse.click=AsyncMock()

    assert not click_blocked_before_dispatch(error)
    with pytest.raises(NeedsAttention,match='waiting for scheduled navigations'):
        asyncio.run(click_profile_message_button(page,AsyncMock(),lambda _:None))

    button.click.assert_awaited_once()
    button.evaluate.assert_not_awaited()
    page.mouse.click.assert_not_awaited()


def test_wait_for_message_editor_prefers_douyin_composer():
    from app.browser import wait_for_message_editor
    other=Mock(is_visible=AsyncMock(return_value=True),get_attribute=AsyncMock(return_value='search-editor'))
    composer=Mock(is_visible=AsyncMock(return_value=True),
                  get_attribute=AsyncMock(return_value='zone-container messageEditorinputArea'),
                  evaluate=AsyncMock(return_value=True))
    page=Mock();page.locator.return_value.all=AsyncMock(return_value=[other,composer])
    assert asyncio.run(wait_for_message_editor(page,'[contenteditable=true]',AsyncMock())) is composer


def test_wait_for_editor_closes_wrong_message_list_immediately():
    from app.browser import wait_for_message_editor
    title=Mock(is_visible=AsyncMock(side_effect=[True,False]),
               evaluate=AsyncMock(return_value={'panel':True,'point':{'x':900,'y':100}}))
    page=Mock();page.locator.return_value.all=AsyncMock(return_value=[])
    page.get_by_text.return_value.all=AsyncMock(return_value=[title])
    page.mouse.click=AsyncMock()
    logs=[]
    result=asyncio.run(wait_for_message_editor(
        page,'[contenteditable=true]',AsyncMock(),logs.append))
    assert result is None
    assert '已点击关闭按钮' in logs[-1]


def test_follow_finishes_before_opening_chat(monkeypatch):
    from app.browser import Browser
    from types import SimpleNamespace
    events=[]
    async def follow(*args):events.append('follow_confirmed')
    async def open_chat(*args):events.append('open_chat')
    monkeypatch.setattr('app.browser.follow_profile_button',follow)
    monkeypatch.setattr('app.browser.click_profile_message_button',open_chat)
    monkeypatch.setattr('app.browser.wait_for_message_editor',AsyncMock(side_effect=RuntimeError('stop at editor')))
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    page=Mock(goto=AsyncMock())
    page.get_by_text.return_value.all=AsyncMock(return_value=[])
    browser=Browser(SimpleNamespace(pages=[page]),{'message_editor':'[contenteditable=true]'})
    browser.check_reachable=AsyncMock()
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
    browser.check_reachable=AsyncMock()
    browser.guard=AsyncMock()
    browser.private_account_reason=AsyncMock(return_value=None)
    with pytest.raises(NeedsAttention):
        asyncio.run(browser.send({'url':'https://www.douyin.com/user/test'},'test',lambda:None))
    page.get_by_text.assert_not_called()
