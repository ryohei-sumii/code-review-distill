#!/usr/bin/env node
// One-shot impact brief (small-change path) — Node port of scripts/impact_brief.py.
// Orchestrates the sibling scripts and distills only the cross-repo signals.

import { execFileSync } from "node:child_process";
import { writeFileSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const JS = path.dirname(new URL(import.meta.url).pathname);
const TOP_IMPACT = 5;

function runJson(script, args) {
  let out;
  try { out = execFileSync("node", [path.join(JS, script), ...args], { encoding: "utf8", maxBuffer: 1 << 28 }); }
  catch (e) { out = e.stdout || ""; }
  try { return JSON.parse(out.trim()); } catch { return null; }
}
function runText(script, args) {
  try { return execFileSync("node", [path.join(JS, script), ...args], { encoding: "utf8", maxBuffer: 1 << 28 }); }
  catch (e) { return e.stdout || ""; }
}

function parseArgs(argv) {
  const a = { range: null, staged: false, cwd: ".", json: false };
  for (let i = 0; i < argv.length; i++) {
    const x = argv[i];
    if (x === "--range") a.range = argv[++i];
    else if (x === "--staged") a.staged = true;
    else if (x === "--cwd") a.cwd = argv[++i];
    else if (x === "--json") a.json = true;
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
  const flags = (l1.risk_flags || []).filter((f) => f === "code_changed_without_tests" || f === "contains_deletions");

  let highImpact = [];
  let reviewOrder = l1.review_order || [];
  const l1Path = path.join(tmpdir(), `crd-l1-${process.pid}-${Date.now()}.json`);
  writeFileSync(l1Path, JSON.stringify(l1));
  let l2;
  try { l2 = runJson("symbol_impact.mjs", ["--root", cwd, "--diff-json", l1Path, "--compact"]); }
  finally { try { unlinkSync(l1Path); } catch { /* */ } }
  if (l2 && l2.ok) {
    reviewOrder = l2.review_order || reviewOrder;
    const pub = (l2.symbols || []).filter((s) => s.exported).sort((x, y) => y.blast_radius - x.blast_radius);
    for (const s of pub.slice(0, TOP_IMPACT)) {
      if (s.blast_radius > 0) highImpact.push({ symbol: s.name, file: s.file, blast_radius: s.blast_radius });
    }
  }

  let breaking = [];
  if (a.range) {
    const rc = runJson("refactor_check.mjs", ["--range", a.range, "--cwd", cwd]);
    if (rc && rc.ok) {
      const inv = rc.invariants || {};
      breaking = [...new Set([...(inv.public_signatures_changed || []), ...(inv.public_api_removed || [])])].sort();
    }
  }

  const brief = {
    files: totals.files || 0,
    additions: totals.additions || 0,
    deletions: totals.deletions || 0,
    flags, breaking_changes: breaking, high_impact: highImpact,
    review_order: reviewOrder.slice(0, TOP_IMPACT),
  };

  if (a.json) { process.stdout.write(JSON.stringify(brief, null, 2) + "\n"); return; }

  const bits = [`${brief.files} file(s), +${brief.additions}/-${brief.deletions}`];
  if (flags.includes("code_changed_without_tests")) bits.push("no tests touched");
  if (breaking.length) bits.push("BREAKING: " + breaking.join(", "));
  console.log(bits.join(" · "));
  if (highImpact.length) {
    console.log("high impact:");
    for (const h of highImpact) console.log(`  ${h.symbol} (${h.file}) — ${h.blast_radius} caller file(s)`);
  }
  if (!breaking.length && !highImpact.length) console.log("no public-API impact or breaking changes detected");
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main();
