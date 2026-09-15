import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
from pathlib import Path
from PySide6.QtWidgets import QApplication
from app.db import DB
from app.ui import Window,STYLE


def test_window():
    app=QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    with tempfile.TemporaryDirectory() as folder:
        db=DB(Path(folder)/'test.sqlite3')
        w=Window(db,folder,start_engine=False)
        w.show(); app.processEvents()
        assert w.tabs.count()==6
        assert w.fields['base_url'].text()==''
        assert w.customers.rowCount()==0
        db.import_customers([{'uid':'MS4wLjABAAAAexample_123456','name':'示例客户','text':'我想咨询一下价格'}])
        w.refresh_customers()
        assert w.customers.rowCount()==1
        w.filter.setCurrentIndex(3)
        assert w.customers.rowCount()==0
        w.close(); app.processEvents()
