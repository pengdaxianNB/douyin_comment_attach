import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from app.db import DB
from app.ui import Window


def test_cluster_controls_limit_collection_and_exclude_other_mode(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'db.sqlite3')
    ids=[db.account(f'账号{i}') for i in range(3)]
    db.save_settings({'base_url':'https://example.com/v1','model':'test','collection_accounts':2})
    w=Window(db,tmp_path,start_engine=False); w.engine.ready.set()
    calls=[]; opened=[]; errors=[]
    w.ensure_browser=lambda account,focus=False:opened.append(account['id'])
    def command(op,payload):
        calls.append((op,payload['mode']))
        if op=='start_cluster':
            w.engine.cluster_mode=payload['mode']
            w.engine.running_accounts.update(a['id'] for a in w.engine.cluster_accounts(payload['mode']))
        else:
            w.engine.cluster_mode=None
            w.engine.running_accounts.clear()
    w.engine.command=command
    def report(action):
        try: action()
        except ValueError as exc: errors.append(str(exc))
    w.report=report
    w.toggle_cluster('comments',True)
    assert opened==ids[:2]
    assert w.mode_switches['comments'].isChecked()
    assert not w.mode_switches['messages'].isEnabled()
    w.toggle_cluster('messages',True)
    assert len(calls)==1 and errors
    w.toggle_cluster('comments',False)
    w.toggle_cluster('messages',True)
    assert w.engine.running_accounts==set(ids)
    assert w.mode_switches['messages'].isChecked()
    assert not w.mode_switches['comments'].isEnabled()
    w.close(); app.processEvents()


def test_collection_count_tracks_account_count(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'db.sqlite3')
    w=Window(db,tmp_path,start_engine=False)
    assert not w.fields['collection_accounts'].isEnabled()
    ids=[db.account(str(i)) for i in range(3)]
    w.refresh_accounts()
    assert w.fields['collection_accounts'].maximum()==3
    w.fields['collection_accounts'].setValue(3)
    db.save_settings({'collection_accounts':3})
    db.execute('DELETE FROM accounts WHERE id=?',(ids[0],))
    w.refresh_accounts()
    assert w.fields['collection_accounts'].maximum()==2
    assert w.fields['collection_accounts'].value()==2
    assert db.setting('collection_accounts')==2
    w.close(); app.processEvents()
