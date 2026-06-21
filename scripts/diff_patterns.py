#!/usr/bin/env python3
"""Pattern compression for large diffs (lossless representation).

Big PRs are often repetitive — a codemod, a generated change, a sweeping rename
applies the *same* edit to many files. Listing every such hunk is O(N) waste.
This groups changed files whose hunks are structurally identical (type-2 clone
normalization: identifiers and numbers masked, operators/punctuation kept) and
represents each group once: a pattern + its member files + one real example.

Nothing is dropped — every file appears, either as a pattern member or as a
unique change — so the compression is lossless at the representation level: you
see one example per repeated pattern plus the full member list, and can still
fetch any specific file. For a 200-file codemod this turns O(200) hunks into one
pattern + a handful of exceptions.

Usage:
    python scripts/diff_patterns.py --range main..HEAD --cwd <repo>
    python scripts/diff_patterns.py --file some.diff
    python scripts/diff_patterns.py --range main..HEAD --cwd <repo> --json

Output: { files, patterns[], unique[], compression{} }.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUM = re.compile(r"\d+")


def run_git_diff(args):
    cmd = ["git", "diff", "--no-color", "-M"]
    if args.staged:
        cmd.append("--cached")
    if args.range:
        cmd.append(args.range)
    try:
        out = subprocess.run(cmd, cwd=args.cwd, check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        sys.stderr.write("error: git not found\n")
        sys.exit(2)
    except subprocess.CalledProcessError as e:
        sys.stderr.write("error: git diff failed: %s\n" % e.stderr.strip())
        sys.exit(2)
    return out.stdout


def split_per_file(diff_text):
    """{path: changed-line-block} from a unified diff (the +/- body only)."""
    files = {}
    cur = None
    buf = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if cur is not None:
                files[cur] = "\n".join(buf)
            buf = []
            m = re.match(r"diff --git a/.+ b/(.+)$", line)
            cur = m.group(1) if m else None
        elif cur is not None and line[:1] in "+-" and not line.startswith(("+++", "---")):
            buf.append(line)
    if cur is not None:
        files[cur] = "\n".join(buf)
    return files


def normalize(block):
    """Type-2 clone normalization: mask identifiers and numbers, keep shape."""
    out = []
    for line in block.splitlines():
        if not line:
            continue
        sign, body = line[0], line[1:]
        body = _IDENT.sub("W", body)
        body = _NUM.sub("N", body)
        out.append(sign + re.sub(r"\s+", " ", body).strip())
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description="Lossless pattern compression for large diffs")
    p.add_argument("--range", help="git range base..head")
    p.add_argument("--staged", action="store_true")
    p.add_argument("--file", help="read a unified diff from this file")
    p.add_argument("--cwd", default=".")
    p.add_argument("--json", action="store_true")
    p.add_argument("--min-count", type=int, default=2,
                   help="a pattern needs at least this many files (default 2)")
    args = p.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8", errors="replace") as fh:
            diff = fh.read()
    else:
        diff = run_git_diff(args)

    per_file = split_per_file(diff)

    groups = {}  # norm-hash -> {files, example_path, example_block}
    no_content = []  # pure renames / mode changes — no +/- body, but still changed
    for path, block in per_file.items():
        if not block.strip():
            no_content.append(path)  # keep visible so coverage stays complete
            continue
        key = hashlib.sha1(normalize(block).encode("utf-8")).hexdigest()
        g = groups.setdefault(key, {"files": [], "example_path": path, "example_block": block})
        g["files"].append(path)

    patterns = []
    unique = []
    for g in groups.values():
        if len(g["files"]) >= args.min_count:
            patterns.append({
                "count": len(g["files"]),
                "files": sorted(g["files"]),
                "example_file": g["example_path"],
                "example_hunk": g["example_block"],
            })
        else:
            unique.extend(g["files"])
    unique.extend(no_content)
    patterns.sort(key=lambda x: -x["count"])
    unique.sort()

    distinct_units = len(patterns) + len(unique)
    result = {
        "files": len(per_file),
        "patterns": patterns,
        "unique": unique,
        "compression": {
            "changed_files": len(per_file),
            "distinct_units": distinct_units,
            "collapsed_files": sum(p["count"] for p in patterns),
        },
    }

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    c = result["compression"]
    print("%d changed files -> %d distinct units (%d collapsed into %d patterns)"
          % (c["changed_files"], c["distinct_units"], c["collapsed_files"], len(patterns)))
    for i, pat in enumerate(patterns, 1):
        print("\npattern %d: %d files share this change (e.g. %s)"
              % (i, pat["count"], pat["example_file"]))
        for line in pat["example_hunk"].splitlines()[:6]:
            print("    %s" % line)
    if unique:
        print("\n%d unique change(s) to review individually:" % len(unique))
        for u in unique[:20]:
            print("    %s" % u)


if __name__ == "__main__":
    main()
