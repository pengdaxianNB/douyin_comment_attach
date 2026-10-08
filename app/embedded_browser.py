"""QtWebEngine widgets used by the in-app, per-account browser workspace."""
from pathlib import Path
import threading

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton, QStackedLayout,
    QStackedWidget, QTabWidget, QVBoxLayout, QWidget)
from shiboken6 import delete as delete_qt_object, isValid


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

    def __init__(self, profile_dir, marker, parent=None):
        super().__init__(parent)
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

        self.view = BrowserView(self)
        self.activity_timer = QTimer(self)
        self.activity_timer.timeout.connect(self.keep_page_active)
        self.activity_timer.start(1000)
        self.address = QLineEdit()
        self.address.setPlaceholderText('输入网址后回车')
        self.address.returnPressed.connect(self.navigate_address)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        root.addLayout(toolbar)
        for label, callback, tip in (
                ('←', self.view.back, '后退'), ('→', self.view.forward, '前进'),
                ('↻', self.view.reload, '刷新'), ('首页', self.home, '打开抖音首页'),
                ('重启浏览器', lambda:self.restart_requested.emit(self.marker),
                 '页面无响应时重建当前账号的浏览器')):
            control = QPushButton(label)
            control.setToolTip(tip)
            control.setCursor(Qt.CursorShape.PointingHandCursor)
            control.clicked.connect(callback)
            toolbar.addWidget(control)
        toolbar.addWidget(self.address, 1)
        self.task_status = QLabel('账号任务状态：等待操作')
        self.task_status.setWordWrap(True)
        self.task_status.setToolTip('about:blank 可能是处理完成后释放内存的空白页，请结合任务状态判断运行情况。')
        root.addWidget(self.task_status)
        root.addWidget(self.view, 1)

        self.view.urlChanged.connect(lambda url: self.address.setText(url.toString()))
        self.view.titleChanged.connect(self.title_changed)
        self.reset_page()

    def reset_page(self):
        """重建当前 WebEngine 页面，保留 profile 中的登录数据。"""
        if self.closed:
            return
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
        self.view.setUrl(QUrl('https://www.douyin.com/'))

    def set_task_status(self, status):
        self.task_status.setText('账号任务状态：'+status)

    def navigate_address(self):
        self.view.setUrl(QUrl.fromUserInput(self.address.text().strip()))

    def navigate(self, url):
        self.view.setUrl(QUrl.fromUserInput(str(url)))

    def keep_page_active(self, *_):
        if self.closed:
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
        self.activity_timer.stop()
        unregister_page(self.marker)
        self.view.stop()
        old_page = self.view.page()
        self.view.setPage(QWebEnginePage(QWebEngineProfile.defaultProfile(),self.view))
        if isValid(old_page):
            delete_qt_object(old_page)
        QTimer.singleShot(0,self.profile.deleteLater)
