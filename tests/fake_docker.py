"""A scripted stand-in for onionpress.docker.Docker, for tests (issue #477).

Every module that runs docker takes a Docker from its caller; a test hands
it one of these instead of patching subprocess in the module. *answer* is
called the way a subprocess.run stand-in was, with the argv docker would
have run (the binary first) and the keyword arguments (input, timeout, env,
...), and returns what such a stand-in returned: a CompletedProcess, a
(returncode, stdout, stderr) tuple, an int returncode, a DockerResult or
None (success, no output); it may raise subprocess.TimeoutExpired or
FileNotFoundError, which Docker.run turns into a timed-out or 127 result.
Every argv is kept in `calls`, every stdin in `inputs`.

`popen` runs a real subprocess.Popen when `docker_bin` is an executable
file (a stub script a test wrote), else what `popen_factory` makes.
`over_binary` is a real Docker over such a stub binary, for the tests
that exercise the runner's own subprocess handling.
"""

import os
import subprocess

from onionpress import wp_files
from onionpress.docker import Docker, DockerError, DockerResult


class FakeDocker:
    """The methods of onionpress.docker.Docker, scripted (module docstring)."""

    def __init__(self, answer=None, *, docker_bin="docker", env=None, log_func=None,
                 popen_factory=None):
        self.docker_bin = docker_bin
        self.env = dict(env) if env is not None else {"DOCKER_HOST": "unix:///nowhere/docker.sock"}
        self.log_func = log_func
        self.answer = answer
        self.popen_factory = popen_factory
        self.calls = []       # argv, binary first
        self.inputs = []      # stdin of each call (None without one)
        self.timeouts = []
        self.popen_calls = []

    # -- what a call answers --

    def _result(self, argv, binary=False, **kw):
        try:
            ans = self.answer(argv, **kw) if self.answer is not None else None
        except subprocess.TimeoutExpired:
            msg = f"Command timed out after {kw.get('timeout')}s"
            return DockerResult(-1, b"" if binary else "", msg.encode() if binary else msg,
                                timed_out=True)
        except FileNotFoundError:
            msg = f"Docker binary not found: {argv[0]}"
            return DockerResult(127, b"" if binary else "", msg.encode() if binary else msg)
        if ans is None:
            return DockerResult(0, b"" if binary else "", b"" if binary else "")
        if isinstance(ans, DockerResult):
            return ans
        if isinstance(ans, subprocess.CompletedProcess):
            return DockerResult(ans.returncode, ans.stdout if ans.stdout is not None else "",
                                ans.stderr if ans.stderr is not None else "")
        if isinstance(ans, bool):
            return DockerResult(0 if ans else 1, "", "")
        if isinstance(ans, int):
            return DockerResult(ans, "", "")
        if isinstance(ans, tuple):
            rc, out, err = (tuple(ans) + ("", ""))[:3]
            return DockerResult(rc, out, err)
        raise TypeError(f"FakeDocker: an answer of {type(ans).__name__}")

    # -- Docker's methods --

    def run(self, args, timeout=30, check=False, input=None, extra_env=None,
            quiet=False, binary=False):
        argv = [self.docker_bin] + [str(a) for a in args]
        self.calls.append(argv)
        self.inputs.append(input)
        self.timeouts.append(timeout)
        env = {**self.env, **extra_env} if extra_env else self.env
        result = self._result(argv, binary=binary, input=input, timeout=timeout, env=env,
                              capture_output=True,
                              **({} if binary or isinstance(input, bytes)
                                 else dict(text=True, encoding="utf-8", errors="replace")))
        if not result.ok:
            if not quiet and self.log_func:
                self.log_func(f"docker {' '.join(argv[1:])} — FAILED (rc={result.returncode})")
            if check:
                raise DockerError(argv, result)
        return result

    def exec(self, container, command, timeout=30, check=False, quiet=False, user=None):
        args = ["exec"] + (["-u", user] if user else []) + [container]
        if isinstance(command, str):
            args += ["sh", "-c", command]
        else:
            args += list(command)
        return self.run(args, timeout=timeout, check=check, quiet=quiet)

    def wp(self, *args, timeout=30, check=False, quiet=False, input=None,
           offline=False, nice=False):
        return self.run(wp_files.wp_cli_args(*args, stdin=input is not None,
                                             offline=offline, nice=nice),
                        timeout=timeout, check=check, input=input, quiet=quiet)

    def compose(self, args, compose_files=None, timeout=120, check=False, extra_env=None):
        cmd_args = ["compose"]
        for f in compose_files or ():
            cmd_args.extend(["-f", f])
        cmd_args.extend(str(a) for a in args)
        return self.run(cmd_args, timeout=timeout, check=check, extra_env=extra_env)

    def container_running(self, name):
        result = self.run(["inspect", "-f", "{{.State.Running}}", name], timeout=10)
        return result.ok and result.output.strip() == "true"

    def compose_ps(self, compose_files=None, extra_env=None):
        return []

    def run_to(self, args, *, log_file=None, extra_env=None):
        return self.run(args, timeout=None, extra_env=extra_env, quiet=True).returncode

    def popen(self, args, *, extra_env=None, **kwargs):
        argv = [self.docker_bin] + [str(a) for a in args]
        self.popen_calls.append((argv, kwargs))
        env = {**self.env, **extra_env} if extra_env else self.env
        if self.popen_factory is not None:
            return self.popen_factory(argv, env=env, **kwargs)
        if os.path.isabs(self.docker_bin) and os.access(self.docker_bin, os.X_OK):
            return subprocess.Popen(argv, env=env, **kwargs)
        raise AssertionError("FakeDocker.popen: no stub binary and no popen_factory")

    # -- what the tests read --

    def argvs(self, *first):
        """The recorded argvs (without the binary) starting with *first*."""
        return [argv[1:] for argv in self.calls
                if argv[1:1 + len(first)] == list(first)]


def over_binary(docker_bin, env=None, log_func=None):
    """A real onionpress.docker.Docker over the executable *docker_bin* (a
    stub a test wrote), running children with *env* (default: this
    process's environment)."""
    d = Docker.__new__(Docker)
    d.paths = None
    d.log_func = log_func
    d._docker_bin = docker_bin
    d._env = dict(os.environ if env is None else env)
    return d
