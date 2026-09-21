import asyncio
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.browser import (Browser, CommentPageUnavailable, click_visible_control_at_center,
                         smallest_visible_text_control, unique_visible, visible_in_page_frames)
from app.db import DB
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


def test_creator_filter_lookup_searches_child_frames():
    item=SimpleNamespace(bounding_box=AsyncMock(return_value={'width':180,'height':44}))
    page=SimpleNamespace(frames=[object(),object()])
    with patch('app.browser.smallest_visible_text_control',new=AsyncMock(
            side_effect=[None,item])) as finder:
        frame,found=asyncio.run(visible_in_page_frames(page,'发布时间不限'))

    assert frame is page.frames[1]
    assert found is item
    assert finder.await_count==2


def test_creator_filter_matches_visible_byted_select_input_value():
    item=SimpleNamespace(
        is_visible=AsyncMock(return_value=True),
        input_value=AsyncMock(return_value='发布时间不限'),
        bounding_box=AsyncMock(return_value={'width':100,'height':22}),
    )
    empty=SimpleNamespace(all=AsyncMock(return_value=[]))
    empty.filter=lambda **kwargs:empty
    page=SimpleNamespace(
        locator=lambda selector: SimpleNamespace(all=AsyncMock(return_value=[item]))
            if selector=='input[placeholder="请选择"]' else empty,
        get_by_text=lambda pattern:empty,
    )

    found=asyncio.run(smallest_visible_text_control(
        page,re.compile(r'发布\s*时间\s*不限')))

    assert found is item


def test_creator_filter_clicks_outer_select_without_auto_scroll():
    trigger=SimpleNamespace(
        count=AsyncMock(return_value=1),
        is_visible=AsyncMock(return_value=True),
        bounding_box=AsyncMock(return_value={'x':100,'y':200,'width':140,'height':34}),
    )
    item=SimpleNamespace(locator=lambda selector:trigger)
    page=SimpleNamespace(mouse=SimpleNamespace(click=AsyncMock()))

    asyncio.run(click_visible_control_at_center(page,item))

    page.mouse.click.assert_awaited_once_with(170,217)


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


def test_scan_updates_persisted_video_statuses(tmp_path):
    db=DB(tmp_path/'test.sqlite3')
    good={'aweme_id':'1234567890123456789','desc':'正常视频','author':{}}
    bad={'aweme_id':'2234567890123456789','desc':'失败视频','author':{}}
    db.add_video(good,'keyword','控糖')
    db.add_video(bad,'keyword','控糖')
    engine=Engine(db,tmp_path)
    browser=SimpleNamespace(comments=AsyncMock(side_effect=[[],CommentPageUnavailable('评论区未就绪')]))

    with patch('app.engine.classify_comments',new=AsyncMock()):
        asyncio.run(engine.scan_videos({'name':'主号'},browser,SimpleNamespace(),[good,bad],
                                       {'ai_concurrency':5,'comment_days':7}))

    statuses={row['id']:(row['status'],row['error']) for row in db.video_tasks()}
    assert statuses[good['aweme_id']]==('done','')
    assert statuses[bad['aweme_id']][0]=='failed'
    assert '评论区未就绪' in statuses[bad['aweme_id']][1]


def test_scan_abandons_cached_work_when_video_list_was_cleared(tmp_path):
    db=DB(tmp_path/'test.sqlite3')
    video={'aweme_id':'1234567890123456789','desc':'旧列表视频','author':{}}
    db.add_video(video,'keyword','控糖')
    cached=db.video_tasks()
    db.clear_videos()
    engine=Engine(db,tmp_path)
    logs=[];engine.log.connect(logs.append)
    browser=SimpleNamespace(comments=AsyncMock())

    asyncio.run(engine.scan_videos({'name':'主号'},browser,SimpleNamespace(),cached,
                                   {'ai_concurrency':5,'comment_days':7}))

    browser.comments.assert_not_awaited()
    assert any('列表已清空' in line for line in logs)


def test_rebuild_scans_keywords_before_bloggers(tmp_path):
    db=DB(tmp_path/'test.sqlite3')
    db.execute("INSERT INTO keywords(keyword) VALUES ('控糖')")
    db.execute("INSERT INTO bloggers(id,name) VALUES ('blogger-id','博主甲')")
    order=[]
    async def keyword_videos(keyword,days):
        order.append(('keyword',keyword))
        return [{'aweme_id':'1234567890123456789','desc':'关键词视频','author':{}}]
    async def blogger_videos(uid,days):
        order.append(('blogger',uid))
        return [{'aweme_id':'2234567890123456789','desc':'博主视频','author':{}}]
    browser=SimpleNamespace(keyword_videos=keyword_videos,videos=blogger_videos)
    engine=Engine(db,tmp_path)

    asyncio.run(engine.rebuild_video_list({'id':'main','name':'主号'},browser,{'days':7}))

    assert order==[('keyword','控糖'),('blogger','blogger-id')]
