"""The run's report (docs/E2E-TESTING.md §3.1).

It opens with what was tested on — the build, the host, the platforms
tested with their facts, the platforms NOT tested with the reason, which
node played which role — and then one matrix: a row per test, a column per
platform that was tested. A platform that was not available gets no
column, so a run on one OS can never read as covering another.

results.json holds the same data; `merge()` combines several runs (a Mac
run and a CI Linux run of the same build) into one matrix.
"""

import json
import time
from dataclasses import asdict, dataclass, field

from .platforms import PLATFORM_LABELS, PLATFORMS

OUTCOME_CELLS = {"passed": "pass", "failed": "FAIL", "error": "ERROR", "skipped": "skip"}
NOT_APPLICABLE = "—"


@dataclass
class Outcome:
    test: str               # "test_t0_stack.py::test_first_start" (no platform)
    platform: str           # "macos" | "debian"
    outcome: str            # passed | failed | error | skipped
    reason: str = ""        # failure summary or skip reason
    seconds: float = 0.0


@dataclass
class RunReport:
    build: dict = field(default_factory=dict)
    host: str = ""
    tor: str = "public network"
    tiers: list = field(default_factory=list)
    tested: dict = field(default_factory=dict)       # platform -> facts dict
    not_tested: dict = field(default_factory=dict)   # platform -> reason
    roles: dict = field(default_factory=dict)        # "node[macos]" -> node name
    applies: dict = field(default_factory=dict)      # test -> [platforms] it applies to
    outcomes: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)      # node -> [[label, seconds, ok]]
    tripwire: list = field(default_factory=list)     # [[node, path, line, what]]
    notes: list = field(default_factory=list)
    started: str = ""
    finished: str = ""

    # ── recording ───────────────────────────────────────────────────────
    def start(self):
        self.started = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def finish(self):
        self.finished = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def add(self, outcome):
        # One outcome per (test, platform): a failed teardown after a
        # passed call turns it into an error, never a second row.
        for i, o in enumerate(self.outcomes):
            if o.test == outcome.test and o.platform == outcome.platform:
                if o.outcome == "passed" and outcome.outcome != "passed":
                    self.outcomes[i] = outcome
                return
        self.outcomes.append(outcome)

    # ── verdict ─────────────────────────────────────────────────────────
    @property
    def failures(self):
        return [o for o in self.outcomes if o.outcome in ("failed", "error")]

    @property
    def verdict(self):
        if self.tripwire:
            return "FAIL (tripwire: a public OnionPress service was mentioned in a node's logs)"
        if not self.tested:
            return "NOTHING TESTED (no platform was available)"
        if self.failures:
            return f"FAIL ({len(self.failures)} failed)"
        ran = [o for o in self.outcomes if o.outcome == "passed"]
        if not ran:
            return "NOTHING RAN (every test was skipped)"
        return "PASS (every test that ran passed; untested platforms are listed above)"

    # ── output ──────────────────────────────────────────────────────────
    def columns(self):
        return [p for p in PLATFORMS if p in self.tested]

    def to_json(self):
        data = asdict(self)
        data["verdict"] = self.verdict
        return json.dumps(data, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        data.pop("verdict", None)
        outcomes = [Outcome(**o) for o in data.pop("outcomes", [])]
        report = cls(**data)
        report.outcomes = outcomes
        return report

    def cell(self, test, platform):
        applies = self.applies.get(test)
        if applies is not None and platform not in applies:
            return NOT_APPLICABLE
        for o in self.outcomes:
            if o.test == test and o.platform == platform:
                cell = OUTCOME_CELLS.get(o.outcome, o.outcome)
                if o.outcome == "skipped" and o.reason:
                    cell += f" ({o.reason})"
                return cell
        return "not run"

    def render_markdown(self):
        out = ["# OnionPress e2e run", ""]
        build = ", ".join(f"{k}: {v}" for k, v in sorted(self.build.items())) or "unknown"
        out.append(f"- **Build:** {build}")
        out.append(f"- **Host:** {self.host} · **Tor:** {self.tor}"
                   + (f" · **Tiers:** {', '.join(self.tiers)}" if self.tiers else ""))
        if self.tested:
            for plat in self.columns():
                facts = self.tested[plat]
                detail = ", ".join(f"{k} {v}" for k, v in sorted(facts.items()) if v not in ("", None))
                out.append(f"- **Tested:** {PLATFORM_LABELS.get(plat, plat)} — {detail}")
        else:
            out.append("- **Tested:** nothing")
        for plat in PLATFORMS:
            if plat in self.not_tested:
                out.append(f"- **Not tested:** {PLATFORM_LABELS.get(plat, plat)} — "
                           f"{self.not_tested[plat]}")
        if self.roles:
            out.append("- **Roles:** " + "  ".join(f"{r}={n}" for r, n in sorted(self.roles.items())))
        out.append(f"- **Started:** {self.started} · **Finished:** {self.finished}")
        out += ["", f"**Verdict:** {self.verdict}", ""]

        cols = self.columns()
        tests = []
        for o in self.outcomes:
            if o.test not in tests:
                tests.append(o.test)
        if tests and cols:
            out.append("| Test | " + " | ".join(PLATFORM_LABELS.get(c, c) for c in cols) + " |")
            out.append("|---|" + "---|" * len(cols))
            for t in tests:
                out.append(f"| `{t}` | " + " | ".join(self.cell(t, c) for c in cols) + " |")
            out.append("")
        if self.failures:
            out += ["## Failures", ""]
            for o in self.failures:
                out.append(f"- `{o.test}` on {PLATFORM_LABELS.get(o.platform, o.platform)}: "
                           f"{o.reason.strip().splitlines()[0][:300] if o.reason.strip() else ''}")
            out.append("")
        if self.tripwire:
            out += ["## Tripwire", ""]
            for node, path, line, what in self.tripwire:
                out.append(f"- {node}: {what} mentioned at {path}:{line}")
            out.append("")
        if self.timings:
            out += ["## Waits", "", "| Node | Wait | Seconds | |", "|---|---|---|---|"]
            for node, entries in sorted(self.timings.items()):
                for label, seconds, ok in entries:
                    out.append(f"| {node} | {label} | {seconds:.0f} | {'ok' if ok else 'gave up'} |")
            out.append("")
        if self.notes:
            out += ["## Notes", ""] + [f"- {n}" for n in self.notes] + [""]
        return "\n".join(out)


def merge(reports):
    """One report from several runs. A platform tested in more than one
    run keeps the later run's results (and says so in the notes); a
    platform any run tested is not "not tested"."""
    merged = RunReport()
    builds = {json.dumps(r.build, sort_keys=True) for r in reports}
    if len(builds) > 1:
        merged.notes.append("the merged runs tested different builds: " + "; ".join(sorted(builds)))
    merged.build = reports[-1].build if reports else {}
    merged.host = " + ".join(r.host for r in reports)
    merged.tor = " / ".join(sorted({r.tor for r in reports}))
    merged.started = min((r.started for r in reports if r.started), default="")
    merged.finished = max((r.finished for r in reports if r.finished), default="")
    for r in reports:
        merged.tiers += [t for t in r.tiers if t not in merged.tiers]
        for plat, facts in r.tested.items():
            if plat in merged.tested:
                merged.notes.append(f"{plat} was tested in more than one run; "
                                    f"the results of the run on {r.host} are shown")
                merged.outcomes = [o for o in merged.outcomes if o.platform != plat]
            merged.tested[plat] = facts
        merged.roles.update(r.roles)
        merged.applies.update(r.applies)
        merged.timings.update(r.timings)
        merged.tripwire += r.tripwire
        merged.notes += r.notes
        for o in r.outcomes:
            merged.outcomes.append(o)
    for r in reports:
        for plat, reason in r.not_tested.items():
            if plat not in merged.tested:
                merged.not_tested.setdefault(plat, reason)
    return merged
