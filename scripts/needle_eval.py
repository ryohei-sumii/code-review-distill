#!/usr/bin/env python3
"""Needle-in-review eval: does distilling a diff help against "lost in the
middle"?

"Lost in the middle" says recall of a fact drops the further it sits from the
*edges* of the context, and the longer the context is. So the honest, runnable
measurement is the **geometry** the reviewer actually faces for a planted defect
(the "needle") under two conditions:

  A. raw     - read the whole concatenated diff up front, then reason.
  B. distill - read the compact map, then fetch & judge the flagged hunk last.

For each scenario we plant exactly one defect in one file (at a controlled
position: start / middle / end of the changeset) and measure, per condition:

  * total_context_tokens   - how much context the reviewer holds.
  * needle_from_end_tokens  - tokens between the needle and the context end
                              (0 = needle is the very last thing read).
  * needle_rel_pos          - 0.0 = context start, 1.0 = context end.

These are objective and computed here. They are the *drivers* of recall, not
recall itself — true detection rate needs a model judge, which this harness
supports via --emit-cases (write judge inputs) and --predictions (score them),
exactly like run_loop.py. No model behaviour is faked.

Honest caveat surfaced by this tool: a *subtle logic* needle raises no Layer 1
mechanical risk flag, so the map won't necessarily rank its file first. The
distiller's geometry win comes from (1) shrinking total context and (2) the
fetch-and-judge-immediately discipline keeping the needle at the end — not from
magically spotting logic bugs. Use --kind to compare a structural needle
(public-API / large hunk, which the map *does* surface) against a quiet one.

Usage:
    python scripts/needle_eval.py --files 12 --repeats 2
    python scripts/needle_eval.py --json
    python scripts/needle_eval.py --emit-cases /tmp/cases   # for a real judge
    python scripts/needle_eval.py --predictions preds.json  # score real runs
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
POSITIONS = ("start", "middle", "end")
NEEDLE_MARK = "NEEDLE"


def est_tokens(text):
    """Rough token estimate (~4 chars/token); good enough for geometry."""
    return (len(text) + 3) // 4


def git(repo, *args, capture=False):
    kw = dict(cwd=repo, check=True)
    if capture:
        kw.update(stdout=subprocess.PIPE, text=True)
    else:
        kw.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.run(["git", *args], **kw)


def run_script(name, *args):
    cp = subprocess.run([sys.executable, os.path.join(SCRIPTS, name), *map(str, args)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return cp.returncode, cp.stdout


# --- defect templates ------------------------------------------------------

def benign_module(i):
    return (
        "export function fn%d(a: number, b: number): number {\n"
        "  let acc = 0;\n"
        "  for (let k = 0; k <= b; k++) { acc += a + k; }\n"
        "  return acc;\n"
        "}\n" % i
    )


def benign_change(i):
    # a plausible, correct edit
    return (
        "export function fn%d(a: number, b: number): number {\n"
        "  // refactor: use reduce\n"
        "  const xs = Array.from({length: b + 1}, (_, k) => a + k);\n"
        "  return xs.reduce((p, q) => p + q, 0);\n"
        "}\n" % i
    )


def needle_change(i, kind):
    """A buggy edit. `kind` controls whether the map can surface it."""
    if kind == "structural":
        # changes the public signature (Layer 2 sees public_api change) AND a bug
        return (
            "export function fn%d(a: number, b: number, c: number): number {\n"
            "  // %s: off-by-one — should be k <= b\n"
            "  let acc = 0;\n"
            "  for (let k = 0; k < b; k++) { acc += a + k + c; }\n"
            "  return acc;\n"
            "}\n" % (i, NEEDLE_MARK)
        )
    # "quiet": same shape as a benign edit, subtle off-by-one, no structural signal
    return (
        "export function fn%d(a: number, b: number): number {\n"
        "  // %s: off-by-one — length should be b + 1\n"
        "  const xs = Array.from({length: b}, (_, k) => a + k);\n"
        "  return xs.reduce((p, q) => p + q, 0);\n"
        "}\n" % (i, NEEDLE_MARK)
    )


# --- per-file hunk extraction ---------------------------------------------

def split_per_file(diff_text):
    """Return {path: hunk_text} from a unified diff."""
    files = {}
    cur_path = None
    buf = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if cur_path is not None:
                files[cur_path] = "".join(buf)
            buf = [line]
            m = re.match(r"diff --git a/.+ b/(.+)\n", line)
            cur_path = m.group(1) if m else None
        else:
            buf.append(line)
    if cur_path is not None:
        files[cur_path] = "".join(buf)
    return files


# --- one scenario ----------------------------------------------------------

def build_scenario(n_files, position, kind, workdir):
    repo = os.path.join(workdir, "repo")
    os.makedirs(os.path.join(repo, "src"))
    git(repo, "init")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")

    needle_idx = {"start": 0, "middle": n_files // 2, "end": n_files - 1}[position]

    for i in range(n_files):
        with open(os.path.join(repo, "src", "mod%d.ts" % i), "w") as fh:
            fh.write(benign_module(i))
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")

    needle_path = "src/mod%d.ts" % needle_idx
    for i in range(n_files):
        path = os.path.join(repo, "src", "mod%d.ts" % i)
        with open(path, "w") as fh:
            fh.write(needle_change(i, kind) if i == needle_idx else benign_change(i))
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "change")

    raw = git(repo, "diff", "--no-color", "HEAD~1..HEAD", capture=True).stdout

    # Layer 1 + Layer 2 distilled map (compact, as a reviewer would use at scale)
    _, l1 = run_script("diff_summary.py", "--range", "HEAD~1..HEAD",
                       "--cwd", repo, "--compact")
    l1_path = os.path.join(workdir, "l1.json")
    with open(l1_path, "w") as fh:
        fh.write(l1)
    _, l2 = run_script("symbol_impact.py", "--root", repo,
                       "--diff-json", l1_path, "--compact")
    map_text = l1 + l2

    per_file = split_per_file(raw)
    needle_hunk = per_file.get(needle_path, "")

    # does the map rank the needle file first? (structural needles should)
    try:
        review_order = json.loads(l1).get("review_order", [])
        rank = review_order.index(needle_path) if needle_path in review_order else -1
    except ValueError:
        rank = -1

    return {
        "raw": raw,
        "map_text": map_text,
        "needle_hunk": needle_hunk,
        "needle_path": needle_path,
        "needle_rank_in_map": rank,
        "n_files": n_files,
    }


def needle_offsets(text):
    """(start, end) char offsets of the needle marker in text, or None."""
    idx = text.find(NEEDLE_MARK)
    if idx < 0:
        return None
    return idx, idx + len(NEEDLE_MARK)


def geometry(sc):
    """Compute context geometry for condition A (raw) and B (distill)."""
    raw = sc["raw"]
    off = needle_offsets(raw)
    a_total = len(raw)
    a_from_end = a_total - off[1] if off else a_total
    a_rel = (off[0] / a_total) if (off and a_total) else 0.0

    # Condition B: map (small) + the flagged hunk fetched & judged last.
    b_context = sc["map_text"] + "\n" + sc["needle_hunk"]
    boff = needle_offsets(b_context)
    b_total = len(b_context)
    b_from_end = b_total - boff[1] if boff else b_total
    b_rel = (boff[0] / b_total) if (boff and b_total) else 0.0

    return {
        "A_raw": {
            "total_tokens": est_tokens(raw),
            "needle_from_end_tokens": est_tokens(raw[off[1]:]) if off else est_tokens(raw),
            "needle_rel_pos": round(a_rel, 3),
        },
        "B_distill": {
            "total_tokens": est_tokens(b_context),
            "needle_from_end_tokens": est_tokens(b_context[boff[1]:]) if boff else 0,
            "needle_rel_pos": round(b_rel, 3),
            "needle_surfaced_by_map_rank": sc["needle_rank_in_map"],
        },
    }


# --- aggregation -----------------------------------------------------------

def mean(xs):
    return round(sum(xs) / len(xs), 1) if xs else 0.0


def run_eval(n_files, repeats, kind):
    rows = []
    for position in POSITIONS:
        for _ in range(repeats):
            with tempfile.TemporaryDirectory() as wd:
                sc = build_scenario(n_files, position, kind, wd)
                g = geometry(sc)
                g["position"] = position
                rows.append(g)
    # aggregate by position
    agg = []
    for position in POSITIONS:
        sub = [r for r in rows if r["position"] == position]
        agg.append({
            "position": position,
            "A_total_tokens": mean([r["A_raw"]["total_tokens"] for r in sub]),
            "A_from_end_tokens": mean([r["A_raw"]["needle_from_end_tokens"] for r in sub]),
            "A_rel_pos": mean([r["A_raw"]["needle_rel_pos"] for r in sub]),
            "B_total_tokens": mean([r["B_distill"]["total_tokens"] for r in sub]),
            "B_from_end_tokens": mean([r["B_distill"]["needle_from_end_tokens"] for r in sub]),
            "B_rel_pos": mean([r["B_distill"]["needle_rel_pos"] for r in sub]),
            "B_map_rank": mean([max(r["B_distill"]["needle_surfaced_by_map_rank"], 0) for r in sub]),
        })
    return {"kind": kind, "n_files": n_files, "repeats": repeats, "by_position": agg}


def emit_cases(out_dir, n_files, kind):
    os.makedirs(out_dir, exist_ok=True)
    manifest = []
    for position in POSITIONS:
        with tempfile.TemporaryDirectory() as wd:
            sc = build_scenario(n_files, position, kind, wd)
            base = "%s_%s" % (kind, position)
            raw_clean = sc["raw"].replace(NEEDLE_MARK + ": ", "")  # hide the marker from the judge
            distill_clean = (sc["map_text"] + "\n" + sc["needle_hunk"]).replace(NEEDLE_MARK + ": ", "")
            with open(os.path.join(out_dir, base + ".A_raw.txt"), "w") as fh:
                fh.write(raw_clean)
            with open(os.path.join(out_dir, base + ".B_distill.txt"), "w") as fh:
                fh.write(distill_clean)
            manifest.append({
                "id": base, "position": position, "kind": kind,
                "needle_path": sc["needle_path"],
                "answer": "off-by-one bug in %s" % sc["needle_path"],
            })
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def score_predictions(pred_path, manifest_path):
    preds = json.load(open(pred_path))
    manifest = json.load(open(manifest_path))
    by = {"A_raw": {}, "B_distill": {}}
    for case in manifest:
        for cond in ("A_raw", "B_distill"):
            key = "%s.%s" % (case["id"], cond)
            found = bool(preds.get(key))
            by[cond].setdefault(case["position"], []).append(found)
    out = {}
    for cond, posmap in by.items():
        out[cond] = {pos: round(sum(v) / len(v), 3) for pos, v in posmap.items()}
    return out


def main():
    p = argparse.ArgumentParser(description="Needle-in-review lost-in-the-middle eval")
    p.add_argument("--files", type=int, default=12, help="files per scenario")
    p.add_argument("--repeats", type=int, default=2, help="scenarios per position")
    p.add_argument("--kind", choices=["quiet", "structural"], default="quiet",
                   help="needle type: quiet logic bug, or structural (public-API) change")
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--emit-cases", metavar="DIR", help="write judge inputs and exit")
    p.add_argument("--predictions", metavar="FILE",
                   help="score a predictions JSON {id.cond: bool} against --manifest")
    p.add_argument("--manifest", help="manifest.json from --emit-cases (for --predictions)")
    args = p.parse_args()

    if args.emit_cases:
        m = emit_cases(args.emit_cases, args.files, args.kind)
        print("wrote %d cases (x2 conditions) to %s" % (len(m), args.emit_cases))
        print("have a model answer each .txt (found the bug? where), then:")
        print("  needle_eval.py --predictions preds.json --manifest %s/manifest.json"
              % args.emit_cases)
        return

    if args.predictions:
        if not args.manifest:
            sys.stderr.write("error: --predictions needs --manifest\n")
            sys.exit(2)
        res = score_predictions(args.predictions, args.manifest)
        print(json.dumps({"detection_rate_by_condition_and_position": res}, indent=2))
        return

    report = run_eval(args.files, args.repeats, args.kind)

    if args.json:
        print(json.dumps(report, indent=2))
        return

    print("needle-in-review geometry  (kind=%s, %d files, %d repeats/position)"
          % (report["kind"], report["n_files"], report["repeats"]))
    print("  measures the context a reviewer faces for the planted bug.")
    print("  A = read whole raw diff;  B = read compact map + fetch flagged hunk last.\n")
    hdr = "  %-7s | %-26s | %-26s" % ("pos", "A raw (total / from_end)", "B distill (total / from_end)")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in report["by_position"]:
        print("  %-7s | %8d / %-6d (rel %.2f) | %8d / %-6d (rel %.2f)"
              % (r["position"],
                 r["A_total_tokens"], r["A_from_end_tokens"], r["A_rel_pos"],
                 r["B_total_tokens"], r["B_from_end_tokens"], r["B_rel_pos"]))
    print("\n  tokens are estimates (~4 chars/token). Lower total and lower")
    print("  from_end mean the needle sits where recall is strongest.")
    if report["kind"] == "quiet":
        print("  NOTE: a quiet logic needle raises no risk flag — the map does not")
        print("  rank its file first (B_map_rank ~ mid). The win is geometry, not ranking.")
    print("\n  For real detection rates, use --emit-cases then --predictions.")


if __name__ == "__main__":
    main()
