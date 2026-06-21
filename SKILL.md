---
name: code-review-distill
description: >-
  Use this whenever you are asked to review code changes, a diff, a branch, or a
  pull request — e.g. "review this branch", "review the diff against main",
  "what's the blast radius of these changes", "code review this PR". It distills
  a diff into a compact structured map (per-file stats, hunks, risk flags, and —
  for TypeScript/JavaScript/Python/Go — changed symbols and blast radius) so you
  review large changesets without loading every patch into context. Prefer this
  over reading the raw diff for any multi-file or large change.
---

# code-review-distill

Review code changes cheaply by turning the diff into a small structured map,
then reading the original source only where the map says it matters.

**Why:** the analysis scripts run *outside* the context window. Only their
compact JSON enters the conversation, so you can triage a large PR with a few
kilobytes instead of the whole patch — and you avoid losing earlier findings in
the middle of a long transcript.

## When to use vs. skip

- **Use it** for multi-file changes, large hunks, refactors, or any PR where you
  don't already hold the whole diff in mind.
- **Skip it** for a tiny one- or two-line change — the map costs more than just
  reading the diff. Honest constraint: the savings scale with change size.

## Workflow

1. **Decide the range.**
   - Branch vs. base: `--range main..HEAD` (substitute the real base).
   - Uncommitted work: `--staged`, or no range for the working tree.
   - A saved patch: `--file some.diff`.

2. **Run Layer 1 (language-agnostic).** Always start here.
   ```bash
   python scripts/diff_summary.py --range main..HEAD --cwd <repo> > /tmp/l1.json
   ```
   Read the JSON. Note `risk_flags`, `totals`, and `review_order`. Key signals:
   `code_changed_without_tests`, `file_deleted`, `large_hunk`,
   `large_file_change`, `generated_file`.

   For big changesets add `--compact` (minified JSON, empty fields dropped —
   ~40-60% smaller) to keep the map cheap. Every script that emits JSON accepts
   it.

3. **Run Layer 2 (symbol impact) if relevant.** If any changed file is a
   supported source file (`.ts/.tsx/.js/.jsx`, `.py`, `.go`), get the blast
   radius:
   ```bash
   python scripts/symbol_impact.py --root <repo> --diff-json /tmp/l1.json > /tmp/l2.json
   ```
   - Exit `0`: read `public_api_changes`, `impact_flags`, and per-symbol
     `blast_radius` / `referenced_by`. A changed public symbol with a wide
     blast radius is a top priority. "Public" means: exported (TS/JS),
     non-underscore module-level (Python), or capitalised (Go).
   - Exit `3`: expected fallback (no supported files, or grammar not installed).
     Continue with Layer 1 only — do **not** treat this as an error. To enable:
     `pip install tree-sitter tree-sitter-typescript tree-sitter-python tree-sitter-go --break-system-packages`.

4. **Prioritise.** Walk `review_order` (highest risk first). Spend attention on
   high-risk files and high-blast-radius public API changes; skim generated and
   lock files.

5. **Fetch original source only where it matters.** For the prioritised files,
   read the actual diff hunks or source lines (use the `hunks` ranges from
   Layer 1 to jump straight to the changed region). Don't pull files the map
   marks as low risk.

6. **Review by aspect** for each prioritised file:
   - **logic** — correctness, edge cases, off-by-one, null/undefined.
   - **error handling** — failures swallowed, unchecked returns, resource leaks.
   - **api** — breaking changes to exported symbols (cross-check Layer 2's
     `referenced_by`; flag callers that need updating).
   - **tests** — if `code_changed_without_tests` fired, call it out and say what
     should be covered.
   - **naming / clarity** — only where it genuinely hurts readability.

7. **Report** findings ordered by severity, each tied to `file:line`, with a
   concrete suggested fix. Note explicitly when the map let you skip large
   low-risk regions.

## Companion distillers (same two-layer + fallback philosophy)

Reach for these when the task is adjacent to a plain review:

- **Refactor verification** — `scripts/refactor_check.py --range base..head
  --cwd <repo>`. Compares symbols at both revisions and flags when a "pure
  refactor" silently changed the public API
  (`public_api_changed_during_refactor`). Use when the user calls something a
  refactor and you need to confirm behaviour/API was preserved.
- **Process flow** — `scripts/flow_map.py --dir <src>` emits a Mermaid
  `flowchart` of the internal call graph; `--sequence ENTRY` traces a
  `sequenceDiagram` from one function; `--json` gives the raw graph. Use when
  asked "how does this flow" / "what calls what".
- **Trigger tuning** — `scripts/run_loop.py` scores this `description` against
  `evals/trigger_evalset.json` (precision/recall/trigger-rate, plus words to
  add or reconsider). Use when iterating the skill's own activation accuracy.

## Output reference

- `diff_summary.py` → `{ source, totals, files[], risk_flags[], review_order[] }`.
  Each file: `path, old_path?, status, language, additions, deletions, is_test,
  is_generated, is_binary, hunks[], risk_flags[]`.
- `symbol_impact.py` → `{ ok, languages[], analyzed_files[], symbols[],
  public_api_changes[], impact_flags[] }`, or `{ ok:false, note }` with exit 3
  on graceful fallback. Supports typescript / javascript / python / go.

## Extending

Layer 1 is already language-agnostic. To add a language to Layer 2, add a
registry entry in `symbol_impact.py` (`LANGS`: extensions + tree-sitter loader)
and a per-language `extract_*` walk; Layer 1 needs no changes. `flow_map.py`
takes a parallel `FLOW` entry (function/call node types).
