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
    assert w.videos.item(0,1).text()=='手动录入'
    assert w.videos.item(0,2).text()=='手动录入标题'
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
    assert w.videos.item(0,5).text()=='7690537643185017257'
    assert db.video_tasks()[0]['url']=='https://www.douyin.com/note/7690537643185017257'
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
