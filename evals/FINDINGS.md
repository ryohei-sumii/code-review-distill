# Findings — measured value of code-review-distill

This records what we actually measured about the skill, including the
unflattering parts. The honest summary: **the skill's value is context-cost
reduction on large-file / impact-analysis reviews, not improved bug detection.**
Numbers below are from synthetic benchmarks (`scripts/needle_eval.py` and ad-hoc
scripts); treat them as directional, not precise.

## 1. Map size vs raw diff — the map is *not* smaller than the diff

For multi-file changes the distilled map (Layer 1 + Layer 2 JSON) is **larger**
than the raw diff, not smaller:

| change | raw diff | map | map / raw |
|---|---|---|---|
| 2 files × 1 line | 410 B | 1,971 B | 481% |
| 8 files | 5,496 B | 9,863 B | 180% |
| 60 files | 12,915 B | 47,362 B | 367% |
| 6 large files | 1,314 B | 5,099 B | 388% |

So "read the map instead of the diff" does **not** save bytes for typical diffs.
`--compact` cuts the map ~40–60% but it still isn't smaller than the diff.

## 2. Where the skill *does* save context

The real comparison is against *what you'd otherwise read to understand the
change*, not the diff alone:

- **Large files, small change:** map ≈ 18% of reading the full files.
- **Public-API impact:** the map (≈0.9 KB compact) replaces reading ~40 caller
  files (~5 KB) to answer "who is affected" — and that reading/grep/reasoning
  happens *outside* the context window.

So the win is concentrated in: many large files, or public-API impact analysis
on a big PR. For small/medium diffs the skill is net negative.

## 3. Lost-in-the-middle geometry (objective)

Distilled review keeps the changed hunk at the **end** of context regardless of
changeset size; raw review buries an early change far from the edges:

| changeset | raw: needle dist. from end | distilled: dist. from end |
|---|---|---|
| 6 files | 517 tok | 35 tok |
| 30 files | 2,858 tok | 35 tok |
| 100 files | 9,701 tok | 35 tok |

The recency property is real and scale-invariant — *if* the reviewer fetches the
relevant hunk (see §4, where that "if" breaks).

## 4. Blind detection eval — the skill did **not** improve detection

`needle_eval.py --emit-cases` planted one off-by-one bug in a 120-file change
(no explanatory comment) and had **blind, independent reviewers** (subagents
with no knowledge of the eval or the answer) review each condition. Small
sample (n=1 per cell), single judge model family.

| case | condition | needle in context? | detected? |
|---|---|---|---|
| quiet, start | raw full diff (~12k tok) | yes (buried) | ✅ |
| quiet, middle | raw full diff | yes (buried) | ✅ |
| quiet, end | raw full diff | yes (buried) | ✅ |
| quiet, start | map + top-K hunks | **no** (ranked last) | ❌ |
| structural, start | raw full diff | yes | ❌ (read as intentional) |
| structural, start | map + top-K hunks | yes (surfaced) | ❌ (read as intentional) |

Takeaways:

1. **No lost-in-the-middle effect at ~12k tokens.** The buried off-by-one was
   found at *every* position in the raw diff. The mitigation the skill targets
   only matters at much larger contexts than this test exercised.
2. **Distillation hurt the quiet bug.** A logic bug with no mechanical risk flag
   and zero blast radius is ranked *last* by `review_order`, so it never enters
   the top-K the reviewer reads → not detected. The large map (~19k tok for 120
   files) also exceeded the reviewer's read budget.
3. **The "structural" needle is a poor needle.** Reviewers saw the change but
   judged a signature change as an *intentional* API change, not a hidden bug —
   so it is not a clean test of detection.

## 5. Honest conclusions

- Use the skill for **large-file reviews and public-API impact analysis on big
  PRs**, where it offloads multi-file reading/grepping out of the context
  window. Skip it for small/medium diffs.
- `review_order` surfaces high-blast-radius changes well but **does not surface
  signal-less logic bugs** (no flag, no callers) — a known gap that showed up as
  a real miss. Folding a logic-risk or signature-change signal into
  `review_order` is the most promising next improvement.
- The "fights lost-in-the-middle" claim is unproven at the scales tested; the
  defensible benefit is context-cost reduction and keeping exact data in
  deterministic artifacts (scripts don't round numbers; the model's own context
  summarization can).

## Reproducing

```bash
# context geometry across scales
python scripts/needle_eval.py --files 100 --json

# generate blind-judge inputs, then score a judge's predictions
python scripts/needle_eval.py --emit-cases /tmp/cases --files 120 --kind quiet
python scripts/needle_eval.py --predictions preds.json --manifest /tmp/cases/manifest.json
```
