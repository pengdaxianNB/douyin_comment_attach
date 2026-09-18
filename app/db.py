from contextlib import contextmanager
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse


def user_id(value):
    value = str(value).strip()
    if value.startswith('https://'):
        u = urlparse(value)
        if u.hostname not in ('www.douyin.com', 'douyin.com'):
            raise ValueError('请输入抖音用户主页链接')
        value = u.path.removeprefix('/user/').rstrip('/')
    if not re.fullmatch(r'MS4[\w-]{10,}', value):
        raise ValueError('需要 /user/ 后面的完整 sec_uid（MS4 开头），不是数字抖音号')
    return value


class DB:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,role TEXT NOT NULL CHECK(role IN ('main','sender')),name TEXT NOT NULL,interval INTEGER NOT NULL DEFAULT 120,daily_limit INTEGER NOT NULL DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS one_main ON accounts(role) WHERE role='main';
            CREATE TABLE IF NOT EXISTS bloggers(id TEXT PRIMARY KEY,name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS comments(id TEXT PRIMARY KEY,uid TEXT NOT NULL,name TEXT,text TEXT,comment_time TEXT,video_id TEXT,video_title TEXT,blogger TEXT,target INTEGER,reason TEXT);
            CREATE TABLE IF NOT EXISTS customers(uid TEXT PRIMARY KEY,name TEXT,text TEXT,comment_time TEXT,video_title TEXT,blogger TEXT,url TEXT,status TEXT NOT NULL DEFAULT 'pending',message TEXT DEFAULT '',account_id TEXT,error TEXT DEFAULT '',sent_at TEXT DEFAULT '',updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at TEXT DEFAULT CURRENT_TIMESTAMP,account_id TEXT,uid TEXT,kind TEXT,detail TEXT);
            ''')
            account_columns = {row['name'] for row in c.execute('PRAGMA table_info(accounts)')}
            if 'daily_limit' not in account_columns:
                c.execute('ALTER TABLE accounts ADD COLUMN daily_limit INTEGER NOT NULL DEFAULT 0')
            c.execute("UPDATE customers SET status='failed',error=CASE WHEN error='' THEN '历史待核验记录，发送结果不明，请勿重复联系' ELSE error END WHERE status='uncertain'")

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def rows(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.connect() as c:
            return c.execute(sql, args).rowcount

    def setting(self, key, default=None):
        r = self.rows('SELECT value FROM settings WHERE key=?', (key,))
        return json.loads(r[0]['value']) if r else default

    def save_settings(self, values):
        with self.connect() as c:
            c.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)', [(k,json.dumps(v,ensure_ascii=False)) for k,v in values.items()])

    def account(self, role, name, daily_limit=0):
        ident = uuid.uuid4().hex
        self.execute('INSERT INTO accounts(id,role,name,daily_limit) VALUES (?,?,?,?)',
                     (ident,role,name,max(0,int(daily_limit))))
        return ident

    def sender_quota(self, account):
        """返回从号北京时间今日已私信人数和每日上限。"""
        rows = self.rows('''
            SELECT COALESCE((SELECT daily_limit FROM accounts WHERE id=? AND role='sender'),0) daily_limit,
                   count(DISTINCT uid) used
              FROM events
             WHERE account_id=? AND kind IN ('send_attempt','sent')
               AND date(at,'+8 hours')=date('now','+8 hours')
        ''',(account,account))
        return {'used':int(rows[0]['used'] or 0),'limit':int(rows[0]['daily_limit'] or 0)}

    def ingest(self, comment, target, reason):
        uid = user_id(comment['uid'])
        with self.connect() as c:
            inserted = c.execute('INSERT OR IGNORE INTO comments VALUES (?,?,?,?,?,?,?,?,?,?)', (comment['id'],uid,comment['name'],comment['text'],comment['comment_time'],comment['video_id'],comment['video_title'],comment['blogger'],int(target),reason)).rowcount
            if target and inserted:
                c.execute('''INSERT INTO customers(uid,name,text,comment_time,video_title,blogger,url,updated) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(uid) DO UPDATE SET name=excluded.name,text=excluded.text,comment_time=excluded.comment_time,video_title=excluded.video_title,blogger=excluded.blogger,updated=excluded.updated WHERE customers.status='pending' ''', (uid,comment['name'],comment['text'],comment['comment_time'],comment['video_title'],comment['blogger'],'https://www.douyin.com/user/'+uid,time.time()))
        return bool(inserted)

    def claim(self, account):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            sender = c.execute("SELECT daily_limit FROM accounts WHERE id=? AND role='sender'",(account,)).fetchone()
            if sender is not None and int(sender['daily_limit'] or 0)>0:
                used = c.execute('''SELECT count(DISTINCT uid) FROM events
                    WHERE account_id=? AND kind IN ('send_attempt','sent')
                      AND date(at,'+8 hours')=date('now','+8 hours')''',(account,)).fetchone()[0]
                if used>=int(sender['daily_limit']):
                    return None
            r = c.execute("SELECT * FROM customers WHERE status='pending' ORDER BY updated LIMIT 1").fetchone()
            if r is None:
                return None
            c.execute("UPDATE customers SET status='processing',account_id=?,updated=? WHERE uid=?", (account,time.time(),r['uid']))
            return dict(r)

    def finish(self, uid, account, status, message='', error=''):
        with self.connect() as c:
            c.execute("UPDATE customers SET status=?,message=?,error=?,sent_at=CASE WHEN ?='sent' THEN datetime('now') ELSE sent_at END,updated=? WHERE uid=? AND account_id=?", (status,message,error,status,time.time(),uid,account))
            c.execute('INSERT INTO events(account_id,uid,kind,detail) VALUES (?,?,?,?)',(account,uid,status,error or message))

    def start_send(self, uid, account, message):
        """在点击发送前原子记录一次可能消耗当日额度的提交。"""
        detail = '准备提交发送，结果不明时请勿重复联系'
        with self.connect() as c:
            c.execute("UPDATE customers SET status='failed',message=?,error=?,updated=? WHERE uid=? AND account_id=?",
                      (message,detail,time.time(),uid,account))
            c.execute("INSERT INTO events(account_id,uid,kind,detail) VALUES (?,?,'send_attempt',?)",
                      (account,uid,detail))

    def recover(self):
        self.execute("UPDATE customers SET status='failed',error='应用退出前任务未完成，发送结果不明，请勿重复联系' WHERE status='processing'")

    def requeue_failed(self):
        """将当前所有失败客户原子性地放回等待队列。"""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            failed = c.execute("SELECT uid FROM customers WHERE status='failed'").fetchall()
            if not failed:
                return 0
            now = time.time()
            c.execute("UPDATE customers SET status='pending',error='',sent_at='',updated=? WHERE status='failed'", (now,))
            c.executemany(
                'INSERT INTO events(uid,kind,detail) VALUES (?,?,?)',
                [(row['uid'],'pending','人工批量将失败客户重新排队') for row in failed],
            )
            return len(failed)

    def import_customers(self, rows):
        checked = []
        for r in rows:
            uid = user_id(r.get('uid') or r.get('url',''))
            status = r.get('status','pending')
            if status not in ('pending','sent','failed','skipped'):
                status = 'failed'
            checked.append((uid,*[str(r.get(k,'') or '') for k in ('name','text','comment_time','video_title','blogger')],'https://www.douyin.com/user/'+uid,status,str(r.get('message','')),str(r.get('sent_at','')),time.time()))
        with self.connect() as c:
            before = c.total_changes
            c.executemany('INSERT OR IGNORE INTO customers(uid,name,text,comment_time,video_title,blogger,url,status,message,sent_at,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)', checked)
            return c.total_changes-before

    def backup(self, path):
        with self.connect() as src, sqlite3.connect(path) as dst:
            src.backup(dst)

    def data_page(self, table, search='', page=0, size=100):
        keys = {'comments':'id','customers':'uid','events':'id'}
        if table not in keys:
            raise ValueError('不支持的数据表')
        columns = {'comments':('id','uid','name','text','blogger','video_title','reason'),
                   'customers':('uid','name','text','blogger','video_title','status'),
                   'events':('uid','account_id','kind','detail')}[table]
        where = ' OR '.join(f'COALESCE({col},\'\') LIKE ? ESCAPE \'\\\'' for col in columns)
        pattern = '%'+search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        args = (pattern,)*len(columns)
        with self.connect() as c:
            count = c.execute(f'SELECT count(*) FROM {table} WHERE {where}',args).fetchone()[0]
            page = min(max(0,page),max(0,(count-1)//size))
            rows = [dict(r) for r in c.execute(f'SELECT * FROM {table} WHERE {where} ORDER BY rowid DESC LIMIT ? OFFSET ?',(*args,size,page*size))]
        return rows,count,page

    def delete_data(self, table, ids=None):
        keys = {'comments':'id','customers':'uid','events':'id'}
        if table not in (*keys,'all'):
            raise ValueError('不支持的数据表')
        if table=='all' and ids is not None:
            raise ValueError('全部清空不能指定行')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT 1 FROM customers WHERE status='processing' LIMIT 1").fetchone():
                raise ValueError('仍有处理中的客户，请先停止任务')
            before = c.total_changes
            if table=='all':
                for name in keys:
                    c.execute(f'DELETE FROM {name}')
            elif ids is None:
                c.execute(f'DELETE FROM {table}')
            else:
                c.executemany(f'DELETE FROM {table} WHERE {keys[table]}=?',[(ident,) for ident in ids])
            return c.total_changes-before
