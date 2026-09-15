import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from app.db import DB,user_id
from app.browser import parse_comments

UID='MS4wLjABAAAAexample_customer_123456'

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.db=DB(Path(self.tmp.name)/'test.sqlite3')
    def tearDown(self):self.tmp.cleanup()
    def comment(self,cid='1'):
        return dict(id=cid,uid=UID,name='客户',text='咨询产品价格',comment_time='2026-09-14',video_id='v1',video_title='产品',blogger='博主')
    def test_one_main(self):
        import sqlite3
        self.db.account('main','主号')
        with self.assertRaises(sqlite3.IntegrityError):self.db.account('main','另一个')
        self.db.account('sender','从号一'); self.db.account('sender','从号二')
    def test_dedupe_and_preserve_sent(self):
        self.assertTrue(self.db.ingest(self.comment(),True,'需求明确'))
        self.assertFalse(self.db.ingest(self.comment(),True,'重复'))
        self.db.claim('sender'); self.db.finish(UID,'sender','sent','你好')
        self.db.ingest(self.comment('2'),True,'新增评论')
        self.assertEqual(self.db.rows('SELECT * FROM customers')[0]['status'],'sent')
        self.assertEqual(len(self.db.rows('SELECT * FROM comments')),2)
    def test_atomic_claim(self):
        self.db.ingest(self.comment(),True,'')
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(self.db.claim,[str(n) for n in range(8)]))
        self.assertEqual(sum(r is not None for r in results),1)
    def test_recovery(self):
        self.db.ingest(self.comment(),True,''); self.db.claim('a'); self.db.recover()
        self.assertEqual(self.db.rows('SELECT status FROM customers')[0]['status'],'failed')
        self.assertIsNone(self.db.claim('b'))
    def test_legacy_uncertain_is_migrated_without_requeue(self):
        self.db.import_customers([dict(uid=UID,status='failed')])
        self.db.execute("UPDATE customers SET status='uncertain' WHERE uid=?",(UID,))
        DB(self.db.path)
        row=self.db.rows('SELECT status,error FROM customers WHERE uid=?',(UID,))[0]
        self.assertEqual(row['status'],'failed')
        self.assertIn('请勿重复联系',row['error'])
        self.assertIsNone(self.db.claim('b'))
    def test_import_atomic_and_preserves_status(self):
        self.db.import_customers([dict(uid=UID,status='sent')])
        self.db.import_customers([dict(uid=UID,status='pending')])
        self.assertEqual(self.db.rows('SELECT status FROM customers')[0]['status'],'sent')
        with self.assertRaises(ValueError):self.db.import_customers([dict(uid=UID+'new'),dict(uid='invalid')])
        self.assertEqual(len(self.db.rows('SELECT * FROM customers')),1)
    def test_url(self):
        self.assertEqual(user_id('https://www.douyin.com/user/'+UID+'?a=b'),UID)
        with self.assertRaises(ValueError):user_id('https://evil.com/user/'+UID)
    def test_top_level_only(self):
        base=dict(cid='1',text='咨询',create_time=10,user=dict(sec_uid=UID,nickname='客户'))
        data={'comments':[base,dict(base,cid='2',reply_id='1')]}
        rows=parse_comments(data,{'aweme_id':'vid','desc':'视频'})
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['id'],'1')
    def test_backup(self):
        self.db.ingest(self.comment(),True,''); p=Path(self.tmp.name)/'backup.sqlite3'; self.db.backup(p)
        self.assertEqual(DB(p).rows('SELECT * FROM customers')[0]['uid'],UID)

if __name__=='__main__':unittest.main()
