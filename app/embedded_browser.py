"""QtWebEngine widgets used by the in-app, per-account browser workspace."""
from pathlib import Path
import threading

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget


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


class BrowserView(QWebEngineView):
    """Keep popup/new-window navigation inside the same embedded surface."""

    def createWindow(self, window_type):  # noqa: N802 - Qt virtual method name
        return self


class EmbeddedBrowser(QWidget):
    title_changed = Signal(str)

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
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)

        self.view = BrowserView(self)
        self.view.setPage(QWebEnginePage(self.profile, self.view))
        # Qt exposes the underlying CDP target id. Registering that immutable id
        # avoids relying on the page title/URL, both of which change as users
        # and automation navigate between pages.
        register_page(marker,self.view.page().devToolsId())
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
                ('↻', self.view.reload, '刷新'), ('首页', self.home, '打开抖音首页')):
            control = QPushButton(label)
            control.setToolTip(tip)
            control.setCursor(Qt.CursorShape.PointingHandCursor)
            control.clicked.connect(callback)
            toolbar.addWidget(control)
        toolbar.addWidget(self.address, 1)
        root.addWidget(self.view, 1)

        self.view.urlChanged.connect(lambda url: self.address.setText(url.toString()))
        self.view.titleChanged.connect(self.title_changed)
        # Playwright discovers the right Qt page by this private, unique title.
        # It navigates to Douyin only after the page has been attached.
        self.view.setHtml(
            '<!doctype html><meta charset="utf-8"><title>'+MARKER_PREFIX+marker+'</title>'
            '<style>body{background:#111827;color:#dbe5f3;font:16px sans-serif;display:grid;'
            'place-items:center;height:90vh}p{opacity:.75}</style>'
            '<p>内嵌浏览器已准备，正在连接账号页面…</p>', QUrl('about:blank'))

    def home(self):
        self.view.setUrl(QUrl('https://www.douyin.com/'))

    def navigate_address(self):
        self.view.setUrl(QUrl.fromUserInput(self.address.text().strip()))

    def navigate(self, url):
        self.view.setUrl(QUrl.fromUserInput(str(url)))

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        unregister_page(self.marker)
        self.view.stop()
        old_page = self.view.page()
        self.view.setPage(QWebEnginePage(QWebEngineProfile.defaultProfile(),self.view))
        old_page.deleteLater()
        QTimer.singleShot(0,self.profile.deleteLater)
