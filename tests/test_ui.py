import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
from pathlib import Path
from PySide6.QtWidgets import QApplication,QMessageBox
from app.db import DB
from app.ui import Window,STYLE,format_beijing_time


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
        assert w.video_source_tabs.count()==2
        assert w.video_source_tabs.tabText(0)=='视频来源配置'
        assert w.video_source_tabs.tabText(1)=='待分析视频（0）'
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
        assert w.video_source_tabs.tabText(1)=='待分析视频（1）'
        w.video_search.setText('控糖')
        assert w.videos.rowCount()==1
        w.video_search.setText('不存在的标题')
        assert w.videos.rowCount()==0
        assert '显示 0 / 共 1 个' in w.video_summary.text()
        w.video_search.clear()
        assert w.videos.rowCount()==1
        w.close(); app.processEvents()


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
    assert '主号下一轮将重新找视频' in w.statusBar().currentMessage()
    w.close();app.processEvents()


def test_send_attempt_remains_in_processing_metric(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'test.sqlite3')
    uid='MS4wLjABAAAAprocessing_customer_12345'
    account_id=db.account('sender','从号')
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
    account_id=db.account('sender','从号')
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
    account_id=db.account('sender','从号',daily_limit=10)
    db.execute("INSERT INTO events(account_id,uid,kind) VALUES (?,?,'sent')",
               (account_id,'MS4wLjABAAAAtoday_customer_1234567'))
    w=Window(db,tmp_path,start_engine=False)

    assert w.accounts.item(0,3).text()=='1 / 10'
    db.execute("UPDATE events SET at=datetime('now','-1 day') WHERE account_id=?",(account_id,))
    w.refresh_accounts()
    assert w.accounts.item(0,3).text()=='0 / 10'
    w.close();app.processEvents()
