import os
import sys
from pathlib import Path
from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication,QMessageBox
from app.db import DB
from app.ui import Window,STYLE


def main():
    app=QApplication(sys.argv)
    app.setApplicationName('熊猫领航')
    data=Path(os.environ.get('DOUYIN_DATA_DIR',str(Path(__file__).parent/'data')))
    data.mkdir(parents=True,exist_ok=True)
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
