"""Docker subprocess wrapper for OnionPress.

Provides a high-level interface for running docker and docker compose commands
with the correct environment (DOCKER_HOST, COLIMA_HOME, etc.).
"""

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from .platform import OnionPressPaths, detect_timezone


@dataclass
class DockerResult:
    """Result of a docker command."""
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """Stdout stripped of trailing whitespace."""
        return self.stdout.rstrip()


class DockerError(Exception):
    """Raised when a docker command fails and check=True."""

    def __init__(self, args: list, result: DockerResult):
        self.args_list = args
        self.result = result
        cmd = " ".join(str(a) for a in args)
        msg = f"Docker command failed: {cmd}\nExit code: {result.returncode}"
        if result.stderr:
            msg += f"\nStderr: {result.stderr.rstrip()}"
        if result.timed_out:
            msg += "\n(timed out)"
        super().__init__(msg)


class Docker:
    """Docker subprocess wrapper with OnionPress environment."""

    def __init__(
        self,
        paths: OnionPressPaths,
        log_func: Callable[[str], None] | None = None,
        extra_env: dict | None = None,
    ):
        self.paths = paths
        self.log_func = log_func
        self._docker_bin = self._find_docker(paths)
        self._env = self._build_env(paths, extra_env)

    @staticmethod
    def _find_docker(paths: OnionPressPaths) -> str:
        """Resolve the docker binary path."""
        if paths.bin_dir:
            bundled = os.path.join(paths.bin_dir, "docker")
            if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
                return bundled
        return "docker"  # fall back to PATH

    @staticmethod
    def _build_env(paths: OnionPressPaths, extra_env: dict | None = None) -> dict:
        """Build the environment dict for docker subprocesses."""
        env = os.environ.copy()
        env["DOCKER_HOST"] = f"unix://{paths.docker_socket}"
        env["DOCKER_CONFIG"] = paths.docker_config_dir
        env["COLIMA_HOME"] = paths.colima_home
        env["LIMA_HOME"] = os.path.join(paths.colima_home, "_lima")
        env["LIMA_INSTANCE"] = "onionpress"
        env["TZ"] = detect_timezone()
        if paths.bin_dir:
            env["PATH"] = f"{paths.bin_dir}:{env.get('PATH', '')}"
        if extra_env:
            env.update(extra_env)
        return env

    def _log(self, msg: str) -> None:
        if self.log_func:
            self.log_func(msg)

    def run(
        self,
        args: list,
        timeout: int = 30,
        check: bool = False,
        input: str | None = None,
        extra_env: dict | None = None,
        quiet: bool = False,
    ) -> DockerResult:
        """Run a docker command.

        Args:
            args: Command arguments after 'docker' (e.g. ['ps', '-a']).
            timeout: Timeout in seconds.
            check: If True, raise DockerError on non-zero exit.
            input: Optional stdin string.
            extra_env: Additional env vars merged on top of the base env.
            quiet: If True, suppress FAILED log on non-zero exit.
        """
        cmd = [self._docker_bin] + [str(a) for a in args]
        result = self._subprocess(cmd, timeout=timeout, input=input, extra_env=extra_env)
        if not result.ok:
            if not quiet:
                self._log(f"docker {' '.join(str(a) for a in args)} — FAILED (rc={result.returncode})")
            if check:
                raise DockerError(cmd, result)
        return result

    def exec(
        self,
        container: str,
        command: list | str,
        timeout: int = 30,
        check: bool = False,
        quiet: bool = False,
    ) -> DockerResult:
        """Run a command inside a container via docker exec.

        Args:
            container: Container name.
            command: Command as list or string (string is split by shell).
            timeout: Timeout in seconds.
            check: If True, raise DockerError on non-zero exit.
            quiet: If True, suppress FAILED log on non-zero exit.
        """
        if isinstance(command, str):
            args = ["exec", container, "sh", "-c", command]
        else:
            args = ["exec", container] + list(command)
        return self.run(args, timeout=timeout, check=check, quiet=quiet)

    def _compose_profiles_env(self) -> str:
        """Resolve COMPOSE_PROFILES from the install's SITE_TYPE, per call.

        The wordpress/db and site services carry `profiles:` tags in
        docker-compose.yml, and Compose silently *excludes* profiled
        services from bare `pull`/`down`/`ps` invocations when no profile
        is active — so a compose call without this env var would skip
        pulling the WordPress image and leave wordpress/db running on
        `down`. Resolved lazily (not cached at construction) so a
        SITE_TYPE written mid-session by first-run setup takes effect
        without an app restart.
        """
        site_type = "wordpress"
        try:
            with open(self.paths.config_file, "r", encoding="utf-8",
                      errors="replace") as f:
                for line in f:
                    if line.strip().startswith("SITE_TYPE="):
                        site_type = line.strip().split("=", 1)[1]
                        break
        except OSError:
            pass
        return site_type if site_type in ("wordpress", "static") else "wordpress"

    def compose(
        self,
        args: list,
        compose_files: list | None = None,
        timeout: int = 120,
        check: bool = False,
        extra_env: dict | None = None,
    ) -> DockerResult:
        """Run a docker compose command.

        Args:
            args: Arguments after 'docker compose' (e.g. ['up', '-d']).
            compose_files: Optional list of compose file paths (-f flags).
            timeout: Timeout in seconds.
            check: If True, raise DockerError on non-zero exit.
            extra_env: Additional env vars (secrets, ports) for compose.
        """
        cmd_args = ["compose"]
        if compose_files:
            for f in compose_files:
                cmd_args.extend(["-f", f])
        cmd_args.extend(str(a) for a in args)
        # Guarantee an active profile on every compose call — callers that
        # pass their own COMPOSE_PROFILES (containers.py's _build_env) win.
        if not extra_env or "COMPOSE_PROFILES" not in extra_env:
            extra_env = dict(extra_env or {})
            extra_env["COMPOSE_PROFILES"] = self._compose_profiles_env()
        return self.run(cmd_args, timeout=timeout, check=check, extra_env=extra_env)

    def container_running(self, name: str) -> bool:
        """Check if a container is running."""
        result = self.run(
            ["inspect", "-f", "{{.State.Running}}", name],
            timeout=10,
        )
        return result.ok and result.output.strip() == "true"

    def compose_ps(self, compose_files: list | None = None, extra_env: dict | None = None) -> list[dict]:
        """List compose services as dicts with 'Name', 'State', etc."""
        result = self.compose(
            ["ps", "--format", "json"],
            compose_files=compose_files,
            timeout=30,
            extra_env=extra_env,
        )
        if not result.ok or not result.output:
            return []
        services = []
        for line in result.output.splitlines():
            line = line.strip()
            if line:
                try:
                    services.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return services

    def _subprocess(
        self,
        cmd: list,
        timeout: int,
        input: str | None = None,
        extra_env: dict | None = None,
    ) -> DockerResult:
        """Run a subprocess and return DockerResult."""
        env = self._env
        if extra_env:
            env = {**self._env, **extra_env}
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout,
                env=env,
                input=input,
            )
            return DockerResult(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        except subprocess.TimeoutExpired:
            return DockerResult(
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                timed_out=True,
            )
        except FileNotFoundError:
            return DockerResult(
                returncode=127,
                stdout="",
                stderr=f"Docker binary not found: {cmd[0]}",
            )
