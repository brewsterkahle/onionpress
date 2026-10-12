"""
Mac-side onionname utilities used by the MenubarApp.

No network, no DB — just the shared rules (validation, charset, length) and
local random-suggestion generation from the bundled word lists. The server
(OnionHome) is the ultimate authority on uniqueness and reservation; this
module only needs to pick a plausible default and reject obvious garbage
before we even attempt registration.

Kept intentionally stdlib-only so it works inside the py2app bundle.
"""

import os
import random
import re
import sys

MIN_NAME_LEN = 5
MAX_NAME_LEN = 40

_NAME_ALLOWED_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]$")
_ALL_NUMERIC_RE = re.compile(r"^[0-9]+$")


# ---------------------------------------------------------------------------
# Validation — mirror of onionnames.validate_name on the server side
# ---------------------------------------------------------------------------

def validate_name(name):
    """Return (ok, reason). reason is None on success.

    reason ∈ {'empty', 'too_short', 'too_long', 'invalid_chars', 'all_numeric'}.
    """
    if not isinstance(name, str) or not name:
        return False, "empty"
    if len(name) < MIN_NAME_LEN:
        return False, "too_short"
    if len(name) > MAX_NAME_LEN:
        return False, "too_long"
    if not _NAME_ALLOWED_RE.fullmatch(name):
        return False, "invalid_chars"
    if _ALL_NUMERIC_RE.fullmatch(name):
        return False, "all_numeric"
    return True, None


# ---------------------------------------------------------------------------
# Wordlist discovery
# ---------------------------------------------------------------------------

SUPPORTED_LANGUAGES = {
    "en_US": "en",
    "fr_FR": "fr",
    "es_ES": "es",
    "de_DE": "de",
    "nl_NL": "nl",
    "pt_BR": "pt",
    "ja": "ja",
    "zh_CN": "zh",
    "ar": "ar",
}


def _candidate_wordlist_dirs():
    """Yield paths where wordlists/ might live, in priority order.

    Covers the py2app bundle layout (RESOURCEPATH), a normal Python run from
    the source tree (walking up from this file to app/Resources/docker/tor),
    and an explicit override via ONIONNAME_WORDLISTS_DIR.
    """
    override = os.environ.get("ONIONNAME_WORDLISTS_DIR")
    if override:
        yield override

    resource_path = os.environ.get("RESOURCEPATH")
    if resource_path:
        yield os.path.join(resource_path, "wordlists")

    this = os.path.dirname(os.path.realpath(__file__))
    # Dev checkout: src/onionpress/ → …/app/Resources/docker/tor/wordlists
    for up in (2, 3, 4):
        project_root = os.path.realpath(
            os.path.join(this, *([".."] * up))
        )
        candidate = os.path.join(
            project_root, "app", "Resources", "docker", "tor", "wordlists"
        )
        if candidate:
            yield candidate

    # Sibling to the calling script (py2app often flattens resources here).
    if getattr(sys, "frozen", False):
        yield os.path.join(os.path.dirname(sys.executable), "..",
                           "Resources", "wordlists")


def find_wordlist_dir():
    for candidate in _candidate_wordlist_dirs():
        if candidate and os.path.isdir(candidate):
            # Sanity: must contain en.py
            if os.path.isfile(os.path.join(candidate, "en.py")):
                return candidate
    return None


# ---------------------------------------------------------------------------
# Wordlist loading — we read en.py etc. as source text, not as Python
# packages, so we don't depend on sys.path being set up in any particular way.
# ---------------------------------------------------------------------------

_WORDLIST_CACHE = {}


def _read_python_list(source, name):
    """Return the string literal list assigned to `name` in the given source.

    Looks for `NAME = [...]` and evaluates with a stripped-down namespace.
    Accepts both triple-single and triple-double quoted lists.
    """
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*(\[.*?\])",
                      source, re.DOTALL | re.MULTILINE)
    if not match:
        return []
    try:
        return list(eval(match.group(1), {"__builtins__": {}}, {}))
    except Exception:
        return []


def load_wordlist(lang):
    """Load (ADJECTIVES, NOUNS) for a language code.

    `lang` may be a locale ('en_US') or a bare code ('en'). Falls back to
    English on any failure. Returns (None, None) only if English is missing
    too.
    """
    code = SUPPORTED_LANGUAGES.get(lang, lang if isinstance(lang, str) else "en")
    if code not in SUPPORTED_LANGUAGES.values():
        code = "en"

    if code in _WORDLIST_CACHE:
        return _WORDLIST_CACHE[code]

    wordlists_dir = find_wordlist_dir()
    if not wordlists_dir:
        return None, None

    def _load(c):
        path = os.path.join(wordlists_dir, f"{c}.py")
        if not os.path.isfile(path):
            return None, None
        try:
            with open(path, encoding="utf-8") as f:
                source = f.read()
        except OSError:
            return None, None
        adjectives = _read_python_list(source, "ADJECTIVES")
        nouns = _read_python_list(source, "NOUNS")
        if not adjectives or not nouns:
            return None, None
        return adjectives, nouns

    adjectives, nouns = _load(code)
    if adjectives is None and code != "en":
        adjectives, nouns = _load("en")
        code = "en"
    _WORDLIST_CACHE[code] = (adjectives, nouns)
    return adjectives, nouns


# ---------------------------------------------------------------------------
# Local suggestions
# ---------------------------------------------------------------------------

def suggest_name_local(lang="en", max_attempts=20):
    """Pick a random adjective-noun for the given language.

    The server is the authority on availability — we just produce a name
    that passes validation. Returns None if the wordlists are unavailable.
    """
    adjectives, nouns = load_wordlist(lang)
    if not adjectives or not nouns:
        return None
    rnd = random.SystemRandom()
    for _ in range(max_attempts):
        candidate = f"{rnd.choice(adjectives)}-{rnd.choice(nouns)}"
        ok, _ = validate_name(candidate)
        if ok:
            return candidate
    return None


def suggest_names_local(lang="en", count=5):
    """Return up to `count` distinct local suggestions (best-effort)."""
    seen = set()
    out = []
    for _ in range(count * 4):
        name = suggest_name_local(lang)
        if not name:
            break
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
        if len(out) >= count:
            break
    return out
