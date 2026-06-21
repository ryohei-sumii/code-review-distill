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


FANOUT_BATCH = 10


def build_large_scale(cwd, scope, l1, l2, has_range):
    """Enable the large-scale toolkit: pattern compression (3), deterministic
    checks (2), and a fan-out review plan (1). Only called for big changesets."""
    # (3) lossless pattern compression — collapse repeated/codemod hunks.
    patterns = run_json("diff_patterns.py", "--cwd", cwd, "--json", *scope) or {}
    pat_list = patterns.get("patterns", [])
    unique = patterns.get("unique", [])

    # (1) fan-out plan: one representative per pattern + every unique file are the
    # distinct units to review (each in its own isolated context / sub-agent).
    units = [p["example_file"] for p in pat_list] + unique
    batches = [units[i:i + FANOUT_BATCH] for i in range(0, len(units), FANOUT_BATCH)]

    # (2) deterministic checks — surface only violations, independent of N.
    breaking = []
    if has_range:
        rc = run_json("refactor_check.py", "--range", scope[1], "--cwd", cwd)
        if rc and rc.get("ok"):
            inv = rc.get("invariants", {})
            breaking = sorted(set(inv.get("public_signatures_changed", []))
                              | set(inv.get("public_api_removed", [])))

    # the signal-bearing public changes (replaces the verbose per-file list).
    high_impact = []
    if l2 and l2.get("ok"):
        pub = [s for s in l2.get("symbols", [])
               if s.get("exported") and s.get("blast_radius", 0) > 0]
        pub.sort(key=lambda s: -s["blast_radius"])
        high_impact = [{"symbol": s["name"], "file": s["file"],
                        "blast_radius": s["blast_radius"]} for s in pub[:20]]

    return {
        "compression": patterns.get("compression", {}),
        "patterns": [{"count": p["count"], "example_file": p["example_file"],
                      "files": p["files"], "example_hunk": p["example_hunk"]}
                     for p in pat_list],
        "unique": unique,
        "high_impact": high_impact,
        "review_batches": batches,
        "checks": {
            "risk_flags": l1.get("risk_flags", []),
            "impact_flags": (l2 or {}).get("impact_flags", []),
            "breaking_changes": breaking,
        },
    }


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
                        "radius (default %d)" % DEFAULT_MIN_BLAST)
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

    # Layer 2 (whole-repo scan) is computed lazily and at most once: a small diff
    # short-circuits to the brief without needing it, so we never pay for it there.
    l2_box = {}

    def get_l2():
        if "v" not in l2_box:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                json.dump(l1, fh)
                l1_path = fh.name
            try:
                l2_box["v"] = run_json("symbol_impact.py", "--root", cwd,
                                       "--diff-json", l1_path, "--compact")
            finally:
                os.unlink(l1_path)
        return l2_box["v"]

    small = files <= args.max_brief_files and lines <= args.max_brief_lines
    if args.force:
        mode, reason = args.force, "forced"
    elif small:
        mode = "brief"
        reason = "%d file(s), %d line(s) <= brief thresholds (%d/%d)" % (
            files, lines, args.max_brief_files, args.max_brief_lines)
    elif files >= args.large_files:
        mode = "full"
        reason = "%d file(s) >= %d -> full (ordering helps)" % (files, args.large_files)
    else:
        l2 = get_l2()
        max_blast = 0
        if l2 and l2.get("ok"):
            max_blast = max((s.get("blast_radius", 0) for s in l2.get("symbols", [])
                             if s.get("exported")), default=0)
        mode = "full" if max_blast >= args.min_blast else "brief"
        reason = ("%d file(s), %d line(s), max blast radius %d -> %s "
                  "(full when blast >= %d)" % (files, lines, max_blast, mode, args.min_blast))

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

    # full map: reuse the L1/L2 (L2 is memoized — computed once at most).
    l2 = get_l2()
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
    # Large scale only: turn on pattern compression (3) + deterministic checks
    # (2) + a fan-out review plan (1). The compressed view (patterns + unique)
    # covers every changed file losslessly, so the verbose per-file `prioritized`
    # (reasons x N — the bloat) is dropped here; `review_order` (paths only) and
    # the signal-bearing `high_impact` are kept.
    if files >= args.large_files:
        out["large_scale"] = build_large_scale(cwd, scope, l1, l2, bool(args.range))
        # patterns + unique enumerate every changed file losslessly, so the two
        # O(N) verbose fields are redundant here and dropped to keep it bounded.
        out.pop("prioritized", None)
        out.pop("review_order", None)

    if args.json:
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    print("mode: full map (%s)" % reason)
    if out["risk_flags"]:
        print("risk: " + ", ".join(out["risk_flags"]))
    if out["impact_flags"]:
        print("impact: " + ", ".join(out["impact_flags"]))
    ls = out.get("large_scale")
    if ls:
        c = ls["compression"]
        print("large-scale toolkit:")
        print("  patterns: %d changed files -> %d distinct review units (%d collapsed)"
              % (c.get("changed_files", 0), c.get("distinct_units", 0),
                 c.get("collapsed_files", 0)))
        for i, pat in enumerate(ls["patterns"][:5], 1):
            print("    pattern %d: %d files like %s" % (i, pat["count"], pat["example_file"]))
        if ls["checks"]["breaking_changes"]:
            print("  BREAKING: " + ", ".join(ls["checks"]["breaking_changes"]))
        print("  fan-out: review %d batch(es) of <=%d units in isolated context"
              % (len(ls["review_batches"]), FANOUT_BATCH))
        if ls["high_impact"]:
            print("  high impact:")
            for h in ls["high_impact"][:10]:
                print("    %s (%s) — %d caller file(s)"
                      % (h["symbol"], h["file"], h["blast_radius"]))
        return
    print("review in this order (highest priority first):")
    for r in (out["prioritized"][:10] or [{"path": p} for p in review_order[:10]]):
        reasons = ("  — " + "; ".join(r["reasons"])) if r.get("reasons") else ""
        print("  %s%s" % (r["path"], reasons))
    print("\n(fetch hunks for the top files via diff_summary; this is the triage order)")


if __name__ == "__main__":
    main()
