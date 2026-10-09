"""Search-card regressions in a real browser with all network traffic mocked."""
import asyncio
import json
import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from app.browser import Browser


@pytest.mark.parametrize('source',['nested_api','modal_links','chunked_stream'])
def test_advice_above_search_works_does_not_trigger_loading_recovery(monkeypatch,source):
    monkeypatch.setattr('app.browser.SEARCH_READY_TIMEOUT',5)
    video_id='1234567890123456789'
    note_id='2234567890123456789'
    later_id='3234567890123456789'
    advice={'data':[{'data':{'title':'专家建议','content':'内容来自小荷医典'}}]}
    initial=(
        {'data':advice['data']+[{'data':json.dumps({'sub_card_list':[
            {'aweme_info':{'aweme_id':video_id,'desc':'视频'}},
            {'aweme_info':{'aweme_id':note_id,'desc':'图文','aweme_type':68}}]})}]}
        if source=='nested_api' else advice)
    stream_body=None
    if source=='chunked_stream':
        # Captured from the provided medical query: three JSON records framed
        # with hexadecimal UTF-8 BYTE lengths despite application/json MIME.
        packets=[{'status_code':0,'data':[{'type':83,'common_aladdin':{
                     'display':json.dumps({'title':'专家建议','text':'内容来自小荷医典'},ensure_ascii=False)}}]},
                 {'time_cost':{'1st_screen_state':'hit_predict'},'ack':1},
                 {'status_code':0,'data':[
                     {'type':1,'aweme_info':{'aweme_id':video_id,'desc':'视频'}},
                     {'type':1,'aweme_info':{'aweme_id':note_id,'desc':'图文','aweme_type':68}}]}]
        chunks=[json.dumps(packet,ensure_ascii=False).encode() for packet in packets]
        stream_body=b''.join(f'{len(chunk):x}\r\n'.encode()+chunk+b'\r\n' for chunk in chunks)+b'0\r\n\r\n'
    html=f"""
        <meta charset="utf-8">
        <section style="height:1200px">专家建议：内容来自小荷医典</section>
        <main>
          <a href="/jingxuan?modal_id={video_id}">视频</a>
          <a href="/note/{note_id}">图文</a>
          <a href="https://example.com/?modal_id=9234567890123456789">无关链接</a>
        </main>
        <script>fetch('/aweme/v1/web/general/search/single/?keyword=测试')</script>
    """
    # The API case has no work links; the DOM case has no API work records.
    if source in ('nested_api','chunked_stream'):
        html=html[:html.index('<main>')]+html[html.index('<script>'):]
    if source=='chunked_stream':
        html=html.replace('search/single/','search/stream/')

    async def run():
        async with async_playwright() as pw:
            executable=os.environ.get('PLAYWRIGHT_TEST_EXECUTABLE',pw.chromium.executable_path)
            if not Path(executable).exists():
                pytest.skip('Playwright Chromium is not installed')
            chromium=await pw.chromium.launch(executable_path=executable)
            try:
                page=await chromium.new_page()
                requests=[]
                async def route(request):
                    url=request.request.url
                    requests.append(url)
                    if '/aweme/v1/web/' in url:
                        if '/search/stream/' in url:
                            await request.fulfill(body=stream_body,content_type='application/json')
                            return
                        payload=(initial if 'page=2' not in url else
                                 {'business_data':[{'data':{'aweme_list':[
                                     {'aweme_id':later_id,'desc':'下一页'}]}}]})
                        await request.fulfill(json=payload)
                    else:
                        await request.fulfill(body=html,content_type='text/html')
                await page.route('**/*',route)
                scrolls=[]
                async def scroll(target):
                    scrolls.append(True)
                    await target.evaluate("""async () => {
                        await fetch('/aweme/v1/web/general/search/single/?page=2');
                        const marker=document.createElement('p');
                        marker.textContent='暂时没有更多了';document.body.prepend(marker);
                    }""")
                monkeypatch.setattr('app.browser.human_scroll',scroll)
                browser=Browser(page.context,{},page=page)

                result=await browser.search_videos('干细胞卵巢早衰')

                assert {item['aweme_id'] for item in result}=={video_id,note_id,later_id}
                assert next(item for item in result if item['aweme_id']==note_id)['kind']=='note'
                assert scrolls==[True] and not browser.warnings
                assert len(requests)==3, 'Only the search page and its two API batches are visited'
            finally:
                await chromium.close()
    asyncio.run(run())
