"""The check/confirm protocol between the CLI and its user interfaces
(issue #366, CLI-first phase 2).

The CLI cannot show a native dialog, so every command that would ask the
owner something is run in two steps by a UI (the Mac menubar, the Linux
tray, a script), generalising `import-key`'s ONIONPRESS_IMPORT_KEY_CONFIRMED:

1. The UI runs `<cmd> --check --json`. The CLI does every check it can
   without changing anything and prints one JSON object:
   {"ok": true, ...facts}        nothing to ask; the facts word the UI's
                                 own summary (a restore's address and date)
   {"confirm": "<what>", ...facts}   a question, with what the dialog
                                 needs (an address-change: "current", "new")
   {"error": "<message>"}        refused, or failed; nothing was changed
2. The UI shows its dialog from those facts. Cancel is the default, and no
   answer in 5 minutes is Cancel. Those semantics live in the UI.
3. The UI runs the command again with `--confirmed <what>` for every
   question it asked (or all of them in ONIONPRESS_CONFIRMED, separated by
   commas). Without that the CLI refuses past the check (exit 3, the
   answer naming the question: answer_refused_question): a client that
   skips step 1 gets a refusal, never a silent destructive action. At a
   terminal a command may ask its question itself instead, as the
   launchers always have.

A command may also need a secret before it can check anything (a
restore's password): {"ask": "password", ...facts} (exit 5), and the UI
collects it and runs the check again with it in the environment.

Exit statuses: 0 ok, 1 failed, 3 refused (nothing changed; a run that
stopped at an unanswered question too), 4 a confirmation is needed
(`EXIT_CONFIRM`: only `--check` answers this), 5 an input is needed
(`EXIT_ASK`). With --json the answer is always the last line of stdout,
so a caller parses `last_json_line`.

The questions (`what`): "address-change" (a key import or a restore of
another address replaces this site's onion address: current, new),
"sealed-hub-data-left-out" (a hub's backup restored without its recovery
key: key_id), "hub-holds-key" (an OnionHeaven hub may still hold the key
and OnionPress cannot ask it to let go now: hubs), "prefix-change" (an
ADDRESS_PREFIX the live address does not start with: current, new,
address), "uninstall" and "reset" (everything is deleted).
"""

import json
import os
import sys

CONFIRMED_ENV = "ONIONPRESS_CONFIRMED"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 3
EXIT_CONFIRM = 4
EXIT_ASK = 5

# Every question the CLI asks. A new one is added here, and a test holds
# the UIs' dialogs to this list.
QUESTIONS = (
    "address-change",
    "sealed-hub-data-left-out",
    "hub-holds-key",
    "prefix-change",
    "uninstall",
    "reset",
)

NOT_CONFIRMED_MSG = ("Not done: this needs a confirmation. Run it with "
                     "--check --json to see the question, then again with "
                     "--confirmed {what}.")


class NeedsConfirmation(Exception):
    """A command stopped at a question: *what* and the facts a dialog
    needs. Raised by a check that was not given `--confirmed <what>`."""

    def __init__(self, what, **facts):
        if what not in QUESTIONS:
            raise ValueError(f"unknown question {what!r}")
        super().__init__(what)
        self.what = what
        self.facts = facts

    def answer(self):
        return {"confirm": self.what, **self.facts}


class Refused(Exception):
    """A command refused before changing anything; str() says why."""


def add_arguments(parser, *, check=True):
    """--check, --json and --confirmed WHAT (repeatable) on *parser*."""
    if check:
        parser.add_argument(
            "--check", action="store_true",
            help="Check and say what would be asked; change nothing")
    parser.add_argument(
        "--json", action="store_true",
        help="Answer with one JSON object on the last line of stdout")
    parser.add_argument(
        "--confirmed", action="append", default=[], metavar="WHAT",
        choices=QUESTIONS,
        help="A question --check asked, answered yes (repeatable; or "
             f"${CONFIRMED_ENV}, comma-separated)")
    return parser


def given(args=None, environ=None):
    """The confirmations a run was given: --confirmed flags plus
    $ONIONPRESS_CONFIRMED. Unknown names are ignored."""
    environ = os.environ if environ is None else environ
    names = set(getattr(args, "confirmed", None) or [])
    for name in environ.get(CONFIRMED_ENV, "").replace(",", " ").split():
        names.add(name)
    return frozenset(n for n in names if n in QUESTIONS)


def require(what, confirmed, **facts):
    """Raise NeedsConfirmation(what, **facts) unless *what* was confirmed."""
    if what not in confirmed:
        raise NeedsConfirmation(what, **facts)


def dumps(doc):
    return json.dumps(doc, sort_keys=True)


def last_json_line(text):
    """The JSON object the last line(s) of *text* hold, or None: one line
    `{...}`, or an indented object ending the text (`json.dumps(indent=2)`,
    as check-for-update prints)."""
    lines = (text or "").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    for start in range(len(lines) - 1, -1, -1):
        if lines[start].lstrip().startswith("{"):
            try:
                doc = json.loads("\n".join(lines[start:]))
            except ValueError:
                return None
            return doc if isinstance(doc, dict) else None
        if start == len(lines) - 1 and not lines[start].strip().startswith("}"):
            return None
    return None


def answer_ok(json_output, out=None, text=None, **facts):
    """Print {"ok": true, ...facts} (with --json; else *text*, if any) and
    return 0."""
    out = out or sys.stdout
    if json_output:
        print(dumps({"ok": True, **facts}), file=out)
    elif text:
        print(text, file=out)
    return EXIT_OK


def answer_ask(what, json_output, out=None, err=None, **facts):
    """Print {"ask": what, ...facts}: the command needs *what* (a password)
    before it can check anything. Returns EXIT_ASK."""
    out = out or sys.stdout
    err = err or sys.stderr
    if json_output:
        print(dumps({"ask": what, **facts}), file=out)
    else:
        print(f"ERROR: a {what} is required.", file=err)
    return EXIT_ASK


def answer_confirm(exc, json_output, out=None, err=None):
    """Print the question *exc* asks (JSON, or a sentence) and return
    EXIT_CONFIRM."""
    out = out or sys.stdout
    err = err or sys.stderr
    if json_output:
        print(dumps(exc.answer()), file=out)
    else:
        print(f"CONFIRM {exc.what}: " + ", ".join(
            f"{k}={v}" for k, v in sorted(exc.facts.items())), file=out)
        print(NOT_CONFIRMED_MSG.format(what=exc.what), file=err)
    return EXIT_CONFIRM


def answer_refused_question(exc, json_output, out=None, err=None):
    """A real run (no --check) stopped at the question *exc* asks: refused,
    exit EXIT_REFUSED, nothing changed. The answer still carries the
    question ({"error", "confirm", ...facts}), so a client that skipped
    --check can ask it and run again with --confirmed."""
    out = out or sys.stdout
    err = err or sys.stderr
    message = NOT_CONFIRMED_MSG.format(what=exc.what)
    if json_output:
        print(dumps({"error": message, **exc.answer()}), file=out)
    else:
        print(f"CONFIRM {exc.what}: " + ", ".join(
            f"{k}={v}" for k, v in sorted(exc.facts.items())), file=out)
        print(f"ERROR: {message}", file=err)
    return EXIT_REFUSED


def answer_error(message, json_output, out=None, err=None, code=EXIT_FAILED):
    """Print *message* as the error (JSON, or "ERROR: ..." on stderr) and
    return *code* (EXIT_REFUSED for a refusal)."""
    out = out or sys.stdout
    err = err or sys.stderr
    message = str(message)
    if json_output:
        print(dumps({"error": message}), file=out)
    else:
        print(f"ERROR: {message}", file=err)
    return code
