import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from app.db import DB
from app.ui import Window
from types import SimpleNamespace
from unittest.mock import Mock


def test_cluster_controls_limit_collection_and_exclude_other_mode(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'db.sqlite3')
    ids=[db.account(f'账号{i}') for i in range(3)]
    db.save_settings({'base_url':'https://example.com/v1','model':'test','collection_accounts':2})
    w=Window(db,tmp_path,start_engine=False); w.engine.ready.set()
    calls=[]; opened=[]; errors=[]
    w.ensure_browser=lambda account,focus=False,activate=True:(opened.append(account['id']) or SimpleNamespace(suspend_page=Mock(),set_task_status=Mock()))
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


def test_selected_account_can_start_messages_without_other_accounts(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'db.sqlite3')
    ids=[db.account(f'账号{i}') for i in range(3)]
    db.save_settings({'base_url':'https://example.com/v1','model':'test'})
    w=Window(db,tmp_path,start_engine=False); w.engine.ready.set()
    opened=[]; calls=[]
    w.ensure_browser=lambda account,focus=False,activate=True:(opened.append(account['id']) or SimpleNamespace(suspend_page=Mock(),set_task_status=Mock()))
    def command(op,payload):
        calls.append((op,payload))
        w.engine.cluster_mode=payload['mode']
        w.engine.running_accounts.update(a['id'] for a in
            w.engine.cluster_accounts(payload['mode'],payload.get('account_ids')))
    w.engine.command=command
    w.accounts.selectRow(1)
    w.tabs.setCurrentIndex(1)

    w.single_message_button.click()

    assert opened==[ids[1]]
    assert calls==[('start_cluster',{'mode':'messages','account_ids':[ids[1]]})]
    assert w.engine.running_accounts=={ids[1]}
    assert w.tabs.currentWidget() is w.browser_page
    assert w.mode_switches['messages'].isChecked()
    assert '1 个账号运行中' in w.mode_switches['messages'].text()
    assert not w.single_message_button.isEnabled()
    assert not w.mode_switches['comments'].isEnabled()
    w.close(); app.processEvents()


def test_single_account_start_requires_selection(tmp_path,monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'db.sqlite3'); db.account('账号')
    db.save_settings({'base_url':'https://example.com/v1','model':'test'})
    w=Window(db,tmp_path,start_engine=False); w.engine.ready.set()
    commands=[]; warnings=[]
    w.engine.command=lambda *args:commands.append(args)
    monkeypatch.setattr(QMessageBox,'warning',lambda *args:warnings.append(args[2]))

    w.single_message_button.click()

    assert not commands
    assert not w.browser_widgets
    assert warnings and '请先选择' in warnings[0]
    w.close(); app.processEvents()


def test_starting_all_senders_creates_sleeping_tabs_and_suspends_existing_page(tmp_path):
    from app.embedded_browser import page_id
    from shiboken6 import isValid
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'db.sqlite3')
    ids=[db.account(str(i)) for i in range(10)]
    db.save_settings({'base_url':'https://example.com/v1','model':'test'})
    w=Window(db,tmp_path,start_engine=False); w.timer.stop();w.engine.ready.set()
    account=w.account_rows[0]
    existing=w.ensure_browser(account,False); old_page=existing.view.page()
    w.engine.command=Mock()
    try:
        w.toggle_cluster('messages',True)
        assert set(w.browser_widgets)==set(ids)
        assert all(widget.view is None for widget in w.browser_widgets.values())
        assert all(page_id(ident) is None for ident in ids)
        assert not isValid(old_page)
        w.engine.command.assert_called_once_with('start_cluster',{'mode':'messages'})
        assert w.fields['message_concurrency'].value()==3
        w.fields['message_concurrency'].setValue(2)
        w.save_settings()
        assert db.setting('message_concurrency')==2
    finally:
        w.close();app.processEvents()
