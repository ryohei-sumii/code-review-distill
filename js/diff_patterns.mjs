#!/usr/bin/env node
// Pattern compression for large diffs (lossless) — Node port.
// Pure Node, no dependencies. Mirrors scripts/diff_patterns.py.
//
// Usage:
//   node js/diff_patterns.mjs --range main..HEAD --cwd <repo>
//   node js/diff_patterns.mjs --file some.diff [--json] [--min-count N]

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";

const IDENT = /[A-Za-z_$][A-Za-z0-9_$]*/g;
const NUM = /\d+/g;

function parseArgs(argv) {
  const a = { cwd: ".", range: null, staged: false, file: null, json: false, minCount: 2 };
  for (let i = 0; i < argv.length; i++) {
    const x = argv[i];
    if (x === "--range") a.range = argv[++i];
    else if (x === "--staged") a.staged = true;
    else if (x === "--file") a.file = argv[++i];
    else if (x === "--cwd") a.cwd = argv[++i];
    else if (x === "--json") a.json = true;
    else if (x === "--min-count") a.minCount = +argv[++i];
  }
  return a;
}

function runGitDiff(a) {
  const args = ["diff", "--no-color", "-M"];
  if (a.staged) args.push("--cached");
  if (a.range) args.push(a.range);
  try {
    return execFileSync("git", args, { cwd: a.cwd, encoding: "utf8", maxBuffer: 1 << 28 });
  } catch (e) {
    process.stderr.write("error: git diff failed: " + (e.stderr || e.message) + "\n");
    process.exit(2);
  }
}

function splitPerFile(diffText) {
  const files = {};
  let cur = null;
  let buf = [];
  for (const line of diffText.split("\n")) {
    if (line.startsWith("diff --git ")) {
      if (cur !== null) files[cur] = buf.join("\n");
      buf = [];
      const m = line.match(/^diff --git a\/.+ b\/(.+)$/);
      cur = m ? m[1] : null;
    } else if (cur !== null && (line[0] === "+" || line[0] === "-") &&
               !line.startsWith("+++") && !line.startsWith("---")) {
      buf.push(line);
    }
  }
  if (cur !== null) files[cur] = buf.join("\n");
  return files;
}

function normalize(block) {
  const out = [];
  for (const line of block.split("\n")) {
    if (!line) continue;
    const sign = line[0];
    let body = line.slice(1).replace(IDENT, "W").replace(NUM, "N");
    body = body.replace(/\s+/g, " ").trim();
    out.push(sign + body);
  }
  return out.join("\n");
}

function main() {
  const a = parseArgs(process.argv.slice(2));
  const diff = a.file ? readFileSync(a.file, "utf8") : runGitDiff(a);
  const perFile = splitPerFile(diff);

  const groups = new Map();
  const noContent = [];
  for (const [path, block] of Object.entries(perFile)) {
    if (!block.trim()) { noContent.push(path); continue; }
    const key = createHash("sha1").update(normalize(block)).digest("hex");
    if (!groups.has(key)) groups.set(key, { files: [], example_path: path, example_block: block });
    groups.get(key).files.push(path);
  }

  const patterns = [];
  let unique = [];
  for (const g of groups.values()) {
    if (g.files.length >= a.minCount) {
      patterns.push({
        count: g.files.length,
        files: [...g.files].sort(),
        example_file: g.example_path,
        example_hunk: g.example_block,
      });
    } else {
      unique.push(...g.files);
    }
  }
  unique.push(...noContent);
  patterns.sort((x, y) => y.count - x.count);
  unique = unique.sort();

  const collapsed = patterns.reduce((s, p) => s + p.count, 0);
  const result = {
    files: Object.keys(perFile).length,
    patterns,
    unique,
    compression: {
      changed_files: Object.keys(perFile).length,
      distinct_units: patterns.length + unique.length,
      collapsed_files: collapsed,
    },
  };

  if (a.json) { process.stdout.write(JSON.stringify(result, null, 2) + "\n"); return; }

  const c = result.compression;
  console.log(`${c.changed_files} changed files -> ${c.distinct_units} distinct units ` +
    `(${c.collapsed_files} collapsed into ${patterns.length} patterns)`);
  patterns.forEach((pat, i) => {
    console.log(`\npattern ${i + 1}: ${pat.count} files share this change (e.g. ${pat.example_file})`);
    for (const line of pat.example_hunk.split("\n").slice(0, 6)) console.log("    " + line);
  });
  if (unique.length) {
    console.log(`\n${unique.length} unique change(s) to review individually:`);
    for (const u of unique.slice(0, 20)) console.log("    " + u);
  }
}

main();
