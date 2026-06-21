#!/usr/bin/env python3
"""Refactor invariant checker (companion to the review distiller).

A refactor is *supposed* to change structure without changing observable
behaviour or the public API. This distills two git revisions into a compact
report so an agent can verify that quickly without diffing every file:

  * change map     - which files changed and how their symbol set shifted.
  * symbol table   - the union of symbols, with before/after presence.
  * invariants     - flags when a "pure refactor" silently altered the
                     public API (added / removed / renamed exported symbols).

It reuses the Layer 2 multi-language extractors from symbol_impact.py, so it
supports the same languages (typescript / javascript / python / go) and falls
back gracefully when a grammar is missing.

Usage:
    python scripts/refactor_check.py --range main..HEAD --cwd <repo>
    python scripts/refactor_check.py --base main --head HEAD --cwd <repo>

Output (stdout) JSON:
    {
      "ok": true,
      "base": "...", "head": "...",
      "files": [
        {"path","language","status",
         "added_symbols":[...], "removed_symbols":[...], "kept_symbols": N,
         "public_api_added":[...], "public_api_removed":[...]}
      ],
      "invariants": {
        "public_api_preserved": bool,
        "public_api_added":[...], "public_api_removed":[...]
      },
      "flags": ["public_api_changed_during_refactor", ...]
    }

Exit codes: 0 ok; 3 graceful fallback (no grammar / no supported files);
2 git error.
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import symbol_impact as si  # noqa: E402


def git(args, cwd):
    try:
        out = subprocess.run(
            ["git"] + args, cwd=cwd, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError:
        sys.stderr.write("error: git not found on PATH\n")
        sys.exit(2)
    except subprocess.CalledProcessError as e:
        sys.stderr.write("error: git %s failed: %s\n" % (" ".join(args), e.stderr.strip()))
        sys.exit(2)
    return out.stdout


def git_bytes(args, cwd):
    """Like git() but returns bytes and tolerates failure (returns b'')."""
    try:
        out = subprocess.run(["git"] + args, cwd=cwd, check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return out.stdout
    except subprocess.CalledProcessError:
        return b""


def changed_files(base, head, cwd):
    """Return [(status, old_path, new_path)] from name-status (rename-aware)."""
    out = git(["diff", "--name-status", "-M", "%s..%s" % (base, head)], cwd)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        code = parts[0]
        if code.startswith("R") and len(parts) >= 3:
            rows.append(("renamed", parts[1], parts[2]))
        elif code.startswith("A") and len(parts) >= 2:
            rows.append(("added", None, parts[1]))
        elif code.startswith("D") and len(parts) >= 2:
            rows.append(("deleted", parts[1], None))
        elif len(parts) >= 2:
            rows.append(("modified", parts[1], parts[1]))
    return rows


def symbols_at(rev, path, lang, parsers, cwd):
    """Extract {(kind,name): exported} for `path` at revision `rev`, or {}."""
    if path is None:
        return {}
    parser = parsers.get(lang)
    if parser is None:
        return {}
    src = git_bytes(["show", "%s:%s" % (rev, path)], cwd)
    if not src:
        return {}
    tree = parser.parse(src)
    extractor = si.LANGS[lang]["extract"]
    table = {}
    for kind, name, exported in extractor(src, tree.root_node):
        table[(kind, name)] = exported
    return table


def main():
    p = argparse.ArgumentParser(description="Refactor invariant checker")
    p.add_argument("--range", help="git range base..head")
    p.add_argument("--base", help="base revision (alternative to --range)")
    p.add_argument("--head", default="HEAD", help="head revision")
    p.add_argument("--cwd", default=".", help="repository working directory")
    args = p.parse_args()

    if args.range:
        if ".." not in args.range:
            sys.stderr.write("error: --range must look like base..head\n")
            sys.exit(2)
        base, head = args.range.split("..", 1)
        head = head or "HEAD"
    elif args.base:
        base, head = args.base, args.head
    else:
        sys.stderr.write("error: provide --range or --base\n")
        sys.exit(2)

    cwd = os.path.abspath(args.cwd)
    rows = changed_files(base, head, cwd)

    # Restrict to files whose extension maps to a supported language.
    relevant = []
    for status, old, new in rows:
        probe = new or old
        lang = si.lang_of(probe) if probe else None
        if lang:
            relevant.append((status, old, new, lang))

    if not relevant:
        si.fallback("no supported source files changed; nothing to check")

    langs = sorted({lang for _, _, _, lang in relevant})
    parsers = {}
    skipped = {}
    for lang in langs:
        par, err = si.load_parser(lang)
        if par is None:
            skipped[lang] = err
        else:
            parsers[lang] = par
    if not parsers:
        si.fallback("no required grammars installed (%s)" % "; ".join(skipped.values()),
                    skipped_languages=skipped)

    files_out = []
    global_api_added = []
    global_api_removed = []

    for status, old, new, lang in relevant:
        if lang not in parsers:
            continue
        before = symbols_at(base, old, lang, parsers, cwd)
        after = symbols_at(head, new, lang, parsers, cwd)

        before_keys = set(before)
        after_keys = set(after)
        added = sorted("%s %s" % (k, n) for (k, n) in (after_keys - before_keys))
        removed = sorted("%s %s" % (k, n) for (k, n) in (before_keys - after_keys))
        kept = len(before_keys & after_keys)

        api_added = sorted("%s %s" % (k, n) for (k, n) in (after_keys - before_keys)
                           if after.get((k, n)))
        api_removed = sorted("%s %s" % (k, n) for (k, n) in (before_keys - after_keys)
                             if before.get((k, n)))
        global_api_added.extend(api_added)
        global_api_removed.extend(api_removed)

        files_out.append({
            "path": new or old,
            "language": lang,
            "status": status,
            "added_symbols": added,
            "removed_symbols": removed,
            "kept_symbols": kept,
            "public_api_added": api_added,
            "public_api_removed": api_removed,
        })

    api_preserved = not (global_api_added or global_api_removed)
    flags = []
    if not api_preserved:
        flags.append("public_api_changed_during_refactor")
    if any(f["status"] == "deleted" for f in files_out):
        flags.append("files_deleted")

    result = {
        "ok": True,
        "base": base,
        "head": head,
        "files": files_out,
        "invariants": {
            "public_api_preserved": api_preserved,
            "public_api_added": sorted(set(global_api_added)),
            "public_api_removed": sorted(set(global_api_removed)),
        },
        "flags": flags,
    }
    if skipped:
        result["skipped_languages"] = skipped
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
