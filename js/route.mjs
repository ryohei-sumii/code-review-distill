#!/usr/bin/env node
// Auto-router (recommended entry point) — Node port of scripts/route.py.
// Measures the diff and its blast radius, then picks brief vs full map; large
// changesets get the pattern-compression + checks + fan-out toolkit.

import { execFileSync } from "node:child_process";
import { writeFileSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";

const JS = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_MAX_FILES = 3, DEFAULT_MAX_LINES = 60, DEFAULT_MIN_BLAST = 10, DEFAULT_LARGE_FILES = 25;
const FANOUT_BATCH = 10;

function run(script, args) {
  try { return execFileSync("node", [path.join(JS, script), ...args], { encoding: "utf8", maxBuffer: 1 << 28 }); }
  catch (e) { return e.stdout || ""; }
}
function runJson(script, args) {
  const out = run(script, args);
  try { return JSON.parse(out.trim()); } catch { return null; }
}

function buildLargeScale(cwd, scope, l1, l2, hasRange) {
  const patterns = runJson("diff_patterns.mjs", ["--cwd", cwd, "--json", ...scope]) || {};
  const patList = patterns.patterns || [];
  const unique = patterns.unique || [];
  const units = [...patList.map((p) => p.example_file), ...unique];
  const batches = [];
  for (let i = 0; i < units.length; i += FANOUT_BATCH) batches.push(units.slice(i, i + FANOUT_BATCH));

  let breaking = [];
  if (hasRange) {
    const rc = runJson("refactor_check.mjs", ["--range", scope[1], "--cwd", cwd]);
    if (rc && rc.ok) {
      const inv = rc.invariants || {};
      breaking = [...new Set([...(inv.public_signatures_changed || []), ...(inv.public_api_removed || [])])].sort();
    }
  }

  let highImpact = [];
  if (l2 && l2.ok) {
    const pub = (l2.symbols || []).filter((s) => s.exported && s.blast_radius > 0).sort((a, b) => b.blast_radius - a.blast_radius);
    highImpact = pub.slice(0, 20).map((s) => ({ symbol: s.name, file: s.file, blast_radius: s.blast_radius }));
  }

  return {
    compression: patterns.compression || {},
    patterns: patList.map((p) => ({ count: p.count, example_file: p.example_file, files: p.files, example_hunk: p.example_hunk })),
    unique, high_impact: highImpact, review_batches: batches,
    checks: { risk_flags: l1.risk_flags || [], impact_flags: (l2 || {}).impact_flags || [], breaking_changes: breaking },
  };
}

function parseArgs(argv) {
  const a = { range: null, staged: false, cwd: ".", json: false,
    maxBriefFiles: DEFAULT_MAX_FILES, maxBriefLines: DEFAULT_MAX_LINES,
    minBlast: DEFAULT_MIN_BLAST, largeFiles: DEFAULT_LARGE_FILES, force: null };
  for (let i = 0; i < argv.length; i++) {
    const x = argv[i];
    if (x === "--range") a.range = argv[++i];
    else if (x === "--staged") a.staged = true;
    else if (x === "--cwd") a.cwd = argv[++i];
    else if (x === "--json") a.json = true;
    else if (x === "--max-brief-files") a.maxBriefFiles = +argv[++i];
    else if (x === "--max-brief-lines") a.maxBriefLines = +argv[++i];
    else if (x === "--min-blast") a.minBlast = +argv[++i];
    else if (x === "--large-files") a.largeFiles = +argv[++i];
    else if (x === "--force") a.force = argv[++i];
  }
  return a;
}

function main() {
  const a = parseArgs(process.argv.slice(2));
  const cwd = path.resolve(a.cwd);
  const scope = a.staged ? ["--staged"] : (a.range ? ["--range", a.range] : []);

  const l1 = runJson("diff_summary.mjs", ["--cwd", cwd, "--compact", ...scope]);
  if (!l1) { process.stderr.write("error: could not read the diff\n"); process.exit(2); }
  const totals = l1.totals || {};
  const files = totals.files || 0;
  const lines = (totals.additions || 0) + (totals.deletions || 0);

  let l2cache;
  const getL2 = () => {
    if (l2cache === undefined) {
      const l1Path = path.join(tmpdir(), `crd-route-${process.pid}-${Date.now()}.json`);
      writeFileSync(l1Path, JSON.stringify(l1));
      try { l2cache = runJson("symbol_impact.mjs", ["--root", cwd, "--diff-json", l1Path, "--compact"]); }
      finally { try { unlinkSync(l1Path); } catch { /* */ } }
    }
    return l2cache;
  };

  const small = files <= a.maxBriefFiles && lines <= a.maxBriefLines;
  let mode, reason;
  if (a.force) { mode = a.force; reason = "forced"; }
  else if (small) { mode = "brief"; reason = `${files} file(s), ${lines} line(s) <= brief thresholds (${a.maxBriefFiles}/${a.maxBriefLines})`; }
  else if (files >= a.largeFiles) { mode = "full"; reason = `${files} file(s) >= ${a.largeFiles} -> full (ordering helps)`; }
  else {
    const l2 = getL2();
    let maxBlast = 0;
    if (l2 && l2.ok) maxBlast = Math.max(0, ...(l2.symbols || []).filter((s) => s.exported).map((s) => s.blast_radius));
    mode = maxBlast >= a.minBlast ? "full" : "brief";
    reason = `${files} file(s), ${lines} line(s), max blast radius ${maxBlast} -> ${mode} (full when blast >= ${a.minBlast})`;
  }

  if (mode === "brief") {
    if (a.json) {
      const payload = runJson("impact_brief.mjs", ["--cwd", cwd, "--json", ...scope]);
      process.stdout.write(JSON.stringify({ mode: "brief", reason, brief: payload }, null, 2) + "\n");
    } else {
      console.log(`mode: brief (${reason})`);
      process.stdout.write(run("impact_brief.mjs", ["--cwd", cwd, ...scope]));
    }
    return;
  }

  const l2 = getL2();
  const reviewOrder = (l2 || {}).review_order || l1.review_order || [];
  const out = {
    mode: "full", reason, totals,
    risk_flags: l1.risk_flags || [], review_order: reviewOrder,
    impact_flags: (l2 || {}).impact_flags || [], prioritized: (l2 || {}).prioritized || [],
  };
  if (files >= a.largeFiles) {
    const hasRange = !!a.range && !a.staged;
    const ls = buildLargeScale(cwd, scope, l1, l2, hasRange);
    out.large_scale = ls;
    if ((ls.patterns && ls.patterns.length) || (ls.unique && ls.unique.length)) {
      delete out.prioritized; delete out.review_order;
    }
  }

  if (a.json) { process.stdout.write(JSON.stringify(out, null, 2) + "\n"); return; }

  console.log(`mode: full map (${reason})`);
  if (out.risk_flags.length) console.log("risk: " + out.risk_flags.join(", "));
  if (out.impact_flags.length) console.log("impact: " + out.impact_flags.join(", "));
  const ls = out.large_scale;
  if (ls) {
    const c = ls.compression;
    console.log("large-scale toolkit:");
    console.log(`  patterns: ${c.changed_files || 0} changed files -> ${c.distinct_units || 0} distinct review units (${c.collapsed_files || 0} collapsed)`);
    ls.patterns.slice(0, 5).forEach((pat, i) => console.log(`    pattern ${i + 1}: ${pat.count} files like ${pat.example_file}`));
    if (ls.checks.breaking_changes.length) console.log("  BREAKING: " + ls.checks.breaking_changes.join(", "));
    console.log(`  fan-out: review ${ls.review_batches.length} batch(es) of <=${FANOUT_BATCH} units in isolated context`);
    if (ls.high_impact.length) {
      console.log("  high impact:");
      for (const h of ls.high_impact.slice(0, 10)) console.log(`    ${h.symbol} (${h.file}) — ${h.blast_radius} caller file(s)`);
    }
    return;
  }
  console.log("review in this order (highest priority first):");
  const list = out.prioritized.length ? out.prioritized.slice(0, 10) : reviewOrder.slice(0, 10).map((p) => ({ path: p }));
  for (const r of list) {
    const reasons = r.reasons && r.reasons.length ? "  — " + r.reasons.join("; ") : "";
    console.log(`  ${r.path}${reasons}`);
  }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main();
