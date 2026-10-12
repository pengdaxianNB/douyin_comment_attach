import faulthandler
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


_FAULT_LOG = None


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
    for old in list(logger.handlers):
        if isinstance(old,RotatingFileHandler):
            logger.removeHandler(old)
            old.close()
    logger.addHandler(handler)

    # Native Qt/Chromium faults bypass sys.excepthook. Keep the file handle
    # alive for faulthandler and rotate between launches to bound disk usage.
    global _FAULT_LOG
    if _FAULT_LOG is not None:
        faulthandler.disable()
        _FAULT_LOG.close()
    fault_path = folder/'native-crash.log'
    if fault_path.exists() and fault_path.stat().st_size >= 2*1024*1024:
        fault_path.replace(folder/'native-crash.previous.log')
    _FAULT_LOG = fault_path.open('a',encoding='utf-8')
    faulthandler.enable(file=_FAULT_LOG,all_threads=True)

    def crash(exc_type, exc, tb):
        logger.error('未捕获异常',exc_info=(exc_type,exc,tb))
        sys.__excepthook__(exc_type,exc,tb)
    sys.excepthook = crash
    threading.excepthook = lambda args: crash(args.exc_type,args.exc_value,args.exc_traceback)


def main():
    configure_embedded_browser()
    data=Path(os.environ.get('DOUYIN_DATA_DIR',str(Path(__file__).parent/'data')))
    data.mkdir(parents=True,exist_ok=True)
    lock=QLockFile(str(data/'app.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        app=QApplication(sys.argv)
        QMessageBox.warning(None,'已运行','该数据目录已有应用实例，请勿重复启动。')
        return 1
    try:
        # Acquire the data-directory lock before opening/rotating logs, while
        # still enabling native fault diagnostics before QtWebEngine starts.
        configure_logging(data)
        # UI imports QtWebEngine, so keep it after Chromium flag setup.
        from app.ui import Window,STYLE
        app=QApplication(sys.argv)
        app.setApplicationName('熊猫领航')
        db=DB(data/'customers.sqlite3'); db.recover()
        app.setStyleSheet(STYLE)
        window=Window(db,data); window.showMaximized()
        return app.exec()
    except Exception as exc:
        logging.getLogger('panda').exception('应用启动或运行失败')
        app=QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None,'无法启动',f'{type(exc).__name__}：{exc}\n\n详细信息已写入 {data / "logs" / "app.log"}')
        return 1
    finally:
        lock.unlock()


if __name__=='__main__':sys.exit(main())
