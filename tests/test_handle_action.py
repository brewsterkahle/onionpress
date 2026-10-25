#!/usr/bin/env python3
"""`onionpress handle-action` (onionpress.handle_action, issue #366): the
WordPress Settings page's requests, handled once for both platforms.

The handler runs against a stub launcher (a script that records every
call, its environment and stdin, and answers as told) and a stub Docker
(the shared volume as a dict; the tor container's hostname; `docker ps`).
No real docker: DOCKER_HOST points at nothing, and the launcher is never
the real one.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import backup, confirm, handle_action as ha, wp_files  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402
from onionpress.platform import resolve_paths  # noqa: E402

ADDR = "a" * 56 + ".onion"
NEW_ADDR = "b" * 56 + ".onion"
PW = "page-pw"

# The stub launcher: records argv, the password/confirm environment and
# stdin, answers from $LAUNCHER_SCRIPT (JSON: first argument -> {"rc",
# "stdout", "stderr", "write"}), and may write a file ("write": path).
LAUNCHER_PY = r'''
import json, os, sys
calls = os.environ["LAUNCHER_CALLS"]
script = json.load(open(os.environ["LAUNCHER_SCRIPT"]))
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
rec = {"argv": sys.argv[1:], "stdin": stdin,
       "env": {k: v for k, v in os.environ.items()
               if k in ("ONIONPRESS_BACKUP_PASSWORD", "ONIONPRESS_CONFIRMED",
                        "ONIONPRESS_IN_WATCHER", "ONIONPRESS_REQUEST_ID")}}
with open(calls, "a") as f:
    f.write(json.dumps(rec) + "\n")
key = " ".join(sys.argv[1:])
ans = None
for k in sorted(script, key=len, reverse=True):
    if key == k or key.startswith(k + " ") or sys.argv[1] == k:
        ans = script[k]
        break
ans = ans or {}
if ans.get("write"):
    with open(ans["write"], "wb") as f:
        f.write(b"zip bytes")
sys.stdout.write(ans.get("stdout", ""))
sys.stderr.write(ans.get("stderr", ""))
sys.exit(ans.get("rc", 0))
'''


class FakeDocker:
    """The docker the handler sees: the shared volume (www-data's) as
    files in a sandbox folder, the tor hostname, `docker ps` lines."""

    def __init__(self, sandbox, *, address=ADDR, running=True, curl_code="200"):
        self.sandbox = sandbox
        self.shared = os.path.join(sandbox, "shared")
        os.makedirs(self.shared, exist_ok=True)
        self.address = address
        self.running = running
        self.curl_code = curl_code
        self.calls = []
        self.cp_target = None          # what `docker cp` leaves at the copy
        # The hand-off runs `docker exec -i` itself, on this binary.
        self.docker_bin = os.path.join(sandbox, "docker")
        with open(self.docker_bin, "w") as f:
            f.write('#!/bin/bash\n'
                    '# docker exec -i -u www-data onionpress-wordpress sh -c SCRIPT sh PATH\n'
                    'echo "docker $*" >> "$SANDBOX/docker-calls"\n'
                    'shift 7; exec sh -c "$1" sh "$SANDBOX/shared/${3#/var/lib/onionpress/}"\n')
        os.chmod(self.docker_bin, 0o755)
        self.env = {"SANDBOX": sandbox, "PATH": os.environ.get("PATH", "")}

    def _shared_path(self, path):
        assert path.startswith(wp_files.SHARED_DIR + "/"), path
        return os.path.join(self.shared, path[len(wp_files.SHARED_DIR) + 1:])

    def put(self, name, text):
        with open(os.path.join(self.shared, name), "w") as f:
            f.write(text)

    def get(self, name):
        try:
            with open(os.path.join(self.shared, name)) as f:
                return f.read()
        except OSError:
            return None

    def result(self, name):
        text = self.get(name)
        return json.loads(text) if text else None

    def run(self, args, timeout=30, check=False, input=None, extra_env=None, quiet=False):
        args = [str(a) for a in args]
        self.calls.append(args)
        if args[:4] == ["exec", "-u", "www-data", "onionpress-wordpress"]:
            cmd = args[4:]
            if cmd[0] == "cat":
                text = self.get(os.path.basename(cmd[1]))
                return DockerResult(0 if text is not None else 1, text or "", "")
            if cmd[0] == "rm":
                for p in cmd[2:]:
                    try:
                        os.remove(self._shared_path(p))
                    except OSError:
                        pass
                return DockerResult(0, "", "")
            if cmd[0] == "stat":
                try:
                    return DockerResult(0, str(os.path.getsize(self._shared_path(cmd[-1]))), "")
                except OSError:
                    return DockerResult(1, "", "no such file")
        if args[:5] == ["exec", "-i", "-u", "www-data", "onionpress-wordpress"]:
            # wp_files.put_file_args: sh -c SCRIPT sh PATH MODE
            path, mode = args[-2], args[-1]
            with open(self._shared_path(path), "w") as f:
                f.write(input or "")
            return DockerResult(0, "", "")
        if args[:3] == ["exec", "onionpress-tor", "cat"]:
            return (DockerResult(0, self.address + "\n", "") if self.address
                    else DockerResult(1, "", "no such file"))
        if args[:3] == ["exec", "onionheaven", "curl"]:
            return DockerResult(0, self.curl_code, "")
        if args[0] == "ps":
            if not self.running:
                return DockerResult(0, "", "")
            return DockerResult(0, "onionpress-tor\tUp 2 minutes\nonionpress-wordpress\tUp 2 minutes\n", "")
        if args[0] == "cp":
            src, dest = args[1], args[2]
            if self.cp_target == "link":
                os.symlink("/etc/hostname", dest)
            elif self.cp_target == "fifo":
                os.mkfifo(dest)
            elif self.cp_target == "missing":
                return DockerResult(1, "", "no such file")
            else:
                try:
                    shutil.copy(self._shared_path(src.split(":", 1)[1]), dest)
                except OSError:
                    return DockerResult(1, "", "no such file")
            return DockerResult(0, "", "")
        raise AssertionError(f"unexpected docker call: {args}")


class _Case(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-handle-action-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir)
        self.docker = FakeDocker(os.path.join(self.tmp, "sandbox"))
        self.calls_file = os.path.join(self.tmp, "launcher-calls")
        self.script_file = os.path.join(self.tmp, "launcher-script.json")
        self.script({})
        py = os.path.join(self.tmp, "launcher.py")
        with open(py, "w") as f:
            f.write(LAUNCHER_PY)
        self.launcher = os.path.join(self.tmp, "onionpress")
        with open(self.launcher, "w") as f:
            f.write(f'#!/bin/bash\nexec "{sys.executable}" -I "{py}" "$@"\n')
        os.chmod(self.launcher, 0o755)
        self.logs = []
        self.env = mock.patch.dict(os.environ, {
            "LAUNCHER_CALLS": self.calls_file, "LAUNCHER_SCRIPT": self.script_file,
            "DOCKER_HOST": "unix:///var/empty/no-docker-for-tests.sock"})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("ONIONPRESS_CONFIRMED", None)
        # No sleeping while the stub "starts" the containers.
        p = mock.patch.object(ha.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def script(self, answers):
        with open(self.script_file, "w") as f:
            json.dump(answers, f)

    def host(self, os_name="macos", **kw):
        paths = resolve_paths(data_dir=self.data_dir, app_bundle="")
        return ha.Host(launcher=self.launcher, paths=paths, docker=self.docker,
                       log=self.logs.append, os_name=os_name, **kw)

    def launcher_calls(self):
        try:
            with open(self.calls_file) as f:
                return [json.loads(line) for line in f if line.strip()]
        except OSError:
            return []

    def argvs(self):
        return [c["argv"] for c in self.launcher_calls()]

    def request(self, action):
        self.docker.put("requested-action", action + "\n")


class TestQuietTick(_Case):

    def test_no_request_does_nothing_on_a_mac(self):
        self.assertEqual(ha.handle(self.host()), {"handled": None})
        self.assertEqual(self.argvs(), [])

    def test_linux_refreshes_the_logs(self):
        log_file = os.path.join(self.data_dir, "onionpress.log")
        with open(log_file, "w") as f:
            f.write(f"[2026-10-22 12:00:00] started for {os.path.expanduser('~')}/x\n")
        self.assertEqual(ha.handle(self.host("linux")), {"handled": None})
        text = self.docker.get("recent-logs.txt")
        self.assertIn("started for", text)
        self.assertNotIn(os.path.expanduser("~"), text)

    def test_unknown_action_is_logged_and_dropped(self):
        self.request("drop-tables")
        self.assertEqual(ha.handle(self.host()), {"handled": None, "unknown": "drop-tables"})
        self.assertIsNone(self.docker.get("requested-action"))
        self.assertIn("unknown action 'drop-tables'", "\n".join(self.logs))
        self.assertEqual(self.argvs(), [])

    def test_every_verb_is_known(self):
        self.assertEqual(ha.ACTIONS, ("start", "restart", "stop", "check-reachability",
                                      "import-key-file", "create-backup", "restore-backup",
                                      "refresh-status", "update"))


class TestStartStop(_Case):

    def test_restart_applies_config_then_restarts(self):
        self.request("restart")
        for os_name in ("macos", "linux"):
            with self.subTest(os_name=os_name):
                open(self.calls_file, "w").close()
                self.request("restart")
                result = ha.handle(self.host(os_name))
                self.assertEqual(result["handled"], "restart")
                want = [["apply-config"], ["restart"]]
                if os_name == "linux":
                    want.append(["write-status"])
                self.assertEqual(self.argvs(), want)
                res = self.docker.result("service-result.json")
                self.assertTrue(res["success"])
                self.assertEqual(res["action"], "restart")
                self.assertIn("tor: Up", res["message"])
                self.assertIsNone(self.docker.get("service-pending"))
                self.assertIsNone(self.docker.get("requested-action"))

    def test_a_failed_start_is_the_pages_failure(self):
        self.script({"start": {"rc": 1, "stderr": "ERROR: no docker"}})
        self.request("start")
        ha.handle(self.host())
        res = self.docker.result("service-result.json")
        self.assertFalse(res["success"])
        self.assertIn("start failed", res["message"])

    def test_stop(self):
        self.request("stop")
        ha.handle(self.host())
        self.assertEqual(self.argvs(), [["stop"]])
        self.assertEqual(self.docker.result("service-result.json")["message"], "OnionPress stopped")

    def test_refresh_status(self):
        self.request("refresh-status")
        self.assertEqual(ha.handle(self.host("linux"))["handled"], "refresh-status")
        self.assertEqual(self.argvs(), [["write-status"]])
        self.request("refresh-status")
        ha.handle(self.host("macos"))
        self.assertEqual(self.argvs(), [["write-status"]])     # the menubar writes it


class TestReachability(_Case):

    def test_reachable(self):
        self.request("check-reachability")
        ha.handle(self.host())
        res = self.docker.result("reachability-result.json")
        self.assertEqual((res["reachable"], res["http_code"], res["address"]), (True, "200", ADDR))

    def test_no_address(self):
        self.docker.address = ""
        self.request("check-reachability")
        ha.handle(self.host())
        res = self.docker.result("reachability-result.json")
        self.assertEqual(res["reachable"], False)
        self.assertIn("No onion address", res["error"])

    def test_curl_failure(self):
        self.docker.curl_code = ""
        self.request("check-reachability")
        ha.handle(self.host())
        res = self.docker.result("reachability-result.json")
        self.assertEqual((res["reachable"], res["http_code"]), (False, "000"))


class TestKeyImport(_Case):
    KEY = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" * 3

    def test_linux_refuses_and_removes_the_key(self):
        self.docker.put("import-key-data", self.KEY)
        self.request("import-key-file")
        ha.handle(self.host("linux"))
        res = self.docker.result("import-result.json")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], ha.IMPORT_REFUSED_LINUX_MSG)
        self.assertIsNone(self.docker.get("import-key-data"))
        self.assertEqual(self.argvs(), [])

    def test_no_key(self):
        self.request("import-key-file")
        ha.handle(self.host())
        self.assertEqual(self.docker.result("import-result.json")["error"], ha.NO_KEY_MSG)

    def _confirm_answer(self):
        return {"import-key - --check --json": {
            "rc": confirm.EXIT_CONFIRM,
            "stdout": json.dumps({"confirm": "address-change", "current": ADDR, "new": NEW_ADDR}) + "\n"}}

    def test_the_mac_stops_at_the_question_and_keeps_the_key_privately(self):
        self.script(self._confirm_answer())
        self.docker.put("import-key-data", self.KEY)
        self.request("import-key-file")
        result = ha.handle(self.host())
        self.assertEqual(result, {"handled": "import-key-file", "confirm": "address-change",
                                  "current": ADDR, "new": NEW_ADDR})
        # The key went to the launcher on stdin, never on argv
        calls = self.launcher_calls()
        self.assertEqual([c["argv"] for c in calls], [["import-key", "-", "--check", "--json"]])
        self.assertEqual(calls[0]["stdin"], self.KEY + "\n")
        self.assertIsNone(self.docker.get("import-result.json"))
        pending = os.path.join(self.data_dir, ha.PENDING_DIR)
        self.assertEqual(stat.S_IMODE(os.stat(pending).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(pending, "key")).st_mode), 0o600)
        with open(os.path.join(pending, "request.json")) as f:
            req = json.load(f)
        self.assertEqual((req["action"], req["confirm"]), ("import-key-file", "address-change"))
        self.assertNotIn(self.KEY, json.dumps(req))
        # Another tick while the dialog is up: nothing else is handled
        self.request("stop")
        result = ha.handle(self.host())
        self.assertEqual(result["pending"], "address-change")
        self.assertEqual(self.docker.get("requested-action"), "stop\n")
        self.assertEqual(len(self.launcher_calls()), 1)

    def test_replace_runs_the_import_with_the_confirmation_then_restarts(self):
        self.script(self._confirm_answer())
        self.docker.put("import-key-data", self.KEY)
        self.request("import-key-file")
        ha.handle(self.host())
        self.docker.address = NEW_ADDR
        result = ha.handle(self.host(), confirmed=frozenset({"address-change"}))
        self.assertEqual(result, {"handled": "import-key-file"})
        calls = self.launcher_calls()
        # The confirmed run checks the key again (it is the same stdin
        # hand-over), then imports and restarts.
        self.assertEqual([c["argv"] for c in calls][1:], [
            ["import-key", "-", "--check", "--json"],
            ["import-key", "-", "--confirmed", "address-change"], ["restart"]])
        self.assertEqual(calls[2]["stdin"], self.KEY + "\n")
        res = self.docker.result("import-result.json")
        self.assertEqual((res["success"], res["address"]), (True, NEW_ADDR))
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, ha.PENDING_DIR)))

    def test_cancel_writes_the_refusal(self):
        self.script(self._confirm_answer())
        self.docker.put("import-key-data", self.KEY)
        self.request("import-key-file")
        ha.handle(self.host())
        result = ha.handle(self.host(), cancel=True)
        self.assertEqual(result, {"handled": "import-key-file", "cancelled": "address-change"})
        res = self.docker.result("import-result.json")
        self.assertEqual((res["success"], res["error"]), (False, ha.IMPORT_NOT_CONFIRMED_MSG))
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, ha.PENDING_DIR)))
        self.assertEqual(len(self.launcher_calls()), 1)

    def test_no_answer_in_time_is_cancel(self):
        self.script(self._confirm_answer())
        self.docker.put("import-key-data", self.KEY)
        self.request("import-key-file")
        ha.handle(self.host())
        req_path = os.path.join(self.data_dir, ha.PENDING_DIR, "request.json")
        with open(req_path) as f:
            req = json.load(f)
        req["asked_at"] = time.time() - ha.PENDING_MAX_AGE - 1
        with open(req_path, "w") as f:
            json.dump(req, f)
        self.request("stop")
        result = ha.handle(self.host())
        self.assertEqual(result["handled"], "stop")
        res = self.docker.result("import-result.json")
        self.assertEqual(res["error"], ha.IMPORT_NOT_CONFIRMED_MSG)
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, ha.PENDING_DIR)))

    def test_the_same_key_needs_no_question(self):
        self.script({"import-key - --check --json": {
            "stdout": json.dumps({"ok": True, "address": ADDR, "unchanged": True}) + "\n"}})
        self.docker.put("import-key-data", self.KEY)
        self.request("import-key-file")
        self.assertEqual(ha.handle(self.host()), {"handled": "import-key-file"})
        res = self.docker.result("import-result.json")
        self.assertEqual((res["success"], res["address"]), (True, ADDR))
        self.assertEqual(len(self.launcher_calls()), 1)

    def test_a_refused_key_never_reaches_the_dialog(self):
        self.script({"import-key - --check --json": {
            "rc": 1, "stderr": "ERROR: Not a usable key: top bit set\n"}})
        self.docker.put("import-key-data", "garbage")
        self.request("import-key-file")
        result = ha.handle(self.host())
        self.assertNotIn("confirm", result)
        res = self.docker.result("import-result.json")
        self.assertFalse(res["success"])
        self.assertIn("top bit", res["error"])
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, ha.PENDING_DIR)))


class TestCreateBackup(_Case):

    def test_no_password(self):
        self.request("create-backup")
        ha.handle(self.host())
        self.assertEqual(self.docker.result("backup-result.json")["error"], ha.NO_PASSWORD_MSG)
        self.assertEqual(self.argvs(), [])

    def test_the_backup_is_made_through_the_launcher_and_handed_to_wordpress(self):
        self._backup_stub(json.dumps({"ok": True, "note": backup.ONION_KEY_LEFT_OUT_NOTE}) + "\n")
        self.docker.put("backup-password", PW + "\n")
        self.request("create-backup")
        ha.handle(self.host())
        calls = self.launcher_calls()
        self.assertEqual(len(calls), 1)
        argv = calls[0]["argv"]
        self.assertEqual(argv[0], "settings-page-backup")
        self.assertRegex(os.path.basename(argv[1]), r"^OnionPress-a{8}-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}\.zip$")
        # The password in the environment, never on argv
        self.assertEqual(calls[0]["env"]["ONIONPRESS_BACKUP_PASSWORD"], PW)
        self.assertNotIn(PW, " ".join(argv))
        self.assertIsNone(self.docker.get("backup-password"))
        res = self.docker.result("backup-result.json")
        self.assertTrue(res["success"])
        self.assertEqual(res["filename"], os.path.basename(argv[1]))
        self.assertEqual(res["note"], backup.ONION_KEY_LEFT_OUT_NOTE)
        # Handed over as www-data with umask 077, the bytes whole
        with open(os.path.join(self.tmp, "sandbox", "docker-calls")) as f:
            handoff = f.read()
        self.assertIn('docker exec -i -u www-data onionpress-wordpress sh -c umask 077; cat > "$1" sh '
                      f'/var/lib/onionpress/{res["filename"]}', handoff)
        self.assertEqual(self.docker.get(res["filename"]), "zip bytes")
        # The local copy is gone
        self.assertFalse(os.path.exists(os.path.dirname(argv[1])))

    def _backup_stub(self, stdout, rc=0):
        """A launcher whose settings-page-backup writes the zip at argv[2]."""
        py = os.path.join(self.tmp, "launcher.py")
        with open(py, "w") as f:
            f.write(LAUNCHER_PY.replace(
                'if ans.get("write"):',
                'if sys.argv[1] == "settings-page-backup" and %r == 0:\n'
                '    open(sys.argv[2], "wb").write(b"zip bytes")\n'
                'if ans.get("write"):' % rc))
        self.script({"settings-page-backup": {"rc": rc, "stdout": stdout}})

    def test_a_refusal_is_the_pages_error_and_nothing_is_handed_over(self):
        self._backup_stub(backup.HUB_BACKUP_REFUSED_MSG + "\n", rc=confirm.EXIT_REFUSED)
        self.docker.put("backup-password", PW)
        self.request("create-backup")
        ha.handle(self.host())
        res = self.docker.result("backup-result.json")
        self.assertEqual((res["success"], res["error"]), (False, backup.HUB_BACKUP_REFUSED_MSG))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "sandbox", "docker-calls")))

    def test_a_json_refusal_too(self):
        self._backup_stub(json.dumps({"error": backup.ONIONHOME_BACKUP_REFUSED_MSG}) + "\n",
                          rc=confirm.EXIT_REFUSED)
        self.docker.put("backup-password", PW)
        self.request("create-backup")
        ha.handle(self.host())
        self.assertEqual(self.docker.result("backup-result.json")["error"],
                         backup.ONIONHOME_BACKUP_REFUSED_MSG)

    def test_a_failure(self):
        self._backup_stub("", rc=1)
        self.docker.put("backup-password", PW)
        self.request("create-backup")
        ha.handle(self.host())
        self.assertEqual(self.docker.result("backup-result.json")["error"], ha.BACKUP_FAILED_MSG)

    def test_errors_are_scrubbed(self):
        home = os.path.expanduser("~")
        self._backup_stub(json.dumps({"error": f"cannot write {home}/.onionpress/x"}) + "\n", rc=1)
        self.docker.put("backup-password", PW)
        self.request("create-backup")
        ha.handle(self.host())
        self.assertNotIn(home, self.docker.get("backup-result.json"))


class TestRestoreBackup(_Case):

    def _upload(self, content=b"PK\x03\x04zip"):
        with open(os.path.join(self.docker.shared, "restore-upload.zip"), "wb") as f:
            f.write(content)

    def test_missing_password_or_file(self):
        self.request("restore-backup")
        ha.handle(self.host())
        self.assertEqual(self.docker.result("restore-result.json")["error"], ha.MISSING_UPLOAD_MSG)
        self.docker.put("restore-password", PW)
        self.docker.cp_target = "missing"
        self.request("restore-backup")
        ha.handle(self.host())
        self.assertEqual(self.docker.result("restore-result.json")["error"], ha.MISSING_UPLOAD_MSG)
        self.assertEqual(self.argvs(), [])

    def test_a_link_or_fifo_is_refused_unread(self):
        for kind in ("link", "fifo"):
            with self.subTest(kind=kind):
                self.docker.put("restore-password", PW)
                self._upload()
                self.docker.cp_target = kind
                self.request("restore-backup")
                ha.handle(self.host())
                res = self.docker.result("restore-result.json")
                self.assertFalse(res["success"])
                self.assertIn("not a regular file", res["error"])
                self.assertEqual(self.argvs(), [])
                self.assertIsNone(self.docker.get("restore-upload.zip"))

    def test_the_copy_is_sized_and_removed(self):
        self.script({"settings-page-restore": {"stdout": '{"ok": true}\n'}})
        self.docker.put("restore-password", PW)
        self._upload(b"x" * 3000)
        self.request("restore-backup")
        ha.handle(self.host())
        stat_call = [c for c in self.docker.calls if "stat" in c]
        self.assertEqual(len(stat_call), 1)
        calls = self.launcher_calls()
        self.assertEqual(calls[0]["argv"][0], "settings-page-restore")
        self.assertEqual(calls[0]["argv"][2:], ["--json"])
        self.assertEqual(calls[0]["env"]["ONIONPRESS_BACKUP_PASSWORD"], PW)
        self.assertNotIn(PW, " ".join(calls[0]["argv"]))
        local_zip = calls[0]["argv"][1]
        self.assertTrue(os.path.basename(os.path.dirname(local_zip)).startswith("onionpress-restore-upload-"))
        self.assertFalse(os.path.exists(os.path.dirname(local_zip)))
        self.assertEqual([c["argv"] for c in calls][1:], [["restart"]])
        res = self.docker.result("restore-result.json")
        self.assertEqual((res["success"], res["address"]), (True, ADDR))

    def test_a_refusal_reaches_the_page(self):
        self.script({"settings-page-restore": {
            "rc": confirm.EXIT_REFUSED,
            "stdout": json.dumps({"error": backup.HUB_DATA_RESTORE_REFUSED_MSG}) + "\n"}})
        self.docker.put("restore-password", PW)
        self._upload()
        self.request("restore-backup")
        ha.handle(self.host())
        res = self.docker.result("restore-result.json")
        self.assertEqual((res["success"], res["error"]), (False, backup.HUB_DATA_RESTORE_REFUSED_MSG))
        self.assertEqual(len(self.launcher_calls()), 1)        # no restart

    def test_a_bash_refusal_is_its_last_line(self):
        self.script({"settings-page-restore": {
            "rc": confirm.EXIT_REFUSED,
            "stdout": "Restore: extracting...\n" + backup.ADDRESS_CHANGE_REFUSED_MSG + "\n"}})
        self.docker.put("restore-password", PW)
        self._upload()
        self.request("restore-backup")
        ha.handle(self.host("linux"))
        self.assertEqual(self.docker.result("restore-result.json")["error"],
                         backup.ADDRESS_CHANGE_REFUSED_MSG)

    def test_a_failure(self):
        self.script({"settings-page-restore": {"rc": 1, "stdout": ""}})
        self.docker.put("restore-password", PW)
        self._upload()
        self.request("restore-backup")
        ha.handle(self.host())
        self.assertEqual(self.docker.result("restore-result.json")["error"], ha.RESTORE_FAILED_MSG)

    def test_another_address_waits_for_the_person_at_the_mac(self):
        # The CLI refuses the unconfirmed run (exit 3) naming the question
        question = {"confirm": "address-change", "current": ADDR, "new": NEW_ADDR}
        self.script({"settings-page-restore": {
            "rc": confirm.EXIT_REFUSED,
            "stdout": json.dumps({"error": "needs a confirmation", **question}) + "\n"}})
        self.docker.put("restore-password", PW)
        self._upload(b"the upload")
        self.request("restore-backup")
        result = ha.handle(self.host())
        self.assertEqual(result, {"handled": "restore-backup", **question})
        pending = os.path.join(self.data_dir, ha.PENDING_DIR)
        with open(os.path.join(pending, "restore-upload.zip"), "rb") as f:
            self.assertEqual(f.read(), b"the upload")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(pending, "password")).st_mode), 0o600)
        self.assertIsNone(self.docker.get("restore-result.json"))
        # Restore: the confirmed run, the password from the private copy
        self.script({"settings-page-restore": {"stdout": '{"ok": true}\n'}})
        self.docker.address = NEW_ADDR
        result = ha.handle(self.host(), confirmed=frozenset({"address-change"}))
        self.assertEqual(result, {"handled": "restore-backup"})
        calls = self.launcher_calls()
        self.assertEqual(calls[1]["argv"][2:], ["--json", "--confirmed", "address-change"])
        self.assertEqual(calls[1]["env"]["ONIONPRESS_BACKUP_PASSWORD"], PW)
        self.assertEqual(calls[2]["argv"], ["restart"])
        self.assertEqual(self.docker.result("restore-result.json")["address"], NEW_ADDR)
        self.assertFalse(os.path.exists(pending))

    def test_cancel_refuses_the_restore(self):
        question = {"confirm": "address-change", "current": ADDR, "new": NEW_ADDR}
        self.script({"settings-page-restore": {"rc": confirm.EXIT_CONFIRM,
                                               "stdout": json.dumps(question) + "\n"}})
        self.docker.put("restore-password", PW)
        self._upload()
        self.request("restore-backup")
        ha.handle(self.host())
        ha.handle(self.host(), cancel=True)
        res = self.docker.result("restore-result.json")
        self.assertEqual(res["error"], backup.ADDRESS_CHANGE_NOT_CONFIRMED_MSG)
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, ha.PENDING_DIR)))


class TestUpdate(_Case):

    def test_linux_only_checks(self):
        self.request("update")
        ha.handle(self.host("linux"))
        self.assertEqual(self.argvs(), [["settings-page-update-check"]])

    def test_mac_up_to_date_is_a_success(self):
        self.script({"update --check --json": {"stdout": json.dumps(
            {"update_available": False, "current": "2.5.6", "latest": "2.5.6"}) + "\n"}})
        self.request("update")
        result = ha.handle(self.host())
        self.assertNotIn("relaunch", result)
        res = self.docker.result("update-result.json")
        self.assertEqual((res["success"], res["update_available"]), (True, False))
        self.assertEqual(res["message"], "OnionPress 2.5.6 is up to date.")
        self.assertEqual(self.argvs(), [["update", "--check", "--json"]])

    def test_mac_failed_check_is_a_failure(self):
        self.script({"update --check --json": {"stdout": json.dumps(
            {"update_available": False, "error": "Update check curl failed: exit=6"}) + "\n"}})
        self.request("update")
        ha.handle(self.host())
        res = self.docker.result("update-result.json")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], f"{ha.UPDATE_CHECK_FAILED_MSG}: Update check curl failed: exit=6")

    def test_mac_reports_a_newer_release_and_never_installs(self):
        self.script({"update --check --json": {"stdout": json.dumps(
            {"update_available": True, "current": "2.5.6", "latest": "9.9.9"}) + "\n"}})
        self.request("update")
        result = ha.handle(self.host())
        self.assertEqual(result, {"handled": "update"})
        self.assertEqual(self.argvs(), [["update", "--check", "--json"]])
        res = self.docker.result("update-result.json")
        self.assertEqual((res["success"], res["update_available"], res["installable"],
                          res["current"], res["latest"]),
                         (True, True, True, "2.5.6", "9.9.9"))
        self.assertEqual(res["message"], ha.MAC_UPDATE_AVAILABLE_MSG.format(latest="9.9.9"))
        self.assertIn("Check for Updates", res["message"])

    def test_no_platform_installs_from_the_page(self):
        # Anything running in WordPress can write requested-action: the
        # handler may check for a release, never download or install one.
        src = open(ha.__file__).read()
        self.assertNotIn('"--yes"', src)
        self.assertNotIn("relaunch", src)


class TestRequestId(_Case):
    """Issue #356: the page's request-id comes back as request_id in the
    action's result, and the action's earlier result goes with the
    request, before the action runs."""

    def request_with_id(self, action, request_id):
        self.docker.put("request-id", request_id + "\n")
        self.request(action)

    def test_the_id_is_echoed_and_the_request_files_removed(self):
        self.request_with_id("check-reachability", "Ab9_-xyz")
        ha.handle(self.host())
        res = self.docker.result("reachability-result.json")
        self.assertEqual((res["reachable"], res["request_id"]), (True, "Ab9_-xyz"))
        self.assertIsNone(self.docker.get("request-id"))
        self.assertIsNone(self.docker.get("requested-action"))

    def test_no_id_means_no_echo(self):
        self.request("check-reachability")
        ha.handle(self.host())
        self.assertNotIn("request_id", self.docker.result("reachability-result.json"))

    def test_a_bad_id_is_dropped_not_echoed(self):
        for bad in ("../x", "a b", "x" * 65, "id;rm -rf /", "\u00e9", '"'):
            with self.subTest(bad=bad):
                self.request_with_id("check-reachability", bad)
                self.logs.clear()
                ha.handle(self.host())
                self.assertNotIn("request_id", self.docker.result("reachability-result.json"))
                self.assertIsNone(self.docker.get("request-id"))
                log = "\n".join(self.logs)
                self.assertIn("request id dropped", log)
                self.assertNotIn(bad, log)

    def test_the_checked_id(self):
        self.assertEqual(ha.checked_request_id(" abc \n"), "abc")
        self.assertEqual(ha.checked_request_id("x" * 64), "x" * 64)
        for bad in ("", None, 7, "x" * 65, "a.b", "a/b"):
            self.assertEqual(ha.checked_request_id(bad), "")

    def test_the_stale_result_is_removed_before_the_action_runs(self):
        for action, name in (("check-reachability", "reachability-result.json"),
                             ("create-backup", "backup-result.json"),
                             ("restore-backup", "restore-result.json"),
                             ("import-key-file", "import-result.json"),
                             ("update", "update-result.json"),
                             ("restart", "service-result.json")):
            with self.subTest(action=action):
                self.docker.put(name, json.dumps({"success": True, "stale": True}))
                self.docker.calls.clear()
                self.request_with_id(action, "req1")
                ha.handle(self.host("linux"))
                rm = [c for c in self.docker.calls
                      if c[:5] == ["exec", "-u", "www-data", "onionpress-wordpress", "rm"]
                      and f"{wp_files.SHARED_DIR}/{name}" in c]
                self.assertEqual(len(rm), 1, self.docker.calls)
                before = self.docker.calls[:self.docker.calls.index(rm[0])]
                self.assertTrue(all(c[4] == "cat" for c in before),
                                f"the removal comes right after the request is read: {before}")
                res = self.docker.result(name)
                if res is not None:        # Linux's update: the launcher writes it
                    self.assertNotIn("stale", res)
                    self.assertEqual(res["request_id"], "req1")

    def test_every_result_file_is_known(self):
        self.assertEqual(set(ha.RESULT_FILES), set(ha.ACTIONS) - {"refresh-status"})
        written = set(re.findall(r'write_result\("([a-z-]+\.json)"', open(ha.__file__).read()))
        self.assertEqual(written, set(ha.RESULT_FILES.values()))

    def test_linux_update_check_hands_the_id_to_the_launcher(self):
        self.request_with_id("update", "upd-1")
        ha.handle(self.host("linux"))
        calls = self.launcher_calls()
        self.assertEqual(calls[-1]["argv"], ["settings-page-update-check"])
        self.assertEqual(calls[-1]["env"].get("ONIONPRESS_REQUEST_ID"), "upd-1")

    def test_a_pending_question_keeps_the_id_for_its_answer(self):
        self.script(TestKeyImport._confirm_answer(self))
        self.docker.put("import-key-data", TestKeyImport.KEY)
        self.request_with_id("import-key-file", "imp-1")
        ha.handle(self.host())
        with open(os.path.join(self.data_dir, ha.PENDING_DIR, "request.json")) as f:
            self.assertEqual(json.load(f)["request_id"], "imp-1")
        self.docker.address = NEW_ADDR
        ha.handle(self.host(), confirmed=frozenset({"address-change"}))
        res = self.docker.result("import-result.json")
        self.assertEqual((res["success"], res["request_id"]), (True, "imp-1"))

    def test_a_cancelled_question_answers_with_its_own_id(self):
        self.script(TestKeyImport._confirm_answer(self))
        self.docker.put("import-key-data", TestKeyImport.KEY)
        self.request_with_id("import-key-file", "imp-2")
        ha.handle(self.host())
        ha.handle(self.host(), cancel=True)
        res = self.docker.result("import-result.json")
        self.assertEqual((res["error"], res["request_id"]), (ha.IMPORT_NOT_CONFIRMED_MSG, "imp-2"))

    def test_a_timed_out_question_then_the_next_request_each_get_their_id(self):
        self.script(TestKeyImport._confirm_answer(self))
        self.docker.put("import-key-data", TestKeyImport.KEY)
        self.request_with_id("import-key-file", "imp-3")
        ha.handle(self.host())
        req_path = os.path.join(self.data_dir, ha.PENDING_DIR, "request.json")
        with open(req_path) as f:
            req = json.load(f)
        req["asked_at"] = time.time() - ha.PENDING_MAX_AGE - 1
        with open(req_path, "w") as f:
            json.dump(req, f)
        self.request_with_id("check-reachability", "reach-1")
        ha.handle(self.host())
        self.assertEqual(self.docker.result("import-result.json")["request_id"], "imp-3")
        self.assertEqual(self.docker.result("reachability-result.json")["request_id"], "reach-1")


class TestApplyConfig(_Case):

    def test_hostile_payload_through_the_allowlist(self):
        from onionpress import config as op_config
        config = os.path.join(self.data_dir, "config")
        with open(config, "w") as f:
            f.write("LAUNCH_ON_LOGIN=yes\nPREVENT_SLEEP=normal\n")
        self.docker.put("config-updates.json", json.dumps({
            "LAUNCH_ON_LOGIN": "no", "PREVENT_SLEEP": "evil\n", "ONIONPRESS_TOR_IMAGE": "x",
            "CLOUDFLARE_TUNNEL_TOKEN": "A" * 40}))
        effects = []
        with mock.patch.object(op_config, "apply_side_effects",
                               side_effect=lambda keys, *a, **k: effects.append(sorted(keys)) or []):
            with open(os.devnull, "w") as devnull:
                accepted = ha.apply_config(self.host(), out=devnull)
        self.assertEqual(sorted(accepted), ["CLOUDFLARE_TUNNEL_TOKEN", "LAUNCH_ON_LOGIN"])
        values = op_config.read_config(config)
        self.assertEqual(values["LAUNCH_ON_LOGIN"], "no")
        self.assertEqual(values["PREVENT_SLEEP"], "normal")
        self.assertNotIn("ONIONPRESS_TOR_IMAGE", values)
        self.assertEqual(values["CLOUDFLARE_TUNNEL_TOKEN"], "A" * 40)
        self.assertIsNone(self.docker.get("config-updates.json"))
        self.assertEqual(sorted(op_config.take_config_changes(self.data_dir)),
                         ["CLOUDFLARE_TUNNEL_TOKEN", "LAUNCH_ON_LOGIN"])
        self.assertEqual(effects, [["CLOUDFLARE_TUNNEL_TOKEN", "LAUNCH_ON_LOGIN"]])
        log = "\n".join(self.logs)
        self.assertNotIn("A" * 40, log)
        self.assertNotIn("evil", log)
        self.assertIn("ONIONPRESS_TOR_IMAGE", log)

    def test_nothing_to_apply(self):
        self.assertEqual(ha.apply_config(self.host()), [])
        self.docker.put("config-updates.json", "{}")
        self.assertEqual(ha.apply_config(self.host()), [])

    def test_truncated_json_is_left_for_the_next_run(self):
        self.docker.put("config-updates.json", '{"UPDATE_ON_LAUNCH": "n')
        with open(os.devnull, "w") as devnull:
            self.assertIsNone(ha.apply_config(self.host(), out=devnull))
        self.assertEqual(self.docker.get("config-updates.json"), '{"UPDATE_ON_LAUNCH": "n')

    def test_the_mac_tick_applies_pending_settings(self):
        """QA on 2026-10-22: config-updates.json sat unapplied while the
        menubar ran handle-action every tick. Every Mac run applies it,
        request or not, and says which keys so the menubar applies what it
        owes at once; Linux's launcher does it itself (write_status)."""
        from onionpress import config as op_config
        config = os.path.join(self.data_dir, "config")
        with open(config, "w") as f:
            f.write("UPDATE_ON_LAUNCH=yes\n")
        self.docker.put("config-updates.json", json.dumps({"UPDATE_ON_LAUNCH": "no"}))
        with mock.patch.object(op_config, "apply_side_effects", return_value=[]):
            result = ha.handle(self.host("linux"))
        self.assertEqual(result, {"handled": None})
        self.assertEqual(op_config.read_value(config, "UPDATE_ON_LAUNCH"), "yes")
        with mock.patch.object(op_config, "apply_side_effects", return_value=[]):
            result = ha.handle(self.host("macos"))
        self.assertEqual(result, {"handled": None, "config": ["UPDATE_ON_LAUNCH"]})
        self.assertEqual(op_config.read_value(config, "UPDATE_ON_LAUNCH"), "no")
        self.assertIsNone(self.docker.get("config-updates.json"))
        self.assertEqual(op_config.take_config_changes(self.data_dir), ["UPDATE_ON_LAUNCH"])
        # With a request in the same tick, both happen
        self.docker.put("config-updates.json", json.dumps({"UPDATE_ON_LAUNCH": "yes"}))
        self.request("check-reachability")
        with mock.patch.object(op_config, "apply_side_effects", return_value=[]):
            result = ha.handle(self.host("macos"))
        self.assertEqual(result, {"handled": "check-reachability", "config": ["UPDATE_ON_LAUNCH"]})
        self.assertEqual(op_config.read_value(config, "UPDATE_ON_LAUNCH"), "yes")


class TestLaunchers(unittest.TestCase):
    """Both launchers delegate; Linux's bash bodies sit behind the three
    internal cases the handler runs."""

    def _read(self, rel):
        with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_linux_handle_action_is_a_delegation(self):
        linux = self._read("linux/onionpress")
        self.assertRegex(linux, r'\n        handle-action\)\n(?:            #.*\n)*'
                                r'            ONIONPRESS_IN_WATCHER=true op_py --data-dir "\$DATA_DIR" '
                                r'handle-action --launcher "\$INSTALL_DIR/onionpress" "\$\{@:2\}"\n'
                                r'            ;;\n')
        for case, body in (("settings-page-backup", 'create_backup "$BACKUP_PASS" "$2" --without-hub-data --without-host-secrets'),
                           ("settings-page-restore", 'restore_backup "$BACKUP_PASS" "$2" --from-settings-page'),
                           ("settings-page-update-check", "update_settings_page_check")):
            with self.subTest(case=case):
                start = linux.index(f"\n        {case})\n")
                branch = linux[start:linux.index("\n            ;;\n", start)]
                self.assertIn(body, branch)
        self.assertNotIn("                create-backup)\n", linux)
        self.assertIn("|handle-action|", linux)
        self.assertEqual(linux.count('umask 077; cat > "$1"'), 0)

    def test_mac_cases_delegate(self):
        mac = self._read("app/MacOS/onionpress")
        self.assertRegex(mac, r'\n        handle-action\)\n(?:            #.*\n)*'
                              r'            bundled_python -m onionpress\.cli --data-dir "\$DATA_DIR" '
                              r'handle-action --launcher "\$SCRIPT_DIR/onionpress" "\$\{@:2\}"\n')
        self.assertRegex(mac, r'\n        apply-config\)\n(?:            #.*\n)*'
                              r'            bundled_python -m onionpress\.cli --data-dir "\$DATA_DIR" apply-config\n')
        self.assertRegex(mac, r'\n        settings-page-backup\|settings-page-restore\)\n(?:            #.*\n)*'
                              r'            setup_db_passwords\n'
                              r'            bundled_python -m onionpress\.cli --data-dir "\$DATA_DIR" "\$1" "\$\{@:2\}"\n')
        self.assertEqual(mac.count('umask 077; cat > "$1"'), 0)

    def test_the_handoff_lives_in_the_handler_only(self):
        found = []
        for dirpath, _, files in os.walk(os.path.join(PROJECT_ROOT, "src")):
            for name in files:
                if name.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dirpath, name), PROJECT_ROOT)
                    if 'umask 077; cat > "$1"' in self._read(rel):
                        found.append(rel)
        self.assertEqual(found, ["src/onionpress/handle_action.py"])
        self.assertNotIn('umask 077; cat > "$1"', self._read("src/menubar.py"))

    def test_the_watcher_unit_runs_it(self):
        self.assertIn("ExecStart=/opt/onionpress/onionpress handle-action",
                      self._read("linux/onionpress-watcher.service"))

    def test_the_mac_filename_pattern(self):
        """tests/test_backup_download.py's pattern must match what the
        handler names a Settings page backup."""
        src = self._read("src/onionpress/handle_action.py")
        self.assertIn('filename = f"OnionPress-{onion_short}-{time.strftime(\'%Y-%m-%d-%H-%M\')}.zip"', src)
        self.assertIn('onion_short = addr.replace(".onion", "")[:8] if addr else "site"', src)


if __name__ == "__main__":
    unittest.main()
