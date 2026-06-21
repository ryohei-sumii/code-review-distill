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

## 6. Routing benchmark — the benefit is driven by blast radius, not file count

`evals/benchmark_routing.py` builds changesets of N files where one changed file
holds a public symbol used by C callers (and a breaking signature change), then
measures, in estimated tokens, the context to **understand the impact** under:

- **A** raw diff only (no impact analysis),
- **B** raw diff + caller files (what you'd read to get impact *without* the skill),
- **C** `route.py` output (skill, auto-routed brief/full).

C vs B (% less context to get the impact), by blast radius. The router is
**blast-radius-aware** (`route.py` runs a cheap L2 first): small diffs and
medium-but-low-impact diffs take the brief; high blast radius or many files
(navigation) take the full map.

| files | callers=2 | callers=10 | callers=50 |
|---|---|---|---|
| 1  | brief, −41%* | brief, **−39%** | brief, **−85%** |
| 2  | brief, **+9%** | brief, **+50%** | brief, **+85%** |
| 5  | brief, **+53%** | full, −6% | full, **+57%** |
| 12 | brief, **+79%** | full, −14% | full, **+35%** |
| 30 | full(nav), −27% | full, −19% | full, +11% |
| 60 | full(nav), −24% | full, −20% | full, −2% |

(Positive = route uses fewer tokens than reading raw + callers. *the 1-file/2-caller
cell is tiny in absolute terms: 127 vs 90 tokens.)

Honest reading:

- **The blast-aware router fixed the worst cells.** Low-impact medium diffs
  (callers=2, 5–12 files) went from −40%/−13% under the old size-only router to
  **+53%/+79%** — they now take the brief instead of paying the full-map tax.
- **Small changes are a clear win** via the brief (39–85% less to get impact).
- **The win scales with blast radius (callers), not file count** — the routing
  variable that matters.
- **Two residual negatives are real and accepted:**
  - *navigation* — 30–60-file diffs route to full even at low blast (you can't
    triage 60 files by eye); the impact-byte metric doesn't credit the ordering
    the map provides, so it shows as overhead here.
  - *threshold border* — blast exactly at `--min-blast` (10) takes full with a
    small overhead; this is a knob, tune per repo.

## 7. Large scale — the full map grows past the raw diff (a cap is needed)

Pushing the benchmark to hundreds of files (estimated tokens):

| files | raw diff | route full map | map / raw |
|---|---|---|---|
| 100 | 5,665 | 7,021 | 124% |
| 300 | 17,315 | 21,071 | 122% |
| 600 | 34,790 | 42,146 | 121% |

The full map is **consistently larger than the raw diff** (~121–124%), at both
low and high blast radius. It lists *every* changed file plus symbols and reasons
in `review_order` / `prioritized`, so it grows linearly with N and overtakes the
diff — it does **not** shrink context at scale.

The fix this points to: the full map only needs the **top-N priorities**, not a
full ranking of all 600 files. Capping `review_order` / `prioritized` to a top-N
(with a "+M more" count) would make the map bounded and sublinear at scale —
finally smaller than the raw diff — without losing the triage value (the long
tail is low priority by construction). Not yet implemented.

## Reproducing

```bash
# routing benefit across diff sizes and blast radii
python evals/benchmark_routing.py --callers 10

# context geometry across scales
python scripts/needle_eval.py --files 100 --json

# generate blind-judge inputs, then score a judge's predictions
python scripts/needle_eval.py --emit-cases /tmp/cases --files 120 --kind quiet
python scripts/needle_eval.py --predictions preds.json --manifest /tmp/cases/manifest.json
```
