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
