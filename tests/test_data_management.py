import pytest
from app.db import DB


def seed(tmp_path):
    db=DB(tmp_path/'db.sqlite3')
    db.account('main','主号')
    db.save_settings({'model':'test'})
    for i in range(105):
        db.ingest(dict(id=str(i),uid='MS4wLjABAAAAcustomer_'+str(i),name='客户',text='需求',comment_time='',video_id='v',video_title='',blogger=''),True,'需求')
    return db


def test_paging_and_search(tmp_path):
    db=seed(tmp_path)
    rows,total,page=db.data_page('comments',page=1)
    assert len(rows)==5 and total==105 and page==1
    assert db.data_page('customers',search='customer_104')[1]==1
    assert db.data_page('customers',search='%')[1]==0


def test_selected_delete_is_scoped(tmp_path):
    db=seed(tmp_path)
    assert db.delete_data('comments',['0','1'])==2
    assert db.data_page('comments')[1]==103
    assert db.data_page('customers')[1]==105
    assert db.delete_data('customers',['MS4wLjABAAAAcustomer_0'])==1
    assert db.data_page('customers')[1]==104


def test_clear_and_backup_preserve_config(tmp_path):
    db=seed(tmp_path)
    db.execute("INSERT INTO events(kind,detail) VALUES ('sent','test')")
    db.backup(tmp_path/'backup.sqlite3')
    assert db.delete_data('all')==211
    assert all(db.data_page(t)[1]==0 for t in ('comments','customers','events'))
    assert db.setting('model')=='test'
    assert len(db.rows('SELECT * FROM accounts'))==1
    assert DB(tmp_path/'backup.sqlite3').data_page('customers')[1]==105


def test_active_customer_blocks_delete(tmp_path):
    db=seed(tmp_path); db.claim('a')
    with pytest.raises(ValueError): db.delete_data('all')
    assert db.data_page('comments')[1]==105
