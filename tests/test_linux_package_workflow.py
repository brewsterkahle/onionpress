#!/usr/bin/env python3
"""Static invariants for .github/workflows/linux-package.yml, build/check-deb.sh,
build/build-linux.sh and linux/install.sh.

The workflow builds the Linux package with the real Debian tools, checks it
and, on a published release, attaches it to the release with a build
provenance attestation. Like tests/test_publish_workflow.py, this pins the
properties that would only be found on release day:

1. It runs anywhere. Every job is on a GitHub-hosted runner, nothing names
   this repository or a GHCR account, and no secret is involved: the
   attestation signs with the run's OIDC identity and the upload goes to
   the repository the run belongs to.

2. Write access is confined to the release job. The build and smoke jobs
   run on pull requests, from forks too, where they execute contributor
   code; they keep the read-only token the workflow declares at the top.
   Only the job gated on the release event may hold `write` permissions.

3. The package CI attests is the package the Mac would build. The build
   step runs build/build-linux.sh, the same script build/release.sh runs,
   and the checks in build/check-deb.sh cover what the Mac's `ar` fallback
   shipped wrong in v2.5.3: files owned by the Mac user, AppleDouble `._`
   members, and what the launcher needs executable. The dependency the
   check requires is the one the control file declares.

4. The smoke test runs the installer twice. Until issue #316 the second
   run nested every copied tree inside the first; install.sh now installs
   through linux/install-tree.sh (stage, then rename into place), and the
   workflow checks the result of a re-run on a real machine.

No PyYAML: CI installs nothing beyond CPython and PyNaCl, so the workflow is
read with regexes, like tests/test_publish_workflow.py.
"""

import os
import re
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

WORKFLOW = ".github/workflows/linux-package.yml"
CHECK = "build/check-deb.sh"
BUILD = "build/build-linux.sh"
RELEASE = "build/release.sh"
INSTALLER = "linux/install.sh"

# What the launcher runs from $SCRIPTS_DIR; a non-executable copy fails
# silently at its `[ -x ]` guard.
PLUGIN_SCRIPTS = ("install_plugin.sh", "activate_plugin.sh")


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _code(text):
    """Text with `#` comment lines removed, so the explanations in the
    headers (which name the traps) are not mistaken for the instructions."""
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _m(pattern):
    """A pattern whose ^ and $ match at line boundaries."""
    return re.compile(pattern, re.MULTILINE)


def _command(text, prefix):
    """The shell command starting with `prefix`, with its `\\`-continued
    lines joined; None if absent."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            parts = [line.strip()]
            while parts[-1].endswith("\\") and i + 1 < len(lines):
                i += 1
                parts.append(lines[i].strip())
            return " ".join(part.rstrip("\\").strip() for part in parts)
    return None


def _jobs(code):
    """{job id: job text} for the top-level jobs: block of a workflow."""
    jobs = {}
    current = None
    in_jobs = False
    for line in code.splitlines():
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if line and not line.startswith(" "):
            break  # another top-level key
        m = re.match(r"^  ([A-Za-z_][\w-]*):\s*$", line)
        if m:
            current = m.group(1)
            jobs[current] = []
            continue
        if current:
            jobs[current].append(line)
    return {job: "\n".join(lines) for job, lines in jobs.items()}


class LinuxPackageWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(WORKFLOW)
        self.code = _code(self.text)
        self.jobs = _jobs(self.code)

    # ── 1. Runs anywhere ──────────────────────────────────────────────────

    def test_every_job_runs_on_a_github_hosted_runner(self):
        runners = re.findall(r"^\s*runs-on:[ \t]*(.+?)[ \t]*$", self.code, re.MULTILINE)
        self.assertTrue(runners, f"{WORKFLOW}: no runs-on lines found")
        offenders = [r for r in runners if not re.fullmatch(r"ubuntu-[a-z0-9.]+(-arm)?", r)]
        self.assertEqual(
            offenders, [],
            f"{WORKFLOW}: jobs not on a GitHub-hosted Ubuntu runner: {offenders}. "
            f"The .deb needs dpkg-deb and lintian, which every Ubuntu runner has; "
            f"a self-hosted runner makes every release depend on one machine.")

    def test_nothing_names_this_repository_or_a_secret(self):
        hardcoded = [line.strip() for line in self.code.splitlines()
                     if re.search(r"brewsterkahle|ghcr\.io/[a-z0-9-]+/|secrets\.", line)]
        self.assertEqual(
            hardcoded, [],
            f"{WORKFLOW}: lines naming an account or a secret, which a fork does "
            f"not have:\n  " + "\n  ".join(hardcoded))
        self.assertIn(
            "github.repository", self.jobs["release"],
            f"{WORKFLOW}: the release upload must go to github.repository, "
            f"the repository the run belongs to.")

    def test_expected_jobs_exist(self):
        self.assertEqual(
            sorted(self.jobs), ["changes", "deb", "install-twice", "release"],
            f"{WORKFLOW}: jobs are {sorted(self.jobs)}")

    # ── 2. Write access only in the release job ───────────────────────────

    def test_workflow_token_is_read_only_by_default(self):
        m = re.search(r"^permissions:\n((?:  .*\n)+)", self.code + "\n", re.MULTILINE)
        self.assertIsNotNone(m, f"{WORKFLOW}: no top-level permissions: block")
        self.assertEqual(
            m.group(1).strip(), "contents: read",
            f"{WORKFLOW}: the top-level permissions must be exactly `contents: read`; "
            f"the build and smoke jobs run contributor code from fork PRs.")

    def test_only_the_release_job_may_write(self):
        for job, body in self.jobs.items():
            writes = re.findall(r"^\s+([a-z-]+):\s*write\b", body, re.MULTILINE)
            if job == "release":
                self.assertEqual(
                    sorted(writes), ["attestations", "contents", "id-token"],
                    f"{WORKFLOW}: the release job needs contents (upload), id-token "
                    f"(Sigstore) and attestations (store) write; has {writes}")
                self.assertRegex(
                    body, _m(r"^\s+if:\s*github\.event_name == 'release'\s*$"),
                    f"{WORKFLOW}: the release job must be gated on the release event")
            else:
                self.assertEqual(
                    writes, [],
                    f"{WORKFLOW}: job {job} holds write permissions {writes}; only "
                    f"the release job may, because every other job runs for pull "
                    f"requests from forks.")

    def test_release_job_runs_after_the_build_and_attests_the_uploaded_file(self):
        body = self.jobs["release"]
        self.assertRegex(body, _m(r"^\s+needs:\s*deb\s*$"), f"{WORKFLOW}: release must need deb")
        self.assertRegex(
            body, r"uses:\s*actions/attest-build-provenance@v\d+",
            f"{WORKFLOW}: the release job must use actions/attest-build-provenance "
            f"pinned to a major version")
        self.assertRegex(
            body, _m(r"^\s+subject-path:\s*onionpress\.deb\s*$"),
            f"{WORKFLOW}: the attestation subject must be onionpress.deb, the file uploaded")
        upload = _command(body, "gh release upload")
        self.assertIsNotNone(upload, f"{WORKFLOW}: no `gh release upload` in the release job")
        for needed in ("onionpress.deb", "--clobber", "github.event.release.tag_name"):
            self.assertIn(
                needed, upload,
                f"{WORKFLOW}: the release upload must carry {needed!r}: onionpress.deb is "
                f"the README's download name, --clobber replaces the Mac-built copy.")

    def test_release_tag_must_match_the_source_version(self):
        body = self.jobs["deb"]
        self.assertIn(
            "github.event.release.tag_name", body,
            f"{WORKFLOW}: the deb job must compare the release tag with the version "
            f"in src/menubar.py, so a tag cut from the wrong commit fails here.")
        self.assertIn("src/menubar.py", body)

    # ── 3. The package CI attests is the one the Mac would build ──────────

    def test_builds_with_the_script_release_sh_uses(self):
        self.assertRegex(
            self.jobs["deb"], _m(r"^\s+bash build/build-linux\.sh\s*$"),
            f"{WORKFLOW}: the deb job must run build/build-linux.sh, not its own copy "
            f"of the packaging steps")
        self.assertRegex(
            _code(_read(RELEASE)), r"build-linux\.sh",
            f"{RELEASE} must still build the .deb through build/build-linux.sh")

    def test_checks_the_package_with_check_deb_including_lintian(self):
        self.assertRegex(
            self.jobs["deb"],
            r"bash build/check-deb\.sh build/onionpress\.deb .*--lintian",
            f"{WORKFLOW}: the deb job must run build/check-deb.sh on the built "
            f"package with --lintian; the checks live in the script so they run "
            f"the same locally.")

    def test_check_deb_requires_the_dependency_the_control_file_declares(self):
        build = _read(BUILD)
        declared = re.findall(r"python3-nacl \(>= [0-9.]+\)", build)
        self.assertEqual(
            len(set(declared)), 1,
            f"{BUILD}: expected one python3-nacl dependency in the control file, "
            f"found {declared}")
        self.assertIn(
            f'REQUIRED_DEPENDENCY="{declared[0]}"', _read(CHECK),
            f"{CHECK}: REQUIRED_DEPENDENCY must be exactly {declared[0]!r}, what "
            f"{BUILD} writes into Depends (PyNaCl 1.5.0 is Debian's and Ubuntu's).")

    def test_control_file_depends_is_one_line_and_description_has_no_dot_line(self):
        build = _read(BUILD)
        m = re.search(r"^Depends: (.*)$", build, re.MULTILINE)
        self.assertIsNotNone(m, f"{BUILD}: no Depends line")
        self.assertFalse(
            m.group(1).rstrip().endswith(","),
            f"{BUILD}: Depends is folded over several lines; a binary package's "
            f"control file wants one line (lintian: multiline-field)")
        m = re.search(r"^Description: .*\n((?: .*\n)+)", build, re.MULTILINE)
        self.assertIsNotNone(m, f"{BUILD}: no Description")
        bad = [line for line in m.group(1).splitlines()
               if line.startswith(" .") and line != " ."]
        self.assertEqual(
            bad, [],
            f"{BUILD}: Description lines starting with a dot are control statements "
            f"to dpkg (v2.5.3's ' .onion address' line): {bad}")

    def test_check_deb_requires_the_plugin_scripts_executable(self):
        check = _read(CHECK)
        for script in PLUGIN_SCRIPTS:
            self.assertIn(
                f"./opt/onionpress/scripts/{script}", check,
                f"{CHECK}: REQUIRED_EXECUTABLES must list scripts/{script}")
        manifest = _read("linux/install-tree.sh")
        for script in PLUGIN_SCRIPTS:
            self.assertRegex(
                manifest, _m(r"^file +app/Resources/scripts/" + re.escape(script)
                             + r" +scripts/" + re.escape(script) + r" +0755$"),
                f"linux/install-tree.sh: scripts/{script} must be installed 0755; "
                f"the .deb stages its tree from this list")
        self.assertIn('"-rwxr-xr-x"', check, f"{CHECK}: must check the mode of each executable")

    def test_check_deb_refuses_foreign_owners_and_build_host_litter(self):
        check = _code(_read(CHECK))
        self.assertIn(
            '$2 != "root/root"', check,
            f"{CHECK}: must fail any member not owned by root/root (dpkg installs "
            f"the recorded owner; v2.5.3 shipped uid 502)")
        self.assertRegex(
            check, r"\\\._",
            f"{CHECK}: must fail AppleDouble `._` members (half of v2.5.3's package)")
        self.assertIn("--fail-on error", check, f"{CHECK}: lintian must fail on errors")

    def test_build_linux_records_root_ownership_in_both_paths(self):
        build = _code(_read(BUILD))
        self.assertIn(
            "dpkg-deb --build --root-owner-group", build,
            f"{BUILD}: dpkg-deb needs --root-owner-group, or the package records "
            f"the building user as owner")
        self.assertIn("--owner=root:0 --group=root:0", build,
                      f"{BUILD}: the GNU tar fallback must record root")
        self.assertIn("--uid 0 --gid 0 --uname root --gname root", build,
                      f"{BUILD}: the bsdtar (macOS) fallback must record root")
        self.assertIn(
            "export COPYFILE_DISABLE=1", build,
            f"{BUILD}: without COPYFILE_DISABLE macOS's tar adds a `._` member per file")
        self.assertRegex(
            build, r'DEBIAN/conffiles" <<EOF\n/etc/xdg/autostart/onionpress-tray\.desktop\nEOF',
            f"{BUILD}: the autostart entry in /etc must be listed in DEBIAN/conffiles")

    # ── 4. The smoke test runs the installer twice ────────────────────────

    def test_smoke_job_runs_install_sh_twice_and_only_for_linux_changes(self):
        body = self.jobs["install-twice"]
        runs = re.findall(r"^\s+run:\s*bash linux/install\.sh\s*$", body, re.MULTILINE)
        self.assertEqual(
            len(runs), 2,
            f"{WORKFLOW}: install-twice must run `bash linux/install.sh` exactly twice, "
            f"found {len(runs)}")
        self.assertRegex(
            body, _m(r"^\s+if:\s*needs\.changes\.outputs\.linux == 'true'\s*$"),
            f"{WORKFLOW}: install-twice must be gated on the changes job")
        self.assertIn(
            "lib/onionpress/onionpress", body,
            f"{WORKFLOW}: the re-run check must look for the nested trees of issue #316")
        changes = self.jobs["changes"]
        self.assertRegex(
            changes, _m(r"^\s+if:\s*github\.event_name == 'pull_request' \|\| github\.event_name == 'workflow_dispatch'\s*$"),
            f"{WORKFLOW}: the changes job (and so the smoke test) runs for pull "
            f"requests and manual dispatch only, never on push or release")
        self.assertIn(
            r"'^(linux/|\.github/workflows/linux-package\.yml)'", changes,
            f"{WORKFLOW}: the changes job must look for linux/ and this workflow")

    def test_installer_installs_through_install_tree(self):
        installer = _code(_read(INSTALLER))
        self.assertRegex(
            installer, _m(r'^\$SUDO bash "\$REPO_DIR/linux/install-tree\.sh" install "\$REPO_DIR" "\$INSTALL_DIR"$'),
            f"{INSTALLER}: the files must go in through linux/install-tree.sh install "
            f"(staged, then renamed into place); a `cp -r` into an existing folder "
            f"nests it (issue #316), which the smoke test would catch on its second run")
        self.assertNotRegex(
            installer, r'cp -r "\$REPO_DIR/[^"]+" "\$INSTALL_DIR/',
            f"{INSTALLER}: no `cp -r` from the checkout into $INSTALL_DIR")

if __name__ == "__main__":
    unittest.main()
