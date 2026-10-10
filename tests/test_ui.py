import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
from pathlib import Path
import pytest
from PySide6.QtTest import QSignalSpy,QTest
from PySide6.QtWidgets import QApplication,QInputDialog,QMessageBox,QFileDialog,QPushButton
from app.db import DB
from app.ui import Window,STYLE,format_beijing_time


@pytest.mark.parametrize('search',[False,True])
def test_csv_import_button_uses_the_selected_source_and_reports_duplicates(tmp_path,monkeypatch,search):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    db.import_keywords(['旧词'],search=search)
    path=tmp_path/'words.csv'
    path.write_text('关键词\n旧词\n新词\n新词\n\n',encoding='utf-8-sig')
    w=Window(db,tmp_path,start_engine=False)
    messages=[]
    monkeypatch.setattr(QFileDialog,'getOpenFileName',lambda *args:(str(path),''))
    monkeypatch.setattr(QMessageBox,'information',lambda *args:messages.append(args[2]))
    page=w.video_source_tabs.widget(1 if search else 0)
    control=next(b for b in page.findChildren(QPushButton) if b.text()=='批量导入 CSV')
    control.click()

    target=w.search_keywords if search else w.keywords
    other=w.keywords if search else w.search_keywords
    assert target.rowCount()==2
    assert other.rowCount()==0
    assert messages and '新增 1 个' in messages[0] and '跳过 2 个重复词' in messages[0]
    assert '下次视频清单重建时生效' in w.statusBar().currentMessage()
    w.close();app.processEvents()


def test_invalid_csv_import_is_atomic_and_warns_without_refreshing_partial_data(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    path=tmp_path/'invalid.csv'
    path.write_text('关键词\n有效词\n错误,多列\n',encoding='utf-8')
    w=Window(db,tmp_path,start_engine=False)
    warnings=[]
    monkeypatch.setattr(QFileDialog,'getOpenFileName',lambda *args:(str(path),''))
    monkeypatch.setattr(QMessageBox,'warning',lambda *args:warnings.append(args[2]))
    w.import_keywords_csv()

    assert db.rows('SELECT * FROM keywords')==[]
    assert w.keywords.rowCount()==0
    assert warnings and '第 3 行' in warnings[0]
    w.close();app.processEvents()


@pytest.mark.parametrize('tab',[0,1])
def test_both_sources_can_download_the_universal_csv_template(tmp_path,monkeypatch,tab):
    from app.keyword_csv import read_keyword_csv,KEYWORD_CSV_EXAMPLES
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    w=Window(db,tmp_path,start_engine=False)
    path=tmp_path/f'template-{tab}'
    monkeypatch.setattr(QFileDialog,'getSaveFileName',lambda *args:(str(path),''))
    page=w.video_source_tabs.widget(tab)
    control=next(b for b in page.findChildren(QPushButton) if b.text()=='下载 CSV 模板')
    control.click()

    saved=path.with_suffix('.csv')
    assert read_keyword_csv(saved)==list(KEYWORD_CSV_EXAMPLES)
    assert saved.read_bytes().startswith(b'\xef\xbb\xbf')
    assert db.rows('SELECT * FROM keywords')==[]
    assert db.rows('SELECT * FROM search_keywords')==[]
    w.close();app.processEvents()


def test_comment_time_is_displayed_in_beijing_time():
    assert format_beijing_time('2026-09-14T08:00:00+00:00') == '2026-09-14 16:00:00'
    assert format_beijing_time('2026-09-14T16:00:00+08:00') == '2026-09-14 16:00:00'
    assert format_beijing_time('2026-09-14T16:00:00') == '2026-09-14 16:00:00'
    assert format_beijing_time('2026-09-14') == '2026-09-14'
    assert format_beijing_time('') == ''


def test_window():
    app=QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    with tempfile.TemporaryDirectory() as folder:
        db=DB(Path(folder)/'test.sqlite3')
        w=Window(db,folder,start_engine=False)
        w.show(); app.processEvents()
        assert w.tabs.count()==7
        assert any(w.tabs.tabText(i)=='内嵌浏览器' for i in range(w.tabs.count()))
        assert w.video_source_tabs.count()==4
        assert w.video_source_tabs.tabText(0)=='抖音指数找视频'
        assert w.video_source_tabs.tabText(1)=='抖音搜索找视频'
        assert w.video_source_tabs.tabText(2)=='指定博主找视频'
        assert w.video_source_tabs.tabText(3)=='待分析视频（0）'
        assert w.fields['base_url'].text()==''
        assert w.fields['comment_days'].value()==7
        assert w.customers.rowCount()==0
        db.import_customers([{'uid':'MS4wLjABAAAAexample_123456','name':'示例客户','text':'我想咨询一下价格',
                              'comment_time':'2026-09-14T08:00:00+00:00'}])
        w.refresh_customers()
        assert w.customers.rowCount()==1
        assert w.customers.item(0,3).text()=='2026-09-14 16:00:00'
        w.filter.setCurrentIndex(3)
        assert w.customers.rowCount()==0
        db.add_video({'aweme_id':'1234567890123456789','desc':'七天控糖方法','author':{}},
                     'keyword','控糖')
        w.refresh_videos()
        assert w.videos.rowCount()==1
        assert w.video_source_tabs.tabText(3)=='待分析视频（1）'
        w.video_search.setText('控糖')
        assert w.videos.rowCount()==1
        w.video_search.setText('不存在的标题')
        assert w.videos.rowCount()==0
        assert '显示 0 / 共 1 个' in w.video_summary.text()
        w.video_search.clear()
        assert w.videos.rowCount()==1
        w.close(); app.processEvents()


def test_browser_workspace_uses_full_height_and_sidebar_can_be_restored(tmp_path):
    app=QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    db=DB(tmp_path/'test.sqlite3'); db.account('测试账号')
    w=Window(db,tmp_path,start_engine=False)
    widget=w.ensure_browser(w.account_rows[0])
    w.show(); app.processEvents()

    assert w.tabs.currentWidget() is w.browser_page
    assert not w.tabs.tabBar().isVisible()
    assert w.sidebar.isVisible()
    assert widget.view.height()>w.centralWidget().height()*0.8
    initial_width=widget.view.width()
    assert w.sidebar.geometry().right()<w.tabs.geometry().left()
    w.sidebar_toggle.click(); app.processEvents()
    assert w.sidebar.isHidden()
    assert widget.view.width()>initial_width+200
    assert widget.view.width()>w.centralWidget().width()*0.95
    assert w.sidebar_toggle.text()=='显示左栏'
    w.sidebar_toggle.click(); app.processEvents()
    assert w.sidebar.isVisible()
    assert abs(widget.view.width()-initial_width)<5
    w.navigation_buttons[w.tabs.currentIndex()].click()
    assert w.navigation_buttons[w.tabs.currentIndex()].isChecked()

    # Function pages remain available from the left; focusing the browser
    # restores the same page/profile and expands it without restarting it.
    w.navigation_buttons[1].click(); app.processEvents()
    assert w.tabs.currentIndex()==1
    original_page=widget.view.page()
    w.browser_focus.click(); app.processEvents()
    assert w.sidebar.isHidden()
    assert w.tabs.currentWidget() is w.browser_page
    assert widget.view.page() is original_page
    assert widget.view.page().isVisible()
    assert w.navigation_buttons[w.tabs.currentIndex()].isChecked()
    w.close(); app.processEvents()


def test_runtime_change_bursts_are_coalesced(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    w=Window(db,tmp_path,start_engine=False)
    w.timer.stop()
    spy=QSignalSpy(w.refresh_timer.timeout)

    for _ in range(50):
        w.schedule_runtime_refresh()
    QTest.qWait(400);app.processEvents()

    assert spy.count()==1
    w.close();app.processEvents()


def test_requeue_all_failed_from_customer_toolbar(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    failed='MS4wLjABAAAAfailed_customer_123456'
    sent='MS4wLjABAAAAsent_customer_1234567'
    db.import_customers([dict(uid=failed,name='失败客户',status='failed'),dict(uid=sent,name='已私信客户',status='sent')])
    db.execute("UPDATE customers SET error='网络异常' WHERE uid=?",(failed,))
    w=Window(db,tmp_path,start_engine=False)
    questions=[]
    monkeypatch.setattr(QMessageBox,'question',lambda *args:(questions.append(args[2]) or QMessageBox.StandardButton.Yes))
    w.requeue_failed()
    rows={r['uid']:r for r in db.rows('SELECT uid,status,error FROM customers')}
    assert rows[failed]['status']=='pending' and rows[failed]['error']==''
    assert rows[sent]['status']=='sent'
    assert questions and '全部 1 位失败客户' in questions[0] and '避免重复私信' in questions[0]
    assert '已将 1 位失败客户重新排队' in w.statusBar().currentMessage()
    w.close(); app.processEvents()


def test_clear_video_list_button_forces_source_rebuild(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    db.prepare_video_scan(24,now=1000)
    db.add_video({'aweme_id':'1234567890123456789','desc':'待清空视频','author':{}},
                 'keyword','控糖')
    w=Window(db,tmp_path,start_engine=False)
    monkeypatch.setattr(QMessageBox,'question',lambda *args:QMessageBox.StandardButton.Yes)

    w.clear_videos()

    assert db.video_tasks()==[]
    assert db.setting('video_list_built_at') is None
    assert w.videos.rowCount()==0
    assert '集群下一轮将重新找视频' in w.statusBar().currentMessage()
    w.close();app.processEvents()


def test_manual_video_can_be_added_and_removed_from_video_page(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    w=Window(db,tmp_path,start_engine=False)
    answers=iter([
        ('https://www.douyin.com/video/1234567890123456789',True),
        ('手动录入标题',True),
    ])
    monkeypatch.setattr(QInputDialog,'getText',lambda *args,**kwargs:next(answers))

    w.add_manual_video()

    assert w.videos.rowCount()==1
    assert w.videos.item(0,2).text()=='手动录入'
    assert w.videos.item(0,3).text()=='手动录入标题'
    assert w.remove_manual_video_button.isEnabled()
    monkeypatch.setattr(QMessageBox,'question',lambda *args:QMessageBox.StandardButton.Yes)
    w.remove_manual_video()
    assert w.videos.rowCount()==0
    assert db.rows('SELECT * FROM manual_videos')==[]
    w.close();app.processEvents()


def test_manual_note_link_can_be_added(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    w=Window(db,tmp_path,start_engine=False)
    answers=iter([('https://www.douyin.com/note/7690537643185017257',True),('',True)])
    monkeypatch.setattr(QInputDialog,'getText',lambda *args,**kwargs:next(answers))

    w.add_manual_video()

    assert w.videos.rowCount()==1
    assert w.videos.item(0,6).text()=='7690537643185017257'
    assert db.video_tasks()[0]['url']=='https://www.douyin.com/note/7690537643185017257'
    w.close();app.processEvents()


@pytest.mark.parametrize('confirm',[True,False])
def test_video_multiselect_delete_uses_filtered_ids_and_preserves_selection(tmp_path,monkeypatch,confirm):
    from PySide6.QtCore import Qt
    from PySide6.QtCore import QItemSelectionModel
    from PySide6.QtWidgets import QAbstractItemView
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    manual=db.add_manual_video('https://www.douyin.com/video/1234567890123456789','选中手动视频')
    db.add_video({'aweme_id':'2','desc':'选中自动视频'},'search','词')
    db.add_video({'aweme_id':'3','desc':'保留视频'},'blogger','博主')
    w=Window(db,tmp_path,start_engine=False)
    w.video_search.setText('选中')
    assert w.videos.selectionMode()==QAbstractItemView.SelectionMode.ExtendedSelection
    selection=w.videos.selectionModel()
    flags=QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    for i in range(2):
        selection.select(w.videos.model().index(i,0),flags)
        w.videos.item(i,0).setCheckState(Qt.CheckState.Checked)
    assert w.delete_videos_button.isEnabled()
    # A refresh inserts a new row before the selected rows: selection follows IDs.
    db.add_video({'aweme_id':'4','desc':'选中但保留视频','create_time':100},'search','词')
    w.refresh_videos()
    assert {w.video_rows[index.row()]['id'] for index in selection.selectedRows()}=={manual,'2'}
    assert {r['id'] for r in w.checked_video_rows()}=={manual,'2'}
    assert w.video_select_all.checkState()==Qt.CheckState.PartiallyChecked
    questions=[]
    def question(*args):
        questions.append(args[2])
        return QMessageBox.StandardButton.Yes if confirm else QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox,'question',question)
    w.delete_selected_videos()
    assert questions and '2 个视频' in questions[0]
    assert {r['id'] for r in db.video_tasks()}==({'3','4'} if confirm else {manual,'2','3','4'})
    assert bool(db.rows('SELECT * FROM manual_videos')) is not confirm
    w.close();app.processEvents()


def test_selected_video_delete_requires_stopped_tasks(tmp_path,monkeypatch):
    from PySide6.QtCore import Qt
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    db.add_video({'aweme_id':'1'},'search','词')
    w=Window(db,tmp_path,start_engine=False)
    w.videos.selectRow(0)
    w.videos.item(0,0).setCheckState(Qt.CheckState.Checked)
    w.engine.running_accounts.add('busy')
    warnings=[]
    monkeypatch.setattr(QMessageBox,'warning',lambda *args:warnings.append(args[2]))
    w.delete_selected_videos()
    assert len(db.video_tasks())==1 and warnings and '停止任务' in warnings[0]
    w.engine.running_accounts.clear()
    w.close();app.processEvents()


def test_video_checkbox_select_all_respects_filter_and_keeps_hidden_checks(tmp_path,monkeypatch):
    from PySide6.QtCore import Qt
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    for ident,title in [('1','目标一'),('2','目标二'),('3','其他视频')]:
        db.add_video({'aweme_id':ident,'desc':title},'search','词')
    w=Window(db,tmp_path,start_engine=False)
    assert not w.delete_videos_button.isEnabled()
    # Highlighting a row alone never queues it for deletion.
    w.videos.selectRow(0)
    assert not w.delete_videos_button.isEnabled()
    assert w.videos.item(0,0).flags() & Qt.ItemFlag.ItemIsUserCheckable
    w.videos.item(0,0).setCheckState(Qt.CheckState.Checked)
    assert w.video_select_all.checkState()==Qt.CheckState.PartiallyChecked
    w.video_select_all.click()
    assert len(w.checked_video_rows())==3
    assert w.video_select_all.checkState()==Qt.CheckState.Checked
    w.video_search.setText('目标')
    assert len(w.checked_video_rows())==2
    w.video_select_all.click()
    assert w.checked_video_rows()==[]
    assert not w.delete_videos_button.isEnabled()
    w.video_select_all.click()
    assert {row['id'] for row in w.checked_video_rows()}=={'1','2'}
    monkeypatch.setattr(QMessageBox,'question',lambda *args:QMessageBox.StandardButton.Yes)
    w.delete_selected_videos()
    assert {row['id'] for row in db.video_tasks()}=={'3'}
    assert not w.video_select_all.isEnabled()
    w.video_search.clear()
    assert {row['id'] for row in w.checked_video_rows()}=={'3'}
    assert w.videos.item(0,0).checkState()==Qt.CheckState.Checked
    assert '（1）' in w.delete_videos_button.text()
    w.close();app.processEvents()


def test_send_attempt_remains_in_processing_metric(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    uid='MS4wLjABAAAAprocessing_customer_12345'
    account_id=db.account('从号')
    db.import_customers([dict(uid=uid,name='处理中客户')])
    db.claim(account_id)
    db.start_send(uid,account_id,'你好')
    w=Window(db,tmp_path,start_engine=False)

    assert w.metric_values['processing'].text()=='1'
    assert w.metric_values['failed'].text()=='0'

    w.close(); app.processEvents()


def test_stopped_account_browser_can_be_manually_restarted(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    account_id=db.account('从号')
    w=Window(db,tmp_path,start_engine=False)
    w.engine.ready.set()
    account=w.account_rows[0]
    widget=w.ensure_browser(account,False)
    old_page=widget.view.page()
    commands=[]
    w.engine.command=lambda op,account=None:commands.append((op,account['id']))

    w.restart_account_browser(account_id)

    assert widget.view.page() is not old_page
    assert commands==[('close',account_id),('login',account_id)]
    assert '浏览器已重启' in w.statusBar().currentMessage()
    w.close();app.processEvents()


def test_account_table_shows_today_sent_count_and_limit(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    account_id=db.account('从号',daily_limit=10)
    db.execute("INSERT INTO events(account_id,uid,kind) VALUES (?,?,'sent')",
               (account_id,'MS4wLjABAAAAtoday_customer_1234567'))
    w=Window(db,tmp_path,start_engine=False)

    assert w.accounts.item(0,2).text()=='1 / 10'
    db.execute("UPDATE events SET at=datetime('now','-1 day') WHERE account_id=?",(account_id,))
    w.refresh_accounts()
    assert w.accounts.item(0,2).text()=='0 / 10'
    w.close();app.processEvents()
