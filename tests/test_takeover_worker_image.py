#!/usr/bin/env python3
"""OnionHeaven takeover workers run the image the farm controller runs.

The farm controller (onionheaven-farm.py, in the onionheaven-farm
container) spawns takeover workers with `docker run` when the onionheaven
container's heartbeat asks. Their image used to be ONIONHEAVEN_IMAGE, which
the compose file never passed into the container, so it was always the
default: the bare ghcr.io/…/onionpress-tor:latest tag, naming whatever was
tagged locally (a stale earlier pull) or whatever the registry served that
day — not the digest-pinned image the rest of the stack runs.

Now the compose file passes ONIONHEAVEN_IMAGE to the controller with its
own image reference (tests/test_image_pins.py checks the two match), and
without it the controller asks Docker for its own container's image ID.
When neither is available no worker is spawned, and the heartbeat's
_spawn_worker records nothing. No Docker here: subprocess is mocked.
"""

import importlib.util
import os
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKER_TOR_DIR = os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "tor")
sys.path.insert(0, DOCKER_TOR_DIR)

import onionheaven_common as oc  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "onionheaven_farm", os.path.join(DOCKER_TOR_DIR, "onionheaven-farm.py"))
farm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(farm)

PIN = ("ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:"
       + "a" * 64)
OWN_ID = "sha256:" + "b" * 64
WORKER = "onionheaven-takeover-0"


def _done(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr="")


class TestTakeoverImage(unittest.TestCase):

    def setUp(self):
        farm._own_image = None
        self.addCleanup(setattr, farm, "_own_image", None)

    def test_env_wins_without_asking_docker(self):
        with mock.patch.dict(os.environ, {"ONIONHEAVEN_IMAGE": PIN}), \
                mock.patch.object(farm.subprocess, "run") as run:
            self.assertEqual(farm.takeover_image(), PIN)
        run.assert_not_called()

    def test_falls_back_to_own_image_id(self):
        env = {"HOSTNAME": "0123456789ab"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(farm.subprocess, "run",
                                  return_value=_done(OWN_ID + "\n")) as run:
            os.environ.pop("ONIONHEAVEN_IMAGE", None)
            self.assertEqual(farm.takeover_image(), OWN_ID)
            self.assertEqual(farm.takeover_image(), OWN_ID)
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["docker", "inspect"])
        self.assertEqual(argv[-1], "0123456789ab")

    def test_none_rather_than_a_tag(self):
        for result in (_done("", 1), _done("ghcr.io/x/y:latest\n"),
                       OSError("no docker")):
            farm._own_image = None
            kwargs = ({"side_effect": result} if isinstance(result, Exception)
                      else {"return_value": result})
            with mock.patch.dict(os.environ, {"HOSTNAME": "abc"}), \
                    mock.patch.object(farm.subprocess, "run", **kwargs):
                os.environ.pop("ONIONHEAVEN_IMAGE", None)
                self.assertIsNone(farm.takeover_image(), result)


class TestSpawnWorkerUsesIt(unittest.TestCase):

    def setUp(self):
        farm._own_image = None
        self.addCleanup(setattr, farm, "_own_image", None)
        self.farm_logs = []
        patcher = mock.patch.object(farm, "log", self.farm_logs.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _spawn(self, env, inspect_result):
        """op_spawn with Docker faked: `ps` lists nothing, `inspect`
        answers inspect_result, `run`/`rm` succeed."""
        def fake(argv, **kwargs):
            if argv[1] == "ps":
                return _done("")
            if argv[1] == "inspect":
                return inspect_result
            return _done("cid\n")
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(farm.subprocess, "run", side_effect=fake) as run:
            os.environ.pop("ONIONHEAVEN_IMAGE", None)
            os.environ.update(env)
            reply = farm.op_spawn({"op": "spawn", "name": WORKER})
        runs = [c.args[0] for c in run.call_args_list if c.args[0][1] == "run"]
        return reply, runs

    def test_docker_run_gets_the_pinned_image(self):
        reply, runs = self._spawn({"ONIONHEAVEN_IMAGE": PIN}, _done("", 1))
        self.assertEqual(reply, {"ok": True, "name": WORKER})
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][-1], PIN)

    def test_no_image_no_worker(self):
        reply, runs = self._spawn({"HOSTNAME": "abc"}, _done("", 1))
        self.assertFalse(reply["ok"])
        self.assertIn("ONIONHEAVEN_IMAGE", reply["error"])
        self.assertEqual(runs, [])

        # The heartbeat side: a refused spawn records no worker and keeps
        # the index for the next attempt.
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE takeover_containers (container_name TEXT PRIMARY KEY,"
            " max_services INTEGER, bootstrapped INTEGER,"
            " assigned_count INTEGER, created_at TEXT)")
        self.addCleanup(conn.close)
        logs = []
        start_index = oc._next_worker_index
        self.addCleanup(setattr, oc, "_next_worker_index", start_index)
        with mock.patch.object(oc, "log", logs.append), \
                mock.patch.object(oc, "farm_request", return_value=reply) as req:
            oc._spawn_worker(conn)
        req.assert_called_once_with("spawn", name=f"onionheaven-takeover-{start_index}")
        self.assertEqual(oc._next_worker_index, start_index)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM takeover_containers").fetchone()[0], 0)
        self.assertTrue(any("Failed to spawn" in m for m in logs), logs)


if __name__ == "__main__":
    unittest.main()
