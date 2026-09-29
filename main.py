import logging
import os
import socket
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication,QMessageBox
from app.db import DB


def configure_embedded_browser():
    """Expose QtWebEngine on a loopback-only CDP port for the worker thread."""
    if not os.environ.get('QTWEBENGINE_REMOTE_DEBUGGING'):
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1',0))
            port = sock.getsockname()[1]
        os.environ['QTWEBENGINE_REMOTE_DEBUGGING'] = '127.0.0.1:'+str(port)
    flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS','')
    required = (
        '--remote-allow-origins=*',
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        '--disable-renderer-backgrounding',
    )
    for option in required:
        key = option.split('=',1)[0]
        if key not in flags:
            flags = (flags+' '+option).strip()
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = flags


def configure_logging(data):
    """Keep a rotating log file: the on-screen log only holds the last 1000 lines."""
    logger = logging.getLogger('panda')
    logger.setLevel(logging.INFO)
    folder = data/'logs'
    folder.mkdir(parents=True,exist_ok=True)
    handler = RotatingFileHandler(folder/'app.log',maxBytes=2*1024*1024,backupCount=5,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s  %(message)s'))
    logger.addHandler(handler)

    def crash(exc_type, exc, tb):
        logger.error('未捕获异常',exc_info=(exc_type,exc,tb))
        sys.__excepthook__(exc_type,exc,tb)
    sys.excepthook = crash
    threading.excepthook = lambda args: crash(args.exc_type,args.exc_value,args.exc_traceback)


def main():
    configure_embedded_browser()
    # Importing the UI also imports QtWebEngine. Keep it after the environment
    # setup so the embedded Chromium process always sees the debugging flags.
    from app.ui import Window,STYLE
    app=QApplication(sys.argv)
    app.setApplicationName('熊猫领航')
    data=Path(os.environ.get('DOUYIN_DATA_DIR',str(Path(__file__).parent/'data')))
    data.mkdir(parents=True,exist_ok=True)
    configure_logging(data)
    lock=QLockFile(str(data/'app.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.warning(None,'已运行','该数据目录已有应用实例，请勿重复启动。')
        return 1
    db=DB(data/'customers.sqlite3'); db.recover()
    app.setStyleSheet(STYLE)
    window=Window(db,data); window.show()
    return app.exec()


if __name__=='__main__':sys.exit(main())
