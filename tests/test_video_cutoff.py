import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from app.browser import (Browser,NeedsAttention,creator_search_videos,human_scroll,is_douyin_host,
                         reached_video_cutoff,search_result_videos)


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


def search_page(pages, end_after, hrefs=()):
    """Fake search page: every wheel turn delivers the next batch of results."""
    callback=None
    state={'wheels':0}
    anchors=[SimpleNamespace(get_attribute=AsyncMock(return_value=href)) for href in hrefs]
    frame=SimpleNamespace(locator=lambda selector:SimpleNamespace(all=AsyncMock(return_value=anchors)))
    marker=SimpleNamespace(is_visible=AsyncMock(side_effect=lambda:state['wheels']>=end_after))
    page=SimpleNamespace(frames=[frame],remove_listener=lambda *args:None,
                         get_by_text=lambda text:SimpleNamespace(first=marker))
    def deliver(index):
        if index<len(pages):
            callback(SimpleNamespace(url='https://www.douyin.com/aweme/v1/web/general/search/single/?keyword=x',
                                     json=AsyncMock(return_value=pages[index])))
    async def wheel(*args):
        state['wheels']+=1
        deliver(state['wheels'])
    page.mouse=SimpleNamespace(move=AsyncMock(),wheel=wheel)
    def on(event,fn):
        nonlocal callback
        callback=fn
    page.on=on
    async def goto(*args,**kwargs):
        deliver(0)
    page.goto=goto
    return page,state


def patch_scroll(monkeypatch, page):
    async def one_wheel(target):
        await target.mouse.wheel(0,1000)
    monkeypatch.setattr('app.browser.human_scroll',one_wheel)


def search_payload(*items):
    return {'data':[{'aweme_info':item} for item in items]}


def test_search_result_videos_reads_only_result_list_and_marks_notes():
    payload={'data':[
        {'aweme_info':{'aweme_id':'1234567890123456789','desc':'视频','create_time':1700000000,'author':{'nickname':'甲'}}},
        {'aweme_info':{'aweme_id':'2234567890123456789','desc':'笔记','aweme_type':68}},
        {'user_list':[{'aweme_id':'3234567890123456789','desc':'相关用户卡片'}]},
        {'aweme_info':{'aweme_id':'bad','desc':'非法ID'}},
    ],'user_list':[{'aweme_id':'4234567890123456789'}]}
    result={video['aweme_id']:video for video in search_result_videos(payload)}
    assert set(result)=={'1234567890123456789','2234567890123456789'}
    assert result['1234567890123456789']['author']=={'nickname':'甲'}
    assert result['2234567890123456789']['kind']=='note'
    assert search_result_videos(None)==[] and search_result_videos({'data':None})==[]


def test_search_videos_scrolls_until_no_more_marker(monkeypatch):
    pages=[search_payload({'aweme_id':'1234567890123456789','desc':'一'}),
           search_payload({'aweme_id':'2234567890123456789','desc':'二'}),
           search_payload({'aweme_id':'3234567890123456789','desc':'三','aweme_type':68})]
    page,state=search_page(pages,end_after=2,hrefs=['/video/5234567890123456789','https://www.douyin.com/note/6234567890123456789'])
    browser=Browser(SimpleNamespace(pages=[page]),{},page=page)
    browser.guard=AsyncMock()
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    patch_scroll(monkeypatch,page)
    result=asyncio.run(browser.search_videos(' 干细胞 '))
    assert state['wheels']==2
    ids={video['aweme_id']:video for video in result}
    assert set(ids)=={'1234567890123456789','2234567890123456789','3234567890123456789',
                      '5234567890123456789','6234567890123456789'}
    assert ids['3234567890123456789']['kind']=='note' and ids['6234567890123456789']['kind']=='note'
    assert not browser.warnings


def test_search_videos_url_and_incomplete_warning(monkeypatch):
    urls=[]
    page,state=search_page([search_payload({'aweme_id':'1234567890123456789','desc':'一'})],end_after=10**9)
    inner=page.goto
    async def goto(url,**kwargs):
        urls.append(url); await inner(url,**kwargs)
    page.goto=goto
    browser=Browser(SimpleNamespace(pages=[page]),{},page=page)
    browser.guard=AsyncMock()
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    patch_scroll(monkeypatch,page)
    result=asyncio.run(browser.search_videos('干细胞'))
    assert urls==['https://www.douyin.com/search/%E5%B9%B2%E7%BB%86%E8%83%9E']
    assert [video['aweme_id'] for video in result]==['1234567890123456789']
    assert '暂时没有更多了' in browser.warnings[0]


def test_search_videos_without_any_result_needs_attention(monkeypatch):
    page,_=search_page([],end_after=10**9)
    browser=Browser(SimpleNamespace(pages=[page]),{},page=page)
    browser.guard=AsyncMock()
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    patch_scroll(monkeypatch,page)
    with pytest.raises(NeedsAttention):
        asyncio.run(browser.search_videos('干细胞'))
    assert asyncio.run(browser.search_videos('  ')) == []


def test_human_scroll_varies_position_distance_and_timing(monkeypatch):
    import random
    sleeps=[]
    async def sleep(seconds): sleeps.append(seconds)
    monkeypatch.setattr('app.browser.asyncio.sleep',sleep)
    totals=set(); positions=set(); scrolled_back=False
    for seed in range(40):
        page=SimpleNamespace(viewport_size={'width':1000,'height':700},
                             mouse=SimpleNamespace(move=AsyncMock(),wheel=AsyncMock()))
        asyncio.run(human_scroll(page,random.Random(seed)))
        deltas=[call.args[1] for call in page.mouse.wheel.await_args_list]
        down=sum(d for d in deltas if d>0)
        assert 600<=down<=1800
        scrolled_back=scrolled_back or any(d<0 for d in deltas)
        totals.add(down)
        x,y=page.mouse.move.await_args.args
        assert 300<=x<=700 and 245<=y<=490
        assert page.mouse.move.await_args.kwargs['steps']>=4
        positions.add((round(x),round(y)))
    assert len(totals)>20 and len(positions)>20 and scrolled_back
    assert len({round(value,2) for value in sleeps})>50
    assert min(sleeps)>=.06
