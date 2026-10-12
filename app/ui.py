import csv
import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
import keyring
from PySide6.QtCore import Qt, QTimer, QItemSelectionModel
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,QPushButton,QTabWidget,QTableWidget,QTableWidgetItem,QHeaderView,QLineEdit,QPlainTextEdit,QSpinBox,QFormLayout,QMessageBox,QFileDialog,QInputDialog,QComboBox,QGroupBox,QAbstractItemView,QScrollArea,QDialog,QCheckBox,QSizePolicy)
from .ai import SERVICE
from .db import user_id
from .engine import Engine, DEFAULTS
from .embedded_browser import EmbeddedBrowser,keep_tab_pages_mounted
from .keyword_csv import read_keyword_csv,write_keyword_template

STATUS = dict(pending='等待私信',processing='处理中',sent='已私信',failed='失败',skipped='已跳过')
VIDEO_STATUS = dict(pending='等待扫描',scanning='扫描中',done='已完成',failed='失败')
BEIJING_TIMEZONE = timezone(timedelta(hours=8))


def format_beijing_time(value):
    """将带时区的评论时间转为工整的北京时间。

    导入数据可能是不带时区的本地时间，这种情况只统一展示格式，
    不做时差推断，避免将用户导入的北京时间重复加 8 小时。
    """
    text = str(value or '').strip()
    if not text:
        return ''
    if len(text) == 10:
        try:
            return datetime.strptime(text, '%Y-%m-%d').strftime('%Y-%m-%d')
        except ValueError:
            return text
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(BEIJING_TIMEZONE)
    return parsed.strftime('%Y-%m-%d %H:%M:%S')


def button(text, callback):
    b = QPushButton(text)
    if text in ('开启持续任务','保存设置','新增博主','打开客户主页'):
        b.setProperty('variant','primary')
    elif text in ('备份并清空','删除账号','删除博主'):
        b.setProperty('variant','danger')
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.clicked.connect(callback)
    return b


def table(headers):
    t = QTableWidget(0,len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    t.horizontalHeader().setStretchLastSection(True)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.verticalHeader().hide()
    t.verticalHeader().setDefaultSectionSize(48)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    t.setShowGrid(False)
    t.setWordWrap(False)
    return t


class NoWheelSpinBox(QSpinBox):
    # Ignore the wheel so scrolling the settings page never changes a value by accident.
    def __init__(self):
        super().__init__(); self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    def wheelEvent(self, event): event.ignore()


class NoWheelComboBox(QComboBox):
    def __init__(self):
        super().__init__(); self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    def wheelEvent(self, event): event.ignore()


def populate(t, rows):
    t.setRowCount(len(rows))
    for i,row in enumerate(rows):
        for j,val in enumerate(row):
            item = QTableWidgetItem(str(val or ''))
            item.setToolTip(str(val or ''))
            t.setItem(i,j,item)


class BloggerDialog(QDialog):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle('新增监控博主')
        self.resize(640,240)
        layout = QVBoxLayout(self); layout.setSpacing(14)
        form = QFormLayout(); layout.addLayout(form)
        self.url = QLineEdit(); self.url.setPlaceholderText('https://www.douyin.com/user/MS4…')
        self.name = QLineEdit(); self.name.setPlaceholderText('选填，留空时使用主页 ID 作为备注')
        form.addRow('博主主页链接',self.url); form.addRow('备注名称（选填）',self.name)
        self.error = QLabel(); self.error.setWordWrap(True); self.error.setStyleSheet('color:#f48b96;'); layout.addWidget(self.error)
        actions = QHBoxLayout(); actions.addStretch(); layout.addLayout(actions)
        cancel = button('取消',self.reject); cancel.setAutoDefault(False); actions.addWidget(cancel)
        save = button('保存博主',self.save); save.setDefault(True); actions.addWidget(save)
        self.url.setFocus()

    def save(self):
        try:
            uid = user_id(self.url.text())
            name = self.name.text().strip() or uid
            with self.db.connect() as c:
                inserted = c.execute('INSERT OR IGNORE INTO bloggers(id,name) VALUES (?,?)',(uid,name)).rowcount
            if not inserted:
                self.error.setText('该博主已在监控列表中，无需重复添加。')
                return
        except Exception as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class Window(QMainWindow):
    def __init__(self, db, data, start_engine=True):
        super().__init__()
        self.db,self.data = db,Path(data)
        self.states = {}
        self.setWindowTitle('熊猫领航 · 评论客户工作台')
        self.resize(1320,860)
        self.engine = Engine(db,data)
        self.engine.log.connect(self.add_log)
        self.engine.status.connect(self.account_status)
        # AI classification can finish several comments at once. Coalesce its
        # change burst instead of rebuilding thousands of QTableWidgetItems for
        # every individual result queued on the GUI thread.
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(300)
        self.refresh_timer.timeout.connect(self.refresh_runtime_views)
        self.engine.changed.connect(self.schedule_runtime_refresh)
        self.engine.browser_reset.connect(self.reset_browser_page)
        self.engine.browser_state.connect(self.change_browser_state)
        root = QWidget(); self.setCentralWidget(root)
        layout = QHBoxLayout(root); layout.setContentsMargins(8,8,8,4); layout.setSpacing(8)
        self.sidebar = QScrollArea(); self.sidebar.setObjectName('sidebar'); self.sidebar.setFixedWidth(236)
        self.sidebar.setWidgetResizable(True); self.sidebar.setFrameShape(QScrollArea.Shape.NoFrame)
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        panel = QWidget(); panel.setObjectName('sidebarPanel'); self.sidebar.setWidget(panel)
        side = QVBoxLayout(panel); side.setContentsMargins(12,14,12,14); side.setSpacing(14)
        layout.addWidget(self.sidebar)
        brand = QVBoxLayout(); brand.setSpacing(6); side.addLayout(brand)
        eyebrow = QLabel('熊猫领航  /  客户运营'); eyebrow.setObjectName('eyebrow'); brand.addWidget(eyebrow)
        title = QLabel('评论客户工作台'); title.setObjectName('sidebarTitle'); brand.addWidget(title)
        subtitle = QLabel('发现需求 · 语义筛选 · 个性化触达'); subtitle.setObjectName('muted'); brand.addWidget(subtitle)
        self.navigation = QVBoxLayout(); self.navigation.setSpacing(6); side.addLayout(self.navigation)
        self.navigation_buttons = []
        dashboard_title = QLabel('客户仪表盘'); dashboard_title.setObjectName('sectionTitle'); side.addWidget(dashboard_title)
        metrics_row = QGridLayout(); metrics_row.setSpacing(8); side.addLayout(metrics_row)
        self.metric_values = {}
        for index,(key,label) in enumerate([('total','客户总数'),*STATUS.items()]):
            card = QWidget(); card.setObjectName('statCard')
            card_layout = QVBoxLayout(card); card_layout.setContentsMargins(10,10,10,10); card_layout.setSpacing(4)
            caption = QLabel(label); caption.setObjectName('muted'); card_layout.addWidget(caption)
            value = QLabel('0'); value.setObjectName('statValue'); value.setProperty('status',key); card_layout.addWidget(value)
            self.metric_values[key] = value; metrics_row.addWidget(card,index//2,index%2)
        side.addStretch()
        main = QVBoxLayout(); main.setSpacing(6); layout.addLayout(main,1)
        toolbar = QHBoxLayout(); toolbar.setSpacing(10); main.addLayout(toolbar)
        self.sidebar_toggle = button('隐藏左栏',self.toggle_sidebar); self.sidebar_toggle.setObjectName('workspaceControl'); toolbar.addWidget(self.sidebar_toggle)
        self.workspace_title = QLabel(); self.workspace_title.setObjectName('sectionTitle'); toolbar.addWidget(self.workspace_title)
        toolbar.addStretch()
        self.browser_focus = button('浏览器铺满',self.focus_browser); self.browser_focus.setObjectName('workspaceControl'); toolbar.addWidget(self.browser_focus)
        self.tabs = QTabWidget(); self.tabs.setObjectName('workspaceTabs'); keep_tab_pages_mounted(self.tabs)
        self.tabs.tabBar().hide(); self.tabs.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Ignored)
        main.addWidget(self.tabs,1)
        self.browser_widgets = {}
        self.build_customers(); self.build_accounts(); self.build_bloggers(); self.build_browser_workspace(); self.build_settings()
        self.logs = QPlainTextEdit(); self.logs.setReadOnly(True); self.logs.setMaximumBlockCount(1000)
        self.tabs.addTab(self.logs,'运行日志')
        self.build_database()
        self.tabs.currentChanged.connect(lambda: self.refresh_database() if self.tabs.currentWidget() is self.database_page else None)
        self.build_navigation()
        self.tabs.currentChanged.connect(self.sync_navigation)
        self.tabs.setCurrentWidget(self.browser_page)
        self.sync_navigation()
        self.statusBar().showMessage('站点适配待实号验证 · 登录和验证码请在内嵌浏览器中处理')
        self.refresh_accounts(); self.refresh_bloggers(); self.refresh_keywords(); self.refresh_search_keywords(); self.refresh_videos(); self.refresh_customers()
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh_runtime_views); self.timer.start(4000)
        if start_engine:
            self.engine.start()

    def build_navigation(self):
        for index in range(self.tabs.count()):
            control = button(self.tabs.tabText(index),lambda checked=False,index=index:self.tabs.setCurrentIndex(index))
            control.setObjectName('navigationButton'); control.setCheckable(True); control.setAutoExclusive(True)
            self.navigation.addWidget(control); self.navigation_buttons.append(control)

    def sync_navigation(self,*args):
        current = self.tabs.currentIndex()
        for index,control in enumerate(self.navigation_buttons):
            control.setChecked(index==current)
        self.workspace_title.setText(self.tabs.tabText(current))

    def toggle_sidebar(self):
        self.sidebar.setVisible(self.sidebar.isHidden())
        self.sidebar_toggle.setText('显示左栏' if self.sidebar.isHidden() else '隐藏左栏')

    def focus_browser(self):
        self.tabs.setCurrentWidget(self.browser_page)
        self.sidebar.hide()
        self.sidebar_toggle.setText('显示左栏')

    def page(self,name):
        widget = QWidget(); widget.setObjectName('page'); layout = QVBoxLayout(widget); layout.setContentsMargins(22,22,22,20); layout.setSpacing(16)
        if name == 'AI 与任务设置':
            scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.Shape.NoFrame); scroll.setWidget(widget)
            self.tabs.addTab(scroll,name)
        else:
            self.tabs.addTab(widget,name)
        return layout

    def build_customers(self):
        l = self.page('客户工作台')
        section = QLabel('客户列表'); section.setObjectName('sectionTitle'); l.addWidget(section)
        bar = QHBoxLayout(); bar.setSpacing(10); l.addLayout(bar)
        self.search = QLineEdit(); self.search.setPlaceholderText('搜索昵称、评论或博主…'); self.search.setMinimumWidth(220); self.search.textChanged.connect(self.refresh_customers); bar.addWidget(self.search)
        self.filter = QComboBox(); self.filter.setMinimumWidth(140); self.filter.addItem('全部状态','')
        for k,v in STATUS.items(): self.filter.addItem(v,k)
        self.filter.currentIndexChanged.connect(self.refresh_customers); bar.addWidget(self.filter)
        for label,fn in [('导入',self.import_data),('导出',self.export_data),('备份并清空',self.clear_data)]: bar.addWidget(button(label,fn))
        self.customers = table(['昵称','状态','评论内容','评论时间','视频名称','博主','主页 ID','发送账号','私信内容','异常说明'])
        for col,width in enumerate([130,110,300,170,190,130,230,130,270,260]): self.customers.setColumnWidth(col,width)
        self.customers.itemDoubleClicked.connect(self.open_customer)
        l.addWidget(self.customers)
        bar = QHBoxLayout(); l.addLayout(bar)
        for label,fn in [('打开客户主页',self.open_customer),('标记已私信',lambda:self.mark('sent')),('重新排队',lambda:self.mark('pending')),('全部失败重新排队',self.requeue_failed),('人工标记跳过',lambda:self.mark('skipped'))]: bar.addWidget(button(label,fn))
        bar.addStretch()
        hint = QLabel('双击查看客户主页  ·  发送异常不会自动重发  ·  列表显示最近 1,000 条，导出包含全部数据'); hint.setObjectName('muted'); hint.setWordWrap(True); l.addWidget(hint)

    def build_accounts(self):
        l = self.page('账号集群')
        hint = QLabel('所有账号使用独立浏览器和登录目录。集群同时只能运行一种模式；采集账号数量在设置中调整，私信可开启全部账号或仅开启选中账号。')
        hint.setWordWrap(True); l.addWidget(hint)
        controls = QHBoxLayout(); l.addLayout(controls)
        self.mode_switches = {}
        for mode,title,description in [('comments','找评论模式','分配视频来源和评论扫描，AI 筛选目标客户'),('messages','私信模式','使用全部账号或选中账号，按各自间隔和每日上限轮流发送，等待时释放页面')]:
            group = QGroupBox(title); box = QVBoxLayout(group)
            box.addWidget(QLabel(description))
            switch = QPushButton('开启'); switch.setCheckable(True); switch.setCursor(Qt.CursorShape.PointingHandCursor)
            switch.clicked.connect(lambda checked,mode=mode:self.toggle_cluster(mode,checked))
            box.addWidget(switch); controls.addWidget(group)
            self.mode_switches[mode] = switch
            if mode=='messages':
                self.single_message_button = button('开启选中账号私信',self.start_selected_messages)
                box.addWidget(self.single_message_button)
        self.accounts = table(['备注名','发送间隔（秒）','今日已私信 / 上限','当前状态'])
        self.accounts.setColumnWidth(0,220); self.accounts.setColumnWidth(1,170); self.accounts.setColumnWidth(2,180); l.addWidget(self.accounts)
        bar = QHBoxLayout(); l.addLayout(bar)
        for label,fn in [('新增账号',self.add_account),('修改备注 / 间隔 / 上限',self.edit_account),('登录 / 查看内嵌浏览器',lambda:self.account_command('login')),('重启选中浏览器',self.restart_account_browser),('停止选中账号',lambda:self.account_command('stop')),('采集发送诊断',lambda:self.account_command('diagnose')),('删除账号',self.delete_account)]: bar.addWidget(button(label,fn))
        l.addWidget(QLabel('切换模式前请先关闭当前模式。遇到登录或验证提示时，该账号停止，其余账号继续；请本人处理验证。'))

    def build_browser_workspace(self):
        page = QWidget(); page.setObjectName('page')
        layout = QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(0)
        self.browser_tabs = QTabWidget(); keep_tab_pages_mounted(self.browser_tabs); self.browser_tabs.setObjectName('browserTabs')
        # macOS paints a light native strip behind document-mode tabs even when
        # the rest of the application uses a dark stylesheet. Use the regular
        # tab bar and suppress its native base so the workspace stays uniform.
        self.browser_tabs.setDocumentMode(False)
        self.browser_tabs.tabBar().setDrawBase(False)
        self.browser_tabs.tabBar().setExpanding(False)
        self.browser_tabs.setToolTip('账号登录资料独立；私信按需加载页面，等待时休眠。可切换查看，请勿手动操作正在执行任务的账号。')
        layout.addWidget(self.browser_tabs,1)
        empty = QLabel('尚未打开账号页面。请在“账号集群”中选中账号，点击“登录 / 查看内嵌浏览器”。')
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter); empty.setObjectName('muted')
        self.browser_empty = empty; self.browser_tabs.addTab(empty,'使用提示')
        self.tabs.addTab(page,'内嵌浏览器')
        self.browser_page = page

    def ensure_browser(self, account, focus=True, activate=True):
        ident = account['id']
        widget = self.browser_widgets.get(ident)
        if widget is None:
            if self.browser_empty is not None:
                index = self.browser_tabs.indexOf(self.browser_empty)
                if index >= 0: self.browser_tabs.removeTab(index)
                self.browser_empty = None
            widget = EmbeddedBrowser(self.data/'embedded_profiles'/ident,ident,self,activate=activate)
            widget.restart_requested.connect(self.restart_account_browser)
            widget.renderer_failed.connect(lambda ident,reason:self.add_log(ident+'：'+reason))
            widget.set_task_status(self.states.get(ident,'等待操作'))
            self.browser_widgets[ident] = widget
            self.browser_tabs.addTab(widget,account['name'])
        else:
            index = self.browser_tabs.indexOf(widget)
            if index >= 0: self.browser_tabs.setTabText(index,account['name'])
            if activate:
                widget.resume_page()
        if focus:
            self.tabs.setCurrentWidget(self.browser_page)
            self.browser_tabs.setCurrentWidget(widget)
        return widget

    def close_browser(self, ident):
        widget = self.browser_widgets.pop(ident,None)
        if widget is not None:
            index = self.browser_tabs.indexOf(widget)
            if index >= 0: self.browser_tabs.removeTab(index)
            widget.shutdown()
            widget.deleteLater()

    def reset_browser_page(self, ident):
        widget = self.browser_widgets.get(ident)
        if widget is not None:
            widget.reset_page()

    def change_browser_state(self, ident, operation, request):
        error = ''
        try:
            widget = self.browser_widgets.get(ident)
            if operation == 'park':
                if widget is not None:
                    widget.suspend_page()
            elif operation in ('wake','reset'):
                rows = self.db.rows('SELECT * FROM accounts WHERE id=?',(ident,))
                if not rows:
                    raise ValueError('账号不存在')
                widget = self.ensure_browser(rows[0],False,activate=False)
                if operation == 'reset':
                    widget.reset_page()
                else:
                    widget.resume_page()
            else:
                raise ValueError('不支持的浏览器操作')
        except Exception as exc:
            error = str(exc)
        self.engine.complete_browser_request(request,error)

    def restart_account_browser(self, ident=None):
        def action():
            account = (next((r for r in self.account_rows if r['id']==ident),None)
                       if ident else self.selected(self.accounts,self.account_rows))
            if account is None: raise ValueError('账号不存在')
            with self.engine.lock:
                running = account['id'] in self.engine.running_accounts
            if running: raise ValueError('请先停止该账号任务，再重启浏览器')
            if not self.engine.ready.is_set(): raise ValueError('后台正在初始化，请稍后重试')
            widget = self.ensure_browser(account,True)
            self.engine.command('close',account)
            widget.reset_page()
            self.engine.command('login',account)
            self.add_log(account['name']+'：已手动重建浏览器，登录数据保留')
            self.statusBar().showMessage(account['name']+'：浏览器已重启',8000)
        self.report(action)

    def open_preview(self, url):
        with self.engine.lock:
            running = set(self.engine.running_accounts)
        account = next((r for r in self.account_rows if r['id'] not in running),None)
        if account is None:
            raise ValueError('请先添加一个账号，或停止一个账号任务后再打开客户主页')
        widget = self.ensure_browser(account,True)
        widget.navigate(url)

    def sync_cluster_switches(self):
        with self.engine.lock:
            running = len(self.engine.running_accounts)
            current = self.engine.cluster_mode
            stopping = self.engine.cluster_stopping
        for mode,switch in self.mode_switches.items():
            active = mode==current
            switch.setChecked(active)
            switch.setEnabled(not stopping and (current is None or active))
            name = '找评论' if mode=='comments' else '私信'
            switch.setText(f'关闭{name}模式（{running} 个账号运行中）' if active else '开启全部账号私信' if mode=='messages' else f'开启{name}模式')
        self.single_message_button.setEnabled(not self.engine.busy() and bool(self.account_rows))

    def start_selected_messages(self):
        self.toggle_cluster('messages',True,selected_only=True)

    def toggle_cluster(self,mode,enabled,selected_only=False):
        def action():
            if not self.engine.ready.is_set(): raise ValueError('后台正在初始化，请稍后重试')
            payload = {'mode':mode}
            if enabled:
                if self.engine.busy(): raise ValueError('请先停止当前集群任务，再切换模式')
                if not self.db.setting('base_url','') or not self.db.setting('model',''):
                    raise ValueError('请先保存 AI 设置')
                if selected_only:
                    account = self.selected(self.accounts,self.account_rows)
                    payload['account_ids'] = [account['id']]
                accounts = self.engine.cluster_accounts(mode,payload.get('account_ids'))
                for account in accounts:
                    widget = self.ensure_browser(account,False,activate=mode!='messages')
                    if mode == 'messages':
                        widget.suspend_page()
                        widget.set_task_status('等待工作位 · 页面休眠')
            self.engine.command('start_cluster' if enabled else 'stop_cluster',payload)
            if enabled:
                self.tabs.setCurrentWidget(self.browser_page)
                widget = self.browser_widgets.get(accounts[0]['id'])
                if widget is not None: self.browser_tabs.setCurrentWidget(widget)
            self.add_log(('找评论' if mode=='comments' else '私信')+('模式：已请求开启（'+ '、'.join(a['name'] for a in accounts)+'）' if enabled else '模式：已请求停止'))
        self.report(action)
        self.sync_cluster_switches()

    def build_bloggers(self):
        l = self.page('视频来源 / 扫描进度')
        self.video_source_tabs = QTabWidget()
        self.video_source_tabs.setObjectName('videoSourceTabs')
        self.video_source_tabs.setDocumentMode(False)
        self.video_source_tabs.tabBar().setDrawBase(False)
        self.video_source_tabs.tabBar().setExpanding(False)
        l.addWidget(self.video_source_tabs,1)

        keywords_page = QWidget(); keywords_page.setObjectName('subPage')
        keywords_layout = QVBoxLayout(keywords_page); keywords_layout.setContentsMargins(18,18,18,18); keywords_layout.setSpacing(14)
        self.video_source_tabs.addTab(keywords_page,'抖音指数找视频')
        keyword_bar = QHBoxLayout(); keyword_bar.setSpacing(12); keywords_layout.addLayout(keyword_bar)
        keyword_title = QLabel('抖音指数找视频'); keyword_title.setObjectName('sectionTitle'); keyword_bar.addWidget(keyword_title)
        keyword_bar.addStretch()
        keyword_bar.addWidget(button('新增关键词',self.add_keyword))
        keyword_bar.addWidget(button('批量导入 CSV',lambda:self.import_keywords_csv()))
        keyword_bar.addWidget(button('下载 CSV 模板',self.download_keyword_template))
        self.delete_keyword_button = button('删除关键词',self.delete_keyword); keyword_bar.addWidget(self.delete_keyword_button)
        keyword_hint = QLabel('每次重建清单时优先扫描全部关键词，固定打开创作者中心并选择“近7天”。')
        keyword_hint.setObjectName('muted'); keyword_hint.setWordWrap(True); keywords_layout.addWidget(keyword_hint)
        self.keywords = table(['搜索关键词','发布时间筛选'])
        self.keywords.setColumnWidth(0,600); keywords_layout.addWidget(self.keywords,1)
        self.keywords.itemSelectionChanged.connect(self.update_keyword_actions)
        self.update_keyword_actions()

        search_page = QWidget(); search_page.setObjectName('subPage')
        search_layout = QVBoxLayout(search_page); search_layout.setContentsMargins(18,18,18,18); search_layout.setSpacing(14)
        self.video_source_tabs.addTab(search_page,'抖音搜索找视频')
        search_bar = QHBoxLayout(); search_bar.setSpacing(12); search_layout.addLayout(search_bar)
        search_title = QLabel('抖音搜索找视频'); search_title.setObjectName('sectionTitle'); search_bar.addWidget(search_title)
        search_bar.addStretch()
        search_bar.addWidget(button('新增搜索词',self.add_search_keyword))
        search_bar.addWidget(button('批量导入 CSV',lambda:self.import_keywords_csv(search=True)))
        search_bar.addWidget(button('下载 CSV 模板',self.download_keyword_template))
        self.delete_search_button = button('删除搜索词',self.delete_search_keyword); search_bar.addWidget(self.delete_search_button)
        search_hint = QLabel('打开抖音网页版搜索页（douyin.com/search/关键词），不限发布时间，一直向下滑动加载，直到出现“暂时没有更多了”才结束；视频和图文笔记都会收集。视频较多时耗时较长。')
        search_hint.setObjectName('muted'); search_hint.setWordWrap(True); search_layout.addWidget(search_hint)
        self.search_keywords = table(['搜索词','范围'])
        self.search_keywords.setColumnWidth(0,600); search_layout.addWidget(self.search_keywords,1)
        self.search_keywords.itemSelectionChanged.connect(self.update_search_actions)
        self.update_search_actions()

        bloggers_page = QWidget(); bloggers_page.setObjectName('subPage')
        bloggers_layout = QVBoxLayout(bloggers_page); bloggers_layout.setContentsMargins(18,18,18,18); bloggers_layout.setSpacing(14)
        self.video_source_tabs.addTab(bloggers_page,'指定博主找视频')
        bar = QHBoxLayout(); bar.setSpacing(12); bloggers_layout.addLayout(bar)
        title = QLabel('指定博主找视频'); title.setObjectName('sectionTitle'); bar.addWidget(title); bar.addStretch()
        add = button('新增博主',self.add_blogger); add.setMinimumWidth(130); bar.addWidget(add)
        self.edit_blogger_button = button('修改备注',self.edit_blogger); self.edit_blogger_button.setProperty('variant','primary'); self.edit_blogger_button.setMinimumWidth(130); bar.addWidget(self.edit_blogger_button)
        self.delete_blogger_button = button('删除博主',self.delete_blogger); bar.addWidget(self.delete_blogger_button)
        hint = QLabel('关键词扫描完成后再扫描全部博主。选中博主可修改备注，也可以双击列表中的博主进行修改。')
        hint.setObjectName('muted'); hint.setWordWrap(True); bloggers_layout.addWidget(hint)
        self.bloggers = table(['博主备注','主页 ID']); self.bloggers.setColumnWidth(0,480); bloggers_layout.addWidget(self.bloggers,1)
        self.bloggers.itemDoubleClicked.connect(lambda item:self.edit_blogger())
        self.bloggers.itemSelectionChanged.connect(self.update_blogger_actions)
        self.update_blogger_actions()

        videos_page = QWidget(); videos_page.setObjectName('subPage')
        videos_layout = QVBoxLayout(videos_page); videos_layout.setContentsMargins(18,18,18,18); videos_layout.setSpacing(14)
        self.videos_page = videos_page
        self.video_source_tabs.addTab(videos_page,'待分析视频')
        video_bar = QHBoxLayout(); videos_layout.addLayout(video_bar)
        video_title = QLabel('待分析视频列表'); video_title.setObjectName('sectionTitle'); video_bar.addWidget(video_title)
        self.video_search = QLineEdit(); self.video_search.setPlaceholderText('按视频标题模糊搜索…')
        self.video_search.setClearButtonEnabled(True); self.video_search.setMinimumWidth(260)
        self.video_search.textChanged.connect(self.refresh_videos); video_bar.addWidget(self.video_search)
        self._checked_video_ids = set()
        self.video_select_all = QCheckBox('全选当前列表')
        self.video_select_all.stateChanged.connect(self.toggle_all_videos)
        video_bar.addWidget(self.video_select_all)
        video_bar.addStretch()
        self.add_manual_video_button = button('录入视频/笔记链接',self.add_manual_video)
        self.add_manual_video_button.setProperty('variant','primary'); video_bar.addWidget(self.add_manual_video_button)
        self.remove_manual_video_button = button('移除手动视频',self.remove_manual_video)
        video_bar.addWidget(self.remove_manual_video_button)
        self.delete_videos_button = button('删除勾选视频',self.delete_selected_videos)
        self.delete_videos_button.setProperty('variant','danger'); video_bar.addWidget(self.delete_videos_button)
        self.clear_videos_button = button('一键清空',self.clear_videos)
        self.clear_videos_button.setProperty('variant','danger'); video_bar.addWidget(self.clear_videos_button)
        self.video_summary = QLabel('共 0 个'); self.video_summary.setObjectName('muted'); video_bar.addWidget(self.video_summary)
        self.videos = table(['选择','状态','来源','视频标题','作者','发布时间','作品 ID','异常说明'])
        self.videos.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for col,width in enumerate([58,100,160,360,140,160,200,280]): self.videos.setColumnWidth(col,width)
        self.videos.itemSelectionChanged.connect(self.update_manual_video_action)
        self.videos.itemChanged.connect(self.video_checkbox_changed)
        self.update_manual_video_action()
        videos_layout.addWidget(self.videos,1)
        video_hint = QLabel('勾选视频前的选择框，或全选当前列表，停止任务后可批量删除勾选项。找视频中断后会继续未完成的来源；自动来源的视频在下次列表重建时可能重新发现。')
        video_hint.setObjectName('muted'); video_hint.setWordWrap(True); videos_layout.addWidget(video_hint)

    def update_blogger_actions(self):
        selected = bool(self.bloggers.selectionModel().selectedRows())
        self.edit_blogger_button.setEnabled(selected)
        self.delete_blogger_button.setEnabled(selected)

    def update_keyword_actions(self):
        self.delete_keyword_button.setEnabled(bool(self.keywords.selectionModel().selectedRows()))

    def update_search_actions(self):
        self.delete_search_button.setEnabled(bool(self.search_keywords.selectionModel().selectedRows()))

    def update_manual_video_action(self):
        selected = self.videos.selectionModel().selectedRows()
        row = selected[0].row() if len(selected)==1 else -1
        manual = (len(selected)==1 and hasattr(self,'video_rows') and 0 <= row < len(self.video_rows)
                  and self.video_rows[row].get('source_type')=='manual')
        self.remove_manual_video_button.setEnabled(manual)
        checked = len(self.checked_video_rows())
        self.delete_videos_button.setEnabled(bool(checked))
        self.delete_videos_button.setText('删除勾选视频'+(f'（{checked}）' if checked else ''))
        total = self.videos.rowCount()
        state = (Qt.CheckState.Checked if total and checked==total else
                 Qt.CheckState.PartiallyChecked if checked else Qt.CheckState.Unchecked)
        self.video_select_all.blockSignals(True)
        self.video_select_all.setTristate(state==Qt.CheckState.PartiallyChecked)
        self.video_select_all.setCheckState(state)
        self.video_select_all.setEnabled(bool(total))
        self.video_select_all.blockSignals(False)

    def checked_video_rows(self):
        return [row for row in getattr(self,'video_rows',[]) if row['id'] in self._checked_video_ids]

    def video_checkbox_changed(self, item):
        if item.column()!=0:
            return
        ident = item.data(Qt.ItemDataRole.UserRole)
        if not ident:
            return
        if item.checkState()==Qt.CheckState.Checked:
            self._checked_video_ids.add(ident)
        else:
            self._checked_video_ids.discard(ident)
        self.update_manual_video_action()

    def toggle_all_videos(self, state):
        checked = state==Qt.CheckState.Checked.value
        self.videos.blockSignals(True)
        for i,row in enumerate(getattr(self,'video_rows',[])):
            if checked:
                self._checked_video_ids.add(row['id'])
            else:
                self._checked_video_ids.discard(row['id'])
            self.videos.item(i,0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.videos.blockSignals(False)
        self.update_manual_video_action()

    def build_settings(self):
        l = self.page('AI 与任务设置'); forms = {}
        for name in ('监控参数','AI 服务与提示词','页面适配'):
            group = QGroupBox(name); form = QFormLayout(group)
            form.setContentsMargins(18,28,18,18); form.setSpacing(14)
            form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            forms[name] = form; l.addWidget(group)
        self.fields = {}
        settings = self.engine.settings()
        labels = dict(collection_accounts='同时采集账号数（不超过账号总数）',keyword_pause_seconds='每个账号搜索后暂停（秒，0 关闭）',message_concurrency='私信同时工作账号数（默认 3）',thinking_mode='DeepSeek 思考模式',thinking_effort='DeepSeek 思考强度',ai_concurrency='每个账号评论 AI 并发数（1–20）',days='博主近期视频：最近 N 天',comment_days='近期评论：最近 N 天',video_list_reset_hours='视频列表清空重建周期（小时）',poll_seconds='监控轮询间隔（秒）',scroll_limit='每页最大滚动次数',base_url='AI 接口根地址（含 /v1）',model='模型名称',filter_prompt='目标客户筛选提示词',message_prompt='个性化私信提示词',comment_panel='评论滚动区域 CSS（需实页校准）',message_editor='私信输入框 CSS（需实页校准）')
        for k,v in DEFAULTS.items():
            val = settings[k]
            if k in ('thinking_mode','thinking_effort'):
                field = NoWheelComboBox()
                options = [('跟随服务默认','default'),('开启','enabled'),('关闭','disabled')] if k=='thinking_mode' else [('低（low）','low'),('高（high）','high'),('最高（max）','max')]
                for label,value in options: field.addItem(label,value)
                field.setCurrentIndex(max(0,field.findData(val)))
            elif isinstance(v,int):
                field = NoWheelSpinBox(); field.setRange(0 if k=='keyword_pause_seconds' else 1,max(1,len(self.db.rows('SELECT id FROM accounts'))) if k=='collection_accounts' else 20 if k in ('ai_concurrency','message_concurrency') else 86400 if k in ('poll_seconds','keyword_pause_seconds') else 1000); field.setValue(val)
            elif k.endswith('prompt'):
                field = QPlainTextEdit(val); field.setMinimumHeight(300)
            else: field = QLineEdit(val)
            self.fields[k] = field
            section = '监控参数' if isinstance(v,int) else '页面适配' if k in ('comment_panel','message_editor') else 'AI 服务与提示词'
            if k.endswith('prompt'):
                forms[section].addRow(QLabel(labels[k]))
                forms[section].addRow(field)
            else:
                forms[section].addRow(labels[k],field)
        self.fields['collection_accounts'].setToolTip('按账号列表顺序使用前 N 个账号；设置保存后，下次开启找评论模式生效。私信可使用全部账号或仅选中账号。')
        self.fields['keyword_pause_seconds'].setToolTip('默认 60 秒，范围 0–86400 秒。每个账号处理完一个抖音指数关键词或抖音搜索词后暂停；等待期间释放页面内存，保留登录资料，其他账号继续工作。0 表示关闭；下次开启找评论模式生效。')
        self.fields['message_concurrency'].setToolTip('全部启用账号按各自间隔轮流工作；最多同时加载此数量的账号页面，等待时释放页面。下次启动私信时生效。')
        self.fields['thinking_mode'].currentIndexChanged.connect(self.update_thinking_controls)
        self.update_thinking_controls()
        self.api_key = QLineEdit(); self.api_key.setEchoMode(QLineEdit.EchoMode.Password); self.api_key.setPlaceholderText('留空保留已有密钥；保存到系统钥匙串，不导出')
        forms['AI 服务与提示词'].insertRow(2,'API Key',self.api_key)
        # Keep the input column aligned across all parameter groups.
        parameter_labels = [form.itemAt(row,QFormLayout.ItemRole.LabelRole).widget()
            for form in forms.values() for row in range(form.rowCount())
            if form.itemAt(row,QFormLayout.ItemRole.LabelRole) is not None]
        label_width = max(label.sizeHint().width() for label in parameter_labels)
        for label in parameter_labels:
            label.setMinimumWidth(label_width)
        for field in self.fields.values():
            if isinstance(field,(QLineEdit,QSpinBox)):
                field.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.api_key.setAlignment(Qt.AlignmentFlag.AlignLeft)
        l.addWidget(button('保存设置',self.save_settings))
        l.addWidget(QLabel('设置在任务下次开启时生效。评论和视频标题会提交给你配置的 AI 服务用于判断与生成。'))
        l.addStretch()

    def update_thinking_controls(self):
        self.fields['thinking_effort'].setEnabled(self.fields['thinking_mode'].currentData()=='enabled')
        self.fields['thinking_effort'].setToolTip('开启思考模式后生效；跟随默认时不发送思考参数。适用于 DeepSeek 兼容接口。')

    def build_database(self):
        l = self.page('去重数据库')
        self.database_page = self.tabs.widget(self.tabs.count()-1)
        title = QLabel('去重数据管理'); title.setObjectName('sectionTitle'); l.addWidget(title)
        self.data_summary = QLabel(); self.data_summary.setObjectName('muted'); l.addWidget(self.data_summary)
        bar = QHBoxLayout(); l.addLayout(bar)
        self.data_kind = QComboBox()
        for label,name in [('评论去重记录','comments'),('客户去重记录','customers'),('操作历史','events')]: self.data_kind.addItem(label,name)
        bar.addWidget(self.data_kind)
        self.data_search = QLineEdit(); self.data_search.setPlaceholderText('搜索 ID、昵称、评论、博主或记录内容…'); bar.addWidget(self.data_search,1)
        bar.addWidget(button('刷新',self.refresh_database))
        self.data_table = table([]); self.data_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.data_table.itemDoubleClicked.connect(self.show_data_detail); l.addWidget(self.data_table,1)
        pager = QHBoxLayout(); l.addLayout(pager)
        self.data_previous = button('上一页',lambda:self.change_data_page(-1)); pager.addWidget(self.data_previous)
        self.data_pagination = QLabel(); pager.addWidget(self.data_pagination)
        self.data_next = button('下一页',lambda:self.change_data_page(1)); pager.addWidget(self.data_next); pager.addStretch()
        pager.addWidget(button('查看完整记录',self.show_data_detail))
        actions = QHBoxLayout(); l.addLayout(actions)
        for label,scope in [('删除选中记录','selected'),('清空当前数据表','table'),('清空全部去重数据及历史','all')]:
            b = button(label,lambda checked=False,scope=scope:self.delete_database(scope)); b.setProperty('variant','danger'); actions.addWidget(b)
        actions.addStretch()
        hint = QLabel('按 Ctrl / ⌘ 或 Shift 多选。删除前自动备份，需先停止任务。删除评论可使其再次被 AI 判断；删除客户可能使其再次进入私信队列。')
        hint.setWordWrap(True); hint.setObjectName('muted'); l.addWidget(hint)
        self.data_page_number = 0
        self.data_kind.currentIndexChanged.connect(self.reset_data_page)
        self.data_search.textChanged.connect(self.reset_data_page)
        self.refresh_database()

    def reset_data_page(self,*args):
        self.data_page_number = 0
        self.refresh_database()

    def change_data_page(self,step):
        self.data_page_number += step
        self.refresh_database()

    def refresh_database(self):
        if not hasattr(self,'data_table'): return
        counts = {name:self.db.rows('SELECT count(*) n FROM '+name)[0]['n'] for name in ('comments','customers','events')}
        self.data_summary.setText(f"评论 {counts['comments']} 条  ·  客户 {counts['customers']} 位  ·  历史 {counts['events']} 条  ·  每页 100 条")
        kind = self.data_kind.currentData()
        columns = {
            'comments':[('id','评论 ID'),('uid','客户主页 ID'),('name','昵称'),('text','评论'),('comment_time','评论时间'),('video_title','视频'),('blogger','博主'),('target','目标客户'),('reason','AI 判断理由')],
            'customers':[('uid','客户主页 ID'),('name','昵称'),('status','状态'),('text','评论'),('comment_time','评论时间'),('video_title','视频'),('blogger','博主'),('message','私信'),('sent_at','发送时间')],
            'events':[('id','记录 ID'),('at','时间'),('account_id','账号 ID'),('uid','客户主页 ID'),('kind','操作'),('detail','内容')]}
        cols = columns[kind]
        self.data_rows,total,self.data_page_number = self.db.data_page(kind,self.data_search.text(),self.data_page_number)
        self.data_table.setColumnCount(len(cols)); self.data_table.setHorizontalHeaderLabels([label for key,label in cols])
        def display(row,key):
            if key=='target': return '是' if row[key] else '否'
            if key=='status': return STATUS.get(row[key],row[key])
            return row[key]
        populate(self.data_table,[[display(row,key) for key,label in cols] for row in self.data_rows])
        for i,(key,label) in enumerate(cols): self.data_table.setColumnWidth(i,260 if key in ('text','detail','message','reason','uid') else 150)
        pages = max(1,(total+99)//100)
        self.data_pagination.setText(f'第 {self.data_page_number+1} / {pages} 页 · 匹配 {total} 条')
        self.data_previous.setEnabled(self.data_page_number>0); self.data_next.setEnabled(self.data_page_number+1<pages)

    def show_data_detail(self,*args):
        def action():
            r = self.selected(self.data_table,self.data_rows)
            dialog = QDialog(self); dialog.setWindowTitle('完整数据库记录'); dialog.resize(760,520)
            l = QVBoxLayout(dialog); text = QPlainTextEdit(); text.setReadOnly(True)
            text.setPlainText(json.dumps(r,ensure_ascii=False,indent=2)); l.addWidget(text)
            l.addWidget(button('关闭',dialog.accept)); dialog.exec()
        self.report(action)

    def delete_database(self,scope):
        def action():
            if self.engine.busy(): raise ValueError('请先停止所有账号任务，再删除数据库记录')
            kind = self.data_kind.currentData(); ids = None
            if scope=='selected':
                key = 'uid' if kind=='customers' else 'id'
                ids = [self.data_rows[index.row()][key] for index in self.data_table.selectionModel().selectedRows()]
                if not ids: raise ValueError('请先选择需要删除的记录，可多选')
                description = f'删除“{self.data_kind.currentText()}”中选中的 {len(ids)} 条记录'
            elif scope=='all':
                kind = 'all'; description = '清空全部评论、客户与操作历史（不受搜索条件限制）'
            else:
                description = f'清空“{self.data_kind.currentText()}”的全部记录（不受搜索条件限制）'
            note = '账号、博主和 AI 配置保留。'
            if kind=='comments': note += '客户和发送状态保留；已删除评论下轮可能重新分析。'
            elif kind=='customers': note += '评论去重记录保留；旧评论不会自动重新分析，新评论仍可能使客户再次入队。'
            elif kind=='events': note += '评论、客户和发送状态保留。'
            else: note += '再次监控可能重新发现并联系这些客户。'
            if QMessageBox.question(self,'确认删除',description+'？\n\n'+note+'\n\n操作前自动备份数据库。')!=QMessageBox.StandardButton.Yes:return
            folder = self.data/'backups'; folder.mkdir(exist_ok=True)
            path = folder/(datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.sqlite3')
            self.db.backup(path)
            count = self.db.delete_data(kind,ids)
            self.refresh_database(); self.refresh_customers()
            self.add_log(f'数据库删除：{description}，实际删除 {count} 条；备份：{path}')
            QMessageBox.information(self,'删除完成',f'已删除 {count} 条记录。\n备份文件：{path}')
        self.report(action)

    def selected(self, widget, rows):
        n = widget.currentRow()
        if n<0 or n>=len(rows): raise ValueError('请先选择一行')
        return rows[n]

    def report(self, fn):
        try: return fn()
        except Exception as exc: QMessageBox.warning(self,'无法完成',str(exc))

    def add_log(self,text):
        self.logs.appendPlainText(datetime.now().strftime('%H:%M:%S')+'  '+text)
        logging.getLogger('panda').info(text)

    def account_status(self, ident, status):
        self.states[ident]=status
        widget = self.browser_widgets.get(ident)
        if widget is not None:
            widget.set_task_status(status)
        self.refresh_accounts()

    def schedule_runtime_refresh(self):
        if getattr(self,'_closing',False):
            return
        if not self.refresh_timer.isActive():
            self.refresh_timer.start()

    def refresh_runtime_views(self):
        self.refresh_customers()
        self.refresh_accounts()
        self.refresh_videos()

    def refresh_accounts(self):
        self.account_rows = self.db.rows('SELECT * FROM accounts ORDER BY rowid')
        selected = self.accounts.currentRow()
        def quota_text(account):
            quota = self.db.sender_quota(account['id'])
            limit = quota['limit'] if quota['limit'] else '不限'
            return f'{quota["used"]} / {limit}'
        populate(self.accounts,[[r['name'],r['interval'],quota_text(r),self.states.get(r['id'],'未启动')] for r in self.account_rows])
        if 0<=selected<len(self.account_rows): self.accounts.selectRow(selected)
        names = {r['id']:r['name'] for r in self.account_rows}
        for ident,widget in self.browser_widgets.items():
            index = self.browser_tabs.indexOf(widget)
            if index >= 0 and ident in names: self.browser_tabs.setTabText(index,names[ident])
        self.sync_cluster_switches()
        if hasattr(self,'fields'):
            count = len(self.account_rows)
            self.fields['collection_accounts'].setMaximum(max(1,count))
            self.fields['collection_accounts'].setEnabled(count>0)
            saved = self.db.setting('collection_accounts',1)
            if count and saved>count:
                self.db.save_settings({'collection_accounts':count})

    def refresh_bloggers(self):
        self.blogger_rows = self.db.rows('SELECT * FROM bloggers')
        populate(self.bloggers,[[r['name'],r['id']] for r in self.blogger_rows])

    def refresh_keywords(self):
        self.keyword_rows = self.db.rows('SELECT * FROM keywords ORDER BY id')
        populate(self.keywords,[[r['keyword'],'近7天'] for r in self.keyword_rows])

    def refresh_search_keywords(self):
        self.search_rows = self.db.rows('SELECT * FROM search_keywords ORDER BY id')
        populate(self.search_keywords,[[r['keyword'],'全部结果（滑到底）'] for r in self.search_rows])

    def refresh_videos(self):
        old_rows = getattr(self,'video_rows',[])
        selected_ids = {old_rows[index.row()]['id'] for index in self.videos.selectionModel().selectedRows()
                        if index.row()<len(old_rows)}
        all_rows = self.db.video_tasks() if hasattr(self.db,'video_tasks') else []
        self._checked_video_ids.intersection_update(row['id'] for row in all_rows)
        query = self.video_search.text().strip().casefold()
        rows = [row for row in all_rows if query in str(row.get('title','')).casefold()]
        self.video_rows = rows
        view = [['',VIDEO_STATUS.get(r['status'],r['status']),
            ({'blogger':'博主：','keyword':'关键词：','search':'抖音搜索：','manual':''}.get(r['source_type'],'')+r['source_name']),
            r['title'],r['author'],
            self.video_time(r['create_time']),
            r['id'],r['error']] for r in rows]
        # This runs every few seconds; rebuilding thousands of unchanged cells
        # wastes GUI time and resets scrolling/selection for no reason.
        changed = view != getattr(self,'_video_view',None)
        if changed:
            self._video_view = view
            self.videos.blockSignals(True)
            self.videos.clearSelection()
            populate(self.videos,view)
            selection = self.videos.selectionModel()
            for i,row in enumerate(rows):
                checkbox = self.videos.item(i,0)
                checkbox.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                checkbox.setData(Qt.ItemDataRole.UserRole,row['id'])
                checkbox.setCheckState(Qt.CheckState.Checked if row['id'] in self._checked_video_ids else Qt.CheckState.Unchecked)
                if row['id'] in selected_ids:
                    selection.select(self.videos.model().index(i,0),
                        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
            self.videos.blockSignals(False)
        counts = {}
        for row in rows: counts[row['status']] = counts.get(row['status'],0)+1
        detail = '，'.join(VIDEO_STATUS[key]+' '+str(counts[key]) for key in VIDEO_STATUS if counts.get(key))
        prefix = ('显示 '+str(len(rows))+' / 共 '+str(len(all_rows))+' 个'
                  if query else '共 '+str(len(all_rows))+' 个')
        self.video_summary.setText(prefix+((' · '+detail) if detail else ''))
        self.video_source_tabs.setTabText(self.video_source_tabs.indexOf(self.videos_page),'待分析视频（'+str(len(all_rows))+'）')
        colors = {'pending':'#80baff','scanning':'#b7a2ff','done':'#62d7b0','failed':'#f48b96'}
        for i,row in enumerate(rows if changed else ()):
            self.videos.item(i,1).setForeground(QColor(colors.get(row['status'],'#dbe5f3')))
        self.update_manual_video_action()

    @staticmethod
    def video_time(value):
        try:
            return datetime.fromtimestamp(value,BEIJING_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S') if value else ''
        except (TypeError,ValueError,OverflowError,OSError):
            return ''

    def refresh_customers(self):
        counts = {r['status']:r['n'] for r in self.db.rows('SELECT status,count(*) n FROM customers GROUP BY status')}
        for key,label in self.metric_values.items():
            label.setText(str(sum(counts.values()) if key=='total' else counts.get(key,0)))
        if not hasattr(self,'filter'): return
        current = self.customers.currentRow()
        selected_uid = self.customer_rows[current]['uid'] if hasattr(self,'customer_rows') and 0<=current<len(self.customer_rows) else None
        q = '%'+self.search.text()+'%'; status = self.filter.currentData()
        self.customer_rows = self.db.rows("SELECT c.*,a.name sender_name FROM customers c LEFT JOIN accounts a ON c.account_id=a.id WHERE (c.name LIKE ? OR c.text LIKE ? OR c.blogger LIKE ?) AND (?='' OR c.status=?) ORDER BY updated DESC LIMIT 1000",(q,q,q,status,status))
        view = [[r['name'],STATUS.get(r['status'],r['status']),r['text'],format_beijing_time(r['comment_time']),r['video_title'],r['blogger'],r['uid'],r['sender_name'],r['message'],r['error']] for r in self.customer_rows]
        if view == getattr(self,'_customer_view',None): return
        self._customer_view = view
        populate(self.customers,view)
        colors = {'pending':'#80baff','processing':'#b7a2ff','sent':'#62d7b0','failed':'#f48b96','skipped':'#9ba9bd'}
        for i,r in enumerate(self.customer_rows):
            self.customers.item(i,1).setForeground(QColor(colors.get(r['status'],'#dbe5f3')))
            if r['uid']==selected_uid: self.customers.selectRow(i)

    def add_account(self):
        def action():
            name,ok=QInputDialog.getText(self,'新增账号','账号备注：')
            if not ok or not name.strip(): return
            daily_limit,ok=QInputDialog.getInt(self,'每日私信上限','每天最多私信人数（0 表示不限）：',0,0,100000)
            if not ok:return
            self.db.account(name.strip(),daily_limit); self.refresh_accounts()
        self.report(action)

    def edit_account(self):
        def action():
            r=self.selected(self.accounts,self.account_rows)
            name,ok=QInputDialog.getText(self,'修改账号','备注：',text=r['name'])
            if not ok or not name.strip(): return
            interval,ok=QInputDialog.getInt(self,'发送间隔','秒：',r['interval'],1,86400)
            if not ok:return
            daily_limit = r.get('daily_limit',0)
            daily_limit,ok=QInputDialog.getInt(self,'每日私信上限','每天最多私信人数（0 表示不限）：',daily_limit,0,100000)
            if not ok:return
            self.db.execute('UPDATE accounts SET name=?,interval=?,daily_limit=? WHERE id=?',(name.strip(),interval,daily_limit,r['id'])); self.refresh_accounts()
        self.report(action)

    def account_command(self,op):
        def action():
            r=self.selected(self.accounts,self.account_rows)
            if not self.engine.ready.is_set(): raise ValueError('后台正在初始化，请稍后重试')
            if op=='login':
                if r['id'] in self.engine.running_accounts: raise ValueError('请先停止该账号任务，再打开登录页面')
                self.ensure_browser(r,True)
            self.engine.command(op,r)
        self.report(action)

    def delete_account(self):
        def action():
            r=self.selected(self.accounts,self.account_rows)
            if self.engine.busy(): raise ValueError('请先停止所有账号任务')
            if QMessageBox.question(self,'删除账号','删除账号记录？登录目录保留在本地，客户和发送历史保留。')!=QMessageBox.StandardButton.Yes: return
            self.engine.command('close',r)
            self.close_browser(r['id'])
            self.db.execute('DELETE FROM accounts WHERE id=?',(r['id'],)); self.refresh_accounts()
        self.report(action)

    def add_blogger(self):
        dialog = BloggerDialog(self.db,self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_bloggers()
            self.statusBar().showMessage('博主已添加到监控列表',5000)

    def edit_blogger(self):
        def action():
            r = self.selected(self.bloggers,self.blogger_rows)
            name,ok = QInputDialog.getText(self,'修改博主备注','备注名称：',text=r['name'])
            if not ok: return
            name = name.strip()
            if not name: raise ValueError('备注名称不能为空')
            self.db.execute('UPDATE bloggers SET name=? WHERE id=?',(name,r['id']))
            self.refresh_bloggers()
            for i,row in enumerate(self.blogger_rows):
                if row['id']==r['id']: self.bloggers.selectRow(i); break
            self.statusBar().showMessage('博主备注已保存',5000)
        self.report(action)

    def delete_blogger(self):
        def action():
            r=self.selected(self.bloggers,self.blogger_rows)
            self.db.execute('DELETE FROM bloggers WHERE id=?',(r['id'],)); self.refresh_bloggers()
        self.report(action)

    def add_keyword(self):
        def action():
            value,ok = QInputDialog.getText(self,'新增找视频关键词','关键词：')
            value = value.strip()
            if not ok or not value: return
            inserted = self.db.execute('INSERT OR IGNORE INTO keywords(keyword) VALUES (?)',(value,))
            if not inserted: raise ValueError('该关键词已存在')
            self.refresh_keywords()
            self.statusBar().showMessage('关键词已添加；下次视频清单重建时生效',5000)
        self.report(action)

    def import_keywords_csv(self, search=False):
        def action():
            label = '搜索词' if search else '关键词'
            path,_ = QFileDialog.getOpenFileName(self,'批量导入'+label,'','CSV 文件 (*.csv)')
            if not path: return
            values = read_keyword_csv(path)
            result = self.db.import_keywords(values,search=search)
            if search:
                self.refresh_search_keywords()
            else:
                self.refresh_keywords()
            summary = f'新增 {result["added"]} 个{label}，跳过 {result["duplicates"]} 个重复词；下次视频清单重建时生效'
            self.statusBar().showMessage(summary,10000)
            QMessageBox.information(self,'批量导入完成',summary)
        self.report(action)

    def download_keyword_template(self):
        def action():
            path,_ = QFileDialog.getSaveFileName(self,'保存通用批量导入模板','关键词搜索词批量导入模板.csv','CSV 文件 (*.csv)')
            if not path: return
            if not path.lower().endswith('.csv'): path += '.csv'
            write_keyword_template(path)
            self.statusBar().showMessage('模板已保存；保留“关键词”表头，替换示例词，每行一个词，两种来源均可导入',10000)
        self.report(action)

    def delete_keyword(self):
        def action():
            row = self.selected(self.keywords,self.keyword_rows)
            self.db.execute('DELETE FROM keywords WHERE id=?',(row['id'],))
            self.refresh_keywords()
        self.report(action)

    def add_search_keyword(self):
        def action():
            value,ok = QInputDialog.getText(self,'新增抖音搜索词','搜索关键词：')
            value = value.strip()
            if not ok or not value: return
            inserted = self.db.execute('INSERT OR IGNORE INTO search_keywords(keyword) VALUES (?)',(value,))
            if not inserted: raise ValueError('该搜索词已存在')
            self.refresh_search_keywords()
            self.statusBar().showMessage('搜索词已添加；下次视频清单重建时生效',5000)
        self.report(action)

    def delete_search_keyword(self):
        def action():
            row = self.selected(self.search_keywords,self.search_rows)
            self.db.execute('DELETE FROM search_keywords WHERE id=?',(row['id'],))
            self.refresh_search_keywords()
        self.report(action)

    def add_manual_video(self):
        def action():
            url,ok = QInputDialog.getText(
                self,'录入抖音视频 / 笔记','完整链接（https://www.douyin.com/video/… 或 /note/…）：',
                text='https://www.douyin.com/')
            if not ok: return
            title,ok = QInputDialog.getText(
                self,'录入抖音视频 / 笔记','标题（选填，便于搜索）：')
            if not ok: return
            ident = self.db.add_manual_video(url,title)
            self.refresh_videos()
            self.video_search.clear()
            self.video_source_tabs.setCurrentWidget(self.videos_page)
            for index,row in enumerate(self.video_rows):
                if row['id']==ident:
                    self.videos.selectRow(index)
                    break
            self.statusBar().showMessage('链接已加入待分析列表',5000)
        self.report(action)

    def remove_manual_video(self):
        def action():
            selected = self.videos.selectionModel().selectedRows()
            if len(selected)!=1:
                raise ValueError('请先选择一个手动录入视频')
            row = self.video_rows[selected[0].row()]
            if row.get('source_type')!='manual':
                raise ValueError('只能移除通过“录入视频链接”添加的视频')
            if QMessageBox.question(self,'移除手动视频',
                    '确认从手动来源和待分析列表中移除该视频？')!=QMessageBox.StandardButton.Yes:
                return
            self.db.remove_manual_video(row['id'])
            self.refresh_videos()
            self.statusBar().showMessage('手动录入视频已移除',5000)
        self.report(action)

    def delete_selected_videos(self):
        def action():
            rows = self.checked_video_rows()
            if not rows:
                raise ValueError('请先勾选要删除的视频')
            if self.engine.busy():
                raise ValueError('请先停止任务，再删除选中视频')
            text = (f'确认删除当前列表中勾选的 {len(rows)} 个视频？\n\n'
                    '手动录入的视频会同时移除手动来源；自动找到的视频本轮不再分析，下次列表重建时可能重新发现。已保存的评论和客户会保留。')
            if QMessageBox.question(self,'删除勾选视频',text)!=QMessageBox.StandardButton.Yes:
                return
            removed = self.db.delete_videos([row['id'] for row in rows])
            self.refresh_videos()
            self.statusBar().showMessage(f'已删除 {removed} 个勾选视频',5000)
        self.report(action)

    def clear_videos(self):
        def action():
            count = len(self.db.video_tasks())
            if not count:
                self.statusBar().showMessage('待分析视频列表已经为空；集群下一轮会重新找视频',5000)
                return
            text = (f'确认清空全部 {count} 个待分析视频？\n\n'
                    '清空后，集群检测到列表为空时会重新从全部搜索词、关键词和博主来源找视频。')
            if QMessageBox.question(self,'清空待分析视频',text)!=QMessageBox.StandardButton.Yes:
                return
            removed = self.db.clear_videos()
            self.refresh_videos()
            self.statusBar().showMessage(f'已清空 {removed} 个视频；集群下一轮将重新找视频',8000)
        self.report(action)

    def save_settings(self):
        def action():
            values={k:(f.currentData() if isinstance(f,QComboBox) else f.value() if isinstance(f,QSpinBox) else f.toPlainText() if isinstance(f,QPlainTextEdit) else f.text().strip()) for k,f in self.fields.items()}
            from urllib.parse import urlparse
            u=urlparse(values['base_url'])
            if values['base_url'] and u.scheme!='https' and not (u.scheme=='http' and u.hostname in ('localhost','127.0.0.1')): raise ValueError('AI 地址需要 HTTPS，本地服务可用 HTTP')
            if self.api_key.text(): keyring.set_password(SERVICE,'api_key',self.api_key.text()); self.api_key.clear()
            self.db.save_settings(values); self.statusBar().showMessage('设置已保存，下次开启任务时生效',5000)
        self.report(action)

    def open_customer(self,*args):
        self.report(lambda:self.open_preview(self.selected(self.customers,self.customer_rows)['url']))

    def mark(self,status):
        def action():
            if self.engine.busy(): raise ValueError('修改客户状态前请先停止任务')
            r=self.selected(self.customers,self.customer_rows)
            if r['status']=='processing':raise ValueError('客户正在处理中')
            if QMessageBox.question(self,'确认状态','确认将该客户标记为'+STATUS[status]+'？')!=QMessageBox.StandardButton.Yes:return
            self.db.execute("UPDATE customers SET status=?,error='',sent_at=CASE WHEN ?='sent' THEN datetime('now') ELSE '' END WHERE uid=?",(status,status,r['uid']))
            self.db.execute('INSERT INTO events(uid,kind,detail) VALUES (?,?,?)',(r['uid'],status,'人工核验调整状态')); self.refresh_customers()
        self.report(action)

    def requeue_failed(self):
        def action():
            if self.engine.busy(): raise ValueError('批量重新排队前请先停止任务')
            count = self.db.rows("SELECT count(*) n FROM customers WHERE status='failed'")[0]['n']
            if not count:
                self.statusBar().showMessage('当前没有失败客户',5000)
                return
            warning = (f'确认将全部 {count} 位失败客户重新排队？\n\n'
                       '其中可能包含发送结果不明的客户。请先在对应账号的聊天记录中核对，避免重复私信。')
            if QMessageBox.question(self,'批量重新排队',warning)!=QMessageBox.StandardButton.Yes:return
            queued = self.db.requeue_failed()
            self.refresh_customers()
            self.add_log(f'人工批量重新排队：{queued} 位失败客户')
            self.statusBar().showMessage(f'已将 {queued} 位失败客户重新排队',8000)
        self.report(action)

    def import_data(self):
        def action():
            path,_=QFileDialog.getOpenFileName(self,'导入客户','','客户文件 (*.json *.csv)')
            if not path:return
            with open(path,encoding='utf-8-sig',newline='') as f: rows=json.load(f) if path.endswith('.json') else list(csv.DictReader(f))
            if not isinstance(rows,list):raise ValueError('JSON 必须是客户对象数组')
            n=self.db.import_customers(rows); self.refresh_customers(); QMessageBox.information(self,'导入完成',f'新增 {n} 位客户，已存在的客户保持原状态')
        self.report(action)

    def export_data(self):
        def action():
            path,_=QFileDialog.getSaveFileName(self,'导出全部客户','customers.json','JSON (*.json);;CSV (*.csv)')
            if not path:return
            rows=self.db.rows('SELECT * FROM customers ORDER BY updated DESC')
            with open(path,'w',encoding='utf-8-sig' if path.endswith('.csv') else 'utf-8',newline='') as f:
                if path.endswith('.csv'):
                    keys=['uid','name','text','comment_time','video_title','blogger','url','status','message','account_id','error','sent_at','updated']
                    w=csv.DictWriter(f,fieldnames=keys); w.writeheader()
                    for r in rows:
                        w.writerow({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v) for k,v in r.items()})
                else:json.dump(rows,f,ensure_ascii=False,indent=2)
            self.statusBar().showMessage('已导出 '+path,8000)
        self.report(action)

    def clear_data(self):
        def action():
            if self.engine.busy():raise ValueError('清空前请停止所有任务')
            if QMessageBox.question(self,'清空客户','先创建数据库备份，再清空客户及评论去重记录？未来监控可能再次发现这些客户。')!=QMessageBox.StandardButton.Yes:return
            folder=self.data/'backups'; folder.mkdir(exist_ok=True)
            path=folder/(datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.sqlite3'); self.db.backup(path)
            with self.db.connect() as c:
                c.execute('DELETE FROM customers'); c.execute('DELETE FROM comments')
            self.refresh_customers(); QMessageBox.information(self,'已备份并清空',str(path))
        self.report(action)

    def force_shutdown(self):
        # Killing a Python QThread can interrupt SQLite/GIL ownership and
        # crash the whole process. Keep pumping GUI acknowledgements instead.
        if self.engine.isRunning():
            self.add_log('后台清理超过 20 秒，继续等待安全退出；未强制终止线程')
            self.statusBar().showMessage('后台仍在清理，正在等待安全退出…')
        else:
            self.close()

    def closeEvent(self,event):
        if self.engine.isRunning():
            self.setEnabled(False)
            if not getattr(self,'_closing',False):
                self._closing = True
                self.timer.stop()
                self.refresh_timer.stop()
                self.engine.finished.connect(self.close)
                QTimer.singleShot(20000,self.force_shutdown)
            self.engine.command('shutdown')
            self.statusBar().showMessage('正在停止任务并关闭内嵌浏览器…')
            event.ignore()
        else:
            self.timer.stop()
            self.refresh_timer.stop()
            for widget in self.browser_widgets.values():
                widget.shutdown()
            event.accept()


STYLE = '''
QWidget {background:#111827;color:#dbe5f3;font-size:13px;font-family:"PingFang SC","Microsoft YaHei",sans-serif;selection-background-color:#294e7d;selection-color:#ffffff;}
QMainWindow {background:#111827;}
QLabel {background:transparent;}
QLabel#title {font-size:25px;font-weight:700;color:#f1f5fb;}
QLabel#metrics {background:#1a304d;color:#e4efff;border:1px solid #2c4668;padding:22px;border-radius:12px;font-size:15px;}
QTabWidget,QTabBar {background:#111827;border:0;}
QTabBar::tear {background:#111827;border:0;}
QTabWidget::pane {border:1px solid #2c394d;background:#182233;border-radius:10px;}

QTabBar::tab {padding:12px 22px;margin:0 10px 12px 0;color:#cbd9ed;background:#22334b;border:1px solid #496586;border-radius:8px;font-weight:600;}
QTabBar::tab:hover {background:#335780;color:#ffffff;border-color:#89baff;}
QTabBar::tab:selected {color:#ffffff;background:#286ed4;border:1px solid #78b4ff;}
QTabBar::tab:selected:hover {background:#357de3;border-color:#acd0ff;}
QPushButton {background:#223754;color:#9cc8ff;border:1px solid #345173;border-radius:6px;padding:9px 12px;}
QPushButton:hover {background:#2b476b;border-color:#579dff;color:#e0eeff;}
QPushButton:pressed {background:#1a2e49;}
QPushButton:disabled {background:#202b3c;color:#65768d;border-color:#2d394a;}
QPushButton:focus {border-color:#78b4ff;}
QLineEdit,QPlainTextEdit,QSpinBox,QComboBox {background:#111b2b;color:#e0e9f5;border:1px solid #35445b;border-radius:5px;padding:7px;placeholder-text-color:#8495ad;}
QLineEdit:focus,QPlainTextEdit:focus,QSpinBox:focus,QComboBox:focus {border-color:#579dff;}
QComboBox QAbstractItemView {background:#202d41;color:#e0e9f5;border:1px solid #405674;selection-background-color:#294e7d;selection-color:#ffffff;}
QTableWidget {background:#141e2d;alternate-background-color:#192639;color:#dbe5f3;border:1px solid #2c3c52;gridline-color:#26354a;selection-background-color:#294e7d;selection-color:#ffffff;}
QTableWidget::item {padding:4px;}
QHeaderView,QHeaderView::section {background:#223047;color:#aebfd6;}
QHeaderView::section {padding:10px;border:0;border-bottom:1px solid #33445e;}
QTableCornerButton::section {background:#223047;border:0;}
QScrollArea {background:#182233;border:0;}
QScrollBar:vertical {background:#141e2d;width:12px;margin:0;}
QScrollBar:horizontal {background:#141e2d;height:12px;margin:0;}
QScrollBar::handle:vertical {background:#40516b;min-height:28px;border-radius:5px;margin:2px;}
QScrollBar::handle:horizontal {background:#40516b;min-width:28px;border-radius:5px;margin:2px;}
QScrollBar::handle:hover {background:#58749a;}
QScrollBar::add-line,QScrollBar::sub-line {width:0;height:0;}
QScrollBar::add-page,QScrollBar::sub-page {background:transparent;}
QStatusBar {background:#111827;color:#91a3bc;}
QStatusBar::item {border:0;}
QToolTip {background:#263750;color:#edf4ff;border:1px solid #49617f;padding:6px;}
QDialog,QMessageBox,QInputDialog {background:#182233;color:#dbe5f3;}
'''

STYLE += '''
QWidget#page {background:#182233;border-radius:10px;}
QLabel#eyebrow {font-size:12px;color:#8094b0;}
QLabel#title {font-size:28px;font-weight:700;}
QLabel#muted {color:#8fa2bc;font-size:12px;}
QLabel#badge {color:#77d6b7;background:#19332f;border:1px solid #2b5148;border-radius:14px;padding:7px 14px;font-size:12px;}
QWidget#statCard {background:#1b283b;border:1px solid #2c3c53;border-radius:10px;}
QLabel#statValue {font-size:30px;font-weight:600;color:#edf4ff;}
QLabel#statValue[status="pending"] {color:#80baff;}
QLabel#statValue[status="processing"] {color:#b7a2ff;}
QLabel#statValue[status="sent"] {color:#62d7b0;}
QLabel#statValue[status="failed"] {color:#f48b96;}
QLabel#statValue[status="skipped"] {color:#9ba9bd;}
QLabel#sectionTitle {font-size:17px;font-weight:600;color:#eef4fc;}

QTabWidget::tab-bar {alignment:left;}
QTabWidget#browserTabs {background:#182233;border:0;}
QTabWidget#browserTabs::pane {background:#182233;border:1px solid #2c394d;top:-1px;}
QTabWidget#browserTabs QTabBar {background:#182233;border:0;}
QPushButton {padding:10px 16px;}
QPushButton[variant="primary"] {background:#397ddd;color:#ffffff;border:1px solid #4c8be7;}
QPushButton[variant="primary"]:hover {background:#4a8be8;}
QPushButton[variant="danger"] {background:#302732;color:#eaa0aa;border:1px solid #543644;}
QPushButton[variant="danger"]:hover {background:#49303e;}
QGroupBox {background:#182233;border:1px solid #304057;border-radius:8px;margin-top:12px;font-weight:600;}
QGroupBox::title {subcontrol-origin:margin;subcontrol-position:top left;left:16px;padding:0 7px;color:#a8c8f3;}
QLineEdit,QComboBox,QSpinBox {padding:9px;}
'''

STYLE += '''
QPushButton {font-weight:600;border:1px solid #54739a;}
QPushButton:hover {border:1px solid #90bfff;background:#31547b;}
QPushButton:pressed {background:#193653;border-color:#78b4ff;}
QPushButton:disabled,QPushButton[variant="primary"]:disabled,QPushButton[variant="danger"]:disabled {background:#202b3c;color:#718097;border:1px solid #364255;}
'''

STYLE += '''
QPushButton:checked {background:#196149;color:#edfff8;border:1px solid #58c99e;}
QPushButton:checked:hover {background:#237a5d;}
QScrollArea#sidebar,QWidget#sidebarPanel {background:#182233;border:0;border-radius:10px;}
QLabel#sidebarTitle {font-size:19px;font-weight:700;color:#f1f5fb;}
QPushButton#navigationButton {text-align:left;padding:10px 12px;background:transparent;border:1px solid transparent;color:#aebfd6;}
QPushButton#navigationButton:hover {background:#223754;color:#ffffff;}
QPushButton#navigationButton:checked {background:#286ed4;border-color:#4c8be7;color:#ffffff;}
QPushButton#workspaceControl {padding:6px 12px;}
QTabWidget#workspaceTabs::pane {border:0;}
QTabWidget#browserTabs::pane {border:0;}
QTabWidget#browserTabs QTabBar::tab {padding:7px 14px;margin:0 4px 4px 0;border-radius:5px;}
QWidget#embeddedBrowser QPushButton {padding:5px 10px;}
QWidget#embeddedBrowser QLineEdit {padding:5px;}
QLabel#statValue {font-size:24px;}
'''
