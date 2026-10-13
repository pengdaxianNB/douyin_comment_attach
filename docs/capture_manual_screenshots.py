"""Render current UI with isolated synthetic data; never starts tasks or opens a site.

Usage: .venv/bin/python docs/capture_manual_screenshots.py /tmp/panda-manual-images
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
from app.db import DB
from app.ui import Window, STYLE


def capture(output):
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    with tempfile.TemporaryDirectory(prefix='panda-manual-demo-') as demo:
        folder = Path(demo)
        db = DB(folder / 'demo.sqlite3')
        a = db.account('演示账号 A · 咨询', 30)
        b = db.account('演示账号 B · 服务', 20)
        db.execute('UPDATE accounts SET interval=180 WHERE id=?', (b,))
        db.import_keywords(['企业培训', '团队管理', '新员工培训'])
        db.import_keywords(['企业培训怎么选', '团队管理课程'], search=True)
        db.execute('INSERT INTO bloggers VALUES (?,?)', ('MS4wLjABAAAAdemo_blogger_001', '演示博主 · 职场成长'))
        for i, (name, text, status) in enumerate([
            ('演示客户 · 小林', '想了解团队培训的课程安排', 'pending'),
            ('演示客户 · 小周', '企业内训可以定制吗？', 'sent'),
            ('演示客户 · 小陈', '请问怎么咨询？', 'failed'),
            ('演示客户 · 小许', '课程内容能发一份吗？', 'skipped'),
        ]):
            uid = f'MS4wLjABAAAAdemo_customer_{i:03d}'
            db.import_customers([dict(uid=uid, name=name, text=text, status=status,
                comment_time='2026-10-12T08:00:00+00:00', video_title='团队培训：如何选择合适课程', blogger='演示博主 · 职场成长')])
            db.execute('INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?,?)',
                (f'demo_comment_{i}', uid, name, text, '2026-10-12T08:00:00+00:00', '1234567800000000', '团队培训：如何选择合适课程', '演示博主 · 职场成长', 1, '明确表达课程咨询需求（演示）'))
        db.execute("UPDATE customers SET error='页面加载超时（演示）' WHERE status='failed'")
        db.execute("UPDATE customers SET message='你好，看到你想了解企业内训，请问团队规模是多少？',account_id=? WHERE status='sent'", (a,))
        db.execute('INSERT INTO events(account_id,uid,kind,detail) VALUES (?,?,?,?)', (a, 'MS4wLjABAAAAdemo_customer_001', 'sent', '发送操作完成（演示）'))
        for i, title in enumerate(['团队培训：如何选择合适课程', '新员工入职培训清单', '管理能力提升的方法']):
            db.add_video(dict(aweme_id=str(1234567800000000+i), desc=title, create_time=1791788400,
                author=dict(nickname='演示博主 · 职场成长')), 'keyword', '企业培训')
        db.add_manual_video('https://www.douyin.com/note/1234567800000008', '手动图文笔记 · 课程介绍（演示）')
        db.execute("UPDATE videos SET status='done' WHERE id='1234567800000000'")
        window = Window(db, folder, start_engine=False)
        window.timer.stop()
        window.resize(1680, 1020)
        window.show()
        app.processEvents()

        def shot(name, tab=None):
            if tab is not None:
                window.tabs.setCurrentIndex(tab)
            app.processEvents()
            window.grab().save(str(output / f'{name}.png'))

        shot('overview', 0)
        window.accounts.selectRow(0)
        shot('accounts', 1)
        for index, name in enumerate(['source-index', 'source-search', 'source-bloggers', 'source-videos']):
            window.video_source_tabs.setCurrentIndex(index)
            shot(name, 2)
        # Render the actual browser toolbar with an explicitly labeled placeholder.
        # No QWebEngineView is created; no URL is navigated.
        # Mock the profile as well: rendering documentation controls should not
        # start Chromium or touch system/user browser data directories.
        profile_patch = patch('app.embedded_browser.QWebEngineProfile')
        profile_patch.start()
        for row in window.account_rows:
            widget = window.ensure_browser(row, activate=False)
            widget.sleep_hint.setText('网页内容展示区（说明书占位示意）\n\n实际使用时，在此完成抖音登录、查看作品、评论与客户会话。\n本截图未连接抖音，也未登录任何真实账号。')
            widget.address.setText('https://www.douyin.com/（示意地址）')
        window.browser_tabs.setCurrentIndex(0)
        shot('browser', 3)
        window.tabs.setCurrentIndex(4)
        scroll = window.tabs.currentWidget()
        scroll.verticalScrollBar().setValue(0)
        shot('settings-monitor')
        scroll.verticalScrollBar().setValue(window.fields['base_url'].mapTo(scroll.widget(), QPoint(0, 0)).y() - 75)
        shot('settings-ai')
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        shot('settings-adaptation')
        window.logs.setPlainText('以下为说明书示例日志，非真实运行记录。\n\n[演示] 账号 A：开始找评论，领取关键词“企业培训”\n[演示] 已保存来源进度，收集到 3 个作品\n[演示] 搜索后暂停 60 秒，账号停留个人页\n[演示] 所有来源已整理完成，开始扫描近期顶层评论\n[演示] AI 判断：明确课程咨询需求，加入等待私信\n[演示] 账号 B：页面加载超时，记录原因并继续其他任务')
        shot('logs', 5)
        shot('database', 6)
        window.close()
        app.processEvents()
        profile_patch.stop()


if __name__ == '__main__':
    capture(Path(sys.argv[1] if len(sys.argv)>1 else '/tmp/panda-manual-images'))
