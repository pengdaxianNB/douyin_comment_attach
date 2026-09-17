import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
from pathlib import Path
from PySide6.QtWidgets import QApplication
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
        assert w.customers.rowCount()==0
        db.import_customers([{'uid':'MS4wLjABAAAAexample_123456','name':'示例客户','text':'我想咨询一下价格',
                              'comment_time':'2026-09-14T08:00:00+00:00'}])
        w.refresh_customers()
        assert w.customers.rowCount()==1
        assert w.customers.item(0,3).text()=='2026-09-14 16:00:00'
        w.filter.setCurrentIndex(3)
        assert w.customers.rowCount()==0
        w.close(); app.processEvents()
