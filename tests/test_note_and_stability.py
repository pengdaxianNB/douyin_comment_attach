import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from playwright.async_api import Error as PlaywrightError

from app.ai import AI, json_object
from app.browser import (AccountNeedsAttention, Browser, NeedsAttention, comment_page_url,
                         is_note_post, parse_comments)
from app.db import DB, parse_work_url, video_id, work_url
from app.engine import Engine, MAX_TRANSIENT_FAILURES

UID = 'MS4wLjABAAAAexample_customer_123456'
NOTE = 'https://www.douyin.com/note/7690537643185017257'


# ---------- note (图文笔记) links ----------

def test_parse_work_url_accepts_video_and_note_links():
    assert parse_work_url(NOTE) == ('7690537643185017257','note')
    assert parse_work_url(NOTE+'/?from=share') == ('7690537643185017257','note')
    assert parse_work_url('https://douyin.com/video/1234567890123456789') == ('1234567890123456789','video')
    assert video_id(NOTE) == '7690537643185017257'
    assert work_url('7690537643185017257','note') == NOTE
    for bad in ('https://www.douyin.com/user/'+UID,'https://v.douyin.com/abc/',
                'https://evil.com/note/7690537643185017257','http://www.douyin.com/note/7690537643185017257',
                'https://www.douyin.com/note/abc','https://www.douyin.com/note/'):
        with pytest.raises(ValueError):
            parse_work_url(bad)


def test_manual_note_keeps_note_url_and_survives_rescan(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    ident = db.add_manual_video(NOTE)
    row = db.video_tasks()[0]
    assert (row['id'],row['url'],row['source_type']) == (ident,NOTE,'manual')
    assert row['title'] == '手动录入笔记 '+ident
    with pytest.raises(ValueError, match='已在'):
        db.add_manual_video(NOTE)
    # The persistent source is re-added with the same note URL after a reset.
    db.clear_videos()
    item = db.rows('SELECT * FROM manual_videos')[0]
    db.add_video({'aweme_id':item['id'],'desc':item['title'],'url':item['url']},'manual','手动录入',item['url'])
    assert db.video_tasks()[0]['url'] == NOTE


def test_add_video_marks_notes_and_ignores_mismatched_urls(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    db.add_video({'aweme_id':'7690537643185017257','desc':'笔记','kind':'note'},'blogger','博主')
    db.add_video({'aweme_id':'1234567890123456789','desc':'视频','url':NOTE},'keyword','词')
    urls = {r['id']:r['url'] for r in db.video_tasks()}
    assert urls['7690537643185017257'] == NOTE
    assert urls['1234567890123456789'] == 'https://www.douyin.com/video/1234567890123456789'
    # Rediscovering a known video as a note upgrades its page URL.
    db.add_video({'aweme_id':'1234567890123456789','kind':'note'},'blogger','博主')
    urls = {r['id']:r['url'] for r in db.video_tasks()}
    assert urls['1234567890123456789'] == 'https://www.douyin.com/note/1234567890123456789'


def test_note_detection_and_comment_page_url():
    assert is_note_post({'aweme_type':68}) and is_note_post({'images':[{}]})
    assert not is_note_post({'aweme_type':0}) and not is_note_post(None)
    assert comment_page_url({'aweme_id':'7690537643185017257','url':NOTE}) == NOTE
    assert comment_page_url({'aweme_id':'7690537643185017257','kind':'note'}) == NOTE
    assert comment_page_url({'aweme_id':'123'}) == 'https://www.douyin.com/video/123'
    # A URL for a different work must never redirect the scan elsewhere.
    assert comment_page_url({'aweme_id':'1234567890123456789','url':NOTE}) == \
        'https://www.douyin.com/video/1234567890123456789'


def test_comments_open_the_note_page(monkeypatch):
    browser = Browser(SimpleNamespace(pages=[SimpleNamespace()]),{'scroll_limit':1},page=SimpleNamespace())
    capture = AsyncMock(return_value=[])
    monkeypatch.setattr(browser,'capture',capture)

    asyncio.run(browser.comments({'aweme_id':'7690537643185017257','url':NOTE}))

    assert capture.await_args.args[0] == NOTE


# ---------- malformed site data ----------

def test_parse_comments_skips_malformed_records_instead_of_failing():
    good = dict(cid='1',text='咨询',create_time=10,user=dict(sec_uid=UID,nickname='客户'))
    payload = {'comments':[
        good,
        'garbage',
        dict(good,cid='2',user=dict(sec_uid='not-a-sec-uid')),
        dict(good,cid='3',create_time=None),
        dict(good,cid='4',create_time='bad'),
        dict(good,cid='5',text='   '),
        dict(good,cid='6',user='oops'),
    ]}
    rows = parse_comments(payload,{'aweme_id':'v','desc':'d'})
    assert [r['id'] for r in rows] == ['1','3','4']


# ---------- engine resilience ----------

def test_scan_treats_abnormal_api_reply_as_one_bad_video(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    bad = {'aweme_id':'1234567890123456789','desc':'异常','author':{}}
    good = {'aweme_id':'2234567890123456789','desc':'正常','author':{}}
    db.add_video(bad,'keyword','词'); db.add_video(good,'keyword','词')
    engine = Engine(db,tmp_path)
    browser = SimpleNamespace(comments=AsyncMock(side_effect=[NeedsAttention('接口状态异常 5'),[]]))

    with patch('app.engine.classify_comments',new=AsyncMock()):
        asyncio.run(engine.scan_videos({'name':'主号'},browser,SimpleNamespace(),[bad,good],
                                       {'ai_concurrency':5,'comment_days':7}))

    statuses = {r['id']:r['status'] for r in db.video_tasks()}
    assert statuses == {bad['aweme_id']:'failed',good['aweme_id']:'done'}


def test_scan_account_prompt_keeps_video_pending_and_stops(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    video = {'aweme_id':'1234567890123456789','desc':'等待','author':{}}
    db.add_video(video,'keyword','词')
    engine = Engine(db,tmp_path)
    browser = SimpleNamespace(comments=AsyncMock(side_effect=AccountNeedsAttention('页面需要人工处理：扫码登录')))

    with pytest.raises(AccountNeedsAttention):
        asyncio.run(engine.scan_videos({'name':'主号'},browser,SimpleNamespace(),[video],
                                       {'ai_concurrency':5,'comment_days':7}))

    assert db.video_tasks()[0]['status'] == 'pending'


def test_rebuild_survives_playwright_timeout_on_one_source(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    db.execute("INSERT INTO bloggers(id,name) VALUES ('a-id','A 超时博主')")
    db.execute("INSERT INTO bloggers(id,name) VALUES ('b-id','B 正常博主')")
    good = {'aweme_id':'1234567890123456789','desc':'正常','author':{}}
    fresh = SimpleNamespace(videos=AsyncMock(return_value=[good]),s=None)
    frozen = SimpleNamespace(videos=AsyncMock(side_effect=PlaywrightError('Timeout 30000ms exceeded')))
    engine = Engine(db,tmp_path)
    engine.rebuild_browser = AsyncMock(return_value=fresh)

    complete = asyncio.run(engine.rebuild_video_list({'id':'main','name':'主号'},frozen,{'days':7}))

    assert complete is False
    engine.rebuild_browser.assert_awaited_once()
    fresh.videos.assert_awaited_once()
    assert [r['id'] for r in db.video_tasks()] == [good['aweme_id']]


def test_monitor_retries_transient_failure_then_stops_after_limit(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    account_id = db.account('main','主号')
    db.save_settings({'base_url':'https://example.com/v1','model':'test'})
    engine = Engine(db,tmp_path)
    engine.jobs = {}
    engine.open = AsyncMock(return_value=SimpleNamespace(s=None))
    calls = []
    statuses = []
    engine.status.connect(lambda ident,text:statuses.append(text))

    async def flaky(account,b,ai,s):
        calls.append(1)
        raise RuntimeError('页面异常')

    engine.monitor_round = flaky
    with patch('app.engine.get_key',return_value='key'), patch('app.engine.AI'), \
         patch('app.engine.asyncio.sleep',new=AsyncMock()) as sleep:
        asyncio.run(engine.work({'id':account_id,'role':'main','name':'主号'}))

    assert len(calls) == MAX_TRANSIENT_FAILURES
    assert sleep.await_count == MAX_TRANSIENT_FAILURES-1
    assert any('连续' in text and '已停止' in text for text in statuses)


def test_monitor_recovers_after_one_transient_failure(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    account_id = db.account('main','主号')
    db.save_settings({'base_url':'https://example.com/v1','model':'test'})
    engine = Engine(db,tmp_path)
    engine.jobs = {}
    engine.open = AsyncMock(return_value=SimpleNamespace(s=None))
    rounds = []

    async def round_(account,b,ai,s):
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError('偶发异常')
        raise asyncio.CancelledError

    engine.monitor_round = round_
    with patch('app.engine.get_key',return_value='key'), patch('app.engine.AI'), \
         patch('app.engine.asyncio.sleep',new=AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(engine.work({'id':account_id,'role':'main','name':'主号'}))

    assert len(rounds) == 2


def test_ai_failure_returns_customer_to_queue_and_sender_keeps_running(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    account_id = db.account('sender','从号')
    db.save_settings({'base_url':'https://example.com/v1','model':'test'})
    db.import_customers([dict(uid=UID,name='客户')])
    engine = Engine(db,tmp_path)
    engine.jobs = {}
    browser = AsyncMock()
    engine.open = AsyncMock(return_value=browser)
    logs = []; engine.log.connect(logs.append)

    with patch('app.engine.get_key',return_value='key'), patch('app.engine.AI') as ai, \
         patch('app.engine.asyncio.sleep',side_effect=[None,asyncio.CancelledError]):
        ai.return_value.message = AsyncMock(side_effect=[ValueError('HTTP 429：请求限流或额度不足'),'你好'])
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(engine.work({'id':account_id,'role':'sender','name':'从号'}))

    row = db.rows('SELECT status,message FROM customers')[0]
    assert row == {'status':'sent','message':'你好'}
    assert ai.return_value.message.await_count == 2
    assert any('放回队列' in line for line in logs)


def test_persistent_ai_outage_stops_sender_without_losing_customer(tmp_path):
    db = DB(tmp_path/'test.sqlite3')
    account_id = db.account('sender','从号')
    db.save_settings({'base_url':'https://example.com/v1','model':'test'})
    db.import_customers([dict(uid=UID,name='客户')])
    engine = Engine(db,tmp_path)
    engine.jobs = {}
    engine.open = AsyncMock(return_value=AsyncMock())
    statuses = []; engine.status.connect(lambda ident,text:statuses.append(text))

    with patch('app.engine.get_key',return_value='key'), patch('app.engine.AI') as ai, \
         patch('app.engine.asyncio.sleep',new=AsyncMock()):
        ai.return_value.message = AsyncMock(side_effect=ValueError('网络连接失败'))
        asyncio.run(engine.work({'id':account_id,'role':'sender','name':'从号'}))

    assert ai.return_value.message.await_count == MAX_TRANSIENT_FAILURES
    assert db.rows('SELECT status FROM customers')[0]['status'] == 'pending'
    assert any('已停止' in text for text in statuses)


# ---------- AI client ----------

def run_ai(handler, method='classify'):
    logs = []
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    with patch('app.ai.get_key',return_value='key'), \
         patch('app.ai.httpx.AsyncClient',side_effect=lambda **kw:real(transport=transport,**kw)), \
         patch('app.ai.asyncio.sleep',new=AsyncMock()) as sleep:
        ai = AI({'base_url':'https://example.com/v1','model':'m','filter_prompt':'筛选'},logs.append)
        try:
            result = asyncio.run(ai.classify({'text':'想咨询','video_title':'介绍'}))
        except ValueError as exc:
            result = exc
    return result,sleep,'\n'.join(logs)


def test_ai_retries_rate_limit_then_succeeds():
    replies = iter([httpx.Response(429),httpx.Response(503),
                    httpx.Response(200,json={'choices':[{'message':{'content':'{"target":true,"reason":"要买"}'}}]})])
    result,sleep,logs = run_ai(lambda request:next(replies))
    assert result == (True,'要买')
    assert sleep.await_count == 2
    assert '重试' in logs


def test_ai_does_not_retry_authentication_errors():
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(401)
    result,sleep,_ = run_ai(handler)
    assert isinstance(result,ValueError) and '401' in str(result)
    assert len(calls) == 1 and sleep.await_count == 0


def test_ai_gives_up_after_bounded_retries():
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(500)
    result,_,_ = run_ai(handler)
    assert isinstance(result,ValueError) and '500' in str(result)
    assert len(calls) == 3


def test_json_object_tolerates_prose_and_fences():
    assert json_object('```json\n{"target": false, "reason": "x"}\n```') == {'target':False,'reason':'x'}
    assert json_object('判断如下：{"target": true, "reason": "y"} 完毕') == {'target':True,'reason':'y'}
    with pytest.raises(ValueError):
        json_object('没有 JSON')


# ---------- note comment tab ----------

def tab_page(tabs, panel_visible_after_click=True):
    """Page whose comment panel only becomes visible after the tab is clicked."""
    state = {'clicked':False}
    async def click(timeout=None):
        state['clicked'] = True
    items = [SimpleNamespace(is_visible=AsyncMock(return_value=visible),click=AsyncMock(side_effect=click))
             for visible in tabs]
    panel = SimpleNamespace(
        count=AsyncMock(return_value=1),
        is_visible=AsyncMock(side_effect=lambda:state['clicked'] and panel_visible_after_click))
    page = SimpleNamespace(
        locator=lambda selector:panel,
        get_by_text=lambda pattern,exact=False:SimpleNamespace(
            all=AsyncMock(return_value=items),count=AsyncMock(return_value=0)))
    return page,items,panel


def test_comment_tab_pattern_only_matches_the_tab_label():
    from app.browser import COMMENT_TAB
    for text in ('评论(9)','评论（12）','评论 (1.2万)','评论'):
        assert COMMENT_TAB.match(text), text
    for text in ('相关推荐','评论区','留下你的精彩评论吧','评论(9)相关推荐','查看全部评论'):
        assert not COMMENT_TAB.match(text), text


def test_note_page_comment_tab_is_clicked_before_waiting_for_panel(monkeypatch):
    page,items,panel = tab_page([True])
    browser = Browser(SimpleNamespace(pages=[page]),{'comment_panel':'[data-e2e="comment-list"]'},page=page)
    browser.guard = AsyncMock()
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())

    ready = asyncio.run(browser.wait_for_comment_ready([],set()))

    assert ready is panel
    items[0].click.assert_awaited_once()


def test_comment_tab_is_not_clicked_when_ambiguous_or_hidden():
    for tabs in ([True,True],[False],[]):
        page,items,_ = tab_page(tabs)
        browser = Browser(SimpleNamespace(pages=[page]),{},page=page)
        browser.guard = AsyncMock()
        assert asyncio.run(browser.open_comment_tab()) is False
        for item in items:
            item.click.assert_not_awaited()


def test_comment_tab_click_failure_is_not_fatal():
    page,items,_ = tab_page([True])
    items[0].click = AsyncMock(side_effect=PlaywrightError('intercepts pointer events'))
    browser = Browser(SimpleNamespace(pages=[page]),{},page=page)
    browser.guard = AsyncMock()
    assert asyncio.run(browser.open_comment_tab()) is False


def test_short_comment_list_waits_for_api_instead_of_failing(monkeypatch):
    """Note pages fetch comments once the tab opens; a list that fits has no scrollbar."""
    handlers = []
    panel = SimpleNamespace(
        count=AsyncMock(return_value=1),
        is_visible=AsyncMock(return_value=True),
        evaluate=AsyncMock(return_value={'found':False,'moved':False}))
    page = SimpleNamespace(
        goto=AsyncMock(),
        locator=lambda selector:panel,
        on=lambda event,callback:handlers.append(callback),
        remove_listener=lambda *args:None)
    browser = Browser(SimpleNamespace(pages=[page]),
                      {'comment_panel':'#c','scroll_limit':3},log=lambda text:None,page=page)
    browser.guard = AsyncMock()
    browser.wait_for_comment_ready = AsyncMock()
    reply = {'status_code':0,'has_more':0,'comments':[]}
    response = SimpleNamespace(url='https://www.douyin.com/aweme/v1/web/comment/list/?x=1',
                               json=AsyncMock(return_value=reply))

    async def late_response(delay):
        if not any(h for h in handlers if getattr(h,'fired',False)):
            for handler in handlers:
                handler(response)
                handler.fired = True

    monkeypatch.setattr('app.browser.asyncio.sleep',late_response)

    packets = asyncio.run(browser.capture(NOTE,'/aweme/v1/web/comment/list/','comments',3))

    assert len(packets) == 1 and not browser.warnings
