#!/usr/bin/env node
// Layer 1 (language-agnostic) code-review distiller — Node port.
//
// Pure Node: no dependencies, no build step. Runs anywhere Node is present
// (which is anywhere Claude Code runs). Mirrors scripts/diff_summary.py output.
//
// Usage:
//   node js/diff_summary.mjs --range main..HEAD --cwd <repo>
//   node js/diff_summary.mjs --staged --cwd <repo>
//   node js/diff_summary.mjs --file some.diff
//   add --compact for minified JSON with empty fields dropped.

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";

const LARGE_HUNK_LINES = 80;
const LARGE_FILE_LINES = 300;

const TEST_PATTERNS = [
  /(^|\/)tests?\//,
  /(^|\/)__tests__\//,
  /(^|\/)spec\//,
  /\.(test|spec)\.[^/]+$/,
  /_test\.[^/]+$/,
  /(^|\/)test_[^/]+$/,
  /\.feature$/,
];

const GENERATED_PATTERNS = [
  /\.min\.(js|css)$/,
  /(^|\/)(dist|build|out|vendor|node_modules)\//,
  /package-lock\.json$/,
  /yarn\.lock$/,
  /pnpm-lock\.yaml$/,
  /Cargo\.lock$/,
  /poetry\.lock$/,
  /go\.sum$/,
  /\.pb\.go$/,
  /_pb2\.py$/,
  /\.snap$/,
  /(^|\/)generated\//,
  /\.generated\.[^/]+$/,
];

const EXT_LANG = {
  ts: "typescript", tsx: "typescript",
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  py: "python", go: "go", rs: "rust", java: "java", kt: "kotlin",
  rb: "ruby", php: "php", c: "c", h: "c",
  cc: "cpp", cpp: "cpp", cxx: "cpp", hpp: "cpp",
  cs: "csharp", swift: "swift", scala: "scala",
  sh: "shell", bash: "shell",
  json: "json", yaml: "yaml", yml: "yaml", md: "markdown", sql: "sql",
};

const CODE_LANGS = new Set([
  "typescript", "javascript", "python", "go", "rust", "java", "kotlin",
  "ruby", "php", "c", "cpp", "csharp", "swift", "scala", "shell",
]);

const matchesAny = (p, pats) => pats.some((re) => re.test(p));

function guessLanguage(p) {
  const ext = path.extname(p || "").replace(/^\./, "").toLowerCase();
  return EXT_LANG[ext] || "unknown";
}

const HUNK_RE =
  /^@@ -(?<os>\d+)(?:,(?<ol>\d+))? \+(?<ns>\d+)(?:,(?<nl>\d+))? @@(?<ctx>.*)$/;

function stripPrefix(p) {
  return p.startsWith("a/") || p.startsWith("b/") ? p.slice(2) : p;
}

function newFile() {
  return {
    path: null, old_path: null, status: "modified",
    additions: 0, deletions: 0, is_binary: false, hunks: [], _a: null, _b: null,
  };
}

function parseDiff(text) {
  const files = [];
  let cur = null;
  let inHunk = false;
  for (const line of text.split("\n")) {
    if (line.startsWith("diff --git ")) {
      if (cur) files.push(cur);
      cur = newFile();
      inHunk = false;
      const m = line.match(/^diff --git a\/(.+?) b\/(.+)$/);
      if (m) { cur._a = m[1]; cur._b = m[2]; }
      continue;
    }
    if (!cur) continue;
    if (line.startsWith("old mode") || line.startsWith("new mode")) continue;
    if (line.startsWith("new file mode")) { cur.status = "added"; continue; }
    if (line.startsWith("deleted file mode")) { cur.status = "deleted"; continue; }
    if (line.startsWith("rename from ")) { cur.old_path = line.slice(12); cur.status = "renamed"; continue; }
    if (line.startsWith("rename to ")) { cur.path = line.slice(10); cur.status = "renamed"; continue; }
    if (line.startsWith("copy from ") || line.startsWith("copy to ")) continue;
    if (line.startsWith("similarity index") || line.startsWith("dissimilarity index")) continue;
    if (line.startsWith("index ")) continue;
    if (line.startsWith("Binary files") || line.startsWith("GIT binary patch")) { cur.is_binary = true; continue; }
    if (line.startsWith("--- ")) {
      const p = line.slice(4);
      if (p !== "/dev/null") cur.old_path = stripPrefix(p);
      continue;
    }
    if (line.startsWith("+++ ")) {
      const p = line.slice(4);
      if (p !== "/dev/null") cur.path = stripPrefix(p);
      continue;
    }
    const m = line.match(HUNK_RE);
    if (m) {
      cur.hunks.push({
        old_start: +m.groups.os, old_lines: m.groups.ol ? +m.groups.ol : 1,
        new_start: +m.groups.ns, new_lines: m.groups.nl ? +m.groups.nl : 1,
        header: m.groups.ctx.trim(), added: 0, deleted: 0,
      });
      inHunk = true;
      continue;
    }
    if (inHunk && cur.hunks.length) {
      const h = cur.hunks[cur.hunks.length - 1];
      if (line.startsWith("+")) { h.added++; cur.additions++; }
      else if (line.startsWith("-")) { h.deleted++; cur.deletions++; }
    }
  }
  if (cur) files.push(cur);
  return files.map(finalizeFile);
}

function finalizeFile(f) {
  const p = f.path || f._b || f.old_path || f._a;
  const oldPath = f.old_path;
  delete f._a; delete f._b;
  f.path = p;
  if (f.status !== "renamed" && oldPath === p) f.old_path = null;
  f.language = guessLanguage(p || "");
  f.is_test = matchesAny(p || "", TEST_PATTERNS);
  f.is_generated = matchesAny(p || "", GENERATED_PATTERNS);
  f.risk_flags = computeFileRisk(f);
  return f;
}

function computeFileRisk(f) {
  const flags = [];
  if (f.status === "deleted") flags.push("file_deleted");
  if (f.status === "renamed") flags.push("file_renamed");
  if (f.is_binary) flags.push("binary_change");
  const total = f.additions + f.deletions;
  if (total >= LARGE_FILE_LINES) flags.push("large_file_change");
  if (f.hunks.some((h) => h.added + h.deleted >= LARGE_HUNK_LINES)) flags.push("large_hunk");
  if (f.is_generated && !f.is_test) flags.push("generated_file");
  return flags;
}

function fileRiskScore(f) {
  const weights = {
    file_deleted: 30, large_file_change: 25, large_hunk: 15,
    file_renamed: 10, binary_change: 5,
  };
  let score = 0;
  for (const flag of f.risk_flags) score += weights[flag] || 0;
  score += f.additions + f.deletions;
  if (f.is_generated) score -= 100;
  if (f.is_test) score -= 20;
  return score;
}

function computeRepoRisk(files) {
  const flags = [];
  const codeFiles = files.filter((f) => CODE_LANGS.has(f.language) && !f.is_generated && !f.is_test);
  const testFiles = files.filter((f) => f.is_test);
  if (codeFiles.length && !testFiles.length) flags.push("code_changed_without_tests");
  if (files.some((f) => f.status === "deleted")) flags.push("contains_deletions");
  if (files.length >= 20) flags.push("large_changeset");
  return flags;
}

function stripEmpty(obj) {
  if (Array.isArray(obj)) return obj.map(stripEmpty);
  if (obj && typeof obj === "object") {
    const out = {};
    for (const [k, v] of Object.entries(obj)) {
      const sv = stripEmpty(v);
      if (sv === null || (Array.isArray(sv) && sv.length === 0) || sv === "") continue;
      out[k] = sv;
    }
    return out;
  }
  return obj;
}

function emit(result, compact) {
  if (compact) process.stdout.write(JSON.stringify(stripEmpty(result)) + "\n");
  else process.stdout.write(JSON.stringify(result, null, 2) + "\n");
}

function parseArgs(argv) {
  const a = { cwd: ".", range: null, staged: false, file: null, compact: false };
  for (let i = 0; i < argv.length; i++) {
    const x = argv[i];
    if (x === "--range") a.range = argv[++i];
    else if (x === "--staged") a.staged = true;
    else if (x === "--file") a.file = argv[++i];
    else if (x === "--cwd") a.cwd = argv[++i];
    else if (x === "--compact") a.compact = true;
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

function main() {
  const a = parseArgs(process.argv.slice(2));
  let text, source;
  if (a.file) { text = readFileSync(a.file, "utf8"); source = "file"; }
  else { text = runGitDiff(a); source = a.staged ? "staged" : "range"; }

  const files = parseDiff(text);
  for (const f of files) f.risk_score = fileRiskScore(f);
  const ordered = [...files].sort((x, y) => y.risk_score - x.risk_score);

  const result = {
    source,
    totals: {
      files: files.length,
      additions: files.reduce((s, f) => s + f.additions, 0),
      deletions: files.reduce((s, f) => s + f.deletions, 0),
      hunks: files.reduce((s, f) => s + f.hunks.length, 0),
    },
    files,
    risk_flags: computeRepoRisk(files),
    review_order: ordered.map((f) => f.path),
  };
  if (source === "range" && a.range) result.range = a.range;
  emit(result, a.compact);
}

main();
