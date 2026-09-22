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


def video_id(value):
    """Extract a work ID from a full Douyin video URL."""
    text = str(value or '').strip()
    parsed = urlparse(text)
    if parsed.scheme != 'https' or parsed.hostname not in ('www.douyin.com','douyin.com'):
        raise ValueError('请输入完整的抖音视频链接（https://www.douyin.com/video/…）')
    match = re.fullmatch(r'/video/(\d{8,})/?',parsed.path)
    if not match:
        raise ValueError('链接中未找到 /video/数字作品ID；请粘贴视频页地址栏中的完整链接')
    return match.group(1)


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
            CREATE TABLE IF NOT EXISTS keywords(id INTEGER PRIMARY KEY AUTOINCREMENT,keyword TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS manual_videos(id TEXT PRIMARY KEY,url TEXT NOT NULL,title TEXT NOT NULL DEFAULT '',added_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS videos(
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL CHECK(source_type IN ('blogger','keyword','manual')),
                source_name TEXT NOT NULL DEFAULT '',
                source_ref TEXT NOT NULL DEFAULT '',
                create_time INTEGER NOT NULL DEFAULT 0,
                url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','scanning','done','failed')),
                error TEXT NOT NULL DEFAULT '',
                discovered_at REAL NOT NULL,
                scanned_at REAL
            );
            CREATE TABLE IF NOT EXISTS comments(id TEXT PRIMARY KEY,uid TEXT NOT NULL,name TEXT,text TEXT,comment_time TEXT,video_id TEXT,video_title TEXT,blogger TEXT,target INTEGER,reason TEXT);
            CREATE TABLE IF NOT EXISTS customers(uid TEXT PRIMARY KEY,name TEXT,text TEXT,comment_time TEXT,video_title TEXT,blogger TEXT,url TEXT,status TEXT NOT NULL DEFAULT 'pending',message TEXT DEFAULT '',account_id TEXT,error TEXT DEFAULT '',sent_at TEXT DEFAULT '',updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at TEXT DEFAULT CURRENT_TIMESTAMP,account_id TEXT,uid TEXT,kind TEXT,detail TEXT);
            ''')
            account_columns = {row['name'] for row in c.execute('PRAGMA table_info(accounts)')}
            if 'daily_limit' not in account_columns:
                c.execute('ALTER TABLE accounts ADD COLUMN daily_limit INTEGER NOT NULL DEFAULT 0')
            video_schema = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='videos'").fetchone()['sql']
            if "'manual'" not in video_schema:
                c.executescript('''
                ALTER TABLE videos RENAME TO videos_without_manual;
                CREATE TABLE videos(
                    id TEXT PRIMARY KEY,title TEXT NOT NULL DEFAULT '',author TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL CHECK(source_type IN ('blogger','keyword','manual')),
                    source_name TEXT NOT NULL DEFAULT '',source_ref TEXT NOT NULL DEFAULT '',
                    create_time INTEGER NOT NULL DEFAULT 0,url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','scanning','done','failed')),
                    error TEXT NOT NULL DEFAULT '',discovered_at REAL NOT NULL,scanned_at REAL
                );
                INSERT INTO videos SELECT * FROM videos_without_manual;
                DROP TABLE videos_without_manual;
                ''')
            c.execute("UPDATE customers SET status='failed',error=CASE WHEN error='' THEN '历史待核验记录，发送结果不明，请勿重复联系' ELSE error END WHERE status='uncertain'")
            # An interrupted comment scan is safe to retry: comments are
            # deduplicated by cid and no outbound message is sent here.
            c.execute("UPDATE videos SET status='pending',error='' WHERE status='scanning'")

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

    def prepare_video_scan(self, reset_hours, now=None):
        """Prepare one monitoring pass and report whether sources must be rebuilt.

        A fresh list is rebuilt after ``reset_hours``.  Inside that window the
        same videos are retained.  An interrupted pass keeps its completed and
        pending statuses for resume; only a fully finished pass is reset for a
        new comment polling round.
        """
        current = float(time.time() if now is None else now)
        reset_after = max(1, int(reset_hours)) * 3600
        with self.connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key='video_list_built_at'").fetchone()
            has_videos = c.execute('SELECT 1 FROM videos LIMIT 1').fetchone() is not None
            try:
                built_at = float(json.loads(row['value'])) if row else 0.0
            except (TypeError, ValueError, json.JSONDecodeError):
                built_at = 0.0
            rebuild = not has_videos or not built_at or current - built_at >= reset_after
            if rebuild:
                c.execute('DELETE FROM videos')
                c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)',
                          ('video_list_built_at',json.dumps(current)))
            else:
                # A task can be interrupted between marking a video as
                # scanning and restoring it to pending. Recover that one item
                # before deciding whether this is a resume or a new pass.
                c.execute("UPDATE videos SET status='pending',error='' WHERE status='scanning'")
                unfinished = c.execute(
                    "SELECT 1 FROM videos WHERE status='pending' LIMIT 1").fetchone()
                # Explicitly missing/deleted works are terminal for the
                # current discovered list.  Keep them out of every polling
                # pass until the normal list rebuild rediscovers sources.
                if not unfinished:
                    c.execute("""UPDATE videos SET status='pending',error='',scanned_at=NULL
                                 WHERE error NOT LIKE '视频已失效：%'""")
        return rebuild

    def clear_videos(self):
        """Clear the queue and force source discovery on the next main pass."""
        with self.connect() as c:
            count = c.execute('SELECT count(*) FROM videos').fetchone()[0]
            c.execute('DELETE FROM videos')
            c.execute("DELETE FROM settings WHERE key='video_list_built_at'")
        return int(count)

    def add_manual_video(self, url, title=''):
        ident = video_id(url)
        clean_url = 'https://www.douyin.com/video/'+ident
        clean_title = ' '.join(str(title or '').split()) or '手动录入视频 '+ident
        now = time.time()
        with self.connect() as c:
            if c.execute('SELECT 1 FROM videos WHERE id=?',(ident,)).fetchone():
                raise ValueError('该视频已在待分析列表中，无需重复录入')
            if c.execute('SELECT 1 FROM manual_videos WHERE id=?',(ident,)).fetchone():
                raise ValueError('该视频已在手动录入来源中')
            c.execute('INSERT INTO manual_videos(id,url,title,added_at) VALUES (?,?,?,?)',
                      (ident,clean_url,clean_title,now))
            c.execute('''INSERT INTO videos(id,title,author,source_type,source_name,source_ref,create_time,url,discovered_at)
                         VALUES (?,?,?,'manual','手动录入',?,0,?,?)''',
                      (ident,clean_title,'',clean_url,clean_url,now))
        return ident

    def remove_manual_video(self, ident):
        with self.connect() as c:
            if not c.execute('SELECT 1 FROM manual_videos WHERE id=?',(str(ident),)).fetchone():
                raise ValueError('选中的视频不是手动录入来源')
            c.execute('DELETE FROM manual_videos WHERE id=?',(str(ident),))
            c.execute("DELETE FROM videos WHERE id=? AND source_type='manual'",(str(ident),))

    def add_video(self, video, source_type, source_name, source_ref=''):
        """Add a discovered video to the current list, deduplicated by ID."""
        ident = str(video.get('aweme_id') or '').strip()
        if not ident:
            raise ValueError('视频缺少作品 ID')
        title = ' '.join(str(video.get('desc') or '').split())
        author = video.get('author') or {}
        author_name = str(author.get('nickname') or '') if isinstance(author,dict) else str(author)
        try:
            created = int(video.get('create_time') or 0)
        except (TypeError, ValueError):
            created = 0
        now = time.time()
        with self.connect() as c:
            return bool(c.execute('''
                INSERT INTO videos(id,title,author,source_type,source_name,source_ref,create_time,url,discovered_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE videos.title END,
                    author=CASE WHEN excluded.author<>'' THEN excluded.author ELSE videos.author END,
                    create_time=CASE WHEN excluded.create_time>0 THEN excluded.create_time ELSE videos.create_time END
            ''',(ident,title,author_name,source_type,str(source_name),str(source_ref),created,
                 'https://www.douyin.com/video/'+ident,now)).rowcount)

    def video_tasks(self, status=None):
        where = ' WHERE status=?' if status else ''
        args = (status,) if status else ()
        return self.rows('SELECT * FROM videos'+where+' ORDER BY create_time DESC,discovered_at,id',args)

    def set_video_status(self, ident, status, error=''):
        if status not in ('pending','scanning','done','failed'):
            raise ValueError('不支持的视频状态')
        scanned_at = time.time() if status in ('done','failed') else None
        self.execute('UPDATE videos SET status=?,error=?,scanned_at=? WHERE id=?',
                     (status,str(error),scanned_at,str(ident)))

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
            # The durable send_attempt event prevents a possibly submitted
            # message from being omitted from the daily quota.  The customer
            # itself is still actively being handled, so keep it in
            # processing until the send call reports its final outcome.
            # recover() turns any processing row left by an application crash
            # into a failure before work can resume.
            c.execute("UPDATE customers SET status='processing',message=?,error='',updated=? WHERE uid=? AND account_id=?",
                      (message,time.time(),uid,account))
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
                c.execute('DELETE FROM videos')
                c.execute("DELETE FROM settings WHERE key='video_list_built_at'")
            elif ids is None:
                c.execute(f'DELETE FROM {table}')
            else:
                c.executemany(f'DELETE FROM {table} WHERE {keys[table]}=?',[(ident,) for ident in ids])
            return c.total_changes-before
