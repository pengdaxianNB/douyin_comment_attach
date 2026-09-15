import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from app.db import DB
from app.ui import Window


def test_role_controls_are_independent(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'db.sqlite3')
    main=db.account('main','主号'); senders={db.account('sender','从号1'),db.account('sender','从号2')}
    db.save_settings({'base_url':'https://example.com/v1','model':'test'})
    w=Window(db,tmp_path,start_engine=False);w.engine.ready.set()
    calls=[]
    def command(op,account):
        calls.append((op,account['id']))
        if op=='start':w.engine.running_accounts.add(account['id'])
        else:w.engine.running_accounts.discard(account['id'])
    w.engine.command=command
    w.toggle_role('main',True)
    assert calls==[('start',main)]
    w.toggle_role('sender',True)
    assert {ident for op,ident in calls[1:]}==senders
    w.toggle_role('main',False)
    assert w.engine.running_accounts==senders
    assert not w.role_switches['main'].isChecked()
    assert w.role_switches['sender'].isChecked()
    w.close()
