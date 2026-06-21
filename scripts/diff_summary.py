#!/usr/bin/env python3
"""Layer 1 (language-agnostic) code-review distiller.

Parses a `git diff` into a compact, structured JSON summary so a reviewing
agent can prioritise *where* to look without reading the full patch into its
context window.

Output (stdout) is a single JSON object:

    {
      "source": "range" | "staged" | "file",
      "range": "main..HEAD",                # when source == range
      "totals": {"files", "additions", "deletions", "hunks"},
      "files": [ { ...per-file... } ],
      "risk_flags": [ "code_changed_without_tests", ... ],   # repo-wide
      "review_order": [ "path", ... ]       # highest risk first
    }

Per-file object:

    {
      "path", "old_path"?, "status",        # added|modified|deleted|renamed
      "language", "additions", "deletions",
      "is_test", "is_generated", "is_binary",
      "hunks": [ {"old_start","old_lines","new_start","new_lines",
                  "header","added","deleted"} ],
      "risk_flags": [ "large_hunk", "file_deleted", ... ]
    }

Dependencies: Python 3 + git only. Works for any language.
"""

import argparse
import json
import os
import re
import subprocess
import sys

# --- Heuristics ------------------------------------------------------------

# A hunk touching this many lines (added + deleted) is flagged large_hunk.
LARGE_HUNK_LINES = 80
# A file changing this many lines total is flagged large_file_change.
LARGE_FILE_LINES = 300

TEST_PATTERNS = [
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"(^|/)spec/"),
    re.compile(r"\.(test|spec)\.[^/]+$"),
    re.compile(r"_test\.[^/]+$"),
    re.compile(r"(^|/)test_[^/]+$"),
    re.compile(r"\.feature$"),
]

GENERATED_PATTERNS = [
    re.compile(r"\.min\.(js|css)$"),
    re.compile(r"(^|/)(dist|build|out|vendor|node_modules)/"),
    re.compile(r"package-lock\.json$"),
    re.compile(r"yarn\.lock$"),
    re.compile(r"pnpm-lock\.yaml$"),
    re.compile(r"Cargo\.lock$"),
    re.compile(r"poetry\.lock$"),
    re.compile(r"go\.sum$"),
    re.compile(r"\.pb\.go$"),
    re.compile(r"_pb2\.py$"),
    re.compile(r"\.snap$"),
    re.compile(r"(^|/)generated/"),
    re.compile(r"\.generated\.[^/]+$"),
]

# Extension -> language label. Kept small and obvious; "unknown" otherwise.
EXT_LANG = {
    "ts": "typescript", "tsx": "typescript",
    "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
    "py": "python",
    "go": "go",
    "rs": "rust",
    "java": "java",
    "kt": "kotlin",
    "rb": "ruby",
    "php": "php",
    "c": "c", "h": "c",
    "cc": "cpp", "cpp": "cpp", "cxx": "cpp", "hpp": "cpp",
    "cs": "csharp",
    "swift": "swift",
    "scala": "scala",
    "sh": "shell", "bash": "shell",
    "json": "json", "yaml": "yaml", "yml": "yaml",
    "md": "markdown",
    "sql": "sql",
}

CODE_LANGS = {
    "typescript", "javascript", "python", "go", "rust", "java", "kotlin",
    "ruby", "php", "c", "cpp", "csharp", "swift", "scala", "shell",
}


def matches_any(path, patterns):
    return any(p.search(path) for p in patterns)


def guess_language(path):
    _, ext = os.path.splitext(path)
    return EXT_LANG.get(ext.lstrip(".").lower(), "unknown")


# --- Diff parsing ----------------------------------------------------------

HUNK_RE = re.compile(
    r"^@@ -(?P<os>\d+)(?:,(?P<ol>\d+))? \+(?P<ns>\d+)(?:,(?P<nl>\d+))? @@(?P<ctx>.*)$"
)


def parse_diff(text):
    """Parse unified `git diff` text into a list of per-file dicts."""
    files = []
    current = None
    in_hunk = False

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("diff --git "):
            if current is not None:
                files.append(current)
            current = _new_file_record()
            in_hunk = False
            # paths from the "diff --git a/x b/y" header (fallback)
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if m:
                current["_a"] = m.group(1)
                current["_b"] = m.group(2)
            i += 1
            continue

        if current is None:
            i += 1
            continue

        if line.startswith("old mode") or line.startswith("new mode"):
            i += 1
            continue
        if line.startswith("new file mode"):
            current["status"] = "added"
            i += 1
            continue
        if line.startswith("deleted file mode"):
            current["status"] = "deleted"
            i += 1
            continue
        if line.startswith("rename from "):
            current["old_path"] = line[len("rename from "):]
            current["status"] = "renamed"
            i += 1
            continue
        if line.startswith("rename to "):
            current["path"] = line[len("rename to "):]
            current["status"] = "renamed"
            i += 1
            continue
        if line.startswith("copy from ") or line.startswith("copy to "):
            i += 1
            continue
        if line.startswith("similarity index") or line.startswith("dissimilarity index"):
            i += 1
            continue
        if line.startswith("index "):
            i += 1
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            current["is_binary"] = True
            i += 1
            continue
        if line.startswith("--- "):
            path = line[4:]
            if path != "/dev/null":
                current["old_path"] = _strip_prefix(path)
            i += 1
            continue
        if line.startswith("+++ "):
            path = line[4:]
            if path != "/dev/null":
                current["path"] = _strip_prefix(path)
            i += 1
            continue

        m = HUNK_RE.match(line)
        if m:
            hunk = {
                "old_start": int(m.group("os")),
                "old_lines": int(m.group("ol")) if m.group("ol") else 1,
                "new_start": int(m.group("ns")),
                "new_lines": int(m.group("nl")) if m.group("nl") else 1,
                "header": m.group("ctx").strip(),
                "added": 0,
                "deleted": 0,
            }
            current["hunks"].append(hunk)
            in_hunk = True
            i += 1
            continue

        if in_hunk and current["hunks"]:
            hunk = current["hunks"][-1]
            if line.startswith("+"):
                hunk["added"] += 1
                current["additions"] += 1
            elif line.startswith("-"):
                hunk["deleted"] += 1
                current["deletions"] += 1
            # context / "\ No newline" lines are ignored
        i += 1

    if current is not None:
        files.append(current)

    return [_finalize_file(f) for f in files]


def _new_file_record():
    return {
        "path": None,
        "old_path": None,
        "status": "modified",
        "additions": 0,
        "deletions": 0,
        "is_binary": False,
        "hunks": [],
        "_a": None,
        "_b": None,
    }


def _strip_prefix(path):
    # git uses a/ and b/ prefixes by default
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _finalize_file(f):
    path = f["path"] or f["_b"] or f["old_path"] or f["_a"]
    old_path = f["old_path"]
    f.pop("_a", None)
    f.pop("_b", None)
    f["path"] = path

    # Don't report a redundant old_path unless it actually differs.
    if f["status"] != "renamed" and old_path == path:
        f["old_path"] = None

    f["language"] = guess_language(path or "")
    f["is_test"] = matches_any(path or "", TEST_PATTERNS)
    f["is_generated"] = matches_any(path or "", GENERATED_PATTERNS)

    f["risk_flags"] = compute_file_risk(f)
    return f


# --- Risk scoring ----------------------------------------------------------

def compute_file_risk(f):
    flags = []
    if f["status"] == "deleted":
        flags.append("file_deleted")
    if f["status"] == "renamed":
        flags.append("file_renamed")
    if f["is_binary"]:
        flags.append("binary_change")

    total = f["additions"] + f["deletions"]
    if total >= LARGE_FILE_LINES:
        flags.append("large_file_change")
    if any((h["added"] + h["deleted"]) >= LARGE_HUNK_LINES for h in f["hunks"]):
        flags.append("large_hunk")

    if f["is_generated"] and not f["is_test"]:
        flags.append("generated_file")
    return flags


def file_risk_score(f):
    """Heuristic numeric score for ordering. Higher = look first."""
    score = 0
    weights = {
        "file_deleted": 30,
        "large_file_change": 25,
        "large_hunk": 15,
        "file_renamed": 10,
        "binary_change": 5,
    }
    for flag in f["risk_flags"]:
        score += weights.get(flag, 0)
    score += f["additions"] + f["deletions"]
    # Generated/lock files are noise -> push down.
    if f["is_generated"]:
        score -= 100
    # Test files are usually lower priority to scrutinise for bugs.
    if f["is_test"]:
        score -= 20
    return score


def compute_repo_risk(files):
    flags = []
    code_files = [
        f for f in files
        if f["language"] in CODE_LANGS and not f["is_generated"] and not f["is_test"]
    ]
    test_files = [f for f in files if f["is_test"]]
    if code_files and not test_files:
        flags.append("code_changed_without_tests")
    if any(f["status"] == "deleted" for f in files):
        flags.append("contains_deletions")
    if len(files) >= 20:
        flags.append("large_changeset")
    return flags


# --- Diff acquisition ------------------------------------------------------

def run_git_diff(args):
    cmd = ["git", "diff", "--no-color", "-M"]
    if args.staged:
        cmd.append("--cached")
    if args.range:
        cmd.append(args.range)
    try:
        out = subprocess.run(
            cmd, cwd=args.cwd, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError:
        sys.stderr.write("error: git not found on PATH\n")
        sys.exit(2)
    except subprocess.CalledProcessError as e:
        sys.stderr.write("error: git diff failed: %s\n" % e.stderr.strip())
        sys.exit(2)
    return out.stdout


def main():
    p = argparse.ArgumentParser(description="Layer 1 language-agnostic diff distiller")
    p.add_argument("--range", help="git revision range, e.g. main..HEAD")
    p.add_argument("--staged", action="store_true", help="diff the staged index")
    p.add_argument("--file", help="read a unified diff from this file instead of git")
    p.add_argument("--cwd", default=".", help="repository working directory")
    args = p.parse_args()

    if args.file:
        with open(args.file, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        source = "file"
    else:
        text = run_git_diff(args)
        source = "staged" if args.staged else "range"

    files = parse_diff(text)

    ordered = sorted(files, key=file_risk_score, reverse=True)

    result = {
        "source": source,
        "totals": {
            "files": len(files),
            "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files),
            "hunks": sum(len(f["hunks"]) for f in files),
        },
        "files": files,
        "risk_flags": compute_repo_risk(files),
        "review_order": [f["path"] for f in ordered],
    }
    if source == "range" and args.range:
        result["range"] = args.range

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
