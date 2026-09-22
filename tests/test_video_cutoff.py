import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace
from app.browser import Browser,creator_search_videos,is_douyin_host,reached_video_cutoff


def packet(*videos):
    return [{'aweme_list':list(videos),'has_more':True}]


def test_creator_search_parser_accepts_nested_schema_variants_and_ignores_analytics_ids():
    payload={'data':{'list':[
        {'itemId':'1234567890123456789','title':'控糖方法','publishTime':1700000000000,
         'creator':{'nickname':'医生甲'}},
        {'item_id':'2234567890123456789','item_title':'饮食建议',
         'nickname':'医生乙','createTime':'2026-09-18 15:26:39'},
        {'item_id':'3234567890123456789','score':99},
    ]}}
    rows={row['aweme_id']:row for row in creator_search_videos(payload)}
    assert set(rows)=={'1234567890123456789','2234567890123456789'}
    assert rows['1234567890123456789']['create_time']==1700000000
    assert rows['1234567890123456789']['author']['nickname']=='医生甲'
    assert rows['2234567890123456789']['author']['nickname']=='医生乙'
    assert rows['2234567890123456789']['create_time']>0


def test_boundary():
    assert reached_video_cutoff(packet({'create_time':200},{'create_time':99}),100)
    assert not reached_video_cutoff(packet({'create_time':200},{'create_time':100}),100)


def test_old_pin_and_unknown_dates():
    assert not reached_video_cutoff(packet({'create_time':1,'is_top':1},{'create_time':200}),100)
    assert not reached_video_cutoff(packet({'create_time':1,'is_top':True}),100)
    assert not reached_video_cutoff(packet({'create_time':None}),100)
    assert not reached_video_cutoff(packet({'create_time':90},{'create_time':200}),100)
    assert reached_video_cutoff(packet({'create_time':1,'is_top':1},{'create_time':200},{'create_time':90}),100)


def test_douyin_api_subdomains_are_trusted_without_accepting_lookalikes():
    assert is_douyin_host('douyin.com')
    assert is_douyin_host('www.douyin.com')
    assert is_douyin_host('www-hj.douyin.com')
    assert not is_douyin_host('evildouyin.com')
    assert not is_douyin_host('douyin.com.evil.example')
    assert not is_douyin_host(None)


def test_capture_stops_before_scroll(monkeypatch):
    payload=packet({'create_time':200},{'create_time':90})[0]
    callback=None
    page=SimpleNamespace(mouse=SimpleNamespace(move=AsyncMock(),wheel=AsyncMock()))
    def on(event,fn):
        nonlocal callback
        callback=fn
    async def goto(*args,**kwargs):
        callback(SimpleNamespace(url='https://www.douyin.com/aweme/v1/web/aweme/post/',json=AsyncMock(return_value=payload)))
    page.on=on;page.goto=goto;page.remove_listener=lambda *args:None
    browser=Browser(SimpleNamespace(pages=[page]),{})
    browser.guard=AsyncMock()
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    result=asyncio.run(browser.capture('https://www.douyin.com/user/test','/aweme/v1/web/aweme/post/','aweme_list',80,lambda p:reached_video_cutoff(p,100)))
    assert result==[payload]
    page.mouse.wheel.assert_not_called()
    assert '日期边界' in browser.warnings[0]


def test_comments_scroll_container_without_wheeling_page(monkeypatch):
    payload={'comments':[],'has_more':True}
    callback=None
    panel=SimpleNamespace(
        count=AsyncMock(return_value=1),
        is_visible=AsyncMock(return_value=True),
        evaluate=AsyncMock(return_value={'found':True,'moved':True}),
    )
    page=SimpleNamespace(mouse=SimpleNamespace(move=AsyncMock(),wheel=AsyncMock()))
    def on(event,fn):
        nonlocal callback
        callback=fn
    async def goto(*args,**kwargs):
        callback(SimpleNamespace(url='https://www-hj.douyin.com/aweme/v1/web/comment/list/',json=AsyncMock(return_value=payload)))
    page.on=on;page.goto=goto;page.locator=lambda selector:panel
    page.remove_listener=lambda *args:None
    browser=Browser(SimpleNamespace(pages=[page]),{'comment_panel':'#comments'})
    browser.guard=AsyncMock()
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    result=asyncio.run(browser.capture('https://www.douyin.com/video/1','/aweme/v1/web/comment/list/','comments',1))
    assert result==[payload]
    panel.evaluate.assert_awaited_once()
    page.mouse.wheel.assert_not_called()


def test_capture_reopens_page_once_when_first_navigation_has_no_data(monkeypatch):
    payload={'aweme_list':[],'has_more':False}
    callback=None
    visits=0
    logs=[]
    page=SimpleNamespace(mouse=SimpleNamespace(move=AsyncMock(),wheel=AsyncMock()))
    def on(event,fn):
        nonlocal callback
        callback=fn
    async def goto(*args,**kwargs):
        nonlocal visits
        visits+=1
        if visits==2:
            callback(SimpleNamespace(url='https://www.douyin.com/aweme/v1/web/aweme/post/',json=AsyncMock(return_value=payload)))
    page.on=on;page.goto=goto;page.remove_listener=lambda *args:None
    browser=Browser(SimpleNamespace(pages=[page]),{},log=logs.append)
    browser.guard=AsyncMock()
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    result=asyncio.run(browser.capture('https://www.douyin.com/user/test','/aweme/v1/web/aweme/post/','aweme_list',0))
    assert result==[payload]
    assert visits==2
    assert '重新打开当前页面重试' in logs[0]


def test_capture_recovers_when_first_matching_response_is_not_json(monkeypatch):
    payload={'aweme_list':[],'has_more':False}
    callback=None
    visits=0
    logs=[]
    page=SimpleNamespace(mouse=SimpleNamespace(move=AsyncMock(),wheel=AsyncMock()))
    def on(event,fn):
        nonlocal callback
        callback=fn
    async def goto(*args,**kwargs):
        nonlocal visits
        visits+=1
        response=(SimpleNamespace(
            url='https://www.douyin.com/aweme/v1/web/aweme/post/',
            json=AsyncMock(side_effect=ValueError('empty body')))
            if visits==1 else SimpleNamespace(
                url='https://www.douyin.com/aweme/v1/web/aweme/post/',
                json=AsyncMock(return_value=payload)))
        callback(response)
    page.on=on;page.goto=goto;page.remove_listener=lambda *args:None
    browser=Browser(SimpleNamespace(pages=[page]),{},log=logs.append)
    browser.guard=AsyncMock()
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())

    result=asyncio.run(browser.capture(
        'https://www.douyin.com/user/test','/aweme/v1/web/aweme/post/','aweme_list',0))

    assert result==[payload]
    assert visits==2
    assert '页面响应无法解析' in logs[0]
    assert any('有效数据已正常采集' in warning for warning in browser.warnings)
