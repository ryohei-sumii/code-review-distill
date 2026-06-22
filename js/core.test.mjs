// Node-native tests (no deps): run with `node --test js/`.
// Drives the .mjs scripts through their real CLI, like the Python suite.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const JS = path.dirname(fileURLToPath(import.meta.url));

function git(repo, ...args) {
  execFileSync("git", args, { cwd: repo, stdio: "ignore" });
}
function initRepo() {
  const repo = mkdtempSync(path.join(tmpdir(), "crd-"));
  git(repo, "init");
  git(repo, "config", "user.email", "t@t");
  git(repo, "config", "user.name", "t");
  return repo;
}
function write(repo, rel, content) {
  const f = path.join(repo, rel);
  mkdirSync(path.dirname(f), { recursive: true });
  writeFileSync(f, content);
}
function commit(repo, msg) {
  git(repo, "add", "-A");
  git(repo, "commit", "-m", msg);
}
function run(script, ...args) {
  const out = execFileSync("node", [path.join(JS, script), ...args], { encoding: "utf8" });
  return JSON.parse(out);
}

test("layer1: code changed without tests + language", () => {
  const repo = initRepo();
  write(repo, "src/m.ts", "export function add(a,b){return a+b;}\n");
  commit(repo, "init");
  write(repo, "src/m.ts", "export function add(a,b){return a-b;}\n");
  commit(repo, "change");
  const d = run("diff_summary.mjs", "--range", "HEAD~1..HEAD", "--cwd", repo);
  assert.ok(d.risk_flags.includes("code_changed_without_tests"));
  assert.equal(d.totals.files, 1);
  assert.equal(d.files[0].language, "typescript");
  assert.ok("risk_score" in d.files[0]);
});

test("layer1: rename and delete ordering", () => {
  const repo = initRepo();
  write(repo, "a.py", "x = 1\n");
  write(repo, "b.py", "y = 2\n");
  commit(repo, "init");
  git(repo, "mv", "a.py", "renamed.py");
  git(repo, "rm", "b.py");
  commit(repo, "rn+del");
  const d = run("diff_summary.mjs", "--range", "HEAD~1..HEAD", "--cwd", repo);
  const byPath = Object.fromEntries(d.files.map((f) => [f.path, f.status]));
  assert.equal(byPath["renamed.py"], "renamed");
  assert.equal(byPath["b.py"], "deleted");
  assert.ok(d.risk_flags.includes("contains_deletions"));
  assert.equal(d.review_order[0], "b.py");
});

test("layer1: compact drops empty fields, keeps numbers", () => {
  const repo = initRepo();
  write(repo, "a.py", "x = 1\n");
  commit(repo, "init");
  write(repo, "a.py", "x = 2\n");
  commit(repo, "change");
  const v = run("diff_summary.mjs", "--range", "HEAD~1..HEAD", "--cwd", repo);
  const c = run("diff_summary.mjs", "--range", "HEAD~1..HEAD", "--cwd", repo, "--compact");
  assert.deepEqual(c.totals, v.totals);
  for (const f of c.files) assert.notDeepEqual(f.risk_flags ?? ["x"], []);
});

test("patterns: collapse codemod, keep unique, cover all (incl rename)", () => {
  const repo = initRepo();
  for (let i = 0; i < 20; i++) write(repo, `m${i}.ts`, `export function fn${i}(a){ return a; }\n`);
  write(repo, "special.ts", "export function special(a){ return a; }\n");
  write(repo, "old.ts", "export const z = 1;\n");
  commit(repo, "init");
  for (let i = 0; i < 20; i++) write(repo, `m${i}.ts`, `export function fn${i}(a){ return a + 1; }\n`);
  write(repo, "special.ts", "export function special(a){ return a * 2 - 1; }\n");
  git(repo, "mv", "old.ts", "new.ts"); // pure rename
  commit(repo, "change");
  const d = run("diff_patterns.mjs", "--range", "HEAD~1..HEAD", "--cwd", repo, "--json");
  assert.ok(d.patterns.some((p) => p.count === 20));
  assert.ok(d.unique.includes("special.ts"));
  // lossless coverage: every changed file (incl the pure rename) appears
  const covered = new Set(d.unique);
  for (const p of d.patterns) for (const f of p.files) covered.add(f);
  assert.ok(covered.has("new.ts"));
  assert.equal(covered.size, d.compression.changed_files);
});

test("patterns: do not over-merge structurally different changes", () => {
  const repo = initRepo();
  for (let i = 0; i < 6; i++) write(repo, `m${i}.ts`, `export function fn${i}(a){ return a; }\n`);
  commit(repo, "init");
  for (let i = 0; i < 6; i++) {
    write(repo, `m${i}.ts`, i % 2 === 0
      ? `export function fn${i}(a){ return a + 1; }\n`
      : `export function fn${i}(a){ if (a) return a; return 0; }\n`);
  }
  commit(repo, "change");
  const d = run("diff_patterns.mjs", "--range", "HEAD~1..HEAD", "--cwd", repo, "--json");
  assert.equal(d.patterns.length, 2);
});
