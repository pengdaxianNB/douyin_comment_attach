import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from app.db import DB,user_id,video_id
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
    def test_legacy_accounts_gain_unlimited_daily_limit(self):
        import sqlite3
        path=Path(self.tmp.name)/'legacy.sqlite3'
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE accounts(id TEXT PRIMARY KEY,role TEXT,name TEXT,interval INTEGER)")
            connection.execute("INSERT INTO accounts VALUES ('sender','sender','旧从号',120)")
        migrated=DB(path)
        self.assertEqual(migrated.rows("SELECT daily_limit FROM accounts WHERE id='sender'"),[{'daily_limit':0}])

    def test_legacy_video_table_gains_manual_source_support(self):
        import sqlite3
        path=Path(self.tmp.name)/'legacy-videos.sqlite3'
        with sqlite3.connect(path) as connection:
            connection.execute('''CREATE TABLE videos(
                id TEXT PRIMARY KEY,title TEXT NOT NULL DEFAULT '',author TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL CHECK(source_type IN ('blogger','keyword')),
                source_name TEXT NOT NULL DEFAULT '',source_ref TEXT NOT NULL DEFAULT '',
                create_time INTEGER NOT NULL DEFAULT 0,url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','scanning','done','failed')),
                error TEXT NOT NULL DEFAULT '',discovered_at REAL NOT NULL,scanned_at REAL)''')
        migrated=DB(path)
        migrated.add_manual_video('https://www.douyin.com/video/1234567890123456789')
        self.assertEqual(migrated.video_tasks()[0]['source_type'],'manual')
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
    def test_sender_daily_limit_resets_on_beijing_date(self):
        sender=self.db.account('sender','从号',daily_limit=1)
        second=UID+'second'
        self.db.import_customers([dict(uid=UID),dict(uid=second)])
        first=self.db.claim(sender)
        self.db.finish(first['uid'],sender,'sent','你好')
        self.db.execute("INSERT INTO events(account_id,uid,kind) VALUES (?,?, 'sent')",(sender,first['uid']))
        self.assertEqual(self.db.sender_quota(sender),{'used':1,'limit':1})
        self.assertIsNone(self.db.claim(sender))
        self.db.execute("UPDATE events SET at=datetime('now','-1 day') WHERE account_id=?",(sender,))
        self.assertEqual(self.db.sender_quota(sender),{'used':0,'limit':1})
        remaining = second if first['uid']==UID else UID
        self.assertEqual(self.db.claim(sender)['uid'],remaining)
    def test_uncertain_send_attempt_counts_toward_daily_limit(self):
        sender=self.db.account('sender','从号',daily_limit=1)
        self.db.import_customers([dict(uid=UID)])
        self.db.claim(sender)
        self.db.start_send(UID,sender,'你好')
        self.assertEqual(self.db.sender_quota(sender),{'used':1,'limit':1})
        self.assertEqual(self.db.rows('SELECT status FROM customers'),[{'status':'processing'}])
    def test_recovery(self):
        self.db.ingest(self.comment(),True,''); self.db.claim('a'); self.db.recover()
        self.assertEqual(self.db.rows('SELECT status FROM customers')[0]['status'],'failed')
        self.assertIsNone(self.db.claim('b'))
    def test_requeue_failed_only(self):
        second = UID+'second'
        third = UID+'third'
        self.db.import_customers([
            dict(uid=UID,status='failed',message='已生成文案'),
            dict(uid=second,status='failed',message='另一条文案'),
            dict(uid=third,status='sent'),
        ])
        self.db.execute("UPDATE customers SET error='发送失败',sent_at='2026-09-01' WHERE status='failed'")
        self.assertEqual(self.db.requeue_failed(),2)
        rows={r['uid']:r for r in self.db.rows('SELECT uid,status,message,error,sent_at FROM customers')}
        self.assertEqual(rows[UID],dict(uid=UID,status='pending',message='已生成文案',error='',sent_at=''))
        self.assertEqual(rows[second]['status'],'pending')
        self.assertEqual(rows[third]['status'],'sent')
        events=self.db.rows("SELECT uid,kind,detail FROM events WHERE detail='人工批量将失败客户重新排队'")
        self.assertEqual({r['uid'] for r in events},{UID,second})
        self.assertEqual(self.db.requeue_failed(),0)
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
        self.assertEqual(video_id('https://www.douyin.com/video/1234567890123456789?from=web'),
                         '1234567890123456789')
        with self.assertRaises(ValueError):video_id('https://v.douyin.com/example/')
    def test_top_level_only(self):
        base=dict(cid='1',text='咨询',create_time=10,user=dict(sec_uid=UID,nickname='客户'))
        data={'comments':[base,dict(base,cid='2',reply_id='1')]}
        rows=parse_comments(data,{'aweme_id':'vid','desc':'视频'})
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['id'],'1')
    def test_backup(self):
        self.db.ingest(self.comment(),True,''); p=Path(self.tmp.name)/'backup.sqlite3'; self.db.backup(p)
        self.assertEqual(DB(p).rows('SELECT * FROM customers')[0]['uid'],UID)

    def test_video_list_rebuild_then_reuses_links_inside_reset_window(self):
        self.assertTrue(self.db.prepare_video_scan(24,now=1000))
        video=dict(aweme_id='1234567890123456789',desc='测试视频',create_time=900,
                   author={'nickname':'作者'})
        self.db.add_video(video,'keyword','糖尿病逆转','糖尿病逆转')
        self.db.set_video_status(video['aweme_id'],'done')

        self.assertFalse(self.db.prepare_video_scan(24,now=1000+23*3600))
        row=self.db.video_tasks()[0]
        self.assertEqual(row['status'],'pending')
        self.assertEqual(row['title'],'测试视频')

        self.assertTrue(self.db.prepare_video_scan(24,now=1000+24*3600))
        self.assertEqual(self.db.video_tasks(),[])

    def test_video_list_deduplicates_same_work_across_sources(self):
        video=dict(aweme_id='1234567890123456789',desc='同一个视频',author={})
        self.db.add_video(video,'blogger','博主','MS4wLjABAAAAblogger_123456')
        self.db.add_video(video,'keyword','关键词','关键词')
        self.assertEqual(len(self.db.video_tasks()),1)

    def test_clearing_video_list_forces_next_pass_to_rebuild(self):
        video=dict(aweme_id='1234567890123456789',desc='测试视频',author={})
        self.db.prepare_video_scan(24,now=1000)
        self.db.add_video(video,'keyword','关键词')

        self.assertEqual(self.db.clear_videos(),1)
        self.assertEqual(self.db.video_tasks(),[])
        self.assertIsNone(self.db.setting('video_list_built_at'))
        self.assertTrue(self.db.prepare_video_scan(24,now=1001))

    def test_manual_video_is_persistent_source_and_can_be_removed(self):
        ident=self.db.add_manual_video(
            'https://www.douyin.com/video/1234567890123456789?from=copy','手动视频')
        row=self.db.video_tasks()[0]
        self.assertEqual(row['source_type'],'manual')
        self.assertEqual(row['title'],'手动视频')
        self.db.clear_videos()
        self.assertEqual(len(self.db.rows('SELECT * FROM manual_videos')),1)
        self.db.remove_manual_video(ident)
        self.assertEqual(self.db.rows('SELECT * FROM manual_videos'),[])

if __name__=='__main__':unittest.main()
