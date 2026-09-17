import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import pytest

from app.db import DB
from app.engine import Engine
from app.browser import NeedsAttention


def test_completed_send_is_marked_sent():
    uid = 'MS4wLjABAAAAexample_customer_123456'
    with TemporaryDirectory() as folder:
        db = DB(Path(folder) / 'test.sqlite3')
        account_id = db.account('sender', '从号')
        db.save_settings({'base_url': 'https://example.com/v1', 'model': 'test'})
        db.ingest(dict(id='1', uid=uid, name='客户', text='咨询价格',
                       comment_time='', video_id='v1', video_title='', blogger=''), True, '')
        engine = Engine(db, folder)
        engine.jobs = {}
        browser = AsyncMock()

        async def send(customer, message, before_click):
            before_click()
            assert db.rows('SELECT status FROM customers WHERE uid=?', (uid,))[0]['status'] == 'failed'

        browser.send.side_effect = send
        engine.open = AsyncMock(return_value=browser)
        with patch('app.engine.get_key', return_value='key'), \
             patch('app.engine.AI') as ai, \
             patch('app.engine.asyncio.sleep', side_effect=asyncio.CancelledError):
            ai.return_value.message = AsyncMock(return_value='你好')
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(engine.work(dict(id=account_id, role='sender', name='从号')))

        row = db.rows('SELECT status,message,error,sent_at FROM customers WHERE uid=?', (uid,))[0]
        assert row['status'] == 'sent'
        assert row['message'] == '你好'
        assert row['error'] == ''
        assert row['sent_at']


def test_customer_page_error_does_not_stop_sender():
    first_uid = 'MS4wLjABAAAAfirst_customer_1234567'
    second_uid = 'MS4wLjABAAAAsecond_customer_123456'
    with TemporaryDirectory() as folder:
        db = DB(Path(folder) / 'test.sqlite3')
        account_id = db.account('sender', '从号')
        db.save_settings({'base_url': 'https://example.com/v1', 'model': 'test'})
        for index, uid in enumerate((first_uid, second_uid), 1):
            db.ingest(dict(id=str(index), uid=uid, name=f'客户{index}', text='咨询',
                           comment_time='', video_id='v1', video_title='', blogger=''), True, '')
            db.execute('UPDATE customers SET updated=? WHERE uid=?', (index, uid))

        engine = Engine(db, folder)
        engine.jobs = {}
        browser = AsyncMock()
        browser.send.side_effect = [NeedsAttention('消息侧栏关闭失败'), None]
        engine.open = AsyncMock(return_value=browser)
        logs = []
        engine.log.connect(logs.append)

        with patch('app.engine.get_key', return_value='key'), \
             patch('app.engine.AI') as ai, \
             patch('app.engine.asyncio.sleep', side_effect=[None, asyncio.CancelledError]):
            ai.return_value.message = AsyncMock(return_value='你好')
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(engine.work(dict(id=account_id, role='sender', name='从号')))

        rows = {row['uid']: row for row in db.rows(
            'SELECT uid,status,error FROM customers')}
        assert rows[first_uid]['status'] == 'failed'
        assert '消息侧栏关闭失败' in rows[first_uid]['error']
        assert rows[second_uid]['status'] == 'sent'
        assert browser.send.await_count == 2
        assert any('从号将继续运行' in line for line in logs)
