"""Linux descendant containment, including programs which call setsid()."""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import time


def become_subreaper():
    # PR_SET_CHILD_SUBREAPER: orphaned grandchildren are reparented to us, not
    # PID 1. Playwright deliberately detaches Chromium, so a PG alone is unsafe.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot enable Linux child subreaper')


def _info(pid):
    try:
        # comm may contain spaces or parentheses; fields after the LAST ')' are
        # state (3), ppid (4), ... starttime (22). starttime prevents PID reuse.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        return int(fields[1]), int(fields[19]), fields[0]
    except (OSError, ValueError, IndexError):
        return None


def descendants(root=None):
    root = os.getpid() if root is None else root
    table = {int(p.name): _info(int(p.name)) for p in Path('/proc').iterdir() if p.name.isdigit()}
    found = {}
    parents = {root}
    while parents:
        children = {pid for pid, info in table.items() if info and info[0] in parents and pid not in found}
        for pid in children:
            found[pid] = table[pid][1]
        parents = children
    return found


def living(identities):
    result = {}
    for pid, start in identities.items():
        info = _info(pid)
        if info and info[1] == start and info[2] not in ('Z', 'X'):
            result[pid] = start
    return result


def _send(identities, sig):
    for pid in living(identities):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def cleanup(worker):
    """Bounded TERM/KILL/reap, covering both descendant groups and orphans."""
    known = descendants()
    _send(known, signal.SIGTERM)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        worker.poll()  # Reap our direct child separately from adopted children.
        known.update(descendants())
        if not living(known):
            break
        time.sleep(.02)
    known.update(descendants())
    _send(known, signal.SIGKILL)
    try:
        worker.wait(timeout=.5)
    except subprocess.TimeoutExpired:
        pass  # Report any surviving processes rather than waiting unboundedly.
    deadline = time.monotonic() + .5
    while time.monotonic() < deadline:
        known.update(descendants())
        _send(known, signal.SIGKILL)
        # Adopted processes are ours to reap after the worker has been reaped.
        if worker.poll() is not None:
            while True:
                try:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                    if pid == 0:
                        break
                except ChildProcessError:
                    break
        if not living(known):
            return []
        time.sleep(.02)
    return sorted(living(known))
