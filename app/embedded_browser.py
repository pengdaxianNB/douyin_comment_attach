"""QtWebEngine widgets used by the in-app, per-account browser workspace."""
from pathlib import Path
import threading

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton, QStackedLayout,
    QStackedWidget, QTabWidget, QVBoxLayout, QWidget)
from shiboken6 import delete as delete_qt_object, isValid


from .constants import IDLE_URL


MARKER_PREFIX = 'panda-embedded-account:'
_PAGE_IDS = {}
_PAGE_IDS_LOCK = threading.Lock()


def register_page(marker, target_id):
    with _PAGE_IDS_LOCK:
        _PAGE_IDS[marker] = target_id


def unregister_page(marker):
    with _PAGE_IDS_LOCK:
        _PAGE_IDS.pop(marker,None)


def page_id(marker):
    with _PAGE_IDS_LOCK:
        return _PAGE_IDS.get(marker)


def keep_tab_pages_mounted(tabs):
    """Keep non-selected tab contents rendered underneath the current tab."""
    if not isinstance(tabs,QTabWidget):
        return False
    stack = tabs.findChild(QStackedWidget)
    layout = stack.layout() if stack is not None else None
    if not isinstance(layout,QStackedLayout):
        return False
    layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
    return True


class BrowserView(QWebEngineView):
    """Keep popup/new-window navigation inside the same embedded surface."""

    def createWindow(self, window_type):  # noqa: N802 - Qt virtual method name
        return self


class EmbeddedBrowser(QWidget):
    title_changed = Signal(str)
    restart_requested = Signal(str)
    renderer_failed = Signal(str,str)

    def __init__(self, profile_dir, marker, parent=None, activate=True):
        super().__init__(parent)
        self.setObjectName('embeddedBrowser')
        self.marker = marker
        self.closed = False
        profile_dir = Path(profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)

        # A named profile plus explicit paths keeps cookies/local storage isolated
        # for every account and stable between application restarts.
        self.profile = QWebEngineProfile('panda-'+marker, self)
        self.profile.setPersistentStoragePath(str(profile_dir/'storage'))
        self.profile.setCachePath(str(profile_dir/'cache'))
        # The default is selected by Chromium from available system memory and
        # is multiplied by every account profile. Use an explicit per-account
        # ceiling so many sender accounts cannot grow their caches without a
        # bound.
        self.profile.setHttpCacheMaximumSize(128 * 1024 * 1024)
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        # Do not hand custom app links such as bitbrowser:// to Windows. When no
        # matching desktop application is installed, Windows otherwise opens a
        # disruptive "find an app in Microsoft Store" dialog.
        self.profile.settings().setUnknownUrlSchemePolicy(
            QWebEngineSettings.UnknownUrlSchemePolicy.DisallowUnknownUrlSchemes)

        self.view = None
        self.activity_timer = QTimer(self)
        self.activity_timer.timeout.connect(self.keep_page_active)
        self.address = QLineEdit()
        self.address.setPlaceholderText('输入网址后回车')
        self.address.returnPressed.connect(self.navigate_address)

        root = QVBoxLayout(self)
        self.root = root
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        root.addLayout(toolbar)
        self.page_controls = []
        for label, callback, tip in (
                ('←', lambda:self.view.back() if self.view else None, '后退'),
                ('→', lambda:self.view.forward() if self.view else None, '前进'),
                ('↻', lambda:self.view.reload() if self.view else None, '刷新'),
                ('首页', self.home, '打开抖音首页'),
                ('重启浏览器', lambda:self.restart_requested.emit(self.marker),
                 '页面无响应时重建当前账号的浏览器')):
            control = QPushButton(label)
            control.setToolTip(tip)
            control.setCursor(Qt.CursorShape.PointingHandCursor)
            control.clicked.connect(callback)
            toolbar.addWidget(control)
            if label != '重启浏览器':
                self.page_controls.append(control)
        toolbar.addWidget(self.address, 1)
        self.task_status = QLabel('账号任务状态：等待操作')
        self.task_status.setWordWrap(True)
        self.task_status.setToolTip('搜索、评论采集和私信任务空闲时保留账号页面并停留个人页。请结合任务状态判断。')
        root.addWidget(self.task_status)
        self.sleep_hint = QLabel('账号页面已休眠，轮到此账号时自动加载。登录资料保留。')
        self.sleep_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.sleep_hint,1)
        self.set_navigation_enabled(False)
        if activate:
            self.reset_page()

    def park_page(self):
        """Retain the renderer and login profile while waiting for work."""
        self.resume_page()
        if self.view is not None and self.view.url().toString() != IDLE_URL:
            self.navigate(IDLE_URL)

    def suspend_page(self):
        """Destroy the renderer instead of retaining an active blank page."""
        self.activity_timer.stop()
        unregister_page(self.marker)
        if self.view is not None:
            view,self.view = self.view,None
            view.stop()
            page = view.page()
            self.root.removeWidget(view)
            delete_qt_object(view)
            if isValid(page):
                delete_qt_object(page)
        self.address.clear()
        self.set_navigation_enabled(False)
        self.sleep_hint.show()

    def set_navigation_enabled(self, enabled):
        self.address.setEnabled(enabled)
        for control in self.page_controls:
            control.setEnabled(enabled)

    def resume_page(self):
        if self.view is None:
            self.reset_page()

    def reset_page(self):
        """重建当前 WebEngine 页面，保留 profile 中的登录数据。"""
        if self.closed:
            return
        if self.view is None:
            self.view = BrowserView(self)
            self.root.addWidget(self.view,1)
            self.view.urlChanged.connect(lambda url: self.address.setText(url.toString()))
            self.view.titleChanged.connect(self.title_changed)
        self.sleep_hint.hide()
        self.set_navigation_enabled(True)
        self.activity_timer.start(1000)
        old_page = self.view.page()
        unregister_page(self.marker)
        page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(page)
        # QTabWidget hides every non-selected account view.  Qt normally
        # propagates that state to Chromium, which may freeze timers/rendering
        # until the operator selects that account tab.  Automation must keep
        # running independently of which tab the operator is viewing.
        page.lifecycleStateChanged.connect(self.keep_page_active)
        page.recommendedStateChanged.connect(self.keep_page_active)
        page.visibleChanged.connect(self.keep_page_active)
        page.renderProcessTerminated.connect(self.renderer_terminated)
        self.keep_page_active()
        # Qt exposes the underlying CDP target id. Registering that immutable id
        # avoids relying on the page title/URL, both of which change as users
        # and automation navigate between pages.
        register_page(self.marker,page.devToolsId())
        # Playwright discovers the right Qt page by this private, unique title.
        # It navigates to Douyin only after the page has been attached.
        self.view.setHtml(
            '<!doctype html><meta charset="utf-8"><title>'+MARKER_PREFIX+self.marker+'</title>'
            '<style>body{background:#111827;color:#dbe5f3;font:16px sans-serif;display:grid;'
            'place-items:center;height:90vh}p{opacity:.75}</style>'
            '<p>内嵌浏览器已准备，正在连接账号页面…</p>', QUrl('about:blank'))
        if old_page is not None and old_page is not page:
            # QWebEngineView may immediately destroy its original implicit page
            # when setPage() replaces it, while explicitly installed pages stay
            # alive. Destroy the latter immediately so its hung renderer and
            # profile reference cannot survive the reset.
            if isValid(old_page):
                delete_qt_object(old_page)

    def home(self):
        if self.view is not None:
            self.view.setUrl(QUrl('https://www.douyin.com/'))

    def renderer_terminated(self, status, exit_code):
        if self.closed or status == QWebEnginePage.RenderProcessTerminationStatus.NormalTerminationStatus:
            return
        reason = f'浏览器渲染进程退出（{status.name}，退出码 {exit_code}）'
        self.set_task_status(reason+'；运行任务将按异常流程处理，空闲时可点击重启浏览器')
        self.renderer_failed.emit(self.marker,reason)

    def set_task_status(self, status):
        self.task_status.setText('账号任务状态：'+status)

    def navigate_address(self):
        if self.view is not None:
            self.view.setUrl(QUrl.fromUserInput(self.address.text().strip()))

    def navigate(self, url):
        self.resume_page()
        self.view.setUrl(QUrl.fromUserInput(str(url)))

    def keep_page_active(self, *_):
        if self.closed or self.view is None:
            return
        page = self.view.page()
        if page.lifecycleState()!=QWebEnginePage.LifecycleState.Active:
            page.setLifecycleState(QWebEnginePage.LifecycleState.Active)
        if not page.isVisible():
            page.setVisible(True)

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        self.suspend_page()
        self.profile.deleteLater()
