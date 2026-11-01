"""The user interfaces' client of the launcher (issue #526).

The Mac menubar (src/menubar.py) and the Linux tray (linux/onionpress-tray)
are widgets: every operation is an `onionpress <subcommand>` run through
the launcher, and every answer is the one JSON object the CLI prints last
(onionpress.confirm). This module is that client without a widget toolkit,
so both UIs run the same algorithm and the tests run it once, against a
stub launcher:

- `Launcher`: runs the launcher (a password or a hub's recovery key in the
  environment, never on argv; no terminal: stdin is /dev/null unless an
  input is given) and reads its last JSON line (`run_json`).
- `launcher_failure`: what a failed run said.
- `run_confirmed`: the check/confirm loop (`--check --json` → a question →
  `--confirmed <what>`), the questions asked through a callable the UI
  supplies.
- `question_text`: one wording per question in `confirm.QUESTIONS`; the
  UIs render it (Cancel the default, no answer in QUESTION_TIMEOUT_SEC is
  Cancel: those semantics stay in the UI, which owns the dialog).
- `plan_backup` / `run_backup`, `plan_restore` / `run_restore` /
  `restore_summary`: the backup and restore flows, their dialogs an object
  the caller supplies (`BackupUI`, `RestoreUI`).
- `update_outcome`: what an update check's answer means.
- `short_onion`, `site_url`, `about_text`: the copy both UIs show.

Imports: the standard library, `onionpress.confirm` and
`onionpress.launcher_ops` only, so the menubar's docker-free import
closure (tests/test_mac_daemon.py) holds; `onionheaven` is imported
lazily where a restore's summary names a hub.
"""

import os
import subprocess
import threading
from typing import Callable, NamedTuple, Optional

from . import confirm as _confirm
from . import launcher_ops

BACKUP_PASSWORD_ENV = launcher_ops.BACKUP_PASSWORD_ENV
HUB_RECOVERY_KEY_ENV = launcher_ops.HUB_RECOVERY_KEY_ENV

# A question left on the screen is not a yes: every UI dialog answers
# Cancel after this long (onionpress.confirm).
QUESTION_TIMEOUT_SEC = 300

SEALED_HUB_DATA_QUESTION = "sealed-hub-data-left-out"

# Launcher subcommands whose arguments can carry a secret (the old
# `backup <password>` / `restore <password> <zip>` forms, setup --pass,
# import-key). A log gets their name only.
UNLOGGED_ARGS = frozenset({"backup", "restore", "setup", "scrub", "import-key"})


def loggable(argv) -> str:
    """The launcher command line as a log may show it."""
    argv = list(argv)
    if len(argv) > 2 and argv[1] in UNLOGGED_ARGS:
        return f"{argv[0]} {argv[1]} <{len(argv) - 2} args not logged>"
    return " ".join(argv)


def launcher_failure(stdout: str, stderr: str, default: str) -> str:
    """What a failed `onionpress` run said: its last ERROR line, else its
    last line, else *default*."""
    lines = [launcher_ops.strip_log_stamp(ln.strip())
             for ln in (stderr or "").splitlines() + (stdout or "").splitlines()
             if ln.strip()]
    errors = [ln for ln in lines if ln.startswith("ERROR")]
    if errors:
        return errors[-1][len("ERROR:"):].strip() or default
    return lines[-1] if lines else default


class Launcher:
    """Runs `<script> <args>`: the launcher, as the UIs call it."""

    def __init__(self, script: str, log: Optional[Callable[[str], None]] = None):
        self.script = script
        self.log = log or (lambda message: None)

    def _env(self, password, extra_env, env):
        if password is None and not extra_env and not env:
            return None
        full = {**os.environ, **(env or {}), **(extra_env or {})}
        if password is not None:
            full[BACKUP_PASSWORD_ENV] = password
        return full

    def run(self, *args, capture: bool = True, timeout: Optional[int] = 60,
            password: Optional[str] = None, extra_env: Optional[dict] = None,
            env: Optional[dict] = None, input: Optional[str] = None,
            text: bool = True) -> subprocess.CompletedProcess:
        """subprocess.run of the launcher. *password* goes in the
        environment (BACKUP_PASSWORD_ENV), as do *extra_env* (a hub's
        recovery key) and *env*; timeout=None waits however long it
        takes. The launcher gets no terminal: stdin is /dev/null unless
        *input* is given, so a command that would read a password there
        answers {"ask": "password"} instead of waiting. Raises what
        subprocess raises (the error logged without the arguments)."""
        argv = [self.script, *args]
        self.log(f"launcher: {loggable(argv)}")
        kwargs = dict(capture_output=capture, text=text, timeout=timeout,
                      env=self._env(password, extra_env, env))
        if text:
            kwargs.update(encoding="utf-8", errors="replace")
        if input is not None:
            kwargs["input"] = input
        else:
            kwargs["stdin"] = subprocess.DEVNULL
        try:
            return subprocess.run(argv, **kwargs)
        except subprocess.TimeoutExpired:
            # str(TimeoutExpired) is the whole argv.
            self.log(f"launcher timed out after {timeout}s: {loggable(argv)}")
            raise
        except OSError as e:
            self.log(f"launcher failed: {e}")
            raise

    def wait(self, *args, timeout: Optional[int] = None) -> int:
        """Run and wait, the output left on this process's stdout and
        stderr (a `start`, `stop` or `restart`): the exit status, -1 when
        the launcher could not be run or timed out."""
        try:
            return self.run(*args, capture=False, timeout=timeout).returncode
        except (OSError, subprocess.SubprocessError):
            return -1

    def run_async(self, *args) -> None:
        """Fire and forget: for actions nothing waits on."""
        argv = [self.script, *args]
        self.log(f"launcher (async): {loggable(argv)}")
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            self.log(f"launcher async failed: {e}")

    def run_json(self, *args, password: Optional[str] = None,
                 extra_env: Optional[dict] = None, env: Optional[dict] = None,
                 input: Optional[str] = None, timeout: Optional[int] = None,
                 on_line: Optional[Callable[[str], None]] = None):
        """Run `onionpress <args>` and read the JSON object the CLI prints
        last (onionpress.confirm): (rc, answer or None, stdout, stderr).
        With *on_line*, every line the CLI logs on stderr is handed over
        as it arrives (a progress window), its time stamp removed. A
        launcher that could not be run is (-1, None, "", why)."""
        argv = [self.script, *args]
        full_env = self._env(password, extra_env, env)
        try:
            if on_line is None:
                r = self.run(*args, timeout=timeout, password=password,
                             extra_env=extra_env, env=env, input=input)
                return r.returncode, _confirm.last_json_line(r.stdout), r.stdout or "", r.stderr or ""
            self.log(f"launcher: {loggable(argv)}")
            proc = subprocess.Popen(
                argv, env=full_env,
                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace")
            if input is not None:
                proc.stdin.write(input)
                proc.stdin.close()
            err_lines = []

            def drain():
                for line in proc.stderr:
                    line = line.rstrip("\n")
                    err_lines.append(line)
                    try:
                        on_line(launcher_ops.strip_log_stamp(line))
                    except Exception:
                        pass
            t = threading.Thread(target=drain, daemon=True)
            t.start()
            out = proc.stdout.read()
            rc = proc.wait(timeout=timeout)
            t.join(5)
            return rc, _confirm.last_json_line(out), out, "\n".join(err_lines)
        except (OSError, subprocess.SubprocessError) as e:
            self.log(f"onionpress {' '.join(args[:2])}: could not run: {e}")
            return -1, None, "", str(e)


# ─── The check/confirm loop ──────────────────────────────────────────────

class Outcome(NamedTuple):
    """What run_confirmed ended with. rc is None when a question was
    declined (answer is then that question)."""
    rc: Optional[int]
    answer: Optional[dict]
    stdout: str
    stderr: str
    confirmed: list

    @property
    def declined(self) -> bool:
        return self.rc is None

    @property
    def ok(self) -> bool:
        return self.rc == 0 and bool(self.answer) and bool(self.answer.get("ok"))

    @property
    def flags(self) -> list:
        """The --confirmed flags for the real run after a --check."""
        return [f for what in self.confirmed for f in ("--confirmed", what)]

    def failure(self, default: str) -> str:
        return (self.answer or {}).get("error") or launcher_failure(
            self.stdout, self.stderr, default)


def run_confirmed(launcher: Launcher, args, ask, *, action: str = "Continue",
                  confirmed=(), **run_kw) -> Outcome:
    """`onionpress <args>` (with --json), every question the CLI stops at
    asked through ask(what, facts, action=action) and given back as
    `--confirmed <what>` on the next run: a `--check` loop ends with the
    facts and the flags for the real run; a real run refused at a question
    (exit 3) is run again once it is answered. Cancel at any question
    ends it (Outcome.declined). The *run_kw* are run_json's (a secret on
    stdin or in the environment)."""
    confirmed = list(confirmed)
    while True:
        flags = [f for what in confirmed for f in ("--confirmed", what)]
        rc, answer, out, err = launcher.run_json(*args, *flags, **run_kw)
        what = (answer or {}).get("confirm")
        if rc == 0 or not what or what in confirmed:
            return Outcome(rc, answer, out, err, confirmed)
        if not ask(what, answer, action=action):
            return Outcome(None, answer, out, err, confirmed)
        confirmed.append(what)


# ─── The questions, worded once ──────────────────────────────────────────

class QuestionText(NamedTuple):
    """A question's dialog: Cancel and *yes*; *typed* is a word the person
    must type for a yes (the uninstall's DELETE); *secret_field* and
    *alternative* are the sealed-hub-data question's key field and its
    "Restore Without" button. style: "critical" | "warning"."""
    what: str
    title: str
    message: str
    yes: str
    style: str = "critical"
    typed: Optional[str] = None
    secret_field: bool = False
    alternative: Optional[str] = None


def question_text(what: str, facts: Optional[dict], action: str = "Continue") -> Optional[QuestionText]:
    """The dialog for the question *what* the CLI stopped at
    (onionpress.confirm.QUESTIONS), from its facts; None for a question
    this module does not know (the UI then answers no). *action* names
    the command for the yes button ("Uninstall", "Reset", "Change")."""
    facts = facts or {}
    if what == "address-change":
        if facts.get("action") == "restore-backup":
            request = ("restore a backup of another onion address, which replaces "
                       "this site's onion service key and its content")
            note = "The current site's content is replaced by the backup's."
            button = "Restore"
        else:
            request = "replace this site's onion service key"
            note = ("OnionPress keeps a copy of the current key in "
                    "~/.onionpress/replaced-keys/, from which it can be imported again.")
            button = "Replace"
        return QuestionText(
            what, "Replace this site's onion address?",
            f"The OnionPress Settings page in WordPress asked to {request}.\n\n"
            f"Current address:\n{facts.get('current') or '(not known yet)'}\n\n"
            f"New address:\n{facts.get('new') or ''}\n\n"
            "Visitors and links using the current address will stop reaching "
            f"this site. {note}\n\n"
            f"{button} it only if you asked for this yourself.",
            button)
    if what == "hub-holds-key":
        listed = "\n".join(f"• {h.get('onionheaven_address')} (key of {h.get('content_address')})"
                           for h in facts.get("hubs") or [])
        verb = action.lower()
        return QuestionText(
            what, "OnionHeaven Holds Your Key",
            "These OnionHeaven hubs may hold a copy of your onion service private "
            f"key:\n\n{listed}\n\n"
            f"OnionPress asks them to delete it when you {verb}, but only while it "
            f"is running. Start OnionPress and {verb} again, or {verb} anyway and "
            "the hub keeps the key.",
            f"{action} Anyway")
    if what == "uninstall":
        return QuestionText(
            what, "Confirm Uninstall",
            "FINAL CONFIRMATION\n\nType 'DELETE' below to confirm permanent deletion "
            "of all data:",
            "Confirm Deletion", typed="DELETE")
    if what == "reset":
        return QuestionText(
            what, "Reset OnionPress?",
            "This will ERASE everything: WordPress, database, Tor state, onion "
            "address keys, config, and secrets.\n\n"
            "To preserve your data, make a backup first (Backup... in the menu). "
            "This cannot be undone.",
            "Reset")
    if what == "clearnet-tunnel":
        # Only the Settings page asks this (handle_action, #463): code in
        # WordPress could have written the token.
        replace = bool(facts.get("replace"))
        return QuestionText(
            what,
            "Replace the Cloudflare Tunnel token?" if replace
            else "Put this site on the regular internet?",
            "The OnionPress Settings page in WordPress asked to "
            f"{'replace' if replace else 'set'} the Cloudflare Tunnel token.\n\n"
            "A tunnel puts this site on the regular internet. Whoever owns the "
            "tunnel in Cloudflare sees this computer's IP address, which the onion "
            "service otherwise hides, and decides what the tunnel reaches.\n\n"
            "Set it only if you pasted this token yourself.",
            "Set Token")
    if what == "prefix-change":
        return QuestionText(
            what, "Change the Address Prefix?",
            f"The address prefix would change from '{facts.get('current') or ''}' to "
            f"'{facts.get('new') or ''}', but this site's onion address stays\n"
            f"{facts.get('address') or ''}\n"
            "(the prefix is used only when a new address is made: a reset, or a "
            "new install).",
            "Change Prefix", style="warning")
    if what == SEALED_HUB_DATA_QUESTION:
        kid = facts.get("recovery_key_id") or "unknown"
        return QuestionText(
            what, "Restore the Hub's Registrants' Keys?",
            "This backup holds an OnionHeaven hub's registry and its registrants' "
            "onion service keys, sealed to the hub's recovery key (key id "
            f"{kid}). Paste that key to restore them.\n\n"
            "Without it, everything else is restored: registrants that are online "
            "send their keys again within a minute; sites that are offline cannot "
            "be taken over until they come back.",
            "Restore With Key", style="warning", secret_field=True,
            alternative="Restore Without")
    return None


def typed_confirmation(text: Optional[str], word: str) -> bool:
    """Whether what was typed is the question's word (case aside)."""
    return (text or "").strip().upper() == word.upper()


def typed_mismatch_message(text: Optional[str], word: str, action: str) -> str:
    return (f"{action} cancelled. Type '{word}' to confirm.\n\n"
            f"(You typed: '{(text or '').strip()}')")


UNINSTALL_DELETES = (
    "Your onion address and private key",
    "All WordPress content and data",
    "Database and configuration",
)


def uninstall_warning(tail: str) -> str:
    """The first uninstall dialog's text, ending in *tail* (the Mac asks
    about a backup first; the Linux tray says a terminal opens)."""
    items = "\n".join(f"• {item}" for item in UNINSTALL_DELETES)
    return ("CRITICAL WARNING\n\nUninstalling will PERMANENTLY DELETE:\n"
            f"{items}\n\nYour site CANNOT BE RECOVERED unless you have a backup.\n\n"
            f"{tail}")


# ─── Backup ──────────────────────────────────────────────────────────────

class BackupUI:
    """The dialogs plan_backup asks of the person at this desktop."""

    def ask_credentials(self, default_user: str, problem: Optional[str]):
        """(username or None for the CLI's default, password), or None for
        Cancel. *problem*: why the last pair was refused."""
        raise NotImplementedError

    def choose_output(self, facts: dict) -> Optional[str]:
        """Where to write the backup: a path, "" for the CLI's default
        (facts["output"]), or None for Cancel. *facts* is the check's
        answer (filename, output, address, username)."""
        raise NotImplementedError


class BackupPlan(NamedTuple):
    """What the check established: the real run is run_backup."""
    args: list
    env: dict
    username: str
    facts: dict


class BackupResult(NamedTuple):
    ok: bool
    message: str
    answer: Optional[dict]


def plan_backup(launcher: Launcher, ui: BackupUI, *, default_user: str = "",
                log: Callable[[str], None] = lambda m: None) -> Optional[BackupPlan]:
    """The protocol up to the real run of `onionpress backup`: the
    credentials, verified by the CLI (`backup --check --json`, which also
    names the file after the address the live key derives to), then where
    to write it. None when cancelled."""
    problem = None
    while True:
        creds = ui.ask_credentials(default_user, problem)
        if creds is None:
            return None
        username, password = creds
        user_args = ["--user", username] if username else []
        env = {BACKUP_PASSWORD_ENV: password}
        log("Backup: verifying credentials...")
        rc, ans, out, err = launcher.run_json("backup", "--check", "--json", *user_args,
                                              env=env, timeout=120)
        if ans and ans.get("ok"):
            break
        problem = (ans or {}).get("error") or launcher_failure(
            out, err, "Could not verify the credentials")
        log(f"Backup: credential verification failed: {problem}")
    output = ui.choose_output(ans)
    if output is None:
        return None
    args = ["backup", "--json", *(["-o", output] if output else []), *user_args]
    return BackupPlan(args, env, ans.get("username") or username or "", ans)


def backup_done_message(answer: dict, plan: Optional[BackupPlan] = None) -> str:
    """"Backup saved to <file> (<size>)", then what the CLI says every
    time (notes: a hub without a recovery key, an unanswered check)."""
    path = answer.get("output") or ""
    if not path and plan and "-o" in plan.args:
        path = plan.args[plan.args.index("-o") + 1]
    msg = f"Backup saved to {os.path.basename(path)}" if path else "Backup saved."
    size = answer.get("bytes")
    if size:
        msg += f" ({size / (1024 * 1024):.1f} MB)"
    for note in answer.get("notes") or []:
        msg += "\n\n" + note
    return msg


def run_backup(launcher: Launcher, plan: BackupPlan, *, on_line=None,
               log: Callable[[str], None] = lambda m: None) -> BackupResult:
    """The real run of the plan, no time limit (issue #317: the copies
    carry their own, sized to the site)."""
    rc, ans, out, err = launcher.run_json(*plan.args, env=plan.env, on_line=on_line,
                                          timeout=None)
    if rc != 0 or not ans or not ans.get("ok"):
        failure = (ans or {}).get("error") or launcher_failure(out, err, "see the OnionPress log")
        log(f"Backup failed: {failure}")
        return BackupResult(False, f"Backup failed: {failure}", ans)
    return BackupResult(True, backup_done_message(ans, plan), ans)


# ─── Restore ─────────────────────────────────────────────────────────────

class RestoreUI:
    """The dialogs plan_restore asks of the person at this desktop. Each
    returns the answer or None for Cancel; a UI's dialog gives up after
    QUESTION_TIMEOUT_SEC, which is Cancel."""

    def ask_password(self, backup_user: Optional[str]) -> Optional[str]:
        raise NotImplementedError

    def confirm(self, facts: dict) -> bool:
        """The backup's facts (restore_facts_text); True to go on."""
        raise NotImplementedError

    def ask_recovery_key(self, facts: dict, problem: Optional[str]) -> Optional[str]:
        """The hub's recovery key as typed, "" to restore without the hub
        data, None to cancel. *problem*: why the last key was refused."""
        raise NotImplementedError

    def ask_question(self, what: str, facts: dict, action: str = "Restore") -> bool:
        """Any other question the CLI stops at (question_text)."""
        raise NotImplementedError

    def error(self, title: str, message: str) -> None:
        raise NotImplementedError


class RestorePlan(NamedTuple):
    """What the checks established: the real run is run_restore. *facts*
    is the check's answer (what restore_summary needs to know afterwards:
    whether the hub data was sealed)."""
    backup_path: str
    extra_args: list
    password: str
    extra_env: dict
    facts: dict


class RestoreResult(NamedTuple):
    ok: bool
    failure: str
    answer: Optional[dict]


RESTORE_CONFIRM_TITLE = "Restore from this backup?"


def restore_facts_text(facts: dict) -> str:
    """The confirmation dialog's text: what the backup is (the CLI
    validated the address and username and sanitized the rest) and what
    the restore replaces."""
    addr = facts.get("onion_address") or "unknown"
    if facts.get("excludes_onion_key") is True:
        # A Settings page backup holds no key: this site keeps its address
        # (the restore refuses one whose metadata says so but which holds a
        # key: backup.staged_onion_key_address).
        addr += " (its key is not in this backup)"
        warning = ("WARNING: This will overwrite your current site and database. "
                   "This backup was made from the Settings page and holds no onion "
                   "address key, so this site keeps its onion address. This cannot "
                   "be undone.")
    else:
        warning = ("WARNING: This will overwrite your current site, database, and "
                   "onion address. This cannot be undone.")
    return (f"You are about to restore from this backup:\n\n"
            f"Onion address: {addr}\n"
            f"Backup date: {facts.get('backup_date') or 'unknown'}\n"
            f"Username: {facts.get('username') or 'unknown'}\n"
            f"OnionPress version: {facts.get('onionpress_version') or 'unknown'}\n\n"
            f"{warning}")


def plan_restore(launcher: Launcher, backup_path: str, ui: RestoreUI, *,
                 log: Callable[[str], None] = lambda m: None) -> Optional[RestorePlan]:
    """The protocol up to the real run of `onionpress restore <zip>`: the
    password the CLI asks for, the facts for the confirmation dialog
    (address, date, user, version, whether the key is in the backup), and
    the questions: a hub's sealed data ("sealed-hub-data-left-out": the
    recovery key, checked by the CLI against the backup, or restore
    without it, with --confirmed), any other through ui.ask_question.
    None when cancelled or refused (the error shown)."""
    def check(password=None, extra_env=None, flags=()):
        return launcher.run_json("restore", backup_path, "--check", "--json", *flags,
                                 password=password, extra_env=extra_env, timeout=300)

    rc, ans, out, err = check()
    backup_user = (ans or {}).get("backup_user") if (ans or {}).get("ask") == "password" else None
    password = ui.ask_password(backup_user)
    if not password:
        return None
    rc, facts, out, err = check(password)
    if not facts or facts.get("error") or not (facts.get("ok") or facts.get("confirm")):
        failure = (facts or {}).get("error") or launcher_failure(out, err, "Could not read backup")
        log(f"Restore: failed to read backup metadata: {failure}")
        ui.error("Invalid backup", failure)
        return None
    if not ui.confirm(facts):
        return None
    extra_env: dict = {}
    confirmed: list = []
    answer = facts
    while answer.get("confirm"):
        question = answer["confirm"]
        if question in confirmed:
            break
        if question == SEALED_HUB_DATA_QUESTION:
            problem = None
            while True:
                key = ui.ask_recovery_key(facts, problem)
                if key is None:
                    return None
                if not key:
                    log("Restore: the hub's sealed data will not be restored "
                        "(no recovery key given)")
                    confirmed.append(SEALED_HUB_DATA_QUESTION)
                    break
                rc, ans, out, err = check(password, {HUB_RECOVERY_KEY_ENV: key})
                if ans and ans.get("ok"):
                    extra_env[HUB_RECOVERY_KEY_ENV] = key
                    break
                problem = (ans or {}).get("error") or launcher_failure(
                    out, err, "The key could not be checked.")
        elif ui.ask_question(question, answer, action="Restore"):
            confirmed.append(question)
        else:
            log(f"Restore: not confirmed ({question})")
            return None
        flags = [f for what in confirmed for f in ("--confirmed", what)]
        rc, answer, out, err = check(password, extra_env or None, flags)
        if not answer or answer.get("error") or not (answer.get("ok") or answer.get("confirm")):
            failure = (answer or {}).get("error") or launcher_failure(out, err, "Could not read backup")
            log(f"Restore: check failed: {failure}")
            ui.error("Restore not started", failure)
            return None
    flags = [f for what in confirmed for f in ("--confirmed", what)]
    return RestorePlan(backup_path, ["--json", *flags], password, extra_env, facts)


def run_restore(launcher: Launcher, plan: RestorePlan, *, data_dir: str,
                log_path: str) -> RestoreResult:
    """The real run of the plan (install-from-backup: cli.cmd_restore tears
    the install down and rebuilds it from the backup). The password goes
    in the environment: argv is readable by every local user (ps), the
    environment only by this one. So does a hub's recovery key (the CLI
    removes it from its environment before any docker child could inherit
    it). No time limit (issue #317): a fixed one ended a large site's
    restore halfway and left the launcher's steps running; those steps
    carry their own limits, sized to the backup, and fail it when hit. A
    failure says the launcher's last lines, the staging folder kept for
    the next start and the log's path (#323)."""
    launcher.log(f"launcher: {loggable([launcher.script, 'restore', plan.backup_path])}")
    ok, failure, _stderr, _out, answer = launcher_ops.run_launcher_restore(
        launcher.script, plan.backup_path, plan.password,
        data_dir=data_dir, log_path=log_path,
        extra_env=plan.extra_env, extra_args=plan.extra_args, timeout=None)
    return RestoreResult(ok, failure, answer or {})


def restore_summary(answer: dict, facts: dict, address: Optional[str] = None, *,
                    vm_memory_gb: Optional[str] = None) -> str:
    """What a finished restore says: the address (the one the CLI derived
    from the backup's key), what a hub may still hold of a key this
    install no longer has (left_on_hubs), an OnionHeaven hub's VM memory
    (a Mac, when *vm_memory_gb* is given), and whether a sealed backup's
    registry and registrants' keys came back."""
    from . import onionheaven
    answer = answer or {}
    restored_addr = address or answer.get("address") or facts.get("onion_address") or "unknown"
    notes = [f"Onion address: {restored_addr}"]
    left = answer.get("left_on_hubs") or []
    if left:
        notes.append(onionheaven.describe_remaining(left))
    if vm_memory_gb is not None and onionheaven.is_onionheaven_instance(restored_addr):
        try:
            cur_mem_int = int(vm_memory_gb)
        except (TypeError, ValueError):
            cur_mem_int = 1
        if cur_mem_int < 5:
            notes.append("OnionHeaven detected — VM memory will increase to 5 GB on relaunch.")
        else:
            notes.append(f"OnionHeaven detected — VM memory: {vm_memory_gb} GB.")
    if facts.get("onionheaven_data") == "sealed":
        if answer.get("hub_data") == "restored":
            notes.append("OnionHeaven registry and registrants' keys: restored.")
        elif answer.get("hub_data_note"):
            notes.append(answer["hub_data_note"])
        else:
            notes.append("OnionHeaven registry and registrants' keys: not restored.")
    return "Site restored successfully.\n\n" + "\n".join(notes)


# ─── Update check ────────────────────────────────────────────────────────

class UpdateOutcome(NamedTuple):
    """What an update check's answer means. kind: "failed" | "available"
    | "current". title and body word the first and the last; for
    "available" the body is the UI's (how to install differs)."""
    kind: str
    title: str
    body: str
    urgency: str = "normal"
    current: Optional[str] = None
    latest: Optional[str] = None
    release_url: Optional[str] = None
    other_instances: tuple = ()
    active_offsets: tuple = ()
    error: Optional[str] = None


def update_outcome(returncode: int, stdout: str, stderr: str) -> UpdateOutcome:
    """One `onionpress update --check --json` run (cli.check_for_update_report;
    the Linux tray's `check-for-update --json` is the same report). The
    CLI exits 0 whatever happened and says so in its JSON: an `error` (no
    network, GitHub not answering, curl refusing to start) is "Update
    check failed", never "Up to date" (issue #355)."""
    failed = UpdateOutcome("failed", "Update check failed", "")
    if returncode != 0:
        return failed._replace(body=(stderr or "").strip()[:200]
                               or f"onionpress update --check exited {returncode}",
                               error=(stderr or "").strip()[:200] or None)
    data = _confirm.last_json_line(stdout)
    if not data or data.get("error") or "update_available" not in data:
        why = str((data or {}).get("error") or "The update check gave no answer.")[:200]
        return failed._replace(body=why, error=why)
    common = dict(current=data.get("current"), latest=data.get("latest"),
                  release_url=data.get("release_url") or None,
                  other_instances=tuple(data.get("other_instances") or []),
                  active_offsets=tuple(data.get("active_offsets") or []))
    if data.get("update_available"):
        return UpdateOutcome("available", f"OnionPress {data.get('latest')} is available",
                             "", **common)
    return UpdateOutcome("current", "Up to date",
                         f"You're on the current version ({data.get('current')}).",
                         urgency="low", **common)


# ─── The copy both UIs show ──────────────────────────────────────────────

def short_onion(addr: Optional[str]) -> Optional[str]:
    """A 56-character .onion cut for a menu title (the full address is
    one click away: Copy Onion Address)."""
    if addr and len(addr) > 16 and addr.endswith(".onion"):
        return f"{addr[:8]}…{addr[-6:]}"
    return addr


def site_url(address: str, onionname: str = "") -> str:
    """The site's URL as Copy Onion Address gives it: with the
    /<onionname>/ subsite path (app_commands.shareable_url is this), no
    login token."""
    return f"http://{address}/{onionname}/" if onionname else f"http://{address}/"


ABOUT_TAGLINE = "Just Works. Free, forever."
ABOUT_SUBTITLE = "WordPress + Tor Onion Service"
ABOUT_FEATURES = (
    "Full WordPress that you own and run",
    "Internet Archive's Wayback Machine integration",
    "Tor Onion Service with an address you own, forever",
    "Requires visitors to use Tor or Brave browsers",
    "Privacy-first design",
    "Free and open source",
)
ABOUT_CREDIT = "Created by the Internet Archive"
ABOUT_LICENSE = "License: AGPL v3 or later"
ABOUT_WEBSITE = "https://onionpress.org"


def about_comments(device: str = "your computer") -> str:
    """The About dialog's body under the name and version."""
    features = "\n".join(f"• {f}" for f in ABOUT_FEATURES)
    return (f"Run your own website from {device}. {ABOUT_TAGLINE}\n"
            f"{ABOUT_SUBTITLE}\n\nFeatures:\n{features}")


def about_text(version: str, device: str = "your computer") -> str:
    """The whole About text (the Mac's one dialog)."""
    return (f"OnionPress v{version}\n\n{about_comments(device)}\n\n"
            f"{ABOUT_CREDIT}\n{ABOUT_LICENSE}")
