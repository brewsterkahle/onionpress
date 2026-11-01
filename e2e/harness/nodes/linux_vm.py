"""A Debian-based node in a Lima VM (on a Mac: vz; on Linux: QEMU + KVM).

The guest is one of platforms.LINUX_GUESTS (Lima's own templates): Debian
13 by default, Ubuntu 24.04 LTS with `--guest ubuntu-24.04`. Every guest
gets the same .deb, so what differs is what the distro ships: Debian's
docker.io vs Ubuntu's, python3 3.13 vs 3.12, and the apt sources (both
cloud images have the sections the .deb's Depends need).

  <root>/lima/                  LIMA_HOME for every VM node of the run
  <root>/<name>/share/          the VM's only mount (same path inside),
                                writable: the .deb, scripts, evidence

The VM has no home mount and ignores every port forward: Lima would
otherwise forward the guest's 8080 to the host's, where a live install
may be listening. Commands run as the VM's default user through
as-user.sh (`sudo -iu`), so a docker group the .deb just added applies
without a new login.

The .deb comes from --deb, a release, or is built inside the VM from a
copy of the checkout's tracked files (`build/build-linux.sh`, with the
real dpkg-deb).
"""

import json
import os
import shutil
import subprocess
import tarfile

from .. import PROJECT_DIR, safety
from ..platforms import DEFAULT_LINUX_GUEST, LINUX_GUESTS
from ..proc import CommandFailed, run
from .base import Node, wp_port_from_status

AS_USER_SH = """#!/bin/bash
# Run a command as the install's user with a fresh login (group
# membership from /etc/group) and their systemd user session.
u="$1"; shift
uid=$(id -u "$u")
exec sudo -iu "$u" env XDG_RUNTIME_DIR="/run/user/$uid" \\
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"
"""

# What the .deb's postinst prints when it could not start OnionPress itself.
POSTINST_MANUAL_STEP = "One manual step needed to finish"

IGNORE_ALL_PORT_FORWARDS = (
    '.portForwards = ['
    '{"guestIP": "0.0.0.0", "guestPortRange": [1, 65535], "hostPortRange": [1, 65535], "ignore": true}, '
    '{"guestIP": "127.0.0.1", "guestPortRange": [1, 65535], "hostPortRange": [1, 65535], "ignore": true}'
    ']')


def tracked_files(project_dir=PROJECT_DIR):
    out = subprocess.run(["git", "-C", project_dir, "ls-files", "-z"], capture_output=True,
                         check=True).stdout.decode("utf-8")
    return [p for p in out.split("\0") if p]


def guest_template(guest):
    """The Lima template locator for a guest name, or ValueError.
    `template:<name>` is the form Lima 2 takes (`template://` warns)."""
    if guest not in LINUX_GUESTS:
        raise ValueError(f"unknown Linux guest {guest!r}; know {', '.join(LINUX_GUESTS)}")
    return f"template:{guest}"


class LinuxVMNode(Node):
    platform = "linux"
    kind = "linux-vm"

    def __init__(self, name, log, results_dir, *, root, limactl, deb_source,
                 guest=DEFAULT_LINUX_GUEST, cpus=2, memory_gib=4, disk_gib=20):
        super().__init__(name, log, results_dir)
        self.guest = guest
        self.template = guest_template(guest)
        self.root = root
        self.dir = os.path.join(root, name)
        self.share = os.path.join(self.dir, "share")
        self.lima_home = os.path.join(root, "lima")
        self.limactl = limactl
        self.vm = f"ope2e-{name}"
        self.deb_source = deb_source      # ("deb", path) | ("source", project_dir)
        self.cpus, self.memory_gib, self.disk_gib = cpus, memory_gib, disk_gib
        self.user = ""
        self.user_home = ""
        self._docker_prefix = None
        self._uid_value = None

    @property
    def wp_port(self):
        """The port the launcher published WordPress on, from the node's
        status.json once the first start has written it (8080, or 18080
        when something on the guest held 8080, 9050 or 9077, as a system
        tor did on Ubuntu); 8080 until then."""
        return wp_port_from_status(self.read_file(".onionpress/status.json")) or 8080

    def lima_env(self):
        env = {
            "LIMA_HOME": self.lima_home,
            "HOME": os.path.expanduser("~"),   # Lima's download cache
            "PATH": f"{os.path.dirname(self.limactl)}:/usr/bin:/bin:/usr/sbin:/sbin",
            "USER": os.environ.get("USER", ""),
        }
        return env

    # ── running things in the VM ────────────────────────────────────────
    def vm_run(self, argv, *, input=None, timeout=120, check=False):
        return run([self.limactl, "shell", "--workdir", "/", self.vm, *argv], env=self.lima_env(),
                   input=input, timeout=timeout, log=self.log, check=check)

    def script(self, name, text, *, sudo=False, timeout=600, check=True):
        """Write a script into the share and run it in the VM."""
        path = os.path.join(self.share, "scripts", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\nset -euo pipefail\n" + text)
        argv = (["sudo"] if sudo else []) + ["bash", path]
        return self.vm_run(argv, timeout=timeout, check=check)

    def as_user(self, argv, *, input=None, timeout=120):
        return self.vm_run(["bash", os.path.join(self.share, "as-user.sh"), self.user, *argv],
                           input=input, timeout=timeout)

    def exec(self, argv, *, input=None, timeout=120):
        return self.as_user(argv, input=input, timeout=timeout)

    def _docker_argv(self):
        """Rootless Docker if the install set it up, else the system daemon."""
        if self._docker_prefix is None:
            r = self.vm_run(["sudo", "test", "-S", f"/run/user/{self._uid()}/docker.sock"])
            if r.ok:
                self._docker_prefix = ("rootless", ["env", f"DOCKER_HOST=unix:///run/user/{self._uid()}/docker.sock", "docker"])
            else:
                self._docker_prefix = ("system", ["sudo", "docker"])
        return self._docker_prefix

    def _uid(self):
        if self._uid_value is None:
            self._uid_value = self.vm_run(["id", "-u"], check=True).out.strip()
        return self._uid_value

    def docker(self, argv, *, input=None, timeout=120):
        mode, prefix = self._docker_argv()
        if mode == "rootless":
            return self.as_user([*prefix, *argv], input=input, timeout=timeout)
        return self.vm_run([*prefix, *argv], input=input, timeout=timeout)

    def launcher(self, argv, *, input=None, timeout=600):
        return self.as_user(["onionpress", *argv], input=input, timeout=timeout)

    def read_file(self, path):
        r = self.vm_run(["cat", f"{self.user_home}/{path}"])
        return r.out if r.ok else ""

    def home_path(self, rel):
        return f"{self.user_home}/{rel}"

    def fetch_files(self, rel_paths, dest):
        os.makedirs(dest, exist_ok=True)
        tgz = os.path.join(self.share, "evidence.tgz")
        if os.path.exists(tgz):
            os.remove(tgz)
        self.vm_run(["tar", "-C", self.user_home, "--ignore-failed-read", "-czf", tgz, *rel_paths],
                    timeout=300)
        if os.path.exists(tgz):
            with tarfile.open(tgz) as t:
                t.extractall(dest, filter="data")
            os.remove(tgz)

    # ── lifecycle ───────────────────────────────────────────────────────
    def _prepare_deb(self):
        kind, path = self.deb_source
        target = os.path.join(self.share, "onionpress.deb")
        if kind == "deb":
            shutil.copy2(path, target)
            return target
        # Build in the VM from the checkout's tracked files (the working
        # tree as it is, uncommitted edits included).
        src = os.path.join(self.share, "src")
        for rel in tracked_files(path):
            s = os.path.join(path, rel)
            if not os.path.isfile(s) or os.path.islink(s):
                continue
            d = os.path.join(src, rel)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
        self.script("build-deb.sh", f"""
cd {src}
bash build/build-linux.sh
cp build/onionpress.deb {target}
""", timeout=1200)
        return target

    def install(self):
        """Create and boot the VM, write the baseline config, install the .deb
        (its postinst starts OnionPress, as on a real machine)."""
        os.makedirs(self.share, exist_ok=True)
        os.makedirs(self.lima_home, exist_ok=True)
        with open(os.path.join(self.share, "as-user.sh"), "w", encoding="utf-8") as f:
            f.write(AS_USER_SH)
        create = [self.limactl, "create", "--tty=false", f"--name={self.vm}",
                  f"--cpus={self.cpus}", f"--memory={self.memory_gib}", f"--disk={self.disk_gib}",
                  "--containerd=none", f"--mount-only={self.share}:w",
                  f"--set={IGNORE_ALL_PORT_FORWARDS}", self.template]
        run(create, env=self.lima_env(), timeout=1200, log=self.log, check=True)
        run([self.limactl, "start", "--tty=false", self.vm], env=self.lima_env(),
            timeout=1800, log=self.log, check=True)
        self.user = self.vm_run(["id", "-un"], check=True).out.strip()
        self.user_home = self.vm_run(["sh", "-c", 'echo "$HOME"'], check=True).out.strip()

        cfg = os.path.join(self.share, "config")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(safety.baseline_config_text())
        self.script("write-config.sh", f"""
install -d -m 700 "$HOME/.onionpress"
install -m 600 {cfg} "$HOME/.onionpress/config"
""")
        deb = self._prepare_deb()
        r = self.script("install-deb.sh", f"""
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q {deb}
""", sudo=True, timeout=2400)
        if POSTINST_MANUAL_STEP in r.out:
            # The postinst could not finish (the user's systemd manager
            # predates the docker group it just added). Do what it tells a
            # person to do: log out and back in (a restart of the user's
            # manager), then enable the service. The test that the package
            # needs no such step reports it.
            self.install_findings.append(
                "the .deb's postinst asked for a manual step (log out and back in, then "
                "`systemctl --user enable --now onionpress`); the harness did it")
            self.vm_run(["sudo", "systemctl", "restart", f"user@{self._uid()}.service"],
                        timeout=120, check=True)
            r = self.as_user(["systemctl", "--user", "enable", "--now", "onionpress"], timeout=1800)
            if not r.ok:
                raise CommandFailed(r)
        self._docker_prefix = None
        self.installed = True

    def start(self, timeout=1800):
        r = self.as_user(["systemctl", "--user", "start", "onionpress"], timeout=timeout)
        if not r.ok:
            raise CommandFailed(r)
        return r

    def stop(self):
        r = self.as_user(["systemctl", "--user", "stop", "onionpress"], timeout=600)
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

    def collect_evidence(self, dest):
        super().collect_evidence(dest)
        sysd = os.path.join(dest, "systemd")
        os.makedirs(sysd, exist_ok=True)
        uid = self._uid() if self.user else "0"
        for name, argv in (
                ("user-journal.log", ["sudo", "journalctl", f"_UID={uid}", "--no-pager", "-o", "short-iso"]),
                ("onionpress-units.log", ["sudo", "journalctl", "--no-pager", "-o", "short-iso",
                                          f"_SYSTEMD_USER_UNIT=onionpress.service",
                                          "+", f"_SYSTEMD_USER_UNIT=onionpress-daemon.service"]),
                ("docker.log", ["sudo", "journalctl", "--no-pager", "-o", "short-iso", "-u", "docker"]),
                ("user-units.txt", ["bash", os.path.join(self.share, "as-user.sh"), self.user or "root",
                                    "systemctl", "--user", "--no-pager", "--all", "list-units", "onionpress*"]),
                ("groups-and-linger.txt", ["sh", "-c", f"id {self.user or 'root'}; "
                                                       f"loginctl show-user {self.user or 'root'} -p Linger"])):
            r = self.vm_run(argv, timeout=120)
            with open(os.path.join(sysd, name), "w", encoding="utf-8") as f:
                f.write(r.out + r.err)

    def destroy(self, keep=False):
        if keep:
            return
        run([self.limactl, "delete", "--force", self.vm], env=self.lima_env(), timeout=600,
            log=self.log)
        shutil.rmtree(self.dir, ignore_errors=True)

    def facts(self):
        facts = {"kind": self.kind, "guest": self.guest,
                 "install": "deb (" + self.deb_source[0] + ")"}
        r = self.vm_run(["sh", "-c", '. /etc/os-release; echo "$PRETTY_NAME"; uname -m'])
        if r.ok:
            lines = r.out.strip().splitlines()
            facts["os"] = lines[0] if lines else ""
            facts["arch"] = lines[1] if len(lines) > 1 else ""
        mode, _ = self._docker_argv()
        r = self.docker(["version", "--format", "{{.Server.Version}}"], timeout=30)
        if r.ok:
            facts["docker"] = f"Docker {r.out.strip()} ({mode})"
        r = self.vm_run(["dpkg-query", "-W", "-f", "${Version}", "onionpress"])
        if r.ok:
            facts["app_version"] = r.out.strip()
        # Where WordPress answered: 8080, or the launcher's offset when the
        # guest had something on 8080, 9050 or 9077; "assumed" when the
        # launcher never wrote its status.json.
        port = wp_port_from_status(self.read_file(".onionpress/status.json"))
        facts["wp_port"] = f"{port} (status.json)" if port else "8080 (assumed)"
        return facts


def lima_instances(limactl, lima_home):
    """[name] of the VMs under lima_home (for leftover cleanup)."""
    if not os.path.isdir(lima_home):
        return []
    r = run([limactl, "list", "--json"], env={"LIMA_HOME": lima_home, "HOME": os.path.expanduser("~"),
                                              "PATH": "/usr/bin:/bin"}, timeout=60)
    names = []
    for line in r.out.splitlines():
        try:
            names.append(json.loads(line)["name"])
        except (ValueError, KeyError):
            continue
    return names
