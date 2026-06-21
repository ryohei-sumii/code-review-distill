#!/usr/bin/env python3
"""Trigger-accuracy eval loop for the SKILL.md `description`.

A skill only helps if its `description` fires on the right prompts and stays
quiet on the rest. This measures that against a labelled eval set so you can
iterate the description deliberately instead of guessing.

The fire/no-fire decision ultimately belongs to the model that reads the
description at runtime, which a script can't invoke. So this offers two paths:

  * Offline baseline (default, --heuristic): a cheap token-overlap classifier
    using trigger words mined from the description. Lets the whole loop run
    with no model in the harness — good for catching obvious gaps fast.

  * Real judgments (--predictions preds.json): score predictions produced by
    Claude (or any judge). `preds.json` is {"prompt": "fire"|"no_fire", ...}.
    This is the accurate path; the heuristic is only a stand-in.

Reports precision / recall / F1 / trigger-rate, lists false positives and
false negatives, and suggests words to add to (or reconsider in) the
description — the "improvement" half of the loop.

Usage:
    python scripts/run_loop.py --skill SKILL.md --eval evals/trigger_evalset.json
    python scripts/run_loop.py --eval evals/trigger_evalset.json \
        --predictions /tmp/preds.json --json
    python scripts/run_loop.py --make-eval > evals/trigger_evalset.json
"""

import argparse
import json
import os
import re
import sys

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+-]*")

STOPWORDS = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are",
    "this", "that", "these", "those", "with", "your", "you", "i", "it", "if",
    "do", "does", "can", "could", "would", "should", "any", "my", "me", "we",
    "be", "been", "as", "at", "by", "from", "into", "out", "over", "use", "uses",
    "so", "but", "not", "no", "yes", "what", "which", "how", "they", "them",
    "their", "its", "than", "then", "when", "where", "who", "will", "shall",
    "there", "here", "about", "before", "after", "more", "most", "every",
    "each", "all", "some", "one", "two", "e", "g",
}

# A small curated lexicon of strong trigger words for this skill, always
# considered part of the description's vocabulary even if phrasing changes.
CORE_TRIGGERS = {
    "review", "diff", "branch", "pr", "pull", "request", "changes", "changeset",
    "blast", "radius", "impact", "callers", "affected", "staged", "modified",
    "patch", "risky", "audit",
}


def tokens(text):
    return [t for t in TOKEN_RE.findall(text.lower())
            if len(t) > 1 and t not in STOPWORDS]


def read_description(skill_path):
    """Extract the YAML frontmatter `description` from a SKILL.md."""
    with open(skill_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    block = m.group(1) if m else text
    dm = re.search(r"(?ms)^description:\s*(.*?)(?=^\w[\w-]*:\s|\Z)", block)
    if not dm:
        return ""
    raw = dm.group(1)
    # strip YAML block scalar markers and indentation
    raw = re.sub(r"^[>|][+-]?\s*", "", raw.strip())
    return " ".join(line.strip() for line in raw.splitlines())


def load_eval(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    cases = data["cases"] if isinstance(data, dict) else data
    for c in cases:
        if c.get("label") not in ("should_fire", "should_not_fire"):
            raise ValueError("bad label in eval case: %r" % c)
    return cases


def heuristic_predictions(cases, description, threshold):
    vocab = set(tokens(description)) | CORE_TRIGGERS
    preds = {}
    scores = {}
    for c in cases:
        toks = set(tokens(c["prompt"]))
        hits = toks & vocab
        score = len(hits)
        scores[c["prompt"]] = sorted(hits)
        preds[c["prompt"]] = "fire" if score >= threshold else "no_fire"
    return preds, scores, vocab


def score(cases, preds):
    tp = fp = tn = fn = 0
    false_pos, false_neg = [], []
    for c in cases:
        want_fire = c["label"] == "should_fire"
        got = preds.get(c["prompt"], "no_fire")
        got_fire = got == "fire"
        if want_fire and got_fire:
            tp += 1
        elif want_fire and not got_fire:
            fn += 1
            false_neg.append(c["prompt"])
        elif not want_fire and got_fire:
            fp += 1
            false_pos.append(c["prompt"])
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    trigger_rate = (tp + fp) / len(cases) if cases else 0.0
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "trigger_rate": round(trigger_rate, 3),
        "false_positives": false_pos,
        "false_negatives": false_neg,
    }


def suggestions(cases, preds, description):
    """Mine words to add (from FNs) / reconsider (from FPs)."""
    desc_vocab = set(tokens(description)) | CORE_TRIGGERS
    fn_words, fp_words = {}, {}
    for c in cases:
        want_fire = c["label"] == "should_fire"
        got_fire = preds.get(c["prompt"], "no_fire") == "fire"
        if want_fire and not got_fire:
            for t in set(tokens(c["prompt"])) - desc_vocab:
                fn_words[t] = fn_words.get(t, 0) + 1
        if not want_fire and got_fire:
            for t in set(tokens(c["prompt"])) & desc_vocab:
                fp_words[t] = fp_words.get(t, 0) + 1
    add = [w for w, _ in sorted(fn_words.items(), key=lambda kv: -kv[1])][:8]
    reconsider = [w for w, _ in sorted(fp_words.items(), key=lambda kv: -kv[1])][:8]
    return {"consider_adding": add, "ambiguous_in_description": reconsider}


STARTER_EVAL = {
    "description": "Starter eval set for SKILL.md trigger tuning. Replace/extend with prompts you actually see.",
    "cases": [
        {"prompt": "Review this branch against main", "label": "should_fire"},
        {"prompt": "What's the blast radius of this change?", "label": "should_fire"},
        {"prompt": "Write a function that reverses a linked list", "label": "should_not_fire"},
        {"prompt": "What's the capital of France?", "label": "should_not_fire"}
    ],
}


def main():
    p = argparse.ArgumentParser(description="SKILL.md trigger-accuracy eval loop")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p.add_argument("--skill", default=os.path.join(here, "SKILL.md"))
    p.add_argument("--eval", default=os.path.join(here, "evals", "trigger_evalset.json"))
    p.add_argument("--predictions", help="JSON {prompt: 'fire'|'no_fire'} from a real judge")
    p.add_argument("--heuristic", action="store_true",
                   help="force offline heuristic even if --predictions given")
    p.add_argument("--threshold", type=int, default=1,
                   help="min trigger-word hits for heuristic fire (default 1)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    p.add_argument("--make-eval", action="store_true", help="print a starter eval set and exit")
    args = p.parse_args()

    if args.make_eval:
        json.dump(STARTER_EVAL, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    cases = load_eval(args.eval)
    description = read_description(args.skill)

    if args.predictions and not args.heuristic:
        with open(args.predictions, "r", encoding="utf-8") as fh:
            preds = json.load(fh)
        mode = "predictions"
        hit_words = {}
    else:
        preds, hit_words, _ = heuristic_predictions(cases, description, args.threshold)
        mode = "heuristic"

    metrics = score(cases, preds)
    sugg = suggestions(cases, preds, description)

    report = {
        "mode": mode,
        "skill": os.path.relpath(args.skill),
        "eval_set": os.path.relpath(args.eval),
        "n_cases": len(cases),
        "metrics": metrics,
        "suggestions": sugg,
    }

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    m = metrics
    print("trigger eval (%s) over %d cases" % (mode, len(cases)))
    print("  precision=%.3f recall=%.3f f1=%.3f trigger_rate=%.3f"
          % (m["precision"], m["recall"], m["f1"], m["trigger_rate"]))
    print("  tp=%d fp=%d tn=%d fn=%d" % (m["tp"], m["fp"], m["tn"], m["fn"]))
    if m["false_negatives"]:
        print("  MISSED (should fire but didn't):")
        for q in m["false_negatives"]:
            print("    - %s" % q)
    if m["false_positives"]:
        print("  OVER-FIRED (fired but shouldn't):")
        for q in m["false_positives"]:
            print("    - %s" % q)
    if sugg["consider_adding"]:
        print("  consider adding to description: %s" % ", ".join(sugg["consider_adding"]))
    if sugg["ambiguous_in_description"]:
        print("  ambiguous trigger words (cause over-fire): %s"
              % ", ".join(sugg["ambiguous_in_description"]))


if __name__ == "__main__":
    main()
