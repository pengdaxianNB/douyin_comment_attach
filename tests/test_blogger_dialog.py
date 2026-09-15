import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication,QDialog
from app.db import DB
from app.ui import BloggerDialog


def test_blank_remark_saves(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'db.sqlite3'); d=BloggerDialog(db)
    uid='MS4wLjABAAAAexample_blogger_123456'
    d.url.setText('https://www.douyin.com/user/'+uid+'?from_tab_name=main')
    d.save()
    assert d.result()==QDialog.DialogCode.Accepted
    assert db.rows('SELECT * FROM bloggers')==[{'id':uid,'name':uid}]


def test_validation_preserves_input_and_duplicate_remark(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'db.sqlite3'); d=BloggerDialog(db)
    d.url.setText('wrong link');d.name.setText('备注');d.save()
    assert d.url.text()=='wrong link' and d.name.text()=='备注'
    assert d.error.text() and not db.rows('SELECT * FROM bloggers')
    uid='MS4wLjABAAAAexample_blogger_123456'
    d.url.setText('https://www.douyin.com/user/'+uid);d.save()
    other=BloggerDialog(db);other.url.setText(d.url.text());other.name.setText('新的备注');other.save()
    assert '已在监控列表' in other.error.text()
    assert other.result()!=QDialog.DialogCode.Accepted
    assert db.rows('SELECT * FROM bloggers')[0]['name']=='备注'
