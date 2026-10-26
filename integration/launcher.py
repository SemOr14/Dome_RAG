"""Own the application process tree. Red exits the child with 77; launcher cleans up and exits 0."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import psutil


def main():
    env = dict(os.environ, DOME_MANAGED_LAUNCH='1')
    kwargs = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    child = subprocess.Popen([sys.executable, '-m', 'integration.worker'], env=env, **kwargs)
    owned = {}
    parent = psutil.Process(child.pid)
    try:
        while child.poll() is None:
            try:
                for p in parent.children(recursive=True):
                    owned[p.pid] = p.create_time()
            except psutil.Error:
                pass
            time.sleep(.05)
    except KeyboardInterrupt:
        pass
    finally:
        if os.name != 'nt':
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        targets = []
        for pid, born in owned.items():
            try:
                p = psutil.Process(pid)
                if p.create_time() == born:
                    targets.append(p)
                    p.terminate()
            except psutil.Error:
                pass
        if child.poll() is None:
            child.terminate()
        _, alive = psutil.wait_procs(targets, timeout=.3)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        if os.name != 'nt':
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)
    return 0 if child.returncode == 77 else child.returncode


if __name__ == '__main__':
    raise SystemExit(main())
