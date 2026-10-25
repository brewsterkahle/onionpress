"""A Mac node: the build under test in a scratch HOME on this Mac.

Layout under the scratch root (short paths: Colima and Lima put sockets
several levels below HOME):

  <root>/<name>/h/                 HOME (~/.onionpress, ~/OnionPress, the VM)
  <root>/<name>/t/                 TMPDIR: Colima writes its instance config to
                                   $TMPDIR/colima.yaml, so two first starts
                                   under one TMPDIR race (issue #370 notes)
  <root>/<name>/OnionPress.app     a copy of the build under test, never the
                                   app in /Applications: the launcher finds
                                   its menubar and daemon by this path

The environment is built from nothing: no DOCKER_HOST, COLIMA_HOME,
LIMA_HOME or DOCKER_CONFIG from the caller can point the node at the
live install. Colima's image cache is cloned in from the real
~/Library/Caches/colima (an APFS clone costs no space and saves a 350 MB
download).
"""

import os
import platform
import plistlib
import shutil
import socket

from .. import safety
from ..proc import CommandFailed, run
from .base import Node


def _port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port_offset(first=31000, step=1000, tries=20, is_free=_port_free):
    """An offset whose WordPress, SOCKS and proxy ports are all free."""
    for i in range(tries):
        offset = first + i * step
        if all(is_free(p + offset) for p in (8080, 9050, 9077)):
            return offset
    raise RuntimeError("no free port offset for a Mac node")


class MacNode(Node):
    platform = "macos"
    kind = "mac"

    def __init__(self, name, log, results_dir, *, root, real_home, app_source, port_offset):
        super().__init__(name, log, results_dir)
        self.dir = os.path.join(root, name)
        self.root = root
        self.real_home = real_home
        self.home = os.path.join(self.dir, "h")
        self.tmp = os.path.join(self.dir, "t")
        self.app = os.path.join(self.dir, "OnionPress.app")
        self.app_source = app_source      # ("app", path) | ("dmg", path)
        self.port_offset = port_offset

    # ── paths and environment ───────────────────────────────────────────
    @property
    def bin_dir(self):
        return os.path.join(self.app, "Contents", "Resources", "bin")

    @property
    def launcher_path(self):
        return os.path.join(self.app, "Contents", "MacOS", "onionpress")

    @property
    def colima_home(self):
        return os.path.join(self.home, ".onionpress", "colima")

    @property
    def wp_port(self):
        return 8080 + self.port_offset

    def env(self):
        return {
            "HOME": self.home,
            "TMPDIR": self.tmp + "/",
            "PATH": f"{self.bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "USER": os.environ.get("USER", ""),
            "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", "")),
            "LANG": "en_US.UTF-8",
            "ONIONPRESS_PORT_OFFSET": str(self.port_offset),
        }

    def docker_env(self):
        env = self.env()
        env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
        return env

    def colima_env(self):
        env = self.env()
        env["COLIMA_HOME"] = self.colima_home
        env["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
        return env

    # ── primitives ──────────────────────────────────────────────────────
    def exec(self, argv, *, input=None, timeout=120):
        return run(argv, env=self.env(), input=input, timeout=timeout, log=self.log, cwd=self.home)

    def docker(self, argv, *, input=None, timeout=120):
        return run([os.path.join(self.bin_dir, "docker"), *argv], env=self.docker_env(),
                   input=input, timeout=timeout, log=self.log, cwd=self.home)

    def launcher(self, argv, *, input=None, timeout=600):
        return run([self.launcher_path, *argv], env=self.env(), input=input, timeout=timeout,
                   log=self.log, cwd=self.home)

    def read_file(self, path):
        try:
            with open(os.path.join(self.home, path), encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def home_path(self, rel):
        return os.path.join(self.home, rel)

    def fetch_files(self, rel_paths, dest):
        os.makedirs(dest, exist_ok=True)
        for rel in rel_paths:
            src = os.path.join(self.home, rel)
            target = os.path.join(dest, rel.replace("/", "__"))
            if os.path.isdir(src):
                shutil.copytree(src, target, symlinks=True, dirs_exist_ok=True)
            elif os.path.isfile(src):
                shutil.copy2(src, target)

    # ── lifecycle ───────────────────────────────────────────────────────
    def check_daemon_agent(self):
        r = run(["launchctl", "print", f"gui/{os.getuid()}/{safety.DAEMON_AGENT_LABEL}"],
                timeout=20, log=self.log)
        safety.check_daemon_agent(r.out, r.rc, self.root)

    def _copy_app(self):
        kind, path = self.app_source
        if kind == "app":
            run(["ditto", path, self.app], timeout=600, log=self.log, check=True)
        elif kind == "dmg":
            mnt = os.path.join(self.dir, "dmg")
            os.makedirs(mnt, exist_ok=True)
            # diskutil image: hdiutil attach is deprecated (macOS 27), and
            # the DMG build moved to diskutil too (PR #290).
            r = run(["diskutil", "image", "attach", "--mountOptions", "nobrowse", "--readOnly",
                     "--mountPoint", mnt, path], timeout=300, log=self.log)
            if not r.ok:
                run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen",
                     "-mountpoint", mnt, path], timeout=300, log=self.log, check=True)
            try:
                run(["ditto", os.path.join(mnt, "OnionPress.app"), self.app],
                    timeout=600, log=self.log, check=True)
            finally:
                if not run(["diskutil", "eject", mnt], timeout=120, log=self.log).ok:
                    run(["hdiutil", "detach", "-force", mnt], timeout=120, log=self.log)
        else:
            raise ValueError(f"unknown Mac build source {kind}")
        run(["xattr", "-dr", "com.apple.quarantine", self.app], timeout=120, log=self.log)

    def install(self):
        """A fresh install: scratch HOME, the baseline config written before
        anything runs, Colima's cache cloned in, the app copied."""
        self.check_daemon_agent()
        for d in (self.home, self.tmp, self.colima_home):
            os.makedirs(d, exist_ok=True)
        config = os.path.join(self.home, ".onionpress", "config")
        with open(config, "w", encoding="utf-8") as f:
            f.write(safety.baseline_config_text())
        os.chmod(config, 0o600)
        cache = os.path.join(self.real_home, "Library", "Caches", "colima")
        if os.path.isdir(cache):
            os.makedirs(os.path.join(self.home, "Library", "Caches"), exist_ok=True)
            run(["cp", "-Rc", cache, os.path.join(self.home, "Library", "Caches", "colima")],
                timeout=300, log=self.log)
        self._copy_app()
        self.installed = True

    def start(self, timeout=3600):
        """`onionpress start`: on a fresh HOME this is the whole first run
        (Colima VM, images, key, containers)."""
        r = self.launcher(["start"], timeout=timeout)
        if not r.ok:
            raise CommandFailed(r)
        return r

    def stop(self):
        r = self.launcher(["stop"], timeout=600)
        if not r.ok:
            raise CommandFailed(r)
        return r

    def setup_wordpress(self, user, password):
        self.log.add_secret(password)
        r = self.launcher(["setup", "--user", user, "--pass", password,
                           "--email", f"{user}@example.invalid", "--title", "OnionPress e2e"],
                          timeout=900)
        if not r.ok:
            raise CommandFailed(r)
        self.wp_admin_user, self.wp_admin_password = user, password
        return r

    def destroy(self, keep=False):
        """Quit (daemon, containers, VM), delete the VM, boot out a daemon
        agent of ours that is still loaded, remove the scratch dir."""
        if keep:
            # VM and containers stay up for inspection; the daemon goes,
            # since its launchd label is the user's one (safety.py). The
            # next run's leftover cleanup quits the rest.
            if os.path.exists(self.launcher_path):
                self.launcher(["daemon", "stop"], timeout=120)
            return
        if os.path.exists(self.launcher_path):
            self.launcher(["quit", "--no-offline"], timeout=600)
        colima = os.path.join(self.bin_dir, "colima")
        if os.path.exists(colima):
            run([colima, "delete", "--force"], env=self.colima_env(), timeout=600, log=self.log)
        r = run(["launchctl", "print", f"gui/{os.getuid()}/{safety.DAEMON_AGENT_LABEL}"],
                timeout=20, log=self.log)
        try:
            safety.check_daemon_agent(r.out, r.rc, self.root)
            if r.rc == 0:     # loaded, and ours
                run(["launchctl", "bootout", f"gui/{os.getuid()}/{safety.DAEMON_AGENT_LABEL}"],
                    timeout=30, log=self.log)
        except safety.UnsafeTarget:
            pass              # someone else's: never touch it
        shutil.rmtree(self.dir, ignore_errors=True)

    def facts(self):
        facts = {"os": f"macOS {platform.mac_ver()[0]}", "arch": platform.machine(),
                 "kind": self.kind, "install": "app bundle (" + self.app_source[0] + ")",
                 "port_offset": self.port_offset}
        try:
            with open(os.path.join(self.app, "Contents", "Info.plist"), "rb") as f:
                facts["app_version"] = plistlib.load(f).get("CFBundleShortVersionString", "")
        except (OSError, ValueError):
            pass
        colima = os.path.join(self.bin_dir, "colima")
        if os.path.exists(colima):
            r = run([colima, "version"], env=self.colima_env(), timeout=30, log=self.log)
            first = r.out.strip().splitlines()[:1]
            facts["vm"] = first[0] if first else ""
        r = self.docker(["version", "--format", "{{.Server.Version}}"], timeout=30)
        if r.ok:
            facts["docker"] = f"Docker {r.out.strip()} (Colima VM)"
        return facts
