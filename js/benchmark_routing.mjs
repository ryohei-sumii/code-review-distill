#!/usr/bin/env node
// Routing benchmark — Node port of evals/benchmark_routing.py.

import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const JS = path.dirname(new URL(import.meta.url).pathname);
const SIZES = [1, 2, 5, 12, 30, 60];
const toks = (n) => Math.floor((n + 3) / 4);
const git = (repo, ...a) => execFileSync("git", a, { cwd: repo, stdio: "ignore" });

function build(repo, nFiles, callers) {
  mkdirSync(path.join(repo, "src"), { recursive: true });
  git(repo, "init"); git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t");
  writeFileSync(path.join(repo, "src", "m0.ts"), "export function shared(a: number): number { return a; }\n");
  for (let i = 1; i < nFiles; i++) writeFileSync(path.join(repo, "src", `m${i}.ts`), `export function fn${i}(a: number): number { return a; }\n`);
  for (let c = 0; c < callers; c++) writeFileSync(path.join(repo, "src", `caller${c}.ts`), `import { shared } from "./m0"; export const v${c} = shared(${c});\n`);
  git(repo, "add", "-A"); git(repo, "commit", "-m", "init");
  writeFileSync(path.join(repo, "src", "m0.ts"), "export function shared(a: number, b: number): number { return a + b; }\n");
  for (let i = 1; i < nFiles; i++) writeFileSync(path.join(repo, "src", `m${i}.ts`), `export function fn${i}(a: number): number { return a + 1; }\n`);
  git(repo, "add", "-A"); git(repo, "commit", "-m", "change");
}

function runOut(script, args) {
  try { return execFileSync("node", [path.join(JS, script), ...args], { encoding: "utf8", maxBuffer: 1 << 28 }); }
  catch (e) { return e.stdout || ""; }
}

function measure(nFiles, callers) {
  const repo = mkdtempSync(path.join(tmpdir(), "crd-bm-"));
  try {
    build(repo, nFiles, callers);
    const raw = execFileSync("git", ["diff", "HEAD~1..HEAD"], { cwd: repo, encoding: "utf8", maxBuffer: 1 << 28 });
    let callerBytes = 0;
    for (let c = 0; c < callers; c++) callerBytes += readFileSync(path.join(repo, "src", `caller${c}.ts`), "utf8").length;
    const route = runOut("route.mjs", ["--range", "HEAD~1..HEAD", "--cwd", repo, "--json"]);
    let mode = "?"; try { mode = JSON.parse(route).mode || "?"; } catch { /* */ }
    return { n_files: nFiles, A_raw: toks(raw.length), B_raw_plus_callers: toks(raw.length + callerBytes), C_route: toks(route.length), mode };
  } finally { rmSync(repo, { recursive: true, force: true }); }
}

function main() {
  const argv = process.argv.slice(2);
  let callers = 10, sizes = SIZES;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--callers") callers = +argv[++i];
    else if (argv[i] === "--sizes") sizes = argv[++i].split(",").map(Number);
  }
  const rows = sizes.map((n) => measure(n, callers));
  console.log(`routing benefit — estimated tokens (~4 chars/token), ${callers} callers of the changed public symbol\n`);
  console.log(`  ${"files".padEnd(7)} | ${"A raw".padEnd(10)} | ${"B raw+callers".padEnd(22)} | ${"C route".padEnd(14)} | mode`);
  console.log("  " + "-".repeat(70));
  for (const r of rows) {
    console.log(`  ${String(r.n_files).padEnd(7)} | ${String(r.A_raw).padEnd(10)} | ${String(r.B_raw_plus_callers).padEnd(22)} | ${String(r.C_route).padEnd(14)} | ${r.mode}`);
  }
  console.log("\n  C vs B (the saving when you want impact analysis):");
  for (const r of rows) {
    const save = r.B_raw_plus_callers ? 100 * (1 - r.C_route / r.B_raw_plus_callers) : 0;
    console.log(`    ${String(r.n_files).padStart(2)} files: route=${r.C_route} tok vs raw+callers=${r.B_raw_plus_callers} tok  -> ${Math.round(save)}% less`);
  }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main();
