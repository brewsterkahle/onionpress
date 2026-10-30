# Docker's rootless scripts, carried by the .deb

`dockerd-rootless.sh` and `dockerd-rootless-setuptool.sh` are Docker's own,
from the Moby repository's `contrib/` folder, byte for byte. The package
installs them to `/opt/onionpress/docker-rootless/` and the postinst uses
them only as a last resort: after a `dockerd-rootless-setuptool.sh` on
`PATH` (Docker CE's `docker-ce-rootless-extras`) and after
`/usr/share/docker.io/contrib` (Debian's `docker.io`). Ubuntu's `docker.io`
(24.04's 29.1.3) ships neither script, which is why they are here: without
them a first install on Ubuntu fell back to the docker group, silently
(issue #486, e2e run of 2026-10-30).

| | |
|---|---|
| Source | https://github.com/moby/moby, branch `docker-29.x` |
| Commit | `27f3e40d39730e2e29dbcdaee3bd3ddcd3516c60` (2026-10-29) |
| `dockerd-rootless.sh` | sha256 `200203633806081a401e60aefdf68a8fa73fc7dc80aa854c52a69d47710a3488` |
| `dockerd-rootless-setuptool.sh` | sha256 `1c9f0dc93ebb3d75255254ec760d26a912affba7f329ab8abffe8e25eb0b3f94` |
| License | Apache-2.0, Copyright 2013-2026 Docker, Inc. (`linux/copyright`) |

`tests/test_docker_rootless_vendored.py` holds the same hashes: a change to
either file, or a refresh, fails it until the pins (here and there) say so.

## What they need

The wrapper runs `rootlesskit`, `slirp4netns`, `newuidmap`/`newgidmap`
(`uidmap`), `nsenter` (util-linux) and `sysctl` (`procps`), all in the
package's `Depends`, and `dockerd` from `docker.io` or `docker-ce`. The setup
tool writes `~/.config/systemd/user/docker.service` with
`ExecStart=/opt/onionpress/docker-rootless/dockerd-rootless.sh`, so that unit
lives as long as OnionPress is installed, as Debian's does as long as
`docker.io` is.

The wrapper asks RootlessKit for `--detach-netns`, which RootlessKit 2.0
brought; Ubuntu 24.04 ships 1.1.1, which refuses the flag and the daemon
never starts. Before the setup tool starts the unit, the postinst probes
`rootlesskit --help` and, when the flag is missing, writes the drop-in Docker
documents for the purpose, as the user:
`~/.config/systemd/user/docker.service.d/onionpress-rootlesskit.conf` with
`Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS=false`.

## Refreshing

Pick the commit (the head of the `docker-29.x` branch, or the one matching
the dockerd Ubuntu ships), fetch both files from it, and record the new
commit and hashes here and in the test:

```bash
c=<commit>
for f in dockerd-rootless.sh dockerd-rootless-setuptool.sh; do
    curl -sfL -o "linux/docker-rootless/$f" "https://raw.githubusercontent.com/moby/moby/$c/contrib/$f"
done
chmod 0755 linux/docker-rootless/*.sh
sha256sum linux/docker-rootless/*.sh
```

Then run the first-install test on both guests (`make e2e` with
`--guest ubuntu-24.04` and the Debian default) before merging.
