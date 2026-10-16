#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Python side of Contents/Resources/THIRD-PARTY-NOTICES.txt.

build/build-dmg-simple.sh writes the notices file for the binaries it
downloads, then builds the menubar app with py2app and runs this script,
under the build venv's own interpreter, to append what py2app put into
Contents/Resources/MenubarApp: the interpreter and every installed
distribution with a module in the bundle, each as a notice_section-style
entry (the same format as the shell function) with its license texts
verbatim.

What is listed: a distribution counts as bundled when one of its top-level
import names (top_level.txt, else its RECORD) is in the bundle's
Contents/Resources/lib/python3.X/ — a package directory, a module in
lib-dynload/ or site-packages/, or a member of the zip there. The build
tools pip installs beside the app's dependencies (pip, py2app, macholib,
modulegraph, altgraph, setuptools) have no module there and are left out,
by name in the log. Every distribution src/requirements.txt names must be
found, or the check is wrong and the build fails.

Where the texts come from: the distribution's *.dist-info — its
License-File entries (PEP 639: under licenses/ in a new wheel, beside
METADATA in an older one), else any LICENSE*/COPYING*/NOTICE*/AUTHORS*
there. A wheel that ships none (pyobjc-core's does not) gets its text from
the project's repository at the installed version's tag, through LICENSE_URLS
and build/.cache/licenses/, as license_file in the build script does for
the bare binaries. Anything else without a text fails the build: a bundle
without its notices is not a release. The interpreter's text is the
standard library's LICENSE.txt.

The license name: License-Expression (PEP 639), else the License field when
it is a short declaration (some projects put the whole text there), else
the license classifier; a bare "BSD" is told apart as 2- or 3-clause from
the text. None of those fails the build too.

Options exist to point every input at a fake tree, which is how
tests/test_license_declarations.py runs this anywhere.
"""
import argparse
import glob
import os
import platform
import re
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from importlib.metadata import Distribution

RULE = "-" * 70

# Declared License field (lower-cased) -> SPDX identifier. "BSD" is refined
# from the text (bsd_clauses).
SPDX_BY_DECLARED = {
    "mit": "MIT", "mit license": "MIT",
    "apache-2.0": "Apache-2.0", "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0", "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0", "apache license version 2.0": "Apache-2.0",
    "bsd-3-clause": "BSD-3-Clause", "bsd 3-clause": "BSD-3-Clause", "new bsd": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause", "bsd 2-clause": "BSD-2-Clause",
    "bsd": "BSD", "bsd license": "BSD",
    "isc": "ISC", "isc license": "ISC",
    "psf": "PSF-2.0", "psf-2.0": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "mit-0": "MIT-0",
}

SPDX_BY_CLASSIFIER = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: BSD License": "BSD",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
}

# Distributions whose wheel carries no license text: <file name> and where
# the project keeps it at the tag of a version. Keyed by normalized name.
LICENSE_URLS = {
    "pyobjc-core": ("License.txt",
                    "https://raw.githubusercontent.com/ronaldoussoren/pyobjc/"
                    "v{version}/pyobjc-core/License.txt"),
}

# Top-level names in the bundle that are OnionPress's own or py2app's.
OWN_NAMES = {"onionpress", "menubar", "__boot__", "site"}

LICENSE_FILE_RE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.I)


class Problem(Exception):
    """A reason the build must stop, worded for the log."""


def normalize(name):
    """PEP 503 name: pyobjc-framework-Cocoa, PyObjC_Framework.cocoa -> same."""
    return re.sub(r"[-_.]+", "-", name).lower()


def top_name(path):
    """The import name a bundle path or zip member belongs to:
    nacl/..., _cffi_backend.cpython-314-darwin.so, Cocoa/__init__.pyc."""
    first = path.replace("\\", "/").split("/", 1)[0]
    return first.split(".", 1)[0]


def format_section(name, version, license_id, source, where, files):
    """One entry, byte for byte as the build script's notice_section prints
    it: the heading, then each (label, text) verbatim."""
    out = bytearray()
    out += (f"{RULE}\n{name} {version}\nLicense: {license_id}\n"
            f"Source: {source}\nIn this bundle: {where}\n{RULE}\n").encode()
    for label, text in files:
        out += f"\n--- {label} ({name}) ---\n\n".encode()
        out += text
        out += b"\n"
    out += b"\n"
    return bytes(out)


def bundled_names(bundle):
    """{top-level import name: [where in the bundle]} for everything under
    Contents/Resources/lib/python3.X/: the package directories and modules
    there and in lib-dynload/ and site-packages/, and the members of any
    zip there (reported as 'the.zip (name)'). Paths relative to the
    MenubarApp."""
    found = {}

    def add(name, where):
        if name and where not in found.setdefault(name, []):
            found[name].append(where)

    lib = os.path.join(bundle, "Contents", "Resources", "lib")
    for pydir in sorted(glob.glob(os.path.join(lib, "python*"))):
        if not os.path.isdir(pydir):
            continue
        for sub in ("", "lib-dynload", "site-packages"):
            d = os.path.join(pydir, sub)
            if not os.path.isdir(d):
                continue
            for entry in sorted(os.listdir(d)):
                full = os.path.join(d, entry)
                rel = os.path.relpath(full, bundle)
                if entry in ("lib-dynload", "site-packages", "__pycache__") \
                        or entry.startswith("config-"):
                    continue
                if entry.endswith(".zip") and os.path.isfile(full):
                    with zipfile.ZipFile(full) as z:
                        for member in z.namelist():
                            add(top_name(member), f"{rel} ({top_name(member)})")
                    continue
                add(top_name(entry), rel)
    return found


def top_levels(dist):
    """The distribution's top-level import names: top_level.txt, else the
    first component of each file in its RECORD."""
    text = dist.read_text("top_level.txt")
    if text and text.split():
        return set(text.split())
    names = set()
    for f in dist.files or ():
        first = f.parts[0] if f.parts else ""
        if (not first or first.startswith("..") or first == "__pycache__"
                or first.endswith((".dist-info", ".egg-info", ".data"))):
            continue
        names.add(top_name(first))
    return names


def dist_info_license_files(info_dir, metadata):
    """[(label, bytes)] from the dist-info: the License-File entries, else
    the license-looking files beside METADATA or under licenses/."""
    files = []
    declared = metadata.get_all("License-File") or []
    for rel in declared:
        rel = rel.replace("\\", "/").strip()
        for candidate in (os.path.join(info_dir, "licenses", rel), os.path.join(info_dir, rel)):
            if os.path.isfile(candidate):
                with open(candidate, "rb") as f:
                    files.append((rel, f.read()))
                break
        else:
            raise Problem(f"{os.path.basename(info_dir)} declares License-File {rel}, "
                          "which is not in it")
    if declared:
        return files
    candidates = [os.path.join(info_dir, n) for n in sorted(os.listdir(info_dir))
                  if LICENSE_FILE_RE.match(n) and os.path.isfile(os.path.join(info_dir, n))]
    for walk_dir, dirs, names in os.walk(os.path.join(info_dir, "licenses")):
        dirs.sort()
        candidates += [os.path.join(walk_dir, n) for n in sorted(names) if LICENSE_FILE_RE.match(n)]
    for path in candidates:
        with open(path, "rb") as f:
            files.append((os.path.relpath(path, info_dir), f.read()))
    return files


def fetch_license(name, version, file, url, cache_dir, log):
    """The text at url, through build/.cache/licenses/<name>-<version>-<file>
    like the build script's license_file: cache hit, else curl."""
    cached = os.path.join(cache_dir, f"{normalize(name)}-{version}-{file}")
    if os.path.isfile(cached) and os.path.getsize(cached) > 0:
        with open(cached, "rb") as f:
            return f.read()
    log(f"  Fetching {file} of {name} {version}...")
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, file)
        r = subprocess.run(["curl", "-fsSL", "--max-time", "120", "--retry", "3",
                            "-o", dest, url])
        if r.returncode != 0:
            raise Problem(f"could not fetch {url}")
        if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
            raise Problem(f"{file} of {name} {version} is empty")
        with open(dest, "rb") as f:
            text = f.read()
    os.makedirs(cache_dir, exist_ok=True)
    with open(cached, "wb") as f:
        f.write(text)
    return text


def license_files(name, version, info_dir, metadata, cache_dir, log):
    files = dist_info_license_files(info_dir, metadata)
    if files:
        return files
    fallback = LICENSE_URLS.get(normalize(name))
    if fallback:
        file, url = fallback
        return [(file, fetch_license(name, version, file, url.format(version=version),
                                     cache_dir, log))]
    raise Problem(f"{name} {version} ships no license text in {os.path.basename(info_dir)} "
                  "and build/python-notices.py has no LICENSE_URLS entry for it")


def bsd_clauses(text):
    low = text.decode("utf-8", "replace").lower()
    if "neither the name" in low:
        return "BSD-3-Clause"
    if "redistributions in binary form" in low:
        return "BSD-2-Clause"
    return "BSD"


def license_id(name, version, metadata, files):
    expression = (metadata.get("License-Expression") or "").strip()
    if expression:
        return expression
    declared = (metadata.get("License") or "").strip()
    spdx = ""
    if declared and "\n" not in declared and len(declared) <= 80:
        spdx = SPDX_BY_DECLARED.get(declared.lower(), declared)
    if not spdx:
        for classifier in metadata.get_all("Classifier") or []:
            if classifier in SPDX_BY_CLASSIFIER:
                spdx = SPDX_BY_CLASSIFIER[classifier]
                break
    if not spdx:
        raise Problem(f"{name} {version} declares no license "
                      "(no License-Expression, License or license classifier)")
    if spdx == "BSD" and files:
        spdx = bsd_clauses(files[0][1])
    return spdx


def requirement_names(path):
    names = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
            if m:
                names.add(normalize(m.group(0)))
    return names


def interpreter_section(stdlib, version, prefix):
    for candidate in (os.path.join(stdlib, "LICENSE.txt"),
                      os.path.join(os.path.dirname(os.path.dirname(stdlib)), "LICENSE.txt")):
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            with open(candidate, "rb") as f:
                text = f.read()
            break
    else:
        raise Problem(f"no LICENSE.txt in the interpreter's standard library at {stdlib}")
    short = ".".join(version.split(".")[:2])
    digits = re.sub(r"[^0-9]", "", version)
    return format_section(
        "Python", version, "PSF-2.0",
        f"https://www.python.org/downloads/release/python-{digits}/",
        f"{prefix}/Contents/Frameworks/Python.framework (the interpreter) and "
        f"{prefix}/Contents/Resources/lib/python{short} (its standard library)",
        [("LICENSE.txt", text)])


def intro(prefix):
    return f"""\
======================================================================
The menubar app: {prefix}
======================================================================

The menubar app is built with py2app. Beside OnionPress's own code it
carries the Python interpreter and the Python packages below, unmodified
releases of the versions named (PyNaCl rebuilt from its released source),
under {prefix}/Contents/Frameworks and
{prefix}/Contents/Resources/lib. Each one's license,
and its NOTICE where it has one, follows verbatim.

""".encode()


def where_line(hits, prefix):
    """'<prefix>/<dir>, <prefix>/<zip> (a, b)': the bundle paths a
    distribution's modules were found at, zip members grouped by zip."""
    plain, zips = [], {}
    for name in sorted(hits):
        for where in hits[name]:
            m = re.fullmatch(r"(.*\.zip) \((.*)\)", where)
            if m:
                zips.setdefault(m.group(1), []).append(m.group(2))
            elif where not in plain:
                plain.append(where)
    parts = [f"{prefix}/{p}" for p in plain]
    parts += [f"{prefix}/{z} ({', '.join(sorted(set(names)))})" for z, names in sorted(zips.items())]
    return ", ".join(parts)


def harvest(args, log):
    bundled = bundled_names(args.bundle)
    if not bundled:
        raise Problem(f"nothing under {args.bundle}/Contents/Resources/lib/python*/: "
                      "is that the py2app bundle?")
    info_dirs = sorted(d for d in glob.glob(os.path.join(args.site_packages, "*.dist-info"))
                       if os.path.isdir(d))
    if not info_dirs:
        raise Problem(f"no *.dist-info in {args.site_packages}")
    required = requirement_names(args.requirements)
    cache_dir = args.license_cache or os.path.join(tempfile.gettempdir(), "onionpress-licenses")

    out = bytearray(intro(args.where))
    out += interpreter_section(args.stdlib, args.python_version, args.where)
    listed, left_out, accounted, found_required = [], [], set(), set()
    entries = []
    for info_dir in info_dirs:
        dist = Distribution.at(info_dir)
        metadata = dist.metadata
        name, version = metadata["Name"], metadata["Version"]
        if not name or not version:
            raise Problem(f"{os.path.basename(info_dir)} has no Name or Version")
        hits = {t: bundled[t] for t in top_levels(dist) if t in bundled}
        if not hits:
            left_out.append(f"{name} {version}")
            continue
        accounted.update(hits)
        files = license_files(name, version, info_dir, metadata, cache_dir, log)
        spdx = license_id(name, version, metadata, files)
        entries.append((normalize(name), format_section(
            name, version, spdx, f"https://pypi.org/project/{name}/{version}/",
            where_line(hits, args.where), files)))
        listed.append(f"{name} {version} ({spdx}: {', '.join(label for label, _ in files)})")
        if normalize(name) in required:
            found_required.add(normalize(name))
    missing = sorted(required - found_required)
    if missing:
        raise Problem(f"not found in the bundle, though {os.path.basename(args.requirements)} "
                      f"names it: {', '.join(missing)} (not installed, left out by py2app, "
                      "or the bundle check here is wrong)")
    for _, section in sorted(entries):
        out += section
    stdlib_names = getattr(sys, "stdlib_module_names", frozenset())
    unaccounted = sorted(n for n in bundled
                         if n not in accounted and n not in stdlib_names
                         and not n.startswith("_sysconfigdata") and n not in OWN_NAMES)
    log(f"  Python {args.python_version} and {len(entries)} packages in the bundle:")
    for line in listed:
        log(f"    {line}")
    if left_out:
        log(f"  installed in the build venv, not in the bundle (left out): {', '.join(left_out)}")
    if unaccounted:
        log("  NOTE: bundled modules of no installed distribution, the standard library "
            f"or OnionPress: {', '.join(unaccounted)}")
    return bytes(out)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--bundle", required=True, help="the py2app MenubarApp (…/MenubarApp)")
    p.add_argument("--where", required=True,
                   help="the MenubarApp's path inside OnionPress.app, for 'In this bundle'")
    p.add_argument("--requirements", required=True, help="src/requirements.txt")
    p.add_argument("--license-cache", help="build/.cache/licenses")
    p.add_argument("--site-packages", default=sysconfig.get_path("purelib"))
    p.add_argument("--stdlib", default=sysconfig.get_path("stdlib"))
    p.add_argument("--python-version", default=platform.python_version())
    args = p.parse_args(argv)

    def log(line):
        print(line, file=sys.stderr, flush=True)

    try:
        text = harvest(args, log)
    except Problem as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(text)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
