import asyncio
import os
from unittest.mock import AsyncMock,Mock

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS','--disable-gpu')

from PySide6.QtTest import QTest
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import QApplication,QTabWidget,QVBoxLayout,QWidget

from app.browser import Browser
from app.embedded_browser import EmbeddedBrowser,keep_tab_pages_mounted
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


def test_unselected_account_stays_mounted_behind_other_app_tabs(tmp_path):
    app=QApplication.instance() or QApplication([])
    outer=QTabWidget();keep_tab_pages_mounted(outer)
    browser_page=QWidget();browser_layout=QVBoxLayout(browser_page)
    tabs=QTabWidget();keep_tab_pages_mounted(tabs);browser_layout.addWidget(tabs)
    outer.addTab(browser_page,'browsers');outer.addTab(QWidget(),'logs');outer.show()
    first=EmbeddedBrowser(tmp_path/'first','first')
    second=EmbeddedBrowser(tmp_path/'second','second')
    tabs.addTab(first,'first');tabs.addTab(second,'second')
    tabs.setCurrentWidget(second)
    outer.setCurrentIndex(1)
    QTest.qWait(1100);app.processEvents()
    assert first.isVisible()
    assert second.isVisible()
    assert browser_page.isVisible()
    assert first.view.page().isVisible()
    assert first.view.page().lifecycleState()==QWebEnginePage.LifecycleState.Active
    first.shutdown();second.shutdown();outer.close();app.processEvents()
