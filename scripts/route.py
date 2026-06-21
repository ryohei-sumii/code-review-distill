#!/usr/bin/env python3
"""Auto-router: measure the diff, then pick the brief or the full map.

  small change  -> impact_brief.py  (read the diff yourself; get the cross-repo
                   signals — blast radius, breaking changes, test gap).
  large change  -> full map         (diff_summary + symbol_impact, with the
                   impact-aware review_order to triage what to read).

The threshold is a heuristic from evals/FINDINGS.md: the full map only pays off
once the diff is too big to just read directly. Tune with --max-brief-files /
--max-brief-lines, or pin a path with --force {brief,full}.

Usage:
    python scripts/route.py --range main..HEAD --cwd <repo>
    python scripts/route.py --staged --cwd <repo> --json
    python scripts/route.py --range main..HEAD --cwd <repo> --force full

This is the recommended single entry point — call it first; it decides.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MAX_FILES = 3
DEFAULT_MAX_LINES = 60


def run(script, *args):
    cp = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *map(str, args)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return cp.returncode, cp.stdout


def run_json(script, *args):
    _, out = run(script, *args)
    try:
        return json.loads(out.strip())
    except ValueError:
        return None


def main():
    p = argparse.ArgumentParser(description="Auto-route a review to brief or full map")
    p.add_argument("--range", help="git range base..head")
    p.add_argument("--staged", action="store_true", help="route the staged index")
    p.add_argument("--cwd", default=".", help="repository working directory")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    p.add_argument("--max-brief-files", type=int, default=DEFAULT_MAX_FILES,
                   help="use the brief when <= this many files change (default 3)")
    p.add_argument("--max-brief-lines", type=int, default=DEFAULT_MAX_LINES,
                   help="use the brief when <= this many lines change (default 60)")
    p.add_argument("--force", choices=["brief", "full"], help="skip routing")
    args = p.parse_args()

    cwd = os.path.abspath(args.cwd)
    scope = ["--staged"] if args.staged else (["--range", args.range] if args.range else [])

    # Cheap measurement pass (Layer 1).
    l1 = run_json("diff_summary.py", "--cwd", cwd, "--compact", *scope)
    if l1 is None:
        sys.stderr.write("error: could not read the diff\n")
        sys.exit(2)
    totals = l1.get("totals", {})
    files = totals.get("files", 0)
    lines = totals.get("additions", 0) + totals.get("deletions", 0)

    small = files <= args.max_brief_files and lines <= args.max_brief_lines
    mode = args.force or ("brief" if small else "full")
    reason = ("forced" if args.force else
              "%d file(s), %d changed line(s); brief when <=%d files and <=%d lines"
              % (files, lines, args.max_brief_files, args.max_brief_lines))

    if mode == "brief":
        payload = run_json("impact_brief.py", "--cwd", cwd, "--json", *scope)
        if args.json:
            json.dump({"mode": "brief", "reason": reason, "brief": payload},
                      sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            print("mode: brief (%s)" % reason)
            _, text = run("impact_brief.py", "--cwd", cwd, *scope)
            sys.stdout.write(text)
        return

    # full map: reuse the L1 we already computed; add Layer 2 impact.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(l1, fh)
        l1_path = fh.name
    try:
        l2 = run_json("symbol_impact.py", "--root", cwd, "--diff-json", l1_path, "--compact")
    finally:
        os.unlink(l1_path)

    review_order = (l2 or {}).get("review_order") or l1.get("review_order", [])
    out = {
        "mode": "full",
        "reason": reason,
        "totals": totals,
        "risk_flags": l1.get("risk_flags", []),
        "review_order": review_order,
        "impact_flags": (l2 or {}).get("impact_flags", []),
        "prioritized": (l2 or {}).get("prioritized", []),
    }
    if args.json:
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    print("mode: full map (%s)" % reason)
    if out["risk_flags"]:
        print("risk: " + ", ".join(out["risk_flags"]))
    if out["impact_flags"]:
        print("impact: " + ", ".join(out["impact_flags"]))
    print("review in this order (highest priority first):")
    for r in (out["prioritized"][:10] or [{"path": p} for p in review_order[:10]]):
        reasons = ("  — " + "; ".join(r["reasons"])) if r.get("reasons") else ""
        print("  %s%s" % (r["path"], reasons))
    print("\n(fetch hunks for the top files via diff_summary; this is the triage order)")


if __name__ == "__main__":
    main()
