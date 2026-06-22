// Node tests for the orchestration + flow scripts. Needs the WASM grammars.
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, mkdirSync, cpSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const JS = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.dirname(JS);
const git = (repo, ...a) => execFileSync("git", a, { cwd: repo, stdio: "ignore" });
function initRepo() {
  const repo = mkdtempSync(path.join(tmpdir(), "crd-orch-"));
  git(repo, "init"); git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t");
  return repo;
}
function write(repo, rel, c) { const f = path.join(repo, rel); mkdirSync(path.dirname(f), { recursive: true }); writeFileSync(f, c); }
function commit(repo, m) { git(repo, "add", "-A"); git(repo, "commit", "-m", m); }
function runJson(script, ...args) {
  let out; try { out = execFileSync("node", [path.join(JS, script), ...args], { encoding: "utf8" }); } catch (e) { out = e.stdout; }
  return JSON.parse(out);
}

test("refactor_check flags signature breaking change", () => {
  const repo = initRepo();
  write(repo, "api.ts", "export function p(a: number, b: number): number { return a+b; }\nexport function s(x: number): number { return x; }\n");
  commit(repo, "init");
  write(repo, "api.ts", "export function p(b: number, a: number, c: number): string { return ''; }\nexport function s(x: number): number { return x; }\n");
  commit(repo, "change");
  const d = runJson("refactor_check.mjs", "--range", "HEAD~1..HEAD", "--cwd", repo);
  assert.equal(d.invariants.public_api_preserved, true);
  assert.equal(d.invariants.signatures_preserved, false);
  assert.ok(d.flags.includes("public_signature_changed"));
});

test("route picks brief for small, full for many files", () => {
  const small = initRepo();
  write(small, "a.py", "x = 1\n"); commit(small, "init");
  write(small, "a.py", "x = 2\n"); commit(small, "change");
  assert.equal(runJson("route.mjs", "--range", "HEAD~1..HEAD", "--cwd", small, "--json").mode, "brief");

  const big = initRepo();
  for (let i = 0; i < 26; i++) write(big, `m${i}.py`, "x = 1\n");
  commit(big, "init");
  for (let i = 0; i < 26; i++) write(big, `m${i}.py`, "x = 2\n");
  commit(big, "change");
  const d = runJson("route.mjs", "--range", "HEAD~1..HEAD", "--cwd", big, "--json");
  assert.equal(d.mode, "full");
  assert.ok("large_scale" in d);
  assert.ok(!("review_order" in d)); // dropped at large scale (patterns cover all)
});

test("impact_brief surfaces blast radius + breaking on a small change", () => {
  const repo = initRepo();
  write(repo, "api.ts", "export function shared(a){return a;}\n");
  for (let i = 0; i < 4; i++) write(repo, `c${i}.ts`, `import {shared} from "./api"; export const v${i} = shared(${i});\n`);
  commit(repo, "init");
  write(repo, "api.ts", "export function shared(a, b){return a + b;}\n");
  commit(repo, "change");
  const d = runJson("impact_brief.mjs", "--range", "HEAD~1..HEAD", "--cwd", repo, "--json");
  assert.ok(d.breaking_changes.some((b) => b.includes("shared")));
  assert.equal(d.high_impact.find((h) => h.symbol === "shared").blast_radius, 4);
});

test("flow_map builds an internal call graph", () => {
  const repo = initRepo();
  write(repo, "app.ts", "export function main(){ a(); }\nfunction a(){ b(); }\nfunction b(){}\n");
  const g = runJson("flow_map.mjs", "--files", path.join(repo, "app.ts"), "--root", repo, "--json");
  assert.ok(g.nodes.includes("main") && g.nodes.includes("a") && g.nodes.includes("b"));
  assert.ok(g.edges.some((e) => e.from === "a" && e.to === "b"));
});

test("orchestration works when the install path contains a space", () => {
  // sibling-script resolution must use fileURLToPath, not URL.pathname (which
  // percent-encodes spaces and breaks `node js/<sibling>.mjs`).
  const spaced = mkdtempSync(path.join(tmpdir(), "crd has space-"));
  cpSync(JS, path.join(spaced, "js"), { recursive: true });
  symlinkSync(path.join(REPO, "node_modules"), path.join(spaced, "node_modules"));
  const repo = initRepo();
  write(repo, "a.ts", "export function f(a){return a;}\n");
  commit(repo, "init");
  write(repo, "a.ts", "export function f(a,b){return a+b;}\n");
  commit(repo, "change");
  const out = execFileSync("node", [path.join(spaced, "js", "route.mjs"), "--range", "HEAD~1..HEAD", "--cwd", repo, "--json"], { encoding: "utf8" });
  assert.equal(JSON.parse(out).mode, "brief");
});
