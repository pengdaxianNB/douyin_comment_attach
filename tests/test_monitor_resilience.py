import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.browser import Browser, CommentPageUnavailable, unique_visible
from app.engine import Engine, recent_comments


def test_recent_comments_uses_rolling_day_window_and_rejects_unknown_times():
    now = datetime(2026,9,17,8,tzinfo=timezone.utc)
    comments = [
        {'id':'boundary','comment_time':(now-timedelta(days=3)).isoformat()},
        {'id':'recent','comment_time':(now-timedelta(hours=2)).isoformat()},
        {'id':'old','comment_time':(now-timedelta(days=3,seconds=1)).isoformat()},
        {'id':'future','comment_time':(now+timedelta(seconds=1)).isoformat()},
        {'id':'unknown','comment_time':''},
    ]

    assert [item['id'] for item in recent_comments(comments,3,now)] == ['boundary','recent']


def test_wait_for_comment_ready_allows_slow_panel(monkeypatch):
    panel = SimpleNamespace(
        count=AsyncMock(side_effect=[0,0,1]),
        is_visible=AsyncMock(return_value=True),
    )
    page = SimpleNamespace(locator=lambda selector: panel)
    browser = Browser(SimpleNamespace(pages=[page]),{'comment_panel':'#comments'},page=page)
    browser.guard = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr('app.browser.asyncio.sleep',sleep)

    ready = asyncio.run(browser.wait_for_comment_ready([],set()))

    assert ready is panel
    assert sleep.await_count == 2


def test_unique_visible_ignores_hidden_duplicate_panel():
    hidden = SimpleNamespace(is_visible=AsyncMock(return_value=False))
    shown = SimpleNamespace(is_visible=AsyncMock(return_value=True))
    locator = SimpleNamespace(
        count=AsyncMock(return_value=2),
        nth=lambda index: (hidden,shown)[index],
    )

    assert asyncio.run(unique_visible(locator)) is shown


def test_comments_retry_with_backoff_when_panel_is_temporarily_missing(monkeypatch):
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

    assert page.goto.await_count == 3
    assert len(logs) == 2
    assert '第 2/3 次' in logs[0]
    assert '第 3/3 次' in logs[1]


def test_scan_skips_one_bad_video_and_continues(tmp_path):
    engine = Engine(SimpleNamespace(),tmp_path)
    logs = []
    engine.log.connect(logs.append)
    comment = {'id':'c1','comment_time':datetime.now(timezone.utc).isoformat()}
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


def test_scan_filters_old_comments_before_ai(tmp_path):
    engine = Engine(SimpleNamespace(),tmp_path)
    logs = []
    engine.log.connect(logs.append)
    now = datetime.now(timezone.utc)
    recent = {'id':'new','comment_time':(now-timedelta(days=1)).isoformat()}
    old = {'id':'old','comment_time':(now-timedelta(days=8)).isoformat()}
    browser = SimpleNamespace(comments=AsyncMock(return_value=[recent,old]))

    with patch('app.engine.classify_comments',new=AsyncMock()) as classify:
        asyncio.run(engine.scan_videos({'name':'主号'},browser,SimpleNamespace(),
                                       [{'aweme_id':'video'}],
                                       {'ai_concurrency':5,'comment_days':3}))

    assert classify.await_args.args[2] == [recent]
    assert any('过滤 1 条' in line and '未提交 AI' in line for line in logs)


def test_scan_keeps_running_after_three_consecutive_comment_page_failures(tmp_path):
    engine = Engine(SimpleNamespace(),tmp_path)
    logs = []
    engine.log.connect(logs.append)
    browser = SimpleNamespace(comments=AsyncMock(
        side_effect=CommentPageUnavailable('页面未就绪')))
    videos = [{'aweme_id':str(n)} for n in range(4)]

    with patch('app.engine.asyncio.sleep',new=AsyncMock()) as sleep:
        asyncio.run(engine.scan_videos({'name':'主号'},browser,SimpleNamespace(),videos,
                                       {'ai_concurrency':5}))

    assert browser.comments.await_count == 4
    sleep.assert_awaited_once_with(10)
    assert any('保持任务运行' in line for line in logs)
