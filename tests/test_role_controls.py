import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from app.db import DB
from app.ui import Window
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from PySide6.QtCore import Qt


def test_cluster_controls_limit_collection_and_exclude_other_mode(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'db.sqlite3')
    ids=[db.account(f'账号{i}') for i in range(3)]
    db.save_settings({'base_url':'https://example.com/v1','model':'test','collection_accounts':2})
    w=Window(db,tmp_path,start_engine=False); w.engine.ready.set()
    calls=[]; opened=[]; errors=[]
    w.ensure_browser=lambda account,focus=False,activate=True:(opened.append(account['id']) or SimpleNamespace(park_page=Mock(),set_task_status=Mock()))
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


@pytest.mark.parametrize('mode',['comments','messages'])
@pytest.mark.parametrize('chosen_rows',[[1],[0,2]])
def test_checked_accounts_can_start_without_other_accounts(tmp_path,mode,chosen_rows):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'db.sqlite3')
    ids=[db.account(f'账号{i}') for i in range(3)]
    db.save_settings({'base_url':'https://example.com/v1','model':'test'})
    w=Window(db,tmp_path,start_engine=False); w.engine.ready.set()
    opened=[]; calls=[]
    w.ensure_browser=lambda account,focus=False,activate=True:(opened.append(account['id']) or SimpleNamespace(park_page=Mock(),set_task_status=Mock()))
    def command(op,payload):
        calls.append((op,payload))
        w.engine.cluster_mode=payload['mode']
        w.engine.running_accounts.update(a['id'] for a in
            w.engine.cluster_accounts(payload['mode'],payload.get('account_ids')))
    w.engine.command=command
    chosen=[ids[i] for i in chosen_rows]
    w.accounts.selectRow(0)
    for i in chosen_rows:
        w.accounts.item(i,0).setCheckState(Qt.CheckState.Checked)
    w.tabs.setCurrentIndex(1)

    w.selected_mode_buttons[mode].click()

    assert opened==chosen
    assert calls==[('start_cluster',{'mode':mode,'account_ids':chosen})]
    assert w.engine.running_accounts==set(chosen)
    assert w.tabs.currentWidget() is w.browser_page
    assert w.mode_switches[mode].isChecked()
    assert f'{len(chosen)} 个账号运行中' in w.mode_switches[mode].text()
    assert all(not b.isEnabled() for b in w.selected_mode_buttons.values())
    other='comments' if mode=='messages' else 'messages'
    assert not w.mode_switches[other].isEnabled()
    w.close(); app.processEvents()


@pytest.mark.parametrize('mode',['comments','messages'])
def test_selected_account_start_requires_checks_even_with_highlight(tmp_path,monkeypatch,mode):
    from PySide6.QtWidgets import QMessageBox
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'db.sqlite3'); db.account('账号')
    db.save_settings({'base_url':'https://example.com/v1','model':'test'})
    w=Window(db,tmp_path,start_engine=False); w.engine.ready.set()
    commands=[]; warnings=[]
    w.engine.command=lambda *args:commands.append(args)
    monkeypatch.setattr(QMessageBox,'warning',lambda *args:warnings.append(args[2]))
    w.accounts.selectRow(0)

    w.selected_mode_buttons[mode].click()

    assert not commands
    assert not w.browser_widgets
    assert warnings and '请先勾选' in warnings[0]
    w.close(); app.processEvents()


def test_account_checks_survive_refresh_and_follow_account_ids(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=DB(tmp_path/'db.sqlite3')
    ids=[db.account(f'账号{i}') for i in range(3)]
    w=Window(db,tmp_path,start_engine=False)
    w.accounts.selectRow(1)
    w.accounts.item(1,0).setCheckState(Qt.CheckState.Checked)
    assert w.account_select_all.checkState()==Qt.CheckState.PartiallyChecked
    w.account_status(ids[1],'正在找评论')
    assert [r['id'] for r in w.checked_account_rows()]==[ids[1]]
    assert w.accounts.item(1,0).checkState()==Qt.CheckState.Checked
    db.execute('DELETE FROM accounts WHERE id=?',(ids[0],))
    w.refresh_accounts()
    assert w.account_rows[w.accounts.currentRow()]['id']==ids[1]
    assert w.accounts.item(0,0).checkState()==Qt.CheckState.Checked
    w.account_select_all.click()
    assert {r['id'] for r in w.checked_account_rows()}==set(ids[1:])
    assert w.account_select_all.checkState()==Qt.CheckState.Checked
    w.account_select_all.click()
    assert not w.checked_account_rows()
    w.accounts.item(0,0).setCheckState(Qt.CheckState.Checked)
    db.execute('DELETE FROM accounts WHERE id=?',(ids[1],))
    w.refresh_accounts()
    assert not w.checked_account_rows() and not w._checked_account_ids
    assert w.accounts.currentRow()==-1
    db.execute('DELETE FROM accounts')
    w.refresh_accounts()
    assert not w.account_select_all.isEnabled()
    assert all(not b.isEnabled() for b in w.selected_mode_buttons.values())
    w.close();app.processEvents()


def test_starting_all_senders_keeps_all_pages_on_personal_page(tmp_path):
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
        with patch('app.embedded_browser.EmbeddedBrowser.navigate') as navigate:
            w.toggle_cluster('messages',True)
        from app.constants import IDLE_URL
        assert navigate.call_count==10
        assert all(call.args==(IDLE_URL,) for call in navigate.call_args_list)
        assert set(w.browser_widgets)==set(ids)
        assert all(widget.view is not None for widget in w.browser_widgets.values())
        assert all(page_id(ident) is not None for ident in ids)
        assert existing.view.page() is old_page and isValid(old_page)
        w.engine.command.assert_called_once_with('start_cluster',{'mode':'messages'})
        assert w.fields['message_concurrency'].value()==3
        w.fields['message_concurrency'].setValue(2)
        w.save_settings()
        assert db.setting('message_concurrency')==2
    finally:
        w.close();app.processEvents()
