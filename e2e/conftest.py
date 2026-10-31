"""pytest wiring for the e2e suite (docs/E2E-TESTING.md §3).

- One harness Session per run, made in pytest_configure: the host probe,
  the safety guard, the builds and the node objects. An UnsafeTarget there
  stops the run before anything is started.
- Tests take a `node` and are parametrized over the platforms this run
  tests, filtered by their `platform` mark: `any` (the default) runs once
  per tested platform, `mac`/`linux` only there (`linux` is every
  Debian-based guest, whichever --guest ran). A test with no platform to
  run on is reported skipped with the reason.
- Modules marked `incremental` are one scenario per node: after a step
  fails, the rest of that module is skipped on that platform.
- Every outcome goes into the run's report; pytest_sessionfinish collects
  evidence, runs the tripwire, tears the nodes down and writes
  report.md + results.json.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import safety  # noqa: E402
from harness.platforms import DEFAULT_LINUX_GUEST, LINUX_GUESTS, PLATFORMS, canonical_platform  # noqa: E402
from harness.report import Outcome  # noqa: E402
from harness.session import Options, Session  # noqa: E402

PLATFORM_MARKS = {"any": PLATFORMS, "mac": ("macos",), "linux": ("linux",)}

_SESSION_KEY = pytest.StashKey()
_FAILED_STEP = {}        # (module, platform) -> name of the step that failed


def pytest_addoption(parser):
    g = parser.getgroup("onionpress e2e")
    g.addoption("--platforms", default="", help="comma list of macos,linux (default: all the host offers)")
    g.addoption("--max-nodes", type=int, default=4, help="most nodes this run creates")
    g.addoption("--mac-app", default="", help="an OnionPress.app to test (copied, never run in place)")
    g.addoption("--mac-dmg", default="", help="an onionpress.dmg to test")
    g.addoption("--deb", default="", help="an onionpress.deb to test (default: built in the VM)")
    g.addoption("--release", default="", help="test a published release's assets (a tag, or latest)")
    g.addoption("--scratch-root", default=safety.DEFAULT_SCRATCH_ROOT)
    g.addoption("--results-dir", default="")
    g.addoption("--keep", action="store_true", help="leave the nodes running for inspection")
    g.addoption("--limactl", default="", help="limactl to use for Linux VMs")
    g.addoption("--guest", default=DEFAULT_LINUX_GUEST, choices=LINUX_GUESTS,
                help=f"the linux-vm node's distro (default {DEFAULT_LINUX_GUEST})")


def pytest_configure(config):
    config.addinivalue_line("markers", "platform(name): any (default), mac or linux")
    config.addinivalue_line("markers", "tier(name): t0..t4")
    config.addinivalue_line("markers", "incremental: later steps skip after a failed one")
    if config.option.collectonly or config.option.help:
        return
    wanted = [canonical_platform(p.strip())
              for p in config.getoption("--platforms").split(",") if p.strip()] or None
    unknown = [p for p in wanted or () if p not in PLATFORMS]
    if unknown:
        raise pytest.UsageError(f"unknown platform(s) {', '.join(unknown)}; know {', '.join(PLATFORMS)}")
    opts = Options(
        platforms=wanted, max_nodes=config.getoption("--max-nodes"),
        mac_app=config.getoption("--mac-app"), mac_dmg=config.getoption("--mac-dmg"),
        deb=config.getoption("--deb"), release=config.getoption("--release"),
        scratch_root=config.getoption("--scratch-root"),
        results_dir=config.getoption("--results-dir"), keep=config.getoption("--keep"),
        limactl=config.getoption("--limactl"), guest=config.getoption("--guest"))
    try:
        session = Session(opts).prepare()
    except safety.UnsafeTarget as e:
        raise pytest.UsageError(f"refusing to run: {e}") from e
    config.stash[_SESSION_KEY] = session


def _session(config):
    return config.stash.get(_SESSION_KEY, None)


def _platform_mark(item_or_fn):
    mark = item_or_fn.get_closest_marker("platform") if hasattr(item_or_fn, "get_closest_marker") else None
    return mark.args[0] if mark and mark.args else "any"


def pytest_generate_tests(metafunc):
    if "node" not in metafunc.fixturenames:
        return
    session = _session(metafunc.config)
    mark = metafunc.definition.get_closest_marker("platform")
    which = mark.args[0] if mark and mark.args else "any"
    applies = PLATFORM_MARKS[which]
    tested = list(session.nodes) if session else []
    params = [p for p in tested if p in applies]
    if params:
        metafunc.parametrize("node", params, indirect=True, scope="session")
    else:
        reason = (f"no {'/'.join(applies)} node in this run"
                  if session else "collect-only")
        metafunc.parametrize("node", [pytest.param("none", marks=pytest.mark.skip(reason=reason))],
                             indirect=True, scope="session")


def pytest_collection_modifyitems(config, items):
    session = _session(config)
    if not session:
        return
    needed = []
    for item in items:
        callspec = getattr(item, "callspec", None)
        plat = callspec.params.get("node") if callspec else None
        if plat in session.nodes and plat not in needed:
            needed.append(plat)
        test = _test_id(item)
        session.report.applies[test] = list(PLATFORM_MARKS[_platform_mark(item)])
        tier = item.get_closest_marker("tier")
        if tier and tier.args and tier.args[0] not in session.report.tiers:
            session.report.tiers.append(tier.args[0])
    session.report.tiers.sort()
    session.prewarm(needed)


@pytest.fixture(scope="session")
def e2e(request):
    return _session(request.config)


@pytest.fixture(scope="session")
def node(request):
    session = _session(request.config)
    return session.nodes[request.param]


def _test_id(item):
    module = item.module.__name__.rsplit(".", 1)[-1]
    if module.startswith("test_"):
        module = module[len("test_"):]
    return f"{module}::{getattr(item, 'originalname', item.name)}"


def _item_platform(item):
    callspec = getattr(item, "callspec", None)
    return callspec.params.get("node") if callspec else None


def pytest_runtest_setup(item):
    if item.get_closest_marker("incremental") is None:
        return
    key = (item.module.__name__, _item_platform(item))
    if key in _FAILED_STEP:
        pytest.skip(f"after failed step {_FAILED_STEP[key]}")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    session = _session(item.config)
    plat = _item_platform(item)
    if session is None or plat not in session.nodes:
        return rep
    if rep.failed and item.get_closest_marker("incremental") is not None and rep.when in ("setup", "call"):
        _FAILED_STEP.setdefault((item.module.__name__, plat), getattr(item, "originalname", item.name))
    record = None
    if rep.when == "call":
        record = "passed" if rep.passed else "skipped" if rep.skipped else "failed"
    elif rep.when == "setup" and not rep.passed:
        record = "skipped" if rep.skipped else "error"
    elif rep.when == "teardown" and rep.failed:
        record = "error"
    if record:
        reason = ""
        if rep.skipped and isinstance(rep.longrepr, tuple):
            reason = str(rep.longrepr[2]).removeprefix("Skipped: ")
        elif rep.failed:
            reason = getattr(rep, "longreprtext", "") or str(rep.longrepr)
            crash = getattr(getattr(rep.longrepr, "reprcrash", None), "message", "")
            reason = crash or reason
        session.report.add(Outcome(_test_id(item), plat, record, reason, round(rep.duration, 1)))
    return rep


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    e2e_session = _session(session.config)
    if e2e_session is None:
        return
    report = e2e_session.finish()
    if report.tripwire and session.exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    e2e_session = _session(config)
    if e2e_session is None:
        return
    r = e2e_session.report
    tr = terminalreporter
    tr.section("OnionPress e2e")
    tr.line(f"Host: {r.host}")
    for plat, facts in r.tested.items():
        tr.line(f"Tested:     {plat}: {facts.get('os', '')} {facts.get('arch', '')} "
                f"({facts.get('kind', '')}, {facts.get('docker', '')})")
    for plat, why in r.not_tested.items():
        tr.line(f"Not tested: {plat}: {why}")
    tr.line(f"Verdict: {r.verdict}")
    tr.line(f"Report:  {os.path.join(e2e_session.results_dir, 'report.md')}")
