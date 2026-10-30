"""The Docker Engine and Docker Compose versions OnionPress needs, and the
check against what a machine has (`onionpress check-docker`).

The Linux launcher refuses to start on an older one, with this module's
message, instead of failing at the first compose call; the .deb's postinst
prints the same at install time (a warning: an install never fails on it).

The floors are evidence, not taste; a change to either must say what
moved it:

- Compose 2.22.0 (2023-09): `docker compose pull --policy missing`, which
  every start runs (CLAUDE.md, "Starts pull `--policy missing` only"),
  came in 2.22.0; `docker compose ps --format json` has printed one JSON
  object per line since 2.21.0, which is what the launchers' `jq -s`
  reads. Debian 13's docker-compose (2.26), Ubuntu 24.04's and 22.04's
  docker-compose-v2 (2.24 / 2.40) and Docker's docker-compose-plugin all
  pass; the retired Python Compose v1 (1.29, Debian 12's and Ubuntu's
  `docker-compose` package) has no `docker compose` at all.
- Docker Engine 20.10 (2020-12): rootless mode left experimental, which
  the .deb's first install relies on, and the Compose v2 plugin's API
  floor. Debian 12's docker.io (20.10.24) is the oldest a supported
  distribution ships.

tests/test_docker_versions.py ties the floors to the pull flag the
launchers use and to the versions the Mac build pins.
"""

import json
import re
import sys
from dataclasses import dataclass, field

MIN_ENGINE = (20, 10, 0)
MIN_COMPOSE = (2, 22, 0)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text):
    """The first dotted version in *text* as a (major, minor, patch)
    tuple, or None: "v2.40.2", "29.1.3", "26.1.5+dfsg1" and "Docker
    Compose version v2.26.1" all parse; "" and "command not found" do
    not."""
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def format_version(version):
    """(2, 22, 0) -> "2.22"; (29, 1, 3) -> "29.1.3" (a floor's zero patch
    is left off, a found version is printed whole)."""
    if version[2] == 0 and version == tuple(version[:2]) + (0,) and version in (MIN_ENGINE, MIN_COMPOSE):
        return f"{version[0]}.{version[1]}"
    return ".".join(str(n) for n in version)


@dataclass
class Report:
    engine: str = ""            # as found, "" when docker did not answer
    engine_source: str = ""     # "server" | "client" | ""
    compose: str = ""           # as found, "" when `docker compose` is missing
    problems: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.problems

    def as_dict(self):
        return {"ok": self.ok, "engine": self.engine, "engine_source": self.engine_source,
                "compose": self.compose, "problems": list(self.problems),
                "min_engine": format_version(MIN_ENGINE), "min_compose": format_version(MIN_COMPOSE)}


COMPOSE_INSTALL_HINT = ("Debian 13: apt install docker-compose; Ubuntu: apt install "
                        "docker-compose-v2; with Docker's own repository: apt install "
                        "docker-compose-plugin.")
ENGINE_INSTALL_HINT = ("Upgrade Docker (apt upgrade docker.io, or Docker's own "
                       "repository's docker-ce) and start OnionPress again.")


def check(engine_text, compose_text, *, engine_source="server"):
    """The verdict on the version strings a machine gave: *engine_text* is
    `docker version --format {{.Server.Version}}`'s answer (or the
    client's, with engine_source="client", when no daemon answered),
    *compose_text* is `docker compose version --short`'s; either may be
    None when the command failed."""
    report = Report()
    engine = parse_version(engine_text) if engine_text is not None else None
    if engine is None:
        report.problems.append("Docker did not say its version (is Docker installed and running?). "
                               + ENGINE_INSTALL_HINT)
    else:
        report.engine, report.engine_source = format_version(engine), engine_source
        if engine < MIN_ENGINE:
            report.problems.append(
                f"OnionPress needs Docker Engine {format_version(MIN_ENGINE)} or later; this "
                f"machine has {report.engine}"
                + (" (the client's version; the daemon did not answer)" if engine_source == "client" else "")
                + ". " + ENGINE_INSTALL_HINT)
    compose = parse_version(compose_text) if compose_text is not None else None
    if compose is None:
        report.problems.append(
            "`docker compose` (Compose v2, a Docker CLI plugin) is not available: the "
            "`docker-compose` 1.x package of Ubuntu and Debian 12 is the retired Python "
            f"Compose v1 and has no such command. {COMPOSE_INSTALL_HINT}")
    else:
        report.compose = format_version(compose)
        if compose < MIN_COMPOSE:
            report.problems.append(
                f"OnionPress needs Docker Compose {format_version(MIN_COMPOSE)} or later; this "
                f"machine has {report.compose}. {COMPOSE_INSTALL_HINT}")
    return report


def _ask(docker, args):
    """`docker <args>`'s output, stripped, or None when it failed (the
    runner is docker.Docker, issue #477: one binary, environment and
    timeout for every docker call)."""
    try:
        r = docker.run(args, timeout=60, quiet=True)
    except Exception:  # noqa: BLE001 - a missing binary or a timeout is "no answer"
        return None
    if not r.ok:
        return None
    return (r.output or "").strip()


def probe(docker):
    """Ask this machine's docker (a docker.Docker): the server's version,
    else the client's (a daemon that does not answer still has a client of
    the same package on Debian and Ubuntu), and `docker compose version
    --short`."""
    engine = _ask(docker, ["version", "--format", "{{.Server.Version}}"])
    source = "server"
    if not engine:
        engine = _ask(docker, ["version", "--format", "{{.Client.Version}}"])
        source = "client" if engine else ""
    compose = _ask(docker, ["compose", "version", "--short"])
    return check(engine, compose, engine_source=source)


def describe(report):
    """The lines a person reads: what was found, what is needed, how to
    get there."""
    if report.ok:
        src = f" ({report.engine_source})" if report.engine_source == "client" else ""
        return [f"Docker Engine {report.engine}{src}, Docker Compose {report.compose}: OK "
                f"(OnionPress needs Engine {format_version(MIN_ENGINE)}+ and "
                f"Compose {format_version(MIN_COMPOSE)}+)."]
    return list(report.problems)


def check_command(docker, *, json_output=False, out=None):
    """`onionpress check-docker`: exit 0 when this machine's Docker and
    Compose are new enough, 1 when one is too old or `docker compose` is
    missing, 2 when docker itself did not answer at all. *docker* is the
    docker.Docker runner."""
    out = out or sys.stdout
    report = probe(docker)
    if json_output:
        print(json.dumps(report.as_dict()), file=out)
    else:
        for line in describe(report):
            print(line, file=out)
    if report.ok:
        return 0
    return 2 if not report.engine else 1
