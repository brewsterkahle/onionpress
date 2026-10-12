#!/usr/bin/env python3
"""OnionHeaven takeover workers run the image the onionheaven container runs.

The heartbeat in the onionheaven container spawns takeover workers with
`docker run`. Their image used to be ONIONHEAVEN_IMAGE, which the compose
file never passed into the container, so it was always the default: the
bare ghcr.io/…/onionpress-tor:latest tag, naming whatever was tagged
locally (a stale earlier pull) or whatever the registry served that day —
not the digest-pinned image the rest of the stack runs.

Now the compose file passes ONIONHEAVEN_IMAGE with the service's own image
reference (tests/test_image_pins.py checks the two match), and without it
onionheaven_common asks Docker for its own container's image ID. When
neither is available no worker is spawned. No Docker here: subprocess is
mocked.
"""

import os
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "tor"))

import onionheaven_common as oc  # noqa: E402

PIN = ("ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:"
       + "a" * 64)
OWN_ID = "sha256:" + "b" * 64


def _done(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr="")


class TestTakeoverImage(unittest.TestCase):

    def setUp(self):
        oc._own_image = None
        self.addCleanup(setattr, oc, "_own_image", None)

    def test_env_wins_without_asking_docker(self):
        with mock.patch.dict(os.environ, {"ONIONHEAVEN_IMAGE": PIN}), \
                mock.patch.object(oc.subprocess, "run") as run:
            self.assertEqual(oc.takeover_image(), PIN)
        run.assert_not_called()

    def test_falls_back_to_own_image_id(self):
        env = {"HOSTNAME": "0123456789ab"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(oc.subprocess, "run",
                                  return_value=_done(OWN_ID + "\n")) as run:
            os.environ.pop("ONIONHEAVEN_IMAGE", None)
            self.assertEqual(oc.takeover_image(), OWN_ID)
            self.assertEqual(oc.takeover_image(), OWN_ID)
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["docker", "inspect"])
        self.assertEqual(argv[-1], "0123456789ab")

    def test_none_rather_than_a_tag(self):
        for result in (_done("", 1), _done("ghcr.io/x/y:latest\n"),
                       OSError("no docker")):
            oc._own_image = None
            kwargs = ({"side_effect": result} if isinstance(result, Exception)
                      else {"return_value": result})
            with mock.patch.dict(os.environ, {"HOSTNAME": "abc"}), \
                    mock.patch.object(oc.subprocess, "run", **kwargs):
                os.environ.pop("ONIONHEAVEN_IMAGE", None)
                self.assertIsNone(oc.takeover_image(), result)


class TestSpawnWorkerUsesIt(unittest.TestCase):

    def setUp(self):
        oc._own_image = None
        self.addCleanup(setattr, oc, "_own_image", None)
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE takeover_containers (container_name TEXT PRIMARY KEY,"
            " max_services INTEGER, bootstrapped INTEGER,"
            " assigned_count INTEGER, created_at TEXT)")
        self.addCleanup(self.conn.close)
        self.logs = []
        patcher = mock.patch.object(oc, "log", self.logs.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.start_index = oc._next_worker_index
        self.addCleanup(setattr, oc, "_next_worker_index", self.start_index)

    def test_docker_run_gets_the_pinned_image(self):
        with mock.patch.dict(os.environ, {"ONIONHEAVEN_IMAGE": PIN}), \
                mock.patch.object(oc.subprocess, "run",
                                  return_value=_done("cid\n")) as run:
            oc._spawn_worker(self.conn)
        runs = [c.args[0] for c in run.call_args_list if c.args[0][1] == "run"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][-1], PIN)

    def test_no_image_no_worker(self):
        with mock.patch.dict(os.environ, {"HOSTNAME": "abc"}), \
                mock.patch.object(oc.subprocess, "run",
                                  return_value=_done("", 1)) as run:
            os.environ.pop("ONIONHEAVEN_IMAGE", None)
            oc._spawn_worker(self.conn)
        for c in run.call_args_list:
            self.assertNotEqual(c.args[0][1], "run")
        self.assertEqual(oc._next_worker_index, self.start_index)
        self.assertTrue(any("not spawning" in m for m in self.logs))


if __name__ == "__main__":
    unittest.main()
