# code-review-distill

A Claude Code **skill** that reviews code changes cheaply by distilling a diff
into a small, structured map — then reading the original source only where the
map says it matters.

**Why:** the analysis scripts run *outside* the model's context window. Only
their compact JSON enters the conversation, so a large PR can be triaged with a
few kilobytes instead of the whole patch — and earlier findings don't get lost
in the middle of a long transcript.

> Honest constraint: the savings scale with change size. For a one- or two-line
> diff, just read it — the map costs more than it saves. The win shows up on
> multi-file / large changesets.
>
> What this measurably does and does **not** do is recorded in
> [`evals/FINDINGS.md`](evals/FINDINGS.md): the value is context-cost reduction
> on large-file / impact-analysis reviews, **not** improved bug detection (a
> blind eval found no detection gain, and signal-less logic bugs are ranked
> last). Read it before trusting the pitch.

## Layout

```
code-review-distill/
├── SKILL.md                    # command center: when to call what, and how to use output
├── scripts/
│   ├── diff_summary.py         # Layer 1 (language-agnostic) — the core
│   ├── symbol_impact.py        # Layer 2 (TS/JS/Python/Go) — blast radius
│   ├── refactor_check.py       # companion: refactor invariant checker
│   ├── flow_map.py             # companion: call graph -> Mermaid
│   ├── run_loop.py             # companion: SKILL.md trigger-accuracy eval loop
│   └── needle_eval.py          # companion: lost-in-the-middle geometry eval
├── evals/
│   └── trigger_evalset.json    # labelled prompts for run_loop.py
└── tests/
    └── test_distill.py         # pytest smoke + behaviour tests
```

## Install

Only Layer 1 is required (Python 3 + git, no extra deps). The rest use
tree-sitter; install only the grammars you need:

```bash
pip install tree-sitter \
    tree-sitter-typescript tree-sitter-python tree-sitter-go \
    --break-system-packages
```

To use as a personal Claude Code skill:

```bash
mkdir -p ~/.claude/skills
cp -r code-review-distill ~/.claude/skills/
```

It activates on prompts like *"review this branch"*, *"review the diff against
main"*, *"what's the blast radius of these changes"*.

## Two-layer design

- **Layer 1 — `diff_summary.py` (language-agnostic).** Parses `git diff` into
  per-file stats, hunk ranges, rename/delete/test/generated detection, and
  mechanical risk flags, then emits a risk-ordered `review_order`. Works for any
  language with just Python + git.
- **Layer 2 — `symbol_impact.py` (TS/JS/Python/Go).** Uses tree-sitter to find
  changed symbols (with signatures), flag the public API, and estimate blast
  radius — **import-resolved**: how many other files actually import each public
  symbol from its defining file. Same-name locals and cross-module collisions
  are excluded, not merely identifier matches.

Both **fall back gracefully**: Layer 2 exits `3` with a JSON note when no
supported file is present or a grammar is missing, so the review continues on
Layer 1 alone. "Public" means exported (TS/JS), non-underscore module-level
(Python), or capitalised (Go).

## Usage

```bash
# Layer 1: structure the diff (branch vs base / staged / a saved patch)
python scripts/diff_summary.py --range main..HEAD --cwd <repo> > /tmp/l1.json
python scripts/diff_summary.py --staged --cwd <repo>
python scripts/diff_summary.py --file some.diff

# Layer 2: blast radius for the changed files
python scripts/symbol_impact.py --root <repo> --diff-json /tmp/l1.json
python scripts/symbol_impact.py --root <repo> --files a.ts b.py c.go

# Any JSON-emitting script accepts --compact (minified, empty fields dropped,
# referenced_by capped) for ~40-60% smaller maps on large changesets:
python scripts/diff_summary.py --range main..HEAD --cwd <repo> --compact
python scripts/symbol_impact.py --root <repo> --diff-json /tmp/l1.json --compact
# --compact never rounds numbers: all stats and blast_radius stay exact. The
# only summarization is referenced_by being capped (marked referenced_by_truncated);
# use --max-refs 0 for the full caller list, or a higher cap.
python scripts/symbol_impact.py --root <repo> --diff-json /tmp/l1.json --compact --max-refs 0

# Companion: verify a "refactor" didn't change the public API
python scripts/refactor_check.py --range main..HEAD --cwd <repo>

# Companion: process flow as Mermaid
python scripts/flow_map.py --dir <repo>/src                 # flowchart
python scripts/flow_map.py --files a.ts --sequence main     # sequenceDiagram
python scripts/flow_map.py --dir <repo>/src --json          # raw graph

# Companion: measure the SKILL.md description's trigger accuracy
python scripts/run_loop.py                                  # heuristic baseline
python scripts/run_loop.py --predictions preds.json --json  # real judgments

# Companion: quantify the lost-in-the-middle benefit (context geometry)
python scripts/needle_eval.py --files 60                    # raw vs distilled geometry
python scripts/needle_eval.py --emit-cases /tmp/cases       # inputs for a real judge
python scripts/needle_eval.py --predictions preds.json --manifest /tmp/cases/manifest.json
```

## Output schemas (quick reference)

| Script | Output |
|---|---|
| `diff_summary.py` | `{ source, totals, files[], risk_flags[], review_order[] }`; each file has `path, status, language, additions, deletions, risk_score, is_test, is_generated, hunks[], risk_flags[]` |
| `symbol_impact.py` | `{ ok, languages[], analyzed_files[], symbols[], public_api_changes[], impact_flags[] }`, or `{ ok:false, note }` (exit 3). Each symbol carries its `signature`; `blast_radius` is import-resolved. With `--diff-json`, also emits an **impact-aware** `review_order[]` + `prioritized[]` (Layer 1 order re-ranked by blast radius, with score breakdown and `reasons`) |
| `refactor_check.py` | `{ ok, base, head, files[], invariants{public_api_preserved, signatures_preserved, …}, flags[] }`; flags `public_signature_changed` when a kept symbol's signature changes (breaking) |
| `refactor_check.py` | `{ ok, base, head, files[], invariants{public_api_preserved, …}, flags[] }` |
| `flow_map.py` | Mermaid text, or `{ nodes[], edges[], external_calls{} }` with `--json` |
| `run_loop.py` | `{ mode, metrics{precision,recall,f1,trigger_rate,…}, suggestions{} }` |

## Tests

```bash
pip install pytest --break-system-packages
python -m pytest tests/ -q
```

Tests drive each script through its real CLI. Layer 1 tests always run; tests
needing a tree-sitter grammar skip cleanly when it isn't installed.

## Extending to another language

Layer 1 needs no changes. For Layer 2, add a registry entry to `LANGS` in
`symbol_impact.py` (extensions + tree-sitter loader) and a per-language
`extract_*` walk; for call graphs, add a parallel `FLOW` entry in `flow_map.py`
(function / call node types).
