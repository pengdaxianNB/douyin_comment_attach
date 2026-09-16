import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.browser import Browser, CommentPageUnavailable, NeedsAttention
from app.engine import Engine


def test_comments_retry_once_when_panel_is_temporarily_missing(monkeypatch):
    panel = SimpleNamespace(
        count=AsyncMock(return_value=0),
        is_visible=AsyncMock(return_value=False),
    )
    page = SimpleNamespace(
        goto=AsyncMock(),
        locator=lambda selector: panel,
        on=lambda *args: None,
        remove_listener=lambda *args: None,
    )
    logs = []
    browser = Browser(SimpleNamespace(pages=[page]),
                      {'comment_panel':'#comments','scroll_limit':1},log=logs.append)
    browser.guard = AsyncMock()
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())

    with pytest.raises(CommentPageUnavailable,match='评论区'):
        asyncio.run(browser.comments({'aweme_id':'123'}))

    assert page.goto.await_count == 2
    assert len(logs) == 1
    assert '重新打开重试' in logs[0]


def test_scan_skips_one_bad_video_and_continues(tmp_path):
    engine = Engine(SimpleNamespace(),tmp_path)
    logs = []
    engine.log.connect(logs.append)
    comment = {'id':'c1'}
    browser = SimpleNamespace(comments=AsyncMock(side_effect=[
        CommentPageUnavailable('页面未就绪'),[comment],
    ]))
    videos = [
        {'aweme_id':'bad','desc':'特殊作品'},
        {'aweme_id':'good','desc':'正常作品'},
    ]

    with patch('app.engine.classify_comments',new=AsyncMock()) as classify:
        asyncio.run(engine.scan_videos({'name':'主号'},browser,SimpleNamespace(),videos,
                                       {'ai_concurrency':5}))

    classify.assert_awaited_once()
    assert classify.await_args.args[2] == [comment]
    assert any('已跳过视频 bad' in line for line in logs)


def test_scan_stops_after_three_consecutive_comment_page_failures(tmp_path):
    engine = Engine(SimpleNamespace(),tmp_path)
    browser = SimpleNamespace(comments=AsyncMock(
        side_effect=CommentPageUnavailable('页面未就绪')))
    videos = [{'aweme_id':str(n)} for n in range(4)]

    with pytest.raises(NeedsAttention,match='连续 3 个视频'):
        asyncio.run(engine.scan_videos({'name':'主号'},browser,SimpleNamespace(),videos,
                                       {'ai_concurrency':5}))

    assert browser.comments.await_count == 3
