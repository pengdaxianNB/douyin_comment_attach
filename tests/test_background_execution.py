import asyncio
import os
from unittest.mock import AsyncMock,Mock

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS','--disable-gpu')

from PySide6.QtTest import QTest
from PySide6.QtWebEngineCore import QWebEnginePage,QWebEngineSettings
from PySide6.QtWidgets import QApplication,QTabWidget,QVBoxLayout,QWidget

from app.browser import Browser
from app.embedded_browser import EmbeddedBrowser,keep_tab_pages_mounted,page_id
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


def test_completed_page_stays_on_personal_page_without_heap_collection():
    from app.constants import IDLE_URL
    page=Mock(url='https://www.douyin.com/video/123',goto=AsyncMock())
    context=Mock(new_cdp_session=AsyncMock())
    browser=Browser(context,{},page=page)
    asyncio.run(browser.go_idle())
    page.goto.assert_awaited_once_with(IDLE_URL,wait_until='commit',timeout=10000)
    context.new_cdp_session.assert_not_awaited()
    page.url=IDLE_URL
    asyncio.run(browser.go_idle())
    assert page.goto.await_count==1


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


def test_embedded_browser_blocks_unknown_external_url_schemes(tmp_path):
    app=QApplication.instance() or QApplication([])
    browser=EmbeddedBrowser(tmp_path/'profile','account')
    assert browser.profile.settings().unknownUrlSchemePolicy()==(
        QWebEngineSettings.UnknownUrlSchemePolicy.DisallowUnknownUrlSchemes)
    assert browser.profile.httpCacheMaximumSize()==128*1024*1024
    browser.shutdown();app.processEvents()


def test_embedded_browser_can_rebuild_one_page_and_keep_profile(tmp_path):
    app=QApplication.instance() or QApplication([])
    browser=EmbeddedBrowser(tmp_path/'profile','account')
    profile=browser.profile
    old_page=browser.view.page()
    old_target=page_id('account')

    browser.reset_page()

    assert browser.profile is profile
    assert browser.view.page() is not old_page
    assert page_id('account')==browser.view.page().devToolsId()
    assert page_id('account')!=old_target
    browser.shutdown();app.processEvents()


def test_sleeping_account_has_no_view_and_resume_keeps_profile(tmp_path):
    from shiboken6 import isValid
    app=QApplication.instance() or QApplication([])
    browser=EmbeddedBrowser(tmp_path/'profile','sleep-test',activate=False)
    profile=browser.profile
    assert browser.view is None and page_id('sleep-test') is None
    assert not browser.activity_timer.isActive()
    browser.resume_page()
    view=browser.view; page=view.page(); first_target=page_id('sleep-test')
    browser.suspend_page()
    assert browser.view is None and page_id('sleep-test') is None
    assert not isValid(view) and not isValid(page)
    browser.keep_page_active()
    assert browser.view is None and not browser.activity_timer.isActive()
    browser.resume_page()
    assert browser.profile is profile
    assert page_id('sleep-test') != first_target
    browser.show();app.processEvents()
    browser.keep_page_active()
    assert browser.view.page().isVisible()
    browser.shutdown();app.processEvents()


def test_renderer_crash_reports_account_and_preserves_profile(tmp_path):
    from PySide6.QtTest import QSignalSpy
    app=QApplication.instance() or QApplication([])
    browser=EmbeddedBrowser(tmp_path/'profile','crash-test',activate=False)
    profile=browser.profile
    spy=QSignalSpy(browser.renderer_failed)
    browser.renderer_terminated(QWebEnginePage.RenderProcessTerminationStatus.NormalTerminationStatus,0)
    assert spy.count()==0
    browser.renderer_terminated(QWebEnginePage.RenderProcessTerminationStatus.CrashedTerminationStatus,11)
    assert spy.count()==1 and spy.at(0)[0]=='crash-test'
    assert '退出码 11' in spy.at(0)[1]
    assert browser.profile is profile and not browser.closed
    browser.shutdown();app.processEvents()
