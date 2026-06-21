#!/usr/bin/env python3
"""Quantify the routing/brief benefit across diff sizes.

For each changeset size N we build a repo where one changed file defines a public
symbol used by C callers (and contains a breaking signature change), plus N-1
other changed files. We then measure the context a reviewer faces under three
strategies, in estimated tokens (~4 chars/token):

  A  read raw diff            - cheapest, but yields NO impact analysis.
  B  raw diff + caller files  - what you'd actually read to answer "who is
                                affected / is this breaking" WITHOUT the skill.
  C  route.py output          - the skill, auto-picking brief (small) or full
                                map (large); includes blast radius + breaking.

The honest comparison for "I want to understand impact" is C vs B. A is the
floor for "I only want to skim the change".

Usage: python evals/benchmark_routing.py [--callers 10]
Outputs a table to stdout.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
SIZES = [1, 2, 5, 12, 30, 60]


def git(repo, *a):
    subprocess.run(["git", *a], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def toks(n_chars):
    return (n_chars + 3) // 4


def build(repo, n_files, callers):
    os.makedirs(os.path.join(repo, "src"))
    git(repo, "init")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    # m0 defines the public symbol; callers import it.
    with open(os.path.join(repo, "src", "m0.ts"), "w") as fh:
        fh.write("export function shared(a: number): number { return a; }\n")
    for i in range(1, n_files):
        with open(os.path.join(repo, "src", "m%d.ts" % i), "w") as fh:
            fh.write("export function fn%d(a: number): number { return a; }\n" % i)
    for c in range(callers):
        with open(os.path.join(repo, "src", "caller%d.ts" % c), "w") as fh:
            fh.write('import { shared } from "./m0"; export const v%d = shared(%d);\n' % (c, c))
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    # change all m*: m0 gets a breaking signature change; callers untouched.
    with open(os.path.join(repo, "src", "m0.ts"), "w") as fh:
        fh.write("export function shared(a: number, b: number): number { return a + b; }\n")
    for i in range(1, n_files):
        with open(os.path.join(repo, "src", "m%d.ts" % i), "w") as fh:
            fh.write("export function fn%d(a: number): number { return a + 1; }\n" % i)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "change")


def run_out(script, *a):
    cp = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *map(str, a)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return cp.stdout


def measure(n_files, callers):
    with tempfile.TemporaryDirectory() as repo:
        build(repo, n_files, callers)
        raw = subprocess.run(["git", "diff", "HEAD~1..HEAD"], cwd=repo,
                             stdout=subprocess.PIPE, text=True).stdout
        caller_bytes = 0
        for c in range(callers):
            with open(os.path.join(repo, "src", "caller%d.ts" % c)) as fh:
                caller_bytes += len(fh.read())
        route = run_out("route.py", "--range", "HEAD~1..HEAD", "--cwd", repo, "--json")
        mode = "?"
        try:
            mode = json.loads(route).get("mode", "?")
        except ValueError:
            pass
        return {
            "n_files": n_files,
            "A_raw": toks(len(raw)),
            "B_raw_plus_callers": toks(len(raw) + caller_bytes),
            "C_route": toks(len(route)),
            "mode": mode,
        }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--callers", type=int, default=10)
    p.add_argument("--sizes", help="comma-separated file counts (default %s)"
                   % ",".join(map(str, SIZES)))
    args = p.parse_args()

    sizes = [int(x) for x in args.sizes.split(",")] if args.sizes else SIZES
    rows = [measure(n, args.callers) for n in sizes]
    print("routing benefit — estimated tokens (~4 chars/token), %d callers of the "
          "changed public symbol\n" % args.callers)
    print("  %-7s | %-10s | %-22s | %-14s | %-6s" %
          ("files", "A raw", "B raw+callers", "C route", "mode"))
    print("  " + "-" * 70)
    for r in rows:
        print("  %-7d | %-10d | %-22d | %-14d | %-6s" %
              (r["n_files"], r["A_raw"], r["B_raw_plus_callers"], r["C_route"], r["mode"]))
    print("\n  C vs B (the saving when you want impact analysis):")
    for r in rows:
        save = 100 * (1 - r["C_route"] / r["B_raw_plus_callers"]) if r["B_raw_plus_callers"] else 0
        print("    %2d files: route=%d tok vs raw+callers=%d tok  -> %.0f%% less"
              % (r["n_files"], r["C_route"], r["B_raw_plus_callers"], save))


if __name__ == "__main__":
    main()
