#!/usr/bin/env node
// Needle-in-review eval — Node port of scripts/needle_eval.py.
// Measures context geometry (raw vs distilled) and emits blind-judge inputs.

import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const JS = path.dirname(new URL(import.meta.url).pathname);
const POSITIONS = ["start", "middle", "end"];
const NEEDLE_MARK = "NEEDLE";

const estTokens = (n) => Math.floor((n + 3) / 4);
const git = (repo, ...a) => execFileSync("git", a, { cwd: repo, stdio: "ignore" });
function gitCapture(repo, ...a) { return execFileSync("git", a, { cwd: repo, encoding: "utf8", maxBuffer: 1 << 28 }); }
function runScript(name, args) {
  try { return execFileSync("node", [path.join(JS, name), ...args], { encoding: "utf8", maxBuffer: 1 << 28 }); }
  catch (e) { return e.stdout || ""; }
}

function benignModule(i) {
  return `export function fn${i}(a: number, b: number): number {\n  let acc = 0;\n` +
    `  for (let k = 0; k <= b; k++) { acc += a + k; }\n  return acc;\n}\n`;
}
function benignChange(i) {
  return `export function fn${i}(a: number, b: number): number {\n  // refactor: use reduce\n` +
    `  const xs = Array.from({length: b + 1}, (_, k) => a + k);\n  return xs.reduce((p, q) => p + q, 0);\n}\n`;
}
function needleChange(i, kind) {
  if (kind === "structural") {
    return `export function fn${i}(a: number, b: number, c: number): number {\n  let acc = 0;\n` +
      `  for (let k = 0; k < b; k++) { acc += a + k + c; }\n  return acc;  //${NEEDLE_MARK}\n}\n`;
  }
  return `export function fn${i}(a: number, b: number): number {\n` +
    `  const xs = Array.from({length: b}, (_, k) => a + k);\n` +
    `  return xs.reduce((p, q) => p + q, 0);  //${NEEDLE_MARK}\n}\n`;
}

function splitPerFile(diffText) {
  // Keep the whole per-file block (incl the `diff --git` header), matching the
  // Python needle_eval — distinct from diff_patterns which keeps only +/- lines.
  const files = {};
  let cur = null, buf = [];
  for (const line of diffText.split("\n")) {
    if (line.startsWith("diff --git ")) {
      if (cur !== null) files[cur] = buf.join("\n");
      buf = [line];
      const m = line.match(/^diff --git a\/.+ b\/(.+)$/);
      cur = m ? m[1] : null;
    } else if (cur !== null) buf.push(line);
  }
  if (cur !== null) files[cur] = buf.join("\n");
  return files;
}

function buildScenario(nFiles, position, kind, workdir) {
  const repo = path.join(workdir, "repo");
  mkdirSync(path.join(repo, "src"), { recursive: true });
  git(repo, "init"); git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t");
  const needleIdx = { start: 0, middle: Math.floor(nFiles / 2), end: nFiles - 1 }[position];

  for (let i = 0; i < nFiles; i++) writeFileSync(path.join(repo, "src", `mod${i}.ts`), benignModule(i));
  git(repo, "add", "-A"); git(repo, "commit", "-m", "init");

  const needlePath = `src/mod${needleIdx}.ts`;
  for (let i = 0; i < nFiles; i++) {
    writeFileSync(path.join(repo, "src", `mod${i}.ts`), i === needleIdx ? needleChange(i, kind) : benignChange(i));
  }
  git(repo, "add", "-A"); git(repo, "commit", "-m", "change");

  const raw = gitCapture(repo, "diff", "--no-color", "HEAD~1..HEAD");
  const l1 = runScript("diff_summary.mjs", ["--range", "HEAD~1..HEAD", "--cwd", repo, "--compact"]);
  const l1Path = path.join(workdir, "l1.json");
  writeFileSync(l1Path, l1);
  const l2 = runScript("symbol_impact.mjs", ["--root", repo, "--diff-json", l1Path, "--compact"]);
  const mapText = l1 + l2;

  const perFile = splitPerFile(raw);
  const needleHunk = perFile[needlePath] || "";

  let reviewOrder = [];
  try { reviewOrder = JSON.parse(l2).review_order || []; } catch { /* */ }
  if (!reviewOrder.length) { try { reviewOrder = JSON.parse(l1).review_order || []; } catch { /* */ } }
  const rank = reviewOrder.indexOf(needlePath);

  return { raw, mapText, needleHunk, needlePath, needleRank: rank, reviewOrder, perFile, nFiles };
}

function needleOffsets(text) {
  const idx = text.indexOf(NEEDLE_MARK);
  return idx < 0 ? null : [idx, idx + NEEDLE_MARK.length];
}

function geometry(sc) {
  const raw = sc.raw;
  const off = needleOffsets(raw);
  const aTotal = raw.length;
  const aRel = off && aTotal ? off[0] / aTotal : 0.0;
  const bContext = sc.mapText + "\n" + sc.needleHunk;
  const boff = needleOffsets(bContext);
  const bTotal = bContext.length;
  const bRel = boff && bTotal ? boff[0] / bTotal : 0.0;
  const r3 = (x) => Math.round(x * 1000) / 1000;
  return {
    A_raw: { total_tokens: estTokens(raw.length),
      needle_from_end_tokens: off ? estTokens(raw.length - off[1]) : estTokens(raw.length),
      needle_rel_pos: r3(aRel) },
    B_distill: { total_tokens: estTokens(bContext.length),
      needle_from_end_tokens: boff ? estTokens(bContext.length - boff[1]) : 0,
      needle_rel_pos: r3(bRel), needle_surfaced_by_map_rank: sc.needleRank },
  };
}

const mean = (xs) => xs.length ? Math.round((xs.reduce((s, x) => s + x, 0) / xs.length) * 10) / 10 : 0.0;

function runEval(nFiles, repeats, kind) {
  const rows = [];
  for (const position of POSITIONS) {
    for (let r = 0; r < repeats; r++) {
      const wd = mkdtempSync(path.join(tmpdir(), "crd-ne-"));
      try { const sc = buildScenario(nFiles, position, kind, wd); const g = geometry(sc); g.position = position; rows.push(g); }
      finally { rmSync(wd, { recursive: true, force: true }); }
    }
  }
  const agg = [];
  for (const position of POSITIONS) {
    const sub = rows.filter((r) => r.position === position);
    agg.push({
      position,
      A_total_tokens: mean(sub.map((r) => r.A_raw.total_tokens)),
      A_from_end_tokens: mean(sub.map((r) => r.A_raw.needle_from_end_tokens)),
      A_rel_pos: mean(sub.map((r) => r.A_raw.needle_rel_pos)),
      B_total_tokens: mean(sub.map((r) => r.B_distill.total_tokens)),
      B_from_end_tokens: mean(sub.map((r) => r.B_distill.needle_from_end_tokens)),
      B_rel_pos: mean(sub.map((r) => r.B_distill.needle_rel_pos)),
      B_map_rank: mean(sub.map((r) => Math.max(r.B_distill.needle_surfaced_by_map_rank, 0))),
    });
  }
  return { kind, n_files: nFiles, repeats, by_position: agg };
}

function emitCases(outDir, nFiles, kind, topk = 6) {
  mkdirSync(outDir, { recursive: true });
  const manifest = [];
  const strip = (t) => t.replace(new RegExp(`\\s*//\\s*${NEEDLE_MARK}\\b`, "g"), "");
  for (const position of POSITIONS) {
    const wd = mkdtempSync(path.join(tmpdir(), "crd-ne-"));
    try {
      const sc = buildScenario(nFiles, position, kind, wd);
      const base = `${kind}_${position}`;
      const topPaths = sc.reviewOrder.slice(0, topk);
      const topHunks = topPaths.map((p) => sc.perFile[p] || "").join("\n");
      const needleInTopk = topPaths.includes(sc.needlePath);
      writeFileSync(path.join(outDir, base + ".A_raw.txt"), strip(sc.raw));
      writeFileSync(path.join(outDir, base + ".B_distill.txt"), strip(sc.mapText + "\n\n# top hunks:\n" + topHunks));
      manifest.push({ id: base, position, kind, needle_path: sc.needlePath,
        needle_rank_in_map: sc.needleRank, needle_in_topk: needleInTopk, topk,
        answer: `off-by-one bug in ${sc.needlePath}` });
    } finally { rmSync(wd, { recursive: true, force: true }); }
  }
  writeFileSync(path.join(outDir, "manifest.json"), JSON.stringify(manifest, null, 2));
  return manifest;
}

function scorePredictions(predPath, manifestPath) {
  const preds = JSON.parse(readFileSync(predPath, "utf8"));
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const by = { A_raw: {}, B_distill: {} };
  for (const c of manifest) {
    for (const cond of ["A_raw", "B_distill"]) {
      const key = `${c.id}.${cond}`;
      (by[cond][c.position] ||= []).push(!!preds[key]);
    }
  }
  const out = {};
  for (const [cond, posmap] of Object.entries(by)) {
    out[cond] = {};
    for (const [pos, v] of Object.entries(posmap)) out[cond][pos] = Math.round((v.filter(Boolean).length / v.length) * 1000) / 1000;
  }
  return out;
}

function parseArgs(argv) {
  const a = { files: 12, repeats: 2, kind: "quiet", json: false, emitCases: null, predictions: null, manifest: null };
  for (let i = 0; i < argv.length; i++) {
    const x = argv[i];
    if (x === "--files") a.files = +argv[++i];
    else if (x === "--repeats") a.repeats = +argv[++i];
    else if (x === "--kind") a.kind = argv[++i];
    else if (x === "--json") a.json = true;
    else if (x === "--emit-cases") a.emitCases = argv[++i];
    else if (x === "--predictions") a.predictions = argv[++i];
    else if (x === "--manifest") a.manifest = argv[++i];
  }
  return a;
}

function main() {
  const a = parseArgs(process.argv.slice(2));
  if (a.emitCases) {
    const m = emitCases(a.emitCases, a.files, a.kind);
    console.log(`wrote ${m.length} cases (x2 conditions) to ${a.emitCases}`);
    console.log("have a model answer each .txt (found the bug? where), then:");
    console.log(`  needle_eval.mjs --predictions preds.json --manifest ${a.emitCases}/manifest.json`);
    return;
  }
  if (a.predictions) {
    if (!a.manifest) { process.stderr.write("error: --predictions needs --manifest\n"); process.exit(2); }
    console.log(JSON.stringify({ detection_rate_by_condition_and_position: scorePredictions(a.predictions, a.manifest) }, null, 2));
    return;
  }
  const report = runEval(a.files, a.repeats, a.kind);
  if (a.json) { console.log(JSON.stringify(report, null, 2)); return; }
  console.log(`needle-in-review geometry  (kind=${report.kind}, ${report.n_files} files, ${report.repeats} repeats/position)`);
  console.log("  measures the context a reviewer faces for the planted bug.");
  console.log("  A = read whole raw diff;  B = read compact map + fetch flagged hunk last.\n");
  const hdr = `  ${"pos".padEnd(7)} | ${"A raw (total / from_end)".padEnd(26)} | ${"B distill (total / from_end)".padEnd(26)}`;
  console.log(hdr);
  console.log("  " + "-".repeat(hdr.length - 2));
  for (const r of report.by_position) {
    console.log(`  ${r.position.padEnd(7)} | ${String(r.A_total_tokens).padStart(8)} / ${String(r.A_from_end_tokens).padEnd(6)} (rel ${r.A_rel_pos.toFixed(2)}) | ` +
      `${String(r.B_total_tokens).padStart(8)} / ${String(r.B_from_end_tokens).padEnd(6)} (rel ${r.B_rel_pos.toFixed(2)})`);
  }
  console.log("\n  For real detection rates, use --emit-cases then --predictions.");
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main();
