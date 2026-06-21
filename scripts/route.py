#!/usr/bin/env python3
"""Auto-router: measure the diff and its impact, then pick brief or full map.

  brief  -> impact_brief.py  (read the diff yourself; get the cross-repo signals
            — blast radius, breaking changes, test gap). Cheap.
  full   -> full map         (diff_summary + symbol_impact, with the impact-aware
            review_order to triage what to read).

Routing is **blast-radius-aware**, not just size-based. The benchmark in
evals/FINDINGS.md §6 showed the full map only pays when blast radius is large or
there are too many files to triage by eye; for big-but-low-impact diffs it is net
overhead. So:

  * small diff (<= --max-brief-files and <= --max-brief-lines)  -> brief
  * else, full when impact is worth it: max blast radius >= --min-blast, OR the
    changeset is large enough that ordering helps (>= --large-files files)
  * else (large but low-impact)                                 -> brief

Tune those knobs, or pin a path with --force {brief,full}.

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
# Below this blast radius the compact brief is cheaper than the full map for
# getting impact (evals/FINDINGS.md §6); above it, chasing callers is expensive
# enough that the map's structure pays off.
DEFAULT_MIN_BLAST = 10
DEFAULT_LARGE_FILES = 25


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
                   help="a diff this small is always the brief (default 3 files)")
    p.add_argument("--max-brief-lines", type=int, default=DEFAULT_MAX_LINES,
                   help="a diff this small is always the brief (default 60 lines)")
    p.add_argument("--min-blast", type=int, default=DEFAULT_MIN_BLAST,
                   help="go full when a changed public symbol has >= this blast "
                        "radius (default 5)")
    p.add_argument("--large-files", type=int, default=DEFAULT_LARGE_FILES,
                   help="go full when >= this many files change, regardless of "
                        "impact (ordering helps) (default 25)")
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

    # Impact pass (Layer 2) — reused for the full map if we pick it.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(l1, fh)
        l1_path = fh.name
    try:
        l2 = run_json("symbol_impact.py", "--root", cwd, "--diff-json", l1_path, "--compact")
    finally:
        os.unlink(l1_path)
    max_blast = 0
    if l2 and l2.get("ok"):
        max_blast = max((s.get("blast_radius", 0) for s in l2.get("symbols", [])
                         if s.get("exported")), default=0)

    if args.force:
        mode, reason = args.force, "forced"
    else:
        small = files <= args.max_brief_files and lines <= args.max_brief_lines
        worth_full = max_blast >= args.min_blast or files >= args.large_files
        mode = "brief" if (small or not worth_full) else "full"
        reason = ("%d file(s), %d line(s), max blast radius %d -> %s "
                  "(full needs >%d files/%d lines and (blast>=%d or files>=%d))"
                  % (files, lines, max_blast, mode, args.max_brief_files,
                     args.max_brief_lines, args.min_blast, args.large_files))

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

    # full map: reuse the L1/L2 already computed for the routing decision.
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
