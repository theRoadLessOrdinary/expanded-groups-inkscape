"""
Launcher for the Expanded Groups browse/Apply/Rename/Remove panel. This is
the script Inkscape's Extensions menu actually invokes. It does no GTK of
its own and makes no document edits -- it just snapshots the current
trlo:group state to a scratch JSON file and spawns expanded_groups_panel.py
as a fully detached process, then returns immediately. See
expanded_groups_data.py's module docstring for why: keeping this launcher's
own effect() call short is what keeps Inkscape from blocking for the whole
interactive session, and importing only that module (never
expanded_groups_dbus, which pulls in gi) is what keeps this launcher's
stderr silent.
"""

import inkex
import json
import os
import warnings
import sys
import tempfile
import time

from expanded_groups_data import snapshot, PANEL_PID_PATH, PANEL_FOCUS_REQUEST_PATH

VERSION = "2.4.0"


def panel_already_running():
    """True if a panel process is alive and holding PANEL_PID_PATH. A PID
    file left behind by a crashed panel (no clean exit to remove it) reads
    as not-running here, since kill(pid, 0) fails for a dead process."""
    try:
        with open(PANEL_PID_PATH) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def request_panel_focus():
    with open(PANEL_FOCUS_REQUEST_PATH, 'w') as f:
        f.write(str(time.time()))


def _proc_stat(pid):
    """(name, parent pid) of a process, from /proc, or None."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            data = f.read()
    except OSError:
        return None
    name = data[data.index('(') + 1:data.rindex(')')]
    return name, int(data[data.rindex(')') + 2:].split()[1])


def find_inkscape_pid():
    """Inkscape's pid, so the panel can close when Inkscape does. Extensions
    are not direct children of Inkscape, so walk up the ancestors, then fall
    back to the lowest-numbered inkscape process (same as the Tool Panel)."""
    pid = os.getppid()
    while pid > 1:
        stat = _proc_stat(pid)
        if stat is None:
            break
        if stat[0].startswith('inkscape'):
            return pid
        pid = stat[1]
    found = []
    for entry in os.listdir('/proc'):
        if entry.isdigit():
            stat = _proc_stat(int(entry))
            if stat and stat[0].startswith('inkscape'):
                found.append(int(entry))
    return min(found) if found else None


def spawn_detached(argv):
    """
    Classic UNIX double-fork daemonization: fully detaches argv as an
    independent process with no ongoing relationship to this one, instead
    of subprocess.Popen (which leaves a Popen object in this process
    tracking the child -- harmless, but Python's finalizer warns
    ResourceWarning: subprocess N is still running when that object gets
    garbage-collected at this short-lived script's exit, since the child is
    correctly still running at that point).

    First fork's child calls setsid() to start a new session (detach from
    Inkscape's process group/controlling terminal), then forks again and
    exits immediately; the parent (this process) waitpid()s that first
    child, which returns right away since it just exited. The *second*
    fork's child -- the one that actually execs the panel -- is now an
    orphan with no parent process tracking it at all, reparented to init/a
    subreaper, so nothing here or in Inkscape ever waits on it or holds a
    handle to it.
    """
    # Python warns about fork() in a multi-threaded process; Inkscape would
    # show that warning in a dialog that has to be dismissed.
    warnings.simplefilter('ignore')
    pid = os.fork()
    if pid == 0:
        # First child.
        os.setsid()
        pid2 = os.fork()
        if pid2 == 0:
            # Grandchild: this is the one that actually becomes the panel.
            devnull_fd = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull_fd, 0)
            os.dup2(devnull_fd, 1)
            os.dup2(devnull_fd, 2)
            os.execv(sys.executable, [sys.executable] + argv)
            os._exit(127)  # only reached if execv itself failed
        else:
            os._exit(0)  # first child's job is done; grandchild is now orphaned
    else:
        os.waitpid(pid, 0)  # reap the first child -- it exits immediately


class ExpandedGroupsLauncher(inkex.EffectExtension):

    def effect(self):
        if panel_already_running():
            request_panel_focus()
            return

        fd, snapshot_path = tempfile.mkstemp(prefix='expanded_groups_', suffix='.json')
        with os.fdopen(fd, 'w') as f:
            json.dump(snapshot(self.svg), f)

        script_dir = os.path.dirname(os.path.abspath(__file__))
        panel_script = os.path.join(script_dir, 'expanded_groups_panel.py')

        argv = [panel_script, snapshot_path]
        inkscape_pid = find_inkscape_pid()
        if inkscape_pid is not None:
            argv.append(str(inkscape_pid))
        spawn_detached(argv)

        # No self.svg changes were made, so Inkscape has nothing to reload.


if __name__ == '__main__':
    ExpandedGroupsLauncher().run()
