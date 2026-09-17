"""Real browser regressions using local HTML; never contact real recipients."""
import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from app.browser import Browser, NeedsAttention, close_open_message_panel


HTML = """
<meta charset="utf-8">
<style>
body {margin:0} nav {height:70px;display:flex;justify-content:flex-end}
#drawer {position:fixed;right:20px;top:100px;width:500px;height:450px;background:#ddd}
header {height:70px;display:flex;align-items:center;padding:0 24px;gap:22px}
.titlewrap {width:300px;height:50px;display:flex;align-items:center;margin-right:auto}
.icon {width:24px;height:24px;cursor:pointer;flex-shrink:0;padding:0;border:0}
#editor {width:300px;height:100px} #chat {display:none}
</style>
<nav><span>消息</span></nav>
<button data-e2e="user-info-follow-btn">已关注</button>
<button id="message">私信</button>
<div id="drawer"><header><div class="titlewrap"><span>消息</span></div>
<svg id="popout" class="icon"><rect width="24" height="24"/></svg>
CLOSE_CONTROL
</header><input placeholder="搜索"></div>
<div id="chat"><div id="editor" contenteditable="true" class="messageEditorinputArea"></div>
<button class="e2e-send-msg-btn" id="send">发送</button></div>
<script>
window.events=[];window.opens=0;
document.querySelector('#close').addEventListener('click', e=>{
  if(!e.isTrusted)throw new Error('Synthetic clicks do not close this fixture');
  events.push('close');document.querySelector('#drawer').style.display='none';
});
document.querySelector('#popout').onclick=()=>events.push('popout');
document.querySelector('#message').onclick=()=>{
  opens++;events.push('open');
  document.querySelector(opens===1?'#drawer':'#chat').style.display='block';
};
document.querySelector('#send').onclick=()=>events.push('send');
</script>
"""


@pytest.mark.parametrize('control', [
    '<svg id="close" class="icon"><rect width="24" height="24"/></svg>',
    '<button id="close" class="icon"><svg width="24" height="24"><rect width="24" height="24"/></svg></button>',
    '<span id="close" class="icon">×</span>',
])
def test_real_close_then_reopen_chat_without_reloading(control):
    async def run():
        async with async_playwright() as pw:
            executable = os.environ.get('PLAYWRIGHT_TEST_EXECUTABLE',pw.chromium.executable_path)
            if not Path(executable).exists():
                pytest.skip('Playwright Chromium is not installed')
            chromium = await pw.chromium.launch(executable_path=executable)
            try:
                page = await chromium.new_page(viewport={'width':1400,'height':850})
                requests=[]
                async def route(request):
                    requests.append(request.request.url)
                    await request.fulfill(body=HTML.replace('CLOSE_CONTROL',control),content_type='text/html')
                await page.route('**/*',route)
                browser = Browser(page.context,{'message_editor':'[contenteditable=true]'},page=page)
                customer = {'url':'https://www.douyin.com/user/MS4testcustomer'}
                await browser.check_reachable(customer)
                assert await page.evaluate('events')==['close']
                submitted=[]
                await browser.send(customer,'测试消息',lambda:submitted.append(True))
                assert len(requests)==1, 'Reachability and sending must share the profile'
                assert submitted==[True]
                assert await page.evaluate('events')==['close','open','close','open','send']
            finally:
                await chromium.close()
    asyncio.run(run())


def test_profile_slow_load_is_polled_without_repeated_navigation(monkeypatch):
    from unittest.mock import Mock
    page=Mock(url='about:blank',goto=AsyncMock())
    browser=Browser(page.context,{},page=page)
    browser.guard=AsyncMock()
    browser.private_account_reason=AsyncMock(return_value=None)
    monkeypatch.setattr('app.browser.close_open_message_panel',AsyncMock(return_value=False))
    monkeypatch.setattr('app.browser.profile_follow_matches',AsyncMock(side_effect=[[],[],[(Mock(),'已关注')]]))
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    asyncio.run(browser.check_reachable({'url':'https://www.douyin.com/user/MS4testcustomer'}))
    assert page.goto.await_count==1


def test_unclosed_drawer_is_not_reported_as_success(monkeypatch):
    from unittest.mock import Mock
    title=Mock(is_visible=AsyncMock(return_value=True),
               evaluate=AsyncMock(return_value={'panel':True,'point':{'x':100,'y':100}}))
    page=Mock()
    page.get_by_text.return_value.all=AsyncMock(return_value=[title])
    page.mouse.click=AsyncMock()
    monkeypatch.setattr('app.browser.asyncio.sleep',AsyncMock())
    with pytest.raises(NeedsAttention,match='侧栏未关闭'):
        asyncio.run(close_open_message_panel(page,AsyncMock(),lambda _:None))
