"""A stateful fake `docker` for the start traces (tests/test_start_traces.py).

Installed as `docker` on the stub PATH (tests/start_trace.py writes a
one-line wrapper that runs this file with the test's interpreter). Every
call is one line in $OP_CALLS: `docker ARGS`, with a compose file given
by path written as its base name (`-f docker-compose.yml` alone dropped:
compose run from the docker folder reads it anyway, so the launchers'
bare `docker compose` and ContainerManager's `-f <dir>/docker-compose.yml`
record the same), and ` [BIND=<host>]` after a `compose … up` when
ONIONPRESS_BIND_HOST is set (Linux's WordPress port, issue #464).

What it answers comes from the JSON state file $FAKE_DOCKER_STATE, which it
also updates (volumes created and removed):

  volumes        names `volume ls` lists
  volume_ls_fail how many more `volume ls` calls fail (-1: all of them)
  in_use         {volume: [container ids]}: `volume rm` fails while a
                 volume has some; `ps -aq --filter volume=V` lists them and
                 `rm -f` of them frees it, unless `stuck` names the volume
  stuck          volumes `rm -f` cannot free
  wp_installed   `wp core is-installed` answers yes
  htaccess       `test -s …/.htaccess` answers yes
  plugins        {slug: version} in WordPress's plugins folder
  wp_port_host   the host `docker port onionpress-wordpress` reports ("" none)
  cloudflared    `container inspect onionpress-cloudflared` answers yes
  derivatives_marker  the uploads/ marker of a pending derivative rebuild
  rules          [{"match": regex, "rc": n, "stdout": s, "times": k}]: the
                 first rule whose regex matches the argv (joined by spaces)
                 answers; `times` counts down (absent: always)

Anything else succeeds with no output.
"""

import json
import os
import re
import sys


def record(line):
    with open(os.environ["OP_CALLS"], "a", encoding="utf-8") as f:
        f.write(line + "\n")


def shown(args):
    """argv as recorded: compose files by base name, the default file
    alone dropped."""
    if args[:1] != ["compose"]:
        return list(args)
    out, files, i = ["compose"], [], 1
    while i < len(args):
        if args[i] == "-f" and i + 1 < len(args):
            files.append(os.path.basename(args[i + 1]))
            i += 2
            continue
        break
    if files != ["docker-compose.yml"]:
        for name in files:
            out += ["-f", name]
    return out + list(args[i:])


def main(args):
    path = os.environ["FAKE_DOCKER_STATE"]
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    line = "docker " + " ".join(shown(args))
    if args[:1] == ["compose"] and "up" in args and os.environ.get("ONIONPRESS_BIND_HOST"):
        line += f" [BIND={os.environ['ONIONPRESS_BIND_HOST']}]"
    record(line)
    rc, out = answer(state, args)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    return rc


def answer(state, args):
    joined = " ".join(args)
    for rule in state.get("rules", []):
        if rule.get("times", 1) == 0 or not re.search(rule["match"], joined):
            continue
        if "times" in rule:
            rule["times"] -= 1
        return rule.get("rc", 0), rule.get("stdout", "")
    volumes = state.setdefault("volumes", [])
    in_use = state.setdefault("in_use", {})
    if args[:1] == ["info"]:
        return 0, '["name=seccomp"]'
    if args[:2] == ["volume", "ls"]:
        if state.get("volume_ls_fail", 0):
            if state["volume_ls_fail"] > 0:
                state["volume_ls_fail"] -= 1
            return 1, ""
        return 0, "\n".join(volumes)
    if args[:2] == ["volume", "create"]:
        if args[2] not in volumes:
            volumes.append(args[2])
        return 0, args[2]
    if args[:2] == ["volume", "rm"]:
        name = args[-1]
        if in_use.get(name):
            return 1, ""
        if name in volumes:
            volumes.remove(name)
            return 0, name
        return 1, ""
    if args[:1] == ["ps"] and "--filter" in args:
        wanted = args[args.index("--filter") + 1]
        if wanted.startswith("volume="):
            return 0, "\n".join(in_use.get(wanted[len("volume="):], []))
        return 0, ""
    if args[:2] == ["rm", "-f"]:
        for vol, ids in in_use.items():
            if vol not in state.get("stuck", []):
                in_use[vol] = [i for i in ids if i not in args[2:]]
        return 0, ""
    if args[:2] == ["image", "inspect"]:
        return 0, "sha256:0"
    if args[:2] == ["container", "inspect"]:
        if args[-1] == "onionpress-cloudflared":
            return (0, "") if state.get("cloudflared") else (1, "")
        return 1, ""
    if args[:2] == ["network", "inspect"]:
        return 1, ""
    if args[:1] == ["port"]:
        if args[1] == "onionpress-wordpress":
            host = state.get("wp_port_host", "")
            return (0, f"{host}:8080") if host else (1, "")
        if args[1] == "onionpress-tor":
            return 0, "127.0.0.1:9050"
        return 1, ""
    if args[:1] == ["exec"]:
        return exec_answer(state, args, joined)
    return 0, ""


def exec_answer(state, args, joined):
    if re.search(r"\bwp core is-installed\b", joined):
        return (0, "") if state.get("wp_installed") else (1, "")
    if "test -s /var/www/html/.htaccess" in joined:
        return (0, "") if state.get("htaccess", True) else (1, "")
    plugins = state.get("plugins", {})
    m = re.search(r"test -f /var/www/html/wp-content/plugins/([a-z0-9-]+)/", joined)
    if m:
        return (0, "") if m.group(1) in plugins else (1, "")
    m = re.search(r"Version:.* /var/www/html/wp-content/plugins/([a-z0-9-]+)/", joined)
    if m:
        version = plugins.get(m.group(1))
        return (0, version) if version else (1, "")
    if "test -f /var/www/html/wp-content/uploads/.onionpress-regenerate-derivatives" in joined:
        return (0, "") if state.get("derivatives_marker") else (1, "")
    if "mktemp -d /tmp/onionpress-plugin-" in joined:
        return 0, "/tmp/onionpress-plugin-abcdef"
    if re.search(r"\bwp site list\b", joined):
        return 0, "http://site.onion/" if state.get("wp_installed") else ""
    return 0, ""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
