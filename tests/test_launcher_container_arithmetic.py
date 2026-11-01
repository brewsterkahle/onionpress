#!/usr/bin/env python3
"""Container output never reaches bash arithmetic unchecked (issue #466).

Inside `$(( ))`, `(( ))`, `[[ a -gt b ]]`, `let` and `declare -i`, bash
evaluates a variable's VALUE as an expression, and an array subscript in
it runs a command substitution: with `x='a[$(id>/tmp/x)]'`,
`$(( x + 1 ))` runs `id` (bash 3.2 and 5 alike). The Mac launcher's
`detect_container_runtime` read MemTotal out of the tor container with
`docker exec` and put it straight into `$(( ))`: root in that container
(it faces the network) chose a value the next `onionpress start` ran as
the Mac user.

Every number read out of a container (or derived from one) is now held
to `^[0-9]{1,N}$` before arithmetic sees it, else taken as 0. The
census below fails on a new unguarded use: a variable assigned from
docker / wp-cli output (transitively) that appears in an arithmetic
context without a guard line between the assignment and the use.
`[ a -gt b ]` (the test builtin) does not evaluate expressions, but the
census covers it too, so a later change from `[` to `[[` cannot open
the hole.
"""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LAUNCHERS = ("app/MacOS/onionpress", "app/MacOS/launcher.sh", "linux/onionpress")

# No space in it: the launchers strip spaces with tr before the value is
# used, and `$(>file)` (a redirection with no command) creates the file.
HOSTILE = "a[$(>$OP_PWNED)]"


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


# --- the census --------------------------------------------------------

# Commands whose stdout a container (or WordPress) chooses.
TAINT_SOURCES = re.compile(
    r'\$\(\s*(?:"\$BIN_DIR/"?)?(?:docker|wp_cli|wp_cli_in|wp_exec|limactl)\b')
ASSIGN = re.compile(r'^\s*(?:local\s+|export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$')
VAR_REF = re.compile(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)')
# A guard: [[ "$name" =~ ^[0-9]{1,N}$ ]] || name=0  (digits spelled out or
# as a range).
GUARD = re.compile(
    r'\[\[\s*"?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?"?\s*=~\s*\^\[(?:0-9|0123456789)\]'
    r'\{1,\d+\}\$\s*\]\]\s*\|\|\s*\1=')
ARITH_OPS = re.compile(r'\s-(?:gt|lt|eq|ne|ge|le)\s')


def _arithmetic_sites(line):
    """Fragments of `line` that bash evaluates arithmetically."""
    sites = []
    for m in re.finditer(r'\$\(\((.*?)\)\)', line):
        sites.append(m.group(1))
    for m in re.finditer(r'(?<!\$)\(\((.*?)\)\)', line):
        sites.append(m.group(1))
    for m in re.finditer(r'\[\[(.*?)\]\]', line):
        if ARITH_OPS.search(m.group(1)):
            sites.append(m.group(1))
    for m in re.finditer(r'(?<![\[\w])\[ (.*?) \]', line):
        if ARITH_OPS.search(m.group(1)):
            sites.append(m.group(1))
    if re.match(r'\s*(let|declare -i|typeset -i)\s', line):
        sites.append(line)
    return sites


def _names_in(fragment):
    names = set(VAR_REF.findall(fragment))
    # Inside $(( )) a bare identifier is a variable too.
    names.update(re.findall(r'(?<![\$\w])([A-Za-z_][A-Za-z0-9_]*)(?![\w(])', fragment))
    return names


def unguarded_arithmetic(text):
    """[(line number, name)] of tainted names used in arithmetic without a
    guard between their last taint and the use."""
    lines = text.split("\n")
    tainted = {}  # name -> line of last tainting assignment
    guarded_at = {}  # name -> line of last guard
    findings = []
    for n, line in enumerate(lines, 1):
        stripped = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        if not stripped.strip():
            continue
        g = GUARD.search(stripped)
        if g:
            guarded_at[g.group(1)] = n
            continue
        a = ASSIGN.match(stripped)
        if a:
            name, rhs = a.group(1), a.group(2)
            if TAINT_SOURCES.search(rhs):
                tainted[name] = n
            else:
                refs = _names_in(rhs)
                src = [t for t in refs if t in tainted and tainted[t] > guarded_at.get(t, 0)]
                if src:
                    tainted[name] = n
                elif name in tainted:
                    # Reassigned from something clean.
                    del tainted[name]
        for site in _arithmetic_sites(stripped):
            for name in _names_in(site):
                if name in tainted and tainted[name] > guarded_at.get(name, 0):
                    findings.append((n, name))
    return findings


class TestCensus(unittest.TestCase):
    """No launcher puts container output into arithmetic unguarded."""

    def test_no_unguarded_arithmetic(self):
        for rel in LAUNCHERS:
            with self.subTest(launcher=rel):
                self.assertEqual(
                    unguarded_arithmetic(_read(rel)), [],
                    f"{rel}: container output in bash arithmetic without a "
                    "[[ \"$x\" =~ ^[0-9]{1,N}$ ]] || x=0 guard (issue #466)")

    def test_census_sees_the_old_code(self):
        old = (
            "_vm_mem_kb=$(docker exec onionpress-tor awk '/MemTotal/{print $2}' /proc/meminfo)\n"
            '_vm_mem_kb="${_vm_mem_kb:-0}"\n'
            "_vm_mem_gb=$(( (_vm_mem_kb + 524288) / 1048576 ))\n"
        )
        self.assertEqual(unguarded_arithmetic(old), [(3, "_vm_mem_kb")])

    def test_census_sees_a_guard(self):
        new = (
            "_vm_mem_kb=$(docker exec onionpress-tor awk '/MemTotal/{print $2}' /proc/meminfo)\n"
            '[[ "$_vm_mem_kb" =~ ^[0123456789]{1,12}$ ]] || _vm_mem_kb=0\n'
            "_vm_mem_gb=$(( (_vm_mem_kb + 524288) / 1048576 ))\n"
        )
        self.assertEqual(unguarded_arithmetic(new), [])

    def test_census_follows_a_derived_value(self):
        text = (
            'stats=$(docker exec onionheaven sqlite3 /x "SELECT 1")\n'
            "count=$(echo \"$stats\" | cut -d'|' -f1)\n"
            "count=${count:-0}\n"
            'if [[ "$count" -gt 3 ]]; then echo big; fi\n'
            "let total=count+1\n"
        )
        self.assertEqual(unguarded_arithmetic(text), [(4, "count"), (5, "count")])

    def test_census_sees_test_builtin_and_wp_cli(self):
        text = (
            "n=$(wp_cli post list --format=count)\n"
            'if [ "$n" -gt 0 ]; then echo posts; fi\n'
        )
        self.assertEqual(unguarded_arithmetic(text), [(2, "n")])

    def test_the_guards_are_in_place(self):
        mac = _read("app/MacOS/onionpress")
        self.assertIn('[[ "$_vm_mem_kb" =~ ^[0123456789]{1,12}$ ]] || _vm_mem_kb=0', mac)
        self.assertIn('[[ "$_vm_cpus" =~ ^[0123456789]{1,6}$ ]] || _vm_cpus=0', mac)
        # Linux's one (write_status's uptime from docker inspect's StartedAt)
        # went with the bash into onionpress.status_writer (#523), where
        # tests/test_status_writer.py holds the hostile value to 0.
        self.assertNotIn("started_epoch", _read("linux/onionpress"))


# --- the guarded block under bash ---------------------------------------

def _mac_vm_block():
    """detect_container_runtime's reads of the running VM's memory and CPU
    count, from the MemTotal read to the CPU guard."""
    text = _read("app/MacOS/onionpress")
    start = text.index("                _vm_mem_kb=$(docker exec onionpress-tor awk")
    end_marker = "|| _vm_cpus=0\n"
    end = text.index(end_marker, start) + len(end_marker)
    return text[start:end]


DOCKER_STUB = """#!/bin/bash
# docker exec ... awk/nproc and docker inspect answer from $OP_DOCKER_ANSWER.
printf '%s\\n' "$OP_DOCKER_ANSWER"
"""


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestGuardedBlocksUnderBash(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-arith-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        stub = os.path.join(self.bin, "docker")
        with open(stub, "w") as f:
            f.write(DOCKER_STUB)
        os.chmod(stub, 0o755)
        self.pwned = os.path.join(self.tmp, "pwned")
        self.data_dir = os.path.join(self.tmp, "data")
        os.mkdir(self.data_dir)

    def _run(self, block, answer, tail):
        # The block runs inside a function: the Linux one says `local`.
        script = (
            f"DATA_DIR={shlex.quote(self.data_dir)}\n"
            "log() { echo \"$1\"; }\n"
            "block() {\n" + block + tail + "}\nblock\n"
        )
        env = dict(os.environ, OP_DOCKER_ANSWER=answer, OP_PWNED=self.pwned,
                   PATH=self.bin + os.pathsep + os.environ.get("PATH", ""))
        r = subprocess.run(["bash", "-c", script], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_hostile_memtotal_runs_nothing_on_the_mac(self):
        out = self._run(_mac_vm_block(), HOSTILE,
                        'echo "mem=$_vm_mem_gb cpus=$_vm_cpus"\n')
        self.assertFalse(os.path.exists(self.pwned),
                         "the container's value ran a command on the host")
        self.assertEqual(out, "mem=0 cpus=0\n")

    def test_the_old_code_did_run_it(self):
        """The hole the guard closes: the same block without the guard."""
        old = _mac_vm_block().replace(
            '[[ "$_vm_mem_kb" =~ ^[0123456789]{1,12}$ ]] || _vm_mem_kb=0',
            '_vm_mem_kb="${_vm_mem_kb:-0}"')
        self.assertNotEqual(old, _mac_vm_block())
        self._run(old, HOSTILE, "true\n")
        self.assertTrue(os.path.exists(self.pwned))

    def test_real_values_are_taken(self):
        # One stub answers both reads: 4194304 kB is 4 GB; as a CPU count
        # it is past the guard's six digits.
        out = self._run(_mac_vm_block(), "4194304",
                        'echo "mem=$_vm_mem_gb cpus=$_vm_cpus"\n')
        self.assertEqual(out, "mem=4 cpus=0\n")
        out = self._run(_mac_vm_block(), "8",
                        'echo "mem=$_vm_mem_gb cpus=$_vm_cpus"\n')
        self.assertEqual(out, "mem=0 cpus=8\n")

    def test_empty_and_junk_values_are_zero(self):
        for answer in ("", "abc", "-5", "1" * 13, "0x10", "1e3", "4194304abc"):
            with self.subTest(answer=answer):
                out = self._run(_mac_vm_block(), answer,
                                'echo "mem=$_vm_mem_gb cpus=$_vm_cpus"\n')
                self.assertEqual(out, "mem=0 cpus=0\n")


if __name__ == "__main__":
    unittest.main()
