#!/usr/bin/env python3
"""The CLI's edges: what onionpress.app_commands, onionpress.confirm and
onionpress.handle_action do on the paths the main tests leave out (a
docker that raises, a clipboard tool that fails, a web log followed from
the container, an onionname the registrar cannot save, a Mac marker that
cannot be written, a launcher that times out, a backup hand-off that
fails, a confirmed key import that fails). Companion to
tests/test_cli_app_commands.py and tests/test_handle_action.py, whose
harnesses it reuses; nothing here reaches a container or the network.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import app_commands, confirm, handle_action as ha, onionheaven  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402
from onionpress.onionnames_registrar import RegistrarResult  # noqa: E402
from onionpress.platform import resolve_paths  # noqa: E402

from test_cli_app_commands import FakeDocker, StubRegistrar, with_address  # noqa: E402
from test_handle_action import _Case as HandleActionCase  # noqa: E402

ADDR = "a" * 56 + ".onion"
NEW_ADDR = "b" * 56 + ".onion"
HUB = "c" * 56 + ".onion"


class RaisingDocker(FakeDocker):
    def run(self, *a, **kw):
        raise OSError("no docker binary")


class Sandbox(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-cli-edges-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.paths = resolve_paths(data_dir=self.dir, app_bundle="")
        self.out = io.StringIO()
        self.err = io.StringIO()
        p = mock.patch.object(sys, "stderr", self.err)
        p.start()
        self.addCleanup(p.stop)
        self.logged = []

    def log(self, msg):
        self.logged.append(msg)

    def config(self, **values):
        with open(self.paths.config_file, "w") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


# ---------------------------------------------------------------------------
# app_commands
# ---------------------------------------------------------------------------

class TestServedAddress(Sandbox):

    def test_a_docker_that_raises_falls_back_on_the_cache(self):
        self.assertEqual(app_commands.served_address(RaisingDocker(), self.dir), "")
        with open(os.path.join(self.dir, app_commands.ADDRESS_CACHE), "w") as f:
            f.write(ADDR + "\n")
        self.assertEqual(app_commands.served_address(RaisingDocker(), self.dir), ADDR)
        with open(os.path.join(self.dir, app_commands.ADDRESS_CACHE), "w") as f:
            f.write("Starting...\n")
        self.assertEqual(app_commands.served_address(RaisingDocker(), self.dir), "")
        # Without a data dir there is no cache to read.
        self.assertEqual(app_commands.served_address(RaisingDocker()), "")


class TestClipboard(Sandbox):

    def test_a_tool_that_fails_is_the_error(self):
        with mock.patch("onionpress.app_commands.shutil.which",
                        side_effect=lambda name: "/usr/bin/wl-copy" if name == "wl-copy" else None), \
                mock.patch("onionpress.app_commands.subprocess.run",
                           return_value=mock.Mock(returncode=1, stderr=b"no wayland display\n")):
            with self.assertRaises(RuntimeError) as ctx:
                app_commands.copy_to_clipboard("x", "linux")
        self.assertEqual(str(ctx.exception), "wl-copy failed: no wayland display")

    def test_the_tools_per_platform(self):
        self.assertEqual(app_commands.clipboard_tools("macos"), [["pbcopy"]])
        self.assertEqual([t[0] for t in app_commands.clipboard_tools("linux")],
                         ["wl-copy", "xclip", "xsel"])


class FakeProc:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.terminated = False

    def terminate(self):
        self.terminated = True


class TestWebLogFollowedFromTheContainer(Sandbox):

    def setUp(self):
        super().setUp()
        self.docker = FakeDocker()

    def test_only_visitor_lines_are_streamed(self):
        lines = ['1.2.3.4 - "GET / HTTP/1.1" 200\n',
                 f'127.0.0.1 - "GET /.onionpress-health" 200 "{app_commands.HEALTHCHECK_LOG_MARK}"\n',
                 '1.2.3.4 - "GET /feed/ HTTP/1.1" 200\n']
        proc = FakeProc(lines)
        with mock.patch("onionpress.app_commands.subprocess.Popen", return_value=proc) as popen:
            rc = app_commands.logs_command(self.paths, self.docker, "linux", "web", lines=5,
                                           follow=True, out=self.out)
        self.assertEqual(rc, 0)
        self.assertEqual(self.out.getvalue(), lines[0] + lines[2])
        self.assertTrue(proc.terminated)
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["/stub/docker", "logs", "-f", "--tail", "5",
                                app_commands.WORDPRESS_CONTAINER])
        self.assertEqual(popen.call_args.kwargs["env"], self.docker.env)

    def test_ctrl_c_ends_the_follow_cleanly(self):
        def lines():
            yield "visitor\n"
            raise KeyboardInterrupt
        proc = FakeProc(lines())
        with mock.patch("onionpress.app_commands.subprocess.Popen", return_value=proc):
            rc = app_commands.logs_command(self.paths, self.docker, "linux", "web",
                                           follow=True, out=self.out)
        self.assertEqual((rc, self.out.getvalue()), (0, "visitor\n"))
        self.assertTrue(proc.terminated)

    def test_no_docker_binary(self):
        with mock.patch("onionpress.app_commands.subprocess.Popen",
                        side_effect=OSError("not found")):
            rc = app_commands.logs_command(self.paths, self.docker, "linux", "web",
                                           follow=True, out=self.out)
        self.assertEqual(rc, 1)
        self.assertIn("Could not read the web usage log: not found", self.err.getvalue())

    def test_following_a_file_survives_ctrl_c(self):
        with open(self.paths.log_file, "w") as f:
            f.write("x\n")
        with mock.patch("onionpress.app_commands.subprocess.call",
                        side_effect=KeyboardInterrupt) as call:
            rc = app_commands.logs_command(self.paths, self.docker, "linux", "launcher",
                                           lines=-3, follow=True, out=self.out)
        self.assertEqual(rc, 0)
        self.assertEqual(call.call_args.args[0], ["tail", "-n", "0", "-F", self.paths.log_file])

    def test_an_unknown_log_kind(self):
        with self.assertRaises(ValueError):
            app_commands.log_file_for(self.paths, "kernel", "linux")


class TestOpenTarget(Sandbox):

    def test_an_unknown_target_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            app_commands.open_target("mail", self.paths, FakeDocker())


class TestOnionnameShowText(Sandbox):

    def test_a_registered_name(self):
        self.config(ONIONNAME="alice", ONIONNAME_ADDRESS=ADDR, ONIONNAME_REGISTERED="yes")
        self.assertEqual(app_commands.onionname_show(self.paths, out=self.out), 0)
        self.assertEqual(self.out.getvalue(),
                         f"onionname: alice\naddress: {ADDR}\nregistered: yes\n"
                         f"url: http://{ADDR}/alice/\n")

    def test_a_name_recorded_without_an_address(self):
        self.config(ONIONNAME="alice", ONIONNAME_REGISTERED="no")
        self.assertEqual(app_commands.onionname_show(self.paths, out=self.out), 0)
        self.assertEqual(self.out.getvalue(),
                         "onionname: alice\naddress: (not recorded)\nregistered: no\n")


class TestOnionnameSetEdges(Sandbox):

    def _set(self, name, registrar, os_name="linux", json_output=False, docker=None):
        return app_commands.onionname_set(
            name, self.paths, docker or with_address(), os_name, self.log,
            json_output=json_output, out=self.out, registrar=registrar)

    def test_a_registrar_that_raises_is_a_deferred_registration(self):
        registrar = mock.Mock()
        registrar.register.side_effect = RuntimeError("tor down")
        self.assertEqual(self._set("alice", registrar), 0)
        self.assertIn("Recorded 'alice'. Not registered yet (OnionHome did not answer)",
                      self.out.getvalue())
        self.assertTrue(any("registering 'alice' failed: tor down" in m for m in self.logged))
        self.assertTrue(any("OnionPress not running" in m for m in self.logged))
        record = app_commands.onionname_record(self.paths.config_file)
        self.assertEqual((record["name"], record["address"], record["registered"]),
                         ("alice", "", False))

    def test_a_taken_name_in_json(self):
        registrar = StubRegistrar(RegistrarResult(status="collision",
                                                  suggestions=["alice2", "alice3"]))
        self.assertEqual(self._set("alice", registrar, json_output=True), 2)
        self.assertEqual(json.loads(self.out.getvalue()),
                         {"error": "taken", "name": "alice", "suggestions": ["alice2", "alice3"]})
        self.assertEqual(app_commands.onionname_record(self.paths.config_file)["name"], "")

    def test_a_record_that_cannot_be_written(self):
        registrar = StubRegistrar(RegistrarResult(status="ok"))
        with mock.patch("onionpress.app_commands._save_onionname",
                        side_effect=OSError("read-only config")):
            self.assertEqual(self._set("alice", registrar), 2)
        self.assertIn("ERROR: read-only config", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_a_mac_retry_marker_that_cannot_be_written_is_no_failure(self):
        # Something sits at the marker's name: the open fails, the record
        # stays and the answer is the same.
        os.mkdir(os.path.join(self.dir, app_commands.ONIONNAME_RETRY_MARKER))
        registrar = StubRegistrar(RegistrarResult(status="error"))
        self.assertEqual(self._set("alice", registrar, os_name="macos", json_output=True), 0)
        doc = json.loads(self.out.getvalue())
        self.assertTrue(doc["ok"])
        self.assertIn("OnionPress keeps trying", doc["note"])
        self.assertTrue(any("registration deferred (error)" in m for m in self.logged))

    def test_without_the_site_running_on_a_mac(self):
        registrar = StubRegistrar(RegistrarResult(status="ok"))
        self.assertEqual(self._set("alice", registrar, os_name="macos", docker=FakeDocker()), 0)
        self.assertEqual(registrar.calls, [])
        self.assertIn("(start OnionPress)", self.out.getvalue())
        self.assertTrue(os.path.exists(os.path.join(self.dir, app_commands.ONIONNAME_RETRY_MARKER)))


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------

class TestConfirmEdges(unittest.TestCase):

    def test_last_json_line(self):
        f = confirm.last_json_line
        self.assertIsNone(f(""))
        self.assertIsNone(f("plain text\n"))
        self.assertIsNone(f("{not json\n"))
        self.assertIsNone(f("[1, 2]\n"))
        self.assertIsNone(f('"a string"\n'))
        self.assertEqual(f('log line\n{"ok": true}\n\n\n'), {"ok": True})
        self.assertEqual(f('log line\n{\n  "ok": true,\n  "n": 1\n}\n'), {"ok": True, "n": 1})
        self.assertIsNone(f("}\n"))

    def test_answer_ask_in_text(self):
        err = io.StringIO()
        out = io.StringIO()
        rc = confirm.answer_ask("password", False, out=out, err=err, backup_user="x")
        self.assertEqual(rc, confirm.EXIT_ASK)
        self.assertEqual(err.getvalue(), "ERROR: a password is required.\n")
        self.assertEqual(out.getvalue(), "")

    def test_the_other_answers_in_text(self):
        out, err = io.StringIO(), io.StringIO()
        exc = confirm.NeedsConfirmation("reset", deletes=["x"])
        self.assertEqual(confirm.answer_confirm(exc, False, out=out, err=err), confirm.EXIT_CONFIRM)
        self.assertEqual(confirm.answer_refused_question(exc, False, out=out, err=err),
                         confirm.EXIT_REFUSED)
        self.assertEqual(confirm.answer_error("boom", False, out=out, err=err), confirm.EXIT_FAILED)
        self.assertEqual(confirm.answer_error("later", False, out=out, err=err, code=2), 2)
        self.assertIn("boom", err.getvalue())
        self.assertIn("later", err.getvalue())
        # A question in text is the facts on stdout, for a person.
        self.assertEqual(out.getvalue().count("CONFIRM reset"), 2)


# ---------------------------------------------------------------------------
# handle_action
# ---------------------------------------------------------------------------

class TestHostEdges(HandleActionCase):

    def test_default_launcher(self):
        paths = resolve_paths(data_dir=self.data_dir, app_bundle="/Applications/OnionPress.app")
        with mock.patch.dict(os.environ, {"ONIONPRESS_LAUNCHER": "/custom/onionpress"}):
            self.assertEqual(ha.default_launcher(paths, "macos"), "/custom/onionpress")
        os.environ.pop("ONIONPRESS_LAUNCHER", None)
        self.assertEqual(ha.default_launcher(paths, "macos"),
                         "/Applications/OnionPress.app/Contents/MacOS/onionpress")
        self.assertEqual(ha.default_launcher(paths, "linux"), ha.LINUX_LAUNCHER)
        self.assertEqual(ha.default_launcher(resolve_paths(data_dir=self.data_dir, app_bundle=""),
                                             "macos"), ha.LINUX_LAUNCHER)

    def test_a_result_that_cannot_be_written_is_logged(self):
        host = self.host()
        with mock.patch.object(self.docker, "run", side_effect=OSError("no docker")):
            host.write_result("x-result.json", {"success": True})
        self.assertTrue(any("could not write x-result.json: no docker" in m for m in self.logs))

    def test_a_damaged_hostname_is_no_address(self):
        self.docker.address = "garbage"
        self.assertEqual(self.host().served_address(), "")

    def test_containers_when_docker_does_not_answer(self):
        host = self.host()
        with mock.patch.object(self.docker, "run", return_value=DockerResult(1, "", "down")):
            self.assertEqual(host.containers(), [])
            self.assertFalse(host.all_running())
            self.assertFalse(host.wait_running(timeout=0))
            self.assertEqual(host.containers_info(), "")

    def test_containers_info_names_each_container(self):
        self.assertEqual(self.host().containers_info(),
                         "tor: Up 2 minutes, wordpress: Up 2 minutes")

    def test_a_bad_request_id_is_logged_without_its_value(self):
        self.assertEqual(ha.checked_request_id("bad id!", self.logs.append), "")
        self.assertEqual(len(self.logs), 1)
        self.assertNotIn("bad id!", self.logs[0])
        self.assertEqual(ha.checked_request_id(None, self.logs.append), "")
        self.assertEqual(ha.checked_request_id(" ok-1 "), "ok-1")

    def test_a_pending_file_with_an_unknown_question_is_ignored(self):
        d = os.path.join(self.data_dir, ha.PENDING_DIR)
        os.makedirs(d)
        with open(os.path.join(d, "request.json"), "w") as f:
            json.dump({"action": "x", "confirm": "not-a-question", "asked_at": time.time()}, f)
        self.assertEqual(ha.handle(self.host()), {"handled": None})
        with open(os.path.join(d, "request.json"), "w") as f:
            f.write("{truncated")
        self.assertEqual(ha.handle(self.host()), {"handled": None})


class TestHandlerFailures(HandleActionCase):

    def test_a_handler_that_raises_is_the_runs_error(self):
        self.request("check-reachability")
        with mock.patch.object(ha, "_check_reachability", side_effect=RuntimeError("boom")):
            result = ha.handle(self.host())
        self.assertEqual(result, {"handled": "check-reachability", "error": "boom"})
        self.assertTrue(any("action 'check-reachability' failed: boom" in m for m in self.logs))
        # The request is gone: it is never retried.
        self.assertIsNone(self.docker.get("requested-action"))

    def test_a_timed_out_apply_config_does_not_stop_the_start(self):
        self.request("start")
        host = self.host()
        answers = [subprocess.TimeoutExpired("apply-config", 300), mock.Mock(returncode=0)]
        with mock.patch.object(host, "run_launcher", side_effect=answers) as run:
            self.assertEqual(ha.handle(host), {"handled": "start"})
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [["apply-config"], ["start"]])
        self.assertTrue(any("apply-config before start:" in m for m in self.logs))
        res = self.docker.result("service-result.json")
        self.assertTrue(res["success"])
        self.assertIn("Started tor: Up 2 minutes, wordpress: Up 2 minutes", res["message"])
        self.assertIsNone(self.docker.get("service-pending"))

    def test_a_failed_hand_off_to_wordpress(self):
        py = os.path.join(self.tmp, "launcher.py")
        with open(py, "w") as f:
            f.write(LAUNCHER_PY_WRITING_THE_ZIP)
        self.script({"settings-page-backup": {"rc": 0, "stdout": '{"ok": true}\n'}})
        with open(self.docker.docker_bin, "w") as f:
            f.write("#!/bin/bash\nexit 1\n")
        self.docker.put("backup-password", PW + "\n")
        self.request("create-backup")
        ha.handle(self.host())
        res = self.docker.result("backup-result.json")
        self.assertEqual((res["success"], res["error"]), (False, ha.HANDOFF_FAILED_MSG))
        self.assertTrue(any(f"backup failed: {ha.HANDOFF_FAILED_MSG}" in m for m in self.logs))
        # The local copy is removed either way.
        argv = self.launcher_calls()[0]["argv"]
        self.assertFalse(os.path.exists(os.path.dirname(argv[1])))

    def test_a_confirmed_key_import_that_fails(self):
        question = {"confirm": "address-change", "current": ADDR, "new": NEW_ADDR}
        self.script({
            "import-key - --check --json": {"rc": confirm.EXIT_CONFIRM,
                                            "stdout": json.dumps(question) + "\n"},
            "import-key - --confirmed address-change": {"rc": 1, "stderr": "bad key\n"},
        })
        self.docker.put("import-key-data", "a-key")
        self.request("import-key-file")
        result = ha.handle(self.host(), confirmed=frozenset({"address-change"}))
        self.assertEqual(result, {"handled": "import-key-file"})
        self.assertEqual(self.argvs(), [["import-key", "-", "--check", "--json"],
                                        ["import-key", "-", "--confirmed", "address-change"]])
        res = self.docker.result("import-result.json")
        self.assertEqual((res["success"], res["error"]), (False, "bad key"))
        self.assertTrue(any("key import failed: bad key" in m for m in self.logs))

    def test_a_resumed_restore_without_its_upload(self):
        d = os.path.join(self.data_dir, ha.PENDING_DIR)
        os.makedirs(d)
        with open(os.path.join(d, "request.json"), "w") as f:
            json.dump({"action": "restore-backup", "confirm": "address-change", "facts": {},
                       "asked_at": time.time(), "request_id": "r1"}, f)
        with open(os.path.join(d, "password"), "w") as f:
            f.write(PW)
        result = ha.handle(self.host(), confirmed=frozenset({"address-change"}))
        self.assertEqual(result, {"handled": "restore-backup"})
        res = self.docker.result("restore-result.json")
        self.assertEqual((res["success"], res["error"], res["request_id"]),
                         (False, ha.MISSING_UPLOAD_MSG, "r1"))
        self.assertEqual(self.argvs(), [])
        self.assertFalse(os.path.exists(d))

    def test_write_status_without_docker_still_exits_0(self):
        # `onionpress write-status` (#523) is best effort, every 30 s
        # beside the stack: a docker that answers nothing is in the log
        # and the documents say "stopped".
        from test_status_writer import FakeDocker
        from onionpress import cli, daemon_client
        d = tempfile.mkdtemp(prefix="op-write-status-")
        self.addCleanup(shutil.rmtree, d, True)
        out = io.StringIO()
        with mock.patch("onionpress.docker.Docker", return_value=FakeDocker(raising=True)), \
                mock.patch.object(cli, "OnionPressCLI", side_effect=AssertionError("setup ran")), \
                mock.patch("sys.stdout", out):
            self.assertEqual(cli.main(["--data-dir", d, "write-status"]), 0)
        self.assertIn("status: could not read the stack's state", out.getvalue())
        self.assertEqual(daemon_client.read_state(d)["status"]["state"], "stopped")
        with open(os.path.join(d, "status.json")) as f:
            self.assertEqual(json.load(f)["state"], "stopped")


class TestApplyConfigOutput(HandleActionCase):

    def test_the_lines_and_the_side_effects(self):
        host = self.host()
        with open(host.paths.config_file, "w") as f:
            f.write("SHARE_ANALYTICS_WITH_ONIONHOME=no\nPREVENT_SLEEP=normal\n")
        self.docker.put("config-updates.json", json.dumps({
            "SHARE_ANALYTICS_WITH_ONIONHOME": "yes", "PREVENT_SLEEP": "normal", "ONIONPRESS_TOR_IMAGE": "evil",
            "VM_MEMORY": "999"}))
        out = io.StringIO()
        with mock.patch("onionpress.config.apply_side_effects",
                        return_value=["login item installed"]) as effects:
            accepted = ha.apply_config(host, out=out)
        self.assertEqual(accepted, ["SHARE_ANALYTICS_WITH_ONIONHOME", "PREVENT_SLEEP"])
        self.assertEqual(out.getvalue().splitlines(), [
            "changed SHARE_ANALYTICS_WITH_ONIONHOME", "unchanged PREVENT_SLEEP",
            "rejected ONIONPRESS_TOR_IMAGE", "rejected VM_MEMORY"])
        self.assertEqual(effects.call_args.args[0], ["SHARE_ANALYTICS_WITH_ONIONHOME", "PREVENT_SLEEP"])
        self.assertEqual(effects.call_args.args[2], "macos")
        self.assertIn("login item installed", self.logs)
        with open(os.path.join(self.data_dir, ".config-changed")) as f:
            self.assertIn("SHARE_ANALYTICS_WITH_ONIONHOME", f.read())
        self.assertIsNone(self.docker.get("config-updates.json"))
        self.assertTrue(any("ignored (not a setting it may change, or an invalid value): "
                            "ONIONPRESS_TOR_IMAGE, VM_MEMORY" in m for m in self.logs))


PW = "page-pw"

# A launcher stub whose settings-page-backup writes the zip it is asked
# for (test_handle_action's LAUNCHER_PY with that one addition).
from test_handle_action import LAUNCHER_PY  # noqa: E402

LAUNCHER_PY_WRITING_THE_ZIP = LAUNCHER_PY.replace(
    'if ans.get("write"):',
    'if sys.argv[1] == "settings-page-backup":\n'
    '    open(sys.argv[2], "wb").write(b"zip bytes")\n'
    'if ans.get("write"):')


if __name__ == "__main__":
    unittest.main()
