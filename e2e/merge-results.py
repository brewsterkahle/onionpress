#!/usr/bin/env python3
"""Merge several runs' results.json into one report (e.g. a Mac run and a
Linux run of the same build): `e2e/merge-results.py A/results.json
B/results.json [-o merged.md]`."""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness.report import RunReport, merge  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("results", nargs="+")
    ap.add_argument("-o", "--output", default="")
    args = ap.parse_args()
    reports = []
    for path in args.results:
        with open(path, encoding="utf-8") as f:
            reports.append(RunReport.from_json(f.read()))
    text = merge(reports).render_markdown()
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)


if __name__ == "__main__":
    main()
