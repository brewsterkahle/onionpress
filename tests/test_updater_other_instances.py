#!/usr/bin/env python3
"""onionpress.updater's multi-user checks before an update (CLAUDE.md,
Multi-User Support): the update replaces the one /Applications bundle every
macOS user runs, so it has to know who else is running it.

What is held:

- detect_other_instances reads `ps aux` (every user's processes, no
  privilege), counts only OnionPress MenubarApp lines, never the caller's
  own user (its helpers included), and a ps that fails or hangs is "no
  one else" rather than an exception;
- detect_active_offsets is a socket bind test on 8080 + each user slot
  (never lsof, which sees only the caller's processes): a port that
  cannot be bound is an offset in use.

ps and the sockets are stand-ins: nothing is run or bound for real.
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import updater  # noqa: E402

MENUBAR = ("/Applications/OnionPress.app/Contents/Resources/MenubarApp/"
           "Contents/MacOS/MenubarApp")

PS_HEADER = "USER  PID  %CPU %MEM VSZ RSS TT STAT STARTED TIME COMMAND"


def _ps_line(user, pid, command):
    return f"{user} {pid} 0.0 0.1 1 2 ?? S 9:00AM 0:01.00 {command}"


def _ps(lines, rc=0):
    out = "\n".join([PS_HEADER, *lines]) + "\n"
    return mock.Mock(return_value=subprocess.CompletedProcess(["ps", "aux"], rc, stdout=out))


class TestDetectOtherInstances(unittest.TestCase):

    def detect(self, run, user="alice"):
        with mock.patch.object(updater.subprocess, "run", run), \
                mock.patch.dict(os.environ, {"USER": user}):
            return updater.detect_other_instances()

    def test_other_users_menubars_are_found(self):
        run = _ps([
            _ps_line("alice", 100, MENUBAR),
            _ps_line("bob", 200, MENUBAR),
            _ps_line("carol", 300, MENUBAR),
        ])
        self.assertEqual(self.detect(run),
                         [{"user": "bob", "pid": 200}, {"user": "carol", "pid": 300}])
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["ps", "aux"])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_the_callers_own_processes_never_count(self):
        run = _ps([
            _ps_line("alice", os.getpid(), MENUBAR),
            _ps_line("alice", 4242, MENUBAR + " --helper"),
        ])
        self.assertEqual(self.detect(run), [])

    def test_only_onionpress_menubar_lines_count(self):
        run = _ps([
            _ps_line("bob", 200, "/Applications/Other.app/Contents/MacOS/MenubarApp"),
            _ps_line("bob", 201, "/Applications/OnionPress.app/Contents/MacOS/onionpress"),
            _ps_line("dave", 202, "/usr/bin/vim /tmp/OnionPress-notes.txt"),
            "bob",                                         # cut-off line
            "MenubarApp OnionPress",                       # no pid column
            _ps_line("erin", "x", MENUBAR).replace(" x ", " notapid "),
        ])
        self.assertEqual(self.detect(run), [])

    def test_a_failing_or_missing_ps_is_no_one_else(self):
        for exc in (subprocess.TimeoutExpired(["ps"], 5), FileNotFoundError("ps")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(self.detect(mock.Mock(side_effect=exc)), [])

    def test_the_primary_user_is_the_first_other_one(self):
        self.assertEqual(updater._primary_user([{"user": "bob", "pid": 1},
                                                {"user": "carol", "pid": 2}]), "bob")
        self.assertIsNone(updater._primary_user([]))


class _Socket:
    """socket.socket stand-in: ports in *taken* refuse bind."""

    def __init__(self, taken, closed, bound):
        self.taken, self.closed, self.bound = taken, closed, bound

    def bind(self, addr):
        host, port = addr
        assert host == "127.0.0.1", host
        if port in self.taken:
            raise OSError(48, "Address already in use")
        self.bound.append(port)

    def close(self):
        self.closed.append(True)


class TestDetectActiveOffsets(unittest.TestCase):

    def offsets(self, taken, socket_raises=None):
        closed, bound = [], []

        def factory(*a, **k):
            if socket_raises:
                raise socket_raises
            return _Socket(taken, closed, bound)
        with mock.patch.object(updater.socket, "socket", factory):
            result = updater.detect_active_offsets()
        return result, closed, bound

    def test_ports_in_use_are_the_active_offsets(self):
        result, closed, bound = self.offsets({8080, 28080})
        self.assertEqual(result, [0, 20000])
        self.assertEqual(bound, [18080, 38080, 48080])
        # every socket is closed, bound or not: a probe holds no port
        self.assertEqual(len(closed), 5)

    def test_nothing_running(self):
        self.assertEqual(self.offsets(set())[0], [])

    def test_all_five_slots_taken(self):
        self.assertEqual(self.offsets({8080, 18080, 28080, 38080, 48080})[0],
                         [0, 10000, 20000, 30000, 40000])

    def test_no_socket_at_all_is_no_offset(self):
        self.assertEqual(self.offsets(set(), socket_raises=OSError("EMFILE"))[0], [])

    def test_it_is_a_bind_test_not_lsof(self):
        import inspect
        src = inspect.getsource(updater.detect_active_offsets)
        self.assertIn(".bind(", src)
        self.assertNotIn("lsof", src)


if __name__ == "__main__":
    unittest.main()
