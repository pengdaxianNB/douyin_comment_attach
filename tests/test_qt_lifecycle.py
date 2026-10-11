"""Real Qt/CDP integration; all traffic stays on a temporary loopback server."""
import os
from pathlib import Path
import subprocess
import sys


def test_ten_profiles_rebind_three_at_a_time_with_login_storage_preserved():
    root=Path(__file__).resolve().parents[1]
    env=dict(os.environ,QT_QPA_PLATFORM='offscreen',PYTHONPATH=str(root))
    # The probe must own its random DevTools port, independent of pytest's Qt.
    env.pop('QTWEBENGINE_REMOTE_DEBUGGING',None)
    result=subprocess.run([sys.executable,str(root/'tests/cdp_lifecycle_probe.py')],
                          cwd=root,env=env,text=True,capture_output=True,timeout=55)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    assert '20 rebindings, cookies and storage preserved' in result.stdout
