import asyncio
from unittest.mock import AsyncMock,Mock
from app.browser import composer_send_buttons


def control(visible=True,enabled=True,disabled=None):
    return Mock(is_visible=AsyncMock(return_value=visible),is_enabled=AsyncMock(return_value=enabled),get_attribute=AsyncMock(return_value=disabled))


def test_scopes_marker_to_editor_container():
    icon=control();hidden=control(visible=False)
    candidates=Mock(all=AsyncMock(return_value=[hidden,icon]))
    container=Mock();container.locator.return_value=candidates
    editor=Mock();editor.locator.return_value=container
    visible,enabled=asyncio.run(composer_send_buttons(editor))
    assert visible==[icon] and enabled==[icon]
    assert 'ancestor::' in editor.locator.call_args.args[0]
    container.locator.assert_called_once_with('.e2e-send-msg-btn')


def test_disabled_and_duplicate_controls_not_collapsed():
    first=control();second=control(disabled='true')
    editor=Mock();editor.locator.return_value.locator.return_value.all=AsyncMock(return_value=[first,second])
    visible,enabled=asyncio.run(composer_send_buttons(editor))
    assert len(visible)==2 and enabled==[first]
