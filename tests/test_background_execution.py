import asyncio
import os
from unittest.mock import AsyncMock,Mock

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS','--disable-gpu')

from PySide6.QtTest import QTest
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import QApplication,QTabWidget

from app.browser import Browser
from app.embedded_browser import EmbeddedBrowser
from main import configure_embedded_browser


def test_required_background_chromium_flags_are_added(monkeypatch):
    monkeypatch.setenv('QTWEBENGINE_REMOTE_DEBUGGING','127.0.0.1:12345')
    monkeypatch.setenv('QTWEBENGINE_CHROMIUM_FLAGS','--disable-gpu')
    configure_embedded_browser()
    flags=os.environ['QTWEBENGINE_CHROMIUM_FLAGS']
    for expected in ('--remote-allow-origins=*','--disable-background-timer-throttling',
                     '--disable-backgrounding-occluded-windows','--disable-renderer-backgrounding'):
        assert expected in flags


def test_cdp_background_lifecycle_and_focus_are_enabled():
    session=Mock(send=AsyncMock(),detach=AsyncMock())
    context=Mock(new_cdp_session=AsyncMock(return_value=session))
    browser=Browser(context,{},page=Mock())
    asyncio.run(browser.prepare_background_execution())
    calls=[(call.args[0],call.args[1]) for call in session.send.await_args_list]
    assert calls==[
        ('Page.setWebLifecycleState',{'state':'active'}),
        ('Emulation.setFocusEmulationEnabled',{'enabled':True}),
    ]
    session.detach.assert_awaited_once()


def test_hidden_account_tab_keeps_web_page_active(tmp_path):
    app=QApplication.instance() or QApplication([])
    tabs=QTabWidget();tabs.show()
    first=EmbeddedBrowser(tmp_path/'first','first')
    second=EmbeddedBrowser(tmp_path/'second','second')
    tabs.addTab(first,'first');tabs.addTab(second,'second')
    tabs.setCurrentWidget(second)
    QTest.qWait(1100);app.processEvents()
    assert not first.isVisible()
    assert first.view.page().isVisible()
    assert first.view.page().lifecycleState()==QWebEnginePage.LifecycleState.Active
    first.shutdown();second.shutdown();tabs.close();app.processEvents()
