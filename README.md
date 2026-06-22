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
> For small changes, don't compete with "read the diff" on bytes — use
> `js/impact_brief.mjs`, which returns only the cross-repo signals the model
> can't cheaply self-compute (blast radius, breaking signature changes, test
> gap) in a few hundred bytes. Net-positive even on a one-file change.
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
├── package.json                # Node deps (web-tree-sitter + WASM grammars)
├── js/                         # Node implementation (primary — runs with no Python)
│   ├── diff_summary.mjs        # Layer 1 (language-agnostic) — the core
│   ├── symbol_impact.mjs       # Layer 2 (TS/JS/Python/Go) — blast radius
│   ├── refactor_check.mjs      # companion: refactor invariant checker
│   ├── route.mjs               # entry point: auto-picks brief vs full by diff size
│   ├── impact_brief.mjs        # small-change path: cross-repo signals only
│   ├── diff_patterns.mjs       # large-scale: lossless pattern compression
│   ├── flow_map.mjs            # companion: call graph -> Mermaid
│   ├── run_loop.mjs            # companion: trigger-accuracy eval loop
│   ├── needle_eval.mjs         # companion: lost-in-the-middle geometry eval
│   └── *.test.mjs              # node:test suites
├── scripts/                    # Python reference implementation (output-identical)
├── evals/                      # trigger_evalset.json, benchmark, FINDINGS.md
└── tests/                      # pytest suite (for the Python reference)
```

## Install

Node only — no Python, no native build. Node ships with Claude Code; the AST
layer uses `web-tree-sitter` (WASM grammars). The language-agnostic core
(`diff_summary` / `diff_patterns`) needs zero dependencies.

```bash
npm install            # web-tree-sitter + tree-sitter-wasms (WASM, no node-gyp)
```

To use as a personal Claude Code skill:

```bash
mkdir -p ~/.claude/skills
cp -r code-review-distill ~/.claude/skills/
cd ~/.claude/skills/code-review-distill && npm install
```

It activates on prompts like *"review this branch"*, *"review the diff against
main"*, *"what's the blast radius of these changes"*.

> A byte-for-byte Python reference lives in `scripts/` (Python 3 + git,
> `pip install tree-sitter tree-sitter-typescript tree-sitter-python
> tree-sitter-go`). The Node port is primary; the two produce identical JSON.

## Two-layer design

- **Layer 1 — `diff_summary.mjs` (language-agnostic).** Parses `git diff` into
  per-file stats, hunk ranges, rename/delete/test/generated detection, and
  mechanical risk flags, then emits a risk-ordered `review_order`. Works for any
  language with just Python + git.
- **Layer 2 — `symbol_impact.mjs` (TS/JS/Python/Go).** Uses tree-sitter to find
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
# Recommended entry point: measure the diff and auto-pick brief vs full map
node js/route.mjs --range main..HEAD --cwd <repo>
node js/route.mjs --range main..HEAD --cwd <repo> --force full   # override
node js/route.mjs --range main..HEAD --cwd <repo> --max-brief-files 5
# Large changesets (>= --large-files) also get a `large_scale` block: lossless
# pattern compression + deterministic checks + a fan-out review plan.

# Lossless pattern compression on its own (collapse codemod/repeated hunks)
node js/diff_patterns.mjs --range main..HEAD --cwd <repo>

# Or drive the layers manually:
# Layer 1: structure the diff (branch vs base / staged / a saved patch)
node js/diff_summary.mjs --range main..HEAD --cwd <repo> > /tmp/l1.json
node js/diff_summary.mjs --staged --cwd <repo>
node js/diff_summary.mjs --file some.diff

# Layer 2: blast radius for the changed files
node js/symbol_impact.mjs --root <repo> --diff-json /tmp/l1.json
node js/symbol_impact.mjs --root <repo> --files a.ts b.py c.go

# Any JSON-emitting script accepts --compact (minified, empty fields dropped,
# referenced_by capped) for ~40-60% smaller maps on large changesets:
node js/diff_summary.mjs --range main..HEAD --cwd <repo> --compact
node js/symbol_impact.mjs --root <repo> --diff-json /tmp/l1.json --compact
# --compact never rounds numbers: all stats and blast_radius stay exact. The
# only summarization is referenced_by being capped (marked referenced_by_truncated);
# use --max-refs 0 for the full caller list, or a higher cap.
node js/symbol_impact.mjs --root <repo> --diff-json /tmp/l1.json --compact --max-refs 0

# Companion: verify a "refactor" didn't change the public API
node js/refactor_check.mjs --range main..HEAD --cwd <repo>

# Companion: process flow as Mermaid
node js/flow_map.mjs --dir <repo>/src                 # flowchart
node js/flow_map.mjs --files a.ts --sequence main     # sequenceDiagram
node js/flow_map.mjs --dir <repo>/src --json          # raw graph

# Companion: measure the SKILL.md description's trigger accuracy
node js/run_loop.mjs                                  # heuristic baseline
node js/run_loop.mjs --predictions preds.json --json  # real judgments

# Companion: quantify the lost-in-the-middle benefit (context geometry)
node js/needle_eval.mjs --files 60                    # raw vs distilled geometry
node js/needle_eval.mjs --emit-cases /tmp/cases       # inputs for a real judge
node js/needle_eval.mjs --predictions preds.json --manifest /tmp/cases/manifest.json
```

## Output schemas (quick reference)

| Script | Output |
|---|---|
| `diff_summary.mjs` | `{ source, totals, files[], risk_flags[], review_order[] }`; each file has `path, status, language, additions, deletions, risk_score, is_test, is_generated, hunks[], risk_flags[]` |
| `symbol_impact.mjs` | `{ ok, languages[], analyzed_files[], symbols[], public_api_changes[], impact_flags[] }`, or `{ ok:false, note }` (exit 3). Each symbol carries its `signature`; `blast_radius` is import-resolved. With `--diff-json`, also emits an **impact-aware** `review_order[]` + `prioritized[]` (Layer 1 order re-ranked by blast radius, with score breakdown and `reasons`) |
| `refactor_check.mjs` | `{ ok, base, head, files[], invariants{public_api_preserved, signatures_preserved, …}, flags[] }`; flags `public_signature_changed` when a kept symbol's signature changes (breaking) |
| `refactor_check.mjs` | `{ ok, base, head, files[], invariants{public_api_preserved, …}, flags[] }` |
| `flow_map.mjs` | Mermaid text, or `{ nodes[], edges[], external_calls{} }` with `--json` |
| `run_loop.mjs` | `{ mode, metrics{precision,recall,f1,trigger_rate,…}, suggestions{} }` |

## Tests

```bash
npm test                 # node --test js/*.test.mjs  (Node suite)
python -m pytest tests/ -q   # the Python reference suite
```

Both suites drive each script through its real CLI. The Node and Python
implementations are verified to emit identical JSON for the runtime scripts.

## Extending to another language

Layer 1 needs no changes. For Layer 2, add a registry entry to `LANGS` in
`symbol_impact.mjs` (extensions + tree-sitter loader) and a per-language
`extract_*` walk; for call graphs, add a parallel `FLOW` entry in `flow_map.mjs`
(function / call node types).
