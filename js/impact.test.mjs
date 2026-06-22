// Node tests for the AST layer (symbol_impact.mjs). Needs the WASM grammars
// (web-tree-sitter + tree-sitter-wasms, installed via `npm install`).
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const JS = path.dirname(fileURLToPath(import.meta.url));
const git = (repo, ...a) => execFileSync("git", a, { cwd: repo, stdio: "ignore" });
function initRepo() {
  const repo = mkdtempSync(path.join(tmpdir(), "crd-si-"));
  git(repo, "init"); git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t");
  return repo;
}
function write(repo, rel, c) { const f = path.join(repo, rel); mkdirSync(path.dirname(f), { recursive: true }); writeFileSync(f, c); }
function commit(repo, m) { git(repo, "add", "-A"); git(repo, "commit", "-m", m); }
function run(...args) {
  return JSON.parse(execFileSync("node", [path.join(JS, "symbol_impact.mjs"), ...args], { encoding: "utf8" }));
}

test("blast radius is import-resolved (excludes shadow + other module)", () => {
  const repo = initRepo();
  write(repo, "math.ts", "export function add(a,b){return a+b;}\n");
  write(repo, "other.ts", "export function add(a){return a;}\n");
  write(repo, "good_named.ts", 'import {add} from "./math"; export const a = add(1,2);\n');
  write(repo, "good_ns.ts", 'import * as m from "./math"; export const b = m.add(1,2);\n');
  write(repo, "shadow.ts", "function add(x){return x;} export const c = add(5);\n");
  write(repo, "other_user.ts", 'import {add} from "./other"; export const d = add(7);\n');
  commit(repo, "init");
  const d = run("--root", repo, "--files", "math.ts");
  const add = d.symbols.find((s) => s.name === "add");
  assert.equal(add.blast_radius, 2);
  assert.deepEqual([...add.referenced_by].sort(), ["good_named.ts", "good_ns.ts"]);
});

test("captures signature and public-api flags", () => {
  const repo = initRepo();
  write(repo, "api.ts", "export function f(a: number, b: number): number { return a; }\n");
  commit(repo, "init");
  const d = run("--root", repo, "--files", "api.ts");
  const f = d.symbols.find((s) => s.name === "f");
  assert.ok(f.signature.includes("a: number"));
  assert.ok(d.impact_flags.includes("public_api_changed"));
});

test("graceful fallback on unsupported files (exit 3)", () => {
  const repo = initRepo();
  write(repo, "notes.txt", "hello\n");
  commit(repo, "init");
  let code = 0, out = "";
  try { out = execFileSync("node", [path.join(JS, "symbol_impact.mjs"), "--root", repo, "--files", "notes.txt"], { encoding: "utf8" }); }
  catch (e) { code = e.status; out = e.stdout; }
  assert.equal(code, 3);
  assert.equal(JSON.parse(out).ok, false);
});
