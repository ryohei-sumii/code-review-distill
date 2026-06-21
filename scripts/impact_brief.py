#!/usr/bin/env python3
"""One-shot impact brief — the cheap path for small/medium changes.

The full map (Layer 1 + Layer 2 JSON) is bigger than a small raw diff, so for
small changes "read the map instead of the diff" loses. But the model can read a
small diff itself for free; what it CAN'T cheaply compute are the cross-repo
signals:

  * blast radius   - who imports each changed public symbol (whole-repo scan).
  * breaking change - did a kept public symbol's signature change (base vs head
                      AST compare).
  * test gap       - code changed with no test files touched.

This orchestrates the existing tools and distills *only* those signals into a
few hundred bytes — net-positive even on a one-file change, because it adds
information the model would otherwise have to grep the repo / diff two revisions
to get, at negligible context cost. Read the small diff yourself; use this for
the impact.

Usage:
    python scripts/impact_brief.py --range main..HEAD --cwd <repo>
    python scripts/impact_brief.py --staged --cwd <repo>      # no breaking info
    python scripts/impact_brief.py --range main..HEAD --cwd <repo> --json

Exit 0 always (degrades gracefully: missing grammars just drop blast/breaking).
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
TOP_IMPACT = 5


def run_json(script, *args):
    """Run a sibling script; return parsed JSON (or None on non-JSON/empty)."""
    cp = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *map(str, args)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out = cp.stdout.strip()
    try:
        return json.loads(out)
    except ValueError:
        return None


def main():
    p = argparse.ArgumentParser(description="One-shot cross-repo impact brief")
    p.add_argument("--range", help="git range base..head")
    p.add_argument("--staged", action="store_true", help="brief the staged index")
    p.add_argument("--cwd", default=".", help="repository working directory")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = p.parse_args()

    cwd = os.path.abspath(args.cwd)

    # Layer 1 (always works: Python + git).
    l1_args = ["diff_summary.py", "--cwd", cwd, "--compact"]
    if args.staged:
        l1_args.append("--staged")
    elif args.range:
        l1_args += ["--range", args.range]
    l1 = run_json(*l1_args)
    if l1 is None:
        sys.stderr.write("error: could not read the diff\n")
        sys.exit(2)

    totals = l1.get("totals", {})
    flags = [f for f in l1.get("risk_flags", [])
             if f in ("code_changed_without_tests", "contains_deletions")]

    # Layer 2 blast radius (needs the L1 file list; may fall back).
    high_impact = []
    review_order = l1.get("review_order", [])
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(l1, fh)
        l1_path = fh.name
    try:
        l2 = run_json("symbol_impact.py", "--root", cwd, "--diff-json", l1_path, "--compact")
    finally:
        os.unlink(l1_path)
    if l2 and l2.get("ok"):
        review_order = l2.get("review_order", review_order)
        public = [s for s in l2.get("symbols", []) if s.get("exported")]
        public.sort(key=lambda s: -s.get("blast_radius", 0))
        for s in public[:TOP_IMPACT]:
            if s.get("blast_radius", 0) > 0:
                high_impact.append({"symbol": s["name"], "file": s["file"],
                                    "blast_radius": s["blast_radius"]})

    # Breaking changes need two committed revisions (refactor_check).
    breaking = []
    if args.range:
        rc = run_json("refactor_check.py", "--range", args.range, "--cwd", cwd)
        if rc and rc.get("ok"):
            inv = rc.get("invariants", {})
            breaking = sorted(set(inv.get("public_signatures_changed", []))
                              | set(inv.get("public_api_removed", [])))

    brief = {
        "files": totals.get("files", 0),
        "additions": totals.get("additions", 0),
        "deletions": totals.get("deletions", 0),
        "flags": flags,
        "breaking_changes": breaking,
        "high_impact": high_impact,
        "review_order": review_order[:TOP_IMPACT],
    }

    if args.json:
        json.dump(brief, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    # Compact human line(s).
    head = "%d file(s), +%d/-%d" % (brief["files"], brief["additions"], brief["deletions"])
    bits = [head]
    if "code_changed_without_tests" in flags:
        bits.append("no tests touched")
    if breaking:
        bits.append("BREAKING: " + ", ".join(breaking))
    print(" · ".join(bits))
    if high_impact:
        print("high impact:")
        for h in high_impact:
            print("  %s (%s) — %d caller file(s)" % (h["symbol"], h["file"], h["blast_radius"]))
    if not breaking and not high_impact:
        print("no public-API impact or breaking changes detected")


if __name__ == "__main__":
    main()
