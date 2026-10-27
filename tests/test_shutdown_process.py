"""Real process-tree acceptance test using a local emulator and a deliberately hung child/thread."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen

import psutil

ROOT=Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]


def request(port,path,body=None):
    req=Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(body).encode() if body is not None else None,
                headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=3) as r:return json.load(r)


def wait_for(fn,timeout=12):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            result=fn()
            if result:return result
        except (OSError,ValueError):pass
        time.sleep(.1)
    raise AssertionError('deadline exceeded')


def alive(pid):
    try:return psutil.Process(pid).is_running() and psutil.Process(pid).status()!=psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:return False


def test_red_terminates_own_tree_and_port_emulator_survives_normal_restart(tmp_path):
    app_port,emu_port=free_port(),free_port()
    env={**os.environ,'PANEL_PORT':str(app_port),'DEMO_DOME_PORT':str(emu_port),
        'DOME_URL':f'ws://127.0.0.1:{emu_port}','DOME_KEY':'DEMO-KEY-123456','DOME_API_SCOPE':'full',
        'DEPLOYMENT_PROFILE':'service','AGENT_BRAIN':'rules','DEEPSEEK_API_KEY':'','DOME_ALLOW_CONTROL':'1',
        'DATA_API_DB':str(tmp_path/'smoke.sqlite'),'DATA_API_TOKEN':'','PANEL_STATE_DIR':str(tmp_path/'state'),
        'AGENT_LOG_DIR':str(tmp_path/'logs')}
    env['NO_PROXY']='127.0.0.1,localhost'
    env['no_proxy']=env['NO_PROXY']
    emu_log=(tmp_path/'emulator.log').open('w')
    emulator=subprocess.Popen([sys.executable,'-m','integration.demo_dome'],cwd=ROOT,env=env,stdout=emu_log,stderr=subprocess.STDOUT)
    processes=[]
    try:
        def emu_ready():
            with socket.create_connection(('127.0.0.1',emu_port),timeout=.2):return True
        wait_for(emu_ready)
        custom=tmp_path/'inject';custom.mkdir()
        (custom/'sitecustomize.py').write_text('''import os,subprocess,sys,threading,time
if os.getenv("DOME_MANAGED_LAUNCH")=="1":
    p=subprocess.Popen([sys.executable,"-S","-c","import time; time.sleep(3600)"])
    open(os.environ["TEST_CHILD_PID_FILE"],"w").write(str(p.pid))
    threading.Thread(target=lambda: time.sleep(3600),daemon=False).start()
''')
        env.update(PYTHONPATH=str(custom)+os.pathsep+str(ROOT),TEST_CHILD_PID_FILE=str(tmp_path/'hung-child.pid'))
        for cycle in range(2):
            log=(tmp_path/f'app-{cycle}.log').open('w')
            app=subprocess.Popen([sys.executable,'run_panel.py'],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
            processes.append(app)
            try:
                status=wait_for(lambda:request(app_port,'/control/status'))
                assert status['operating_state']=='normal' and not status['connected']
                assert status['agent_mode']=='recommendation'
                child_pid=int(wait_for(lambda:(tmp_path/'hung-child.pid').read_text()))
                if cycle==0:
                    request(app_port,'/control/connect',{})
                    wait_for(lambda: request(app_port,'/control/production')['items'][0]['fresh'])
                    data=request(app_port,'/control/production')
                    assert len(data['items'])==2 and not data['items'][1]['capabilities']['upload']
                request(app_port,'/control/safety/red',{})
                assert app.wait(timeout=6)==0
                wait_for(lambda:not alive(child_pid),3)
                with socket.socket() as s:
                    assert s.connect_ex(('127.0.0.1',app_port))!=0
                assert emulator.poll() is None
            finally:
                if app.poll() is None:
                    parent=psutil.Process(app.pid)
                    for p in parent.children(recursive=True):
                        try:p.kill()
                        except psutil.Error:pass
                    app.kill();app.wait(timeout=3)
                log.close()
    finally:
        emulator.terminate();emulator.wait(timeout=4);emu_log.close()
