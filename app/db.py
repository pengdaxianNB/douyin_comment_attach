from contextlib import contextmanager
import json
import logging
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


WORK_KINDS = ('video','note')


def work_url(ident, kind='video'):
    """Canonical page URL of a video or an image-text note."""
    return 'https://www.douyin.com/'+('note' if kind=='note' else 'video')+'/'+str(ident)


def parse_work_url(value):
    """Extract (work ID, kind) from a full Douyin video or note URL."""
    text = str(value or '').strip()
    parsed = urlparse(text)
    if parsed.scheme != 'https' or parsed.hostname not in ('www.douyin.com','douyin.com'):
        raise ValueError('请输入完整的抖音视频或笔记链接（https://www.douyin.com/video/… 或 /note/…）')
    match = re.fullmatch(r'/(video|note)/(\d{8,})/?',parsed.path)
    if not match:
        raise ValueError('链接中未找到 /video/ 或 /note/ 后的数字作品ID；请粘贴页面地址栏中的完整链接')
    return match.group(2),match.group(1)


def video_id(value):
    """Extract a work ID from a full Douyin video or note URL."""
    return parse_work_url(value)[0]


class DB:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,name TEXT NOT NULL,interval INTEGER NOT NULL DEFAULT 120,daily_limit INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS bloggers(id TEXT PRIMARY KEY,name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS keywords(id INTEGER PRIMARY KEY AUTOINCREMENT,keyword TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS search_keywords(id INTEGER PRIMARY KEY AUTOINCREMENT,keyword TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS manual_videos(id TEXT PRIMARY KEY,url TEXT NOT NULL,title TEXT NOT NULL DEFAULT '',added_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS discovery_sources(
                kind TEXT NOT NULL,source_ref TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY(kind,source_ref));
            CREATE TABLE IF NOT EXISTS video_exclusions(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS videos(
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL CHECK(source_type IN ('blogger','keyword','manual','search')),
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
            # executescript commits existing transactions. All upgrades below
            # use execute so a failed copy cannot publish a half-upgraded DB.
            c.execute('BEGIN IMMEDIATE')
            account_columns = {row['name'] for row in c.execute('PRAGMA table_info(accounts)')}
            if 'daily_limit' not in account_columns:
                c.execute('ALTER TABLE accounts ADD COLUMN daily_limit INTEGER NOT NULL DEFAULT 0')
            if 'role' in account_columns:
                # Preserve UUIDs: browser profiles and sending history use them.
                # SQLite's Python driver does not begin a transaction for DDL.
                # Keep the rename/copy/drop atomic if migration is interrupted.
                c.execute('DROP INDEX IF EXISTS one_main')
                c.execute('ALTER TABLE accounts RENAME TO accounts_before_cluster')
                c.execute('''CREATE TABLE accounts(id TEXT PRIMARY KEY,name TEXT NOT NULL,
                    interval INTEGER NOT NULL DEFAULT 120,daily_limit INTEGER NOT NULL DEFAULT 0)''')
                c.execute('''INSERT INTO accounts SELECT id,name,interval,daily_limit
                    FROM accounts_before_cluster''')
                c.execute('DROP TABLE accounts_before_cluster')
            video_schema = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='videos'").fetchone()['sql']
            if "'manual'" not in video_schema or "'search'" not in video_schema:
                c.execute('ALTER TABLE videos RENAME TO videos_before_source_change')
                c.execute('''CREATE TABLE videos(
                    id TEXT PRIMARY KEY,title TEXT NOT NULL DEFAULT '',author TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL CHECK(source_type IN ('blogger','keyword','manual','search')),
                    source_name TEXT NOT NULL DEFAULT '',source_ref TEXT NOT NULL DEFAULT '',
                    create_time INTEGER NOT NULL DEFAULT 0,url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','scanning','done','failed')),
                    error TEXT NOT NULL DEFAULT '',discovered_at REAL NOT NULL,scanned_at REAL
                )''')
                c.execute('INSERT INTO videos SELECT * FROM videos_before_source_change')
                c.execute('DROP TABLE videos_before_source_change')
            c.execute('CREATE INDEX IF NOT EXISTS customers_status_updated ON customers(status,updated)')
            c.execute('CREATE INDEX IF NOT EXISTS customers_updated ON customers(updated)')
            c.execute('CREATE INDEX IF NOT EXISTS events_account_kind_at_uid ON events(account_id,kind,at,uid)')
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
        if not r:
            return default
        try:
            value = json.loads(r[0]['value'])
            if default is not None and type(value) is not type(default):
                raise ValueError('设置类型错误')
            return value
        except (TypeError, ValueError, RecursionError):
            logging.getLogger('panda').warning('设置 %s 无法解析，使用默认值',key)
            return default

    def save_settings(self, values):
        with self.connect() as c:
            c.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)', [(k,json.dumps(v,ensure_ascii=False)) for k,v in values.items()])

    @staticmethod
    def discovery_key(source):
        kind,row = source
        return kind,str(row['keyword'] if kind in ('keyword','search') else row['id'])

    def pending_discovery_sources(self, sources):
        completed = {(r['kind'],r['source_ref']) for r in self.rows(
            "SELECT kind,source_ref FROM discovery_sources WHERE status='done'")}
        return [source for source in sources if self.discovery_key(source) not in completed]

    def set_discovery_status(self, source, status):
        if status not in ('pending','running','done'):
            raise ValueError('不支持的找视频状态')
        self.execute('UPDATE discovery_sources SET status=? WHERE kind=? AND source_ref=?',
                     (status,*self.discovery_key(source)))

    def finish_video_discovery(self, now=None):
        """Publish a complete list only after every persisted source finished."""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            active = c.execute("SELECT value FROM settings WHERE key='video_discovery_active'").fetchone()
            if not active or not json.loads(active['value']):
                return False
            if c.execute("SELECT 1 FROM discovery_sources WHERE status<>'done' LIMIT 1").fetchone():
                return False
            c.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',[
                ('video_discovery_active','false'),
                ('video_list_built_at',json.dumps(float(time.time() if now is None else now)))])
        return True

    def prepare_video_scan(self, reset_hours, now=None, sources=None):
        """Prepare one monitoring pass and report whether sources must be rebuilt.

        A fresh list is rebuilt after ``reset_hours``.  Inside that window the
        same videos are retained.  An interrupted pass keeps its completed and
        pending statuses for resume; only a fully finished pass is reset for a
        new comment polling round. With ``sources``, discovery is checkpointed
        and the rebuild clock starts only after all sources have completed.
        """
        current = float(time.time() if now is None else now)
        reset_after = max(1, int(reset_hours)) * 3600
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            active = c.execute("SELECT value FROM settings WHERE key='video_discovery_active'").fetchone()
            discovering = bool(active and json.loads(active['value']))
            row = c.execute("SELECT value FROM settings WHERE key='video_list_built_at'").fetchone()
            has_videos = c.execute('SELECT 1 FROM videos LIMIT 1').fetchone() is not None
            has_exclusions = c.execute('SELECT 1 FROM video_exclusions LIMIT 1').fetchone() is not None
            try:
                built_at = float(json.loads(row['value'])) if row else 0.0
            except (TypeError, ValueError, json.JSONDecodeError):
                built_at = 0.0
            # Older versions wrote built_at before discovery completed. On the
            # first upgrade we cannot prove which sources finished, so retain
            # the existing results and rediscover sources once, with deduping.
            legacy_resume = (active is None and sources is not None and bool(sources)
                             and has_videos and (not built_at or current-built_at<reset_after))
            if legacy_resume:
                discovering = True
                c.execute("INSERT OR REPLACE INTO settings VALUES ('video_list_built_at','0')")
            rebuild = discovering or (not has_videos and not has_exclusions) or not built_at or current - built_at >= reset_after
            if rebuild and not discovering:
                c.execute('DELETE FROM videos')
                c.execute('DELETE FROM video_exclusions')
                c.execute('DELETE FROM discovery_sources')
                c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)',
                          ('video_list_built_at',json.dumps(0 if sources is not None else current)))
            if rebuild and sources is not None:
                c.execute("INSERT OR REPLACE INTO settings VALUES ('video_discovery_active','true')")
                keys = {self.discovery_key(source) for source in sources}
                for old in c.execute('SELECT kind,source_ref FROM discovery_sources').fetchall():
                    if (old['kind'],old['source_ref']) not in keys:
                        c.execute('DELETE FROM discovery_sources WHERE kind=? AND source_ref=?',tuple(old))
                c.executemany('INSERT OR IGNORE INTO discovery_sources(kind,source_ref) VALUES (?,?)',keys)
                c.execute("UPDATE discovery_sources SET status='pending' WHERE status='running'")
            if not rebuild:
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
            c.execute('DELETE FROM discovery_sources')
            c.execute('DELETE FROM video_exclusions')
            c.execute("DELETE FROM settings WHERE key IN ('video_list_built_at','video_discovery_active')")
        return int(count)

    def delete_videos(self, ids):
        """Remove selected works, including manual sources, for this list cycle."""
        keys = [(str(ident),) for ident in set(ids)]
        if not keys:
            return 0
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if any(c.execute("SELECT 1 FROM videos WHERE id=? AND status='scanning'",key).fetchone()
                   for key in keys):
                raise ValueError('选中视频正在扫描，请先停止任务')
            removed = 0
            for key in keys:
                removed += c.execute('DELETE FROM videos WHERE id=?',key).rowcount
            c.executemany('DELETE FROM manual_videos WHERE id=?',keys)
            c.executemany('INSERT OR IGNORE INTO video_exclusions VALUES (?)',keys)
        return removed

    def add_manual_video(self, url, title=''):
        ident,kind = parse_work_url(url)
        clean_url = work_url(ident,kind)
        clean_title = ' '.join(str(title or '').split()) or ('手动录入笔记 ' if kind=='note' else '手动录入视频 ')+ident
        now = time.time()
        with self.connect() as c:
            c.execute('DELETE FROM video_exclusions WHERE id=?',(ident,))
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
        # Image-text notes live under /note/. Trust an explicit page URL only
        # when it is a Douyin work URL for this very ID.
        kind = 'note' if video.get('kind')=='note' else 'video'
        page_url = work_url(ident,kind)
        try:
            explicit_id,explicit_kind = parse_work_url(video.get('url'))
            if explicit_id==ident: page_url = work_url(ident,explicit_kind)
        except ValueError:
            pass
        with self.connect() as c:
            if c.execute('SELECT 1 FROM video_exclusions WHERE id=?',(ident,)).fetchone():
                return False
            return bool(c.execute('''
                INSERT INTO videos(id,title,author,source_type,source_name,source_ref,create_time,url,discovered_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE videos.title END,
                    author=CASE WHEN excluded.author<>'' THEN excluded.author ELSE videos.author END,
                    url=CASE WHEN excluded.url LIKE '%/note/%' THEN excluded.url ELSE videos.url END,
                    create_time=CASE WHEN excluded.create_time>0 THEN excluded.create_time ELSE videos.create_time END
            ''',(ident,title,author_name,source_type,str(source_name),str(source_ref),created,
                 page_url,now)).rowcount)

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

    def account(self, name, daily_limit=0):
        ident = uuid.uuid4().hex
        self.execute('INSERT INTO accounts(id,name,daily_limit) VALUES (?,?,?)',
                     (ident,name,max(0,int(daily_limit))))
        return ident

    def sender_quota(self, account):
        """返回账号北京时间今日已私信人数和每日上限。"""
        rows = self.rows('''
            SELECT COALESCE((SELECT daily_limit FROM accounts WHERE id=?),0) daily_limit,
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
            sender = c.execute("SELECT daily_limit FROM accounts WHERE id=?",(account,)).fetchone()
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

    def import_keywords(self, values, search=False):
        """Insert a fully checked batch atomically; duplicates keep existing IDs."""
        checked = []
        for value in values:
            if not isinstance(value,str) or not value.strip() or any(c in value for c in ('\n','\r','\x00')):
                raise ValueError('每行必须是一个非空关键词或搜索词，不能包含换行或无效字符')
            checked.append((value.strip(),))
        table = 'search_keywords' if search else 'keywords'
        with self.connect() as c:
            before = c.total_changes
            c.executemany(f'INSERT OR IGNORE INTO {table}(keyword) VALUES (?)',checked)
            added = c.total_changes-before
        return {'added':added,'duplicates':len(checked)-added}

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
        dst = sqlite3.connect(path)
        try:
            with self.connect() as src:
                src.backup(dst)
        finally:
            # A lingering handle keeps the backup file locked on Windows.
            dst.close()

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
                c.execute('DELETE FROM discovery_sources')
                c.execute('DELETE FROM video_exclusions')
                c.execute("DELETE FROM settings WHERE key IN ('video_list_built_at','video_discovery_active')")
            elif ids is None:
                c.execute(f'DELETE FROM {table}')
            else:
                c.executemany(f'DELETE FROM {table} WHERE {keys[table]}=?',[(ident,) for ident in ids])
            return c.total_changes-before
