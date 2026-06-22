#!/usr/bin/env node
// Trigger-accuracy eval loop — Node port of scripts/run_loop.py.

import { readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const HERE = path.dirname(path.dirname(new URL(import.meta.url).pathname));
const TOKEN_RE = /[a-z0-9][a-z0-9_+-]*/g;

const STOPWORDS = new Set(("the a an to of in on for and or is are this that these those with your you i it if " +
  "do does can could would should any my me we be been as at by from into out over use uses so but not no yes " +
  "what which how they them their its than then when where who will shall there here about before after more " +
  "most every each all some one two e g").split(" "));

const CORE_TRIGGERS = new Set(["review", "diff", "branch", "pr", "pull", "request", "changes", "changeset",
  "blast", "radius", "impact", "callers", "affected", "staged", "modified", "patch", "risky", "audit"]);

function tokens(text) {
  const out = [];
  for (const m of text.toLowerCase().matchAll(TOKEN_RE)) {
    const t = m[0];
    if (t.length > 1 && !STOPWORDS.has(t)) out.push(t);
  }
  return out;
}

function readDescription(skillPath) {
  const text = readFileSync(skillPath, "utf8");
  const m = text.match(/^---\s*\n([\s\S]*?)\n---\s*\n/);
  const block = m ? m[1] : text;
  const lines = block.split("\n");
  const i = lines.findIndex((l) => /^description:/.test(l));
  if (i < 0) return "";
  const parts = [lines[i].replace(/^description:\s*/, "")];
  for (let j = i + 1; j < lines.length; j++) {
    if (/^\w[\w-]*:\s/.test(lines[j])) break; // next top-level key
    parts.push(lines[j]);
  }
  const raw = parts.join("\n").trim().replace(/^[>|][+-]?\s*/, "");
  return raw.split("\n").map((l) => l.trim()).join(" ");
}

function loadEval(p) {
  const data = JSON.parse(readFileSync(p, "utf8"));
  const cases = Array.isArray(data) ? data : data.cases;
  for (const c of cases) if (c.label !== "should_fire" && c.label !== "should_not_fire") throw new Error("bad label: " + JSON.stringify(c));
  return cases;
}

function heuristicPredictions(cases, description, threshold) {
  const vocab = new Set([...tokens(description), ...CORE_TRIGGERS]);
  const preds = {};
  for (const c of cases) {
    const toks = new Set(tokens(c.prompt));
    let hits = 0;
    for (const t of toks) if (vocab.has(t)) hits++;
    preds[c.prompt] = hits >= threshold ? "fire" : "no_fire";
  }
  return preds;
}

function score(cases, preds) {
  let tp = 0, fp = 0, tn = 0, fn = 0;
  const falsePos = [], falseNeg = [];
  for (const c of cases) {
    const wantFire = c.label === "should_fire";
    const gotFire = (preds[c.prompt] || "no_fire") === "fire";
    if (wantFire && gotFire) tp++;
    else if (wantFire && !gotFire) { fn++; falseNeg.push(c.prompt); }
    else if (!wantFire && gotFire) { fp++; falsePos.push(c.prompt); }
    else tn++;
  }
  const precision = (tp + fp) ? tp / (tp + fp) : 1.0;
  const recall = (tp + fn) ? tp / (tp + fn) : 1.0;
  const f1 = (precision + recall) ? (2 * precision * recall) / (precision + recall) : 0.0;
  const triggerRate = cases.length ? (tp + fp) / cases.length : 0.0;
  const r3 = (x) => Math.round(x * 1000) / 1000;
  return { tp, fp, tn, fn, precision: r3(precision), recall: r3(recall), f1: r3(f1),
    trigger_rate: r3(triggerRate), false_positives: falsePos, false_negatives: falseNeg };
}

function suggestions(cases, preds, description) {
  const descVocab = new Set([...tokens(description), ...CORE_TRIGGERS]);
  const fnWords = {}, fpWords = {};
  for (const c of cases) {
    const wantFire = c.label === "should_fire";
    const gotFire = (preds[c.prompt] || "no_fire") === "fire";
    const toks = new Set(tokens(c.prompt));
    if (wantFire && !gotFire) for (const t of toks) if (!descVocab.has(t)) fnWords[t] = (fnWords[t] || 0) + 1;
    if (!wantFire && gotFire) for (const t of toks) if (descVocab.has(t)) fpWords[t] = (fpWords[t] || 0) + 1;
  }
  const top = (o) => Object.entries(o).sort((a, b) => b[1] - a[1]).map((e) => e[0]).slice(0, 8);
  return { consider_adding: top(fnWords), ambiguous_in_description: top(fpWords) };
}

const STARTER_EVAL = {
  description: "Starter eval set for SKILL.md trigger tuning. Replace/extend with prompts you actually see.",
  cases: [
    { prompt: "Review this branch against main", label: "should_fire" },
    { prompt: "What's the blast radius of this change?", label: "should_fire" },
    { prompt: "Write a function that reverses a linked list", label: "should_not_fire" },
    { prompt: "What's the capital of France?", label: "should_not_fire" },
  ],
};

function parseArgs(argv) {
  const a = { skill: path.join(HERE, "SKILL.md"), eval: path.join(HERE, "evals", "trigger_evalset.json"),
    predictions: null, heuristic: false, threshold: 1, json: false, makeEval: false };
  for (let i = 0; i < argv.length; i++) {
    const x = argv[i];
    if (x === "--skill") a.skill = argv[++i];
    else if (x === "--eval") a.eval = argv[++i];
    else if (x === "--predictions") a.predictions = argv[++i];
    else if (x === "--heuristic") a.heuristic = true;
    else if (x === "--threshold") a.threshold = +argv[++i];
    else if (x === "--json") a.json = true;
    else if (x === "--make-eval") a.makeEval = true;
  }
  return a;
}

function main() {
  const a = parseArgs(process.argv.slice(2));
  if (a.makeEval) { process.stdout.write(JSON.stringify(STARTER_EVAL, null, 2) + "\n"); return; }

  const cases = loadEval(a.eval);
  const description = readDescription(a.skill);

  let preds, mode;
  if (a.predictions && !a.heuristic) { preds = JSON.parse(readFileSync(a.predictions, "utf8")); mode = "predictions"; }
  else { preds = heuristicPredictions(cases, description, a.threshold); mode = "heuristic"; }

  const metrics = score(cases, preds);
  const sugg = suggestions(cases, preds, description);
  const report = { mode, skill: path.relative(".", a.skill), eval_set: path.relative(".", a.eval),
    n_cases: cases.length, metrics, suggestions: sugg };

  if (a.json) { process.stdout.write(JSON.stringify(report, null, 2) + "\n"); return; }
  const m = metrics;
  console.log(`trigger eval (${mode}) over ${cases.length} cases`);
  console.log(`  precision=${m.precision.toFixed(3)} recall=${m.recall.toFixed(3)} f1=${m.f1.toFixed(3)} trigger_rate=${m.trigger_rate.toFixed(3)}`);
  console.log(`  tp=${m.tp} fp=${m.fp} tn=${m.tn} fn=${m.fn}`);
  if (m.false_negatives.length) { console.log("  MISSED (should fire but didn't):"); for (const q of m.false_negatives) console.log("    - " + q); }
  if (m.false_positives.length) { console.log("  OVER-FIRED (fired but shouldn't):"); for (const q of m.false_positives) console.log("    - " + q); }
  if (sugg.consider_adding.length) console.log("  consider adding to description: " + sugg.consider_adding.join(", "));
  if (sugg.ambiguous_in_description.length) console.log("  ambiguous trigger words (cause over-fire): " + sugg.ambiguous_in_description.join(", "));
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main();
