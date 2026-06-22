#!/usr/bin/env node
// Layer 2 (multi-language) blast-radius analyser — Node port.
// AST via web-tree-sitter (WASM grammars; no native build). Mirrors
// scripts/symbol_impact.py output.

import Parser from "web-tree-sitter";
import { createRequire } from "node:module";
import { readFileSync, existsSync, statSync, readdirSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
export const WASM_DIR = path.join(path.dirname(require.resolve("tree-sitter-wasms/package.json")), "out");

const WIDE_USE_THRESHOLD = 3;
const MAX_REFS_COMPACT = 5;
const REF_WEIGHT = 5;
const PUBLIC_API_BONUS = 10;
const SKIP_DIRS = new Set([".git", "node_modules", "dist", "build", "out", "vendor",
  ".next", "__pycache__", ".venv", "venv"]);

const norm = (s) => s.replace(/\s+/g, " ").trim();

function funcSignature(node, retFields = ["return_type"]) {
  const params = node.childForFieldName("parameters");
  if (!params) return "";
  let sig = norm(params.text);
  for (const rf of retFields) {
    const r = node.childForFieldName(rf);
    if (r) { sig += " " + norm(r.text); break; }
  }
  return sig;
}

// --- per-language extractors: [kind, name, exported, signature] ---

function extractTs(root) {
  const symbols = [];
  const nameOf = (n) => { const x = n.childForFieldName("name"); return x ? x.text : null; };
  function methods(classNode, className) {
    const body = classNode.childForFieldName("body");
    if (!body) return;
    for (const m of body.namedChildren) {
      if (m.type === "method_definition" || m.type === "method_signature") {
        const nm = m.childForFieldName("name");
        if (nm) symbols.push(["method", `${className}.${nm.text}`, false, funcSignature(m)]);
      }
    }
  }
  function visit(node, exported) {
    const t = node.type;
    if (t === "function_declaration") {
      const nm = nameOf(node);
      if (nm) symbols.push(["function", nm, exported, funcSignature(node)]);
    } else if (t === "class_declaration" || t === "abstract_class_declaration") {
      const nm = nameOf(node);
      if (nm) { symbols.push(["class", nm, exported, ""]); methods(node, nm); }
    } else if (t === "lexical_declaration" || t === "variable_declaration") {
      for (const decl of node.namedChildren) {
        if (decl.type !== "variable_declarator") continue;
        const nmNode = decl.childForFieldName("name");
        if (nmNode && nmNode.type === "identifier") {
          const val = decl.childForFieldName("value");
          let kind = "const", sig = "";
          if (val && ["arrow_function", "function_expression", "function"].includes(val.type)) {
            kind = "function"; sig = funcSignature(val);
          }
          symbols.push([kind, nmNode.text, exported, sig]);
        }
      }
    } else if (["interface_declaration", "type_alias_declaration", "enum_declaration"].includes(t)) {
      const nm = nameOf(node);
      if (nm) {
        const kind = { interface_declaration: "interface", type_alias_declaration: "type", enum_declaration: "enum" }[t];
        symbols.push([kind, nm, exported, ""]);
      }
    }
  }
  for (const child of root.namedChildren) {
    if (child.type === "export_statement") {
      const decl = child.childForFieldName("declaration");
      if (decl) { visit(decl, true); continue; }
      for (const c of child.namedChildren) {
        if (c.type === "export_clause") {
          for (const spec of c.namedChildren) {
            if (spec.type === "export_specifier") {
              const nm = spec.childForFieldName("name");
              if (nm) symbols.push(["reexport", nm.text, true, ""]);
            }
          }
        }
      }
    } else visit(child, false);
  }
  return symbols;
}

function extractPython(root) {
  const symbols = [];
  const isPublic = (n) => !n.startsWith("_");
  function methods(classNode, className) {
    const body = classNode.childForFieldName("body");
    if (!body) return;
    for (const m of body.namedChildren) {
      if (m.type === "function_definition") {
        const nm = m.childForFieldName("name");
        if (nm) symbols.push(["method", `${className}.${nm.text}`, false, funcSignature(m)]);
      }
    }
  }
  for (const child of root.namedChildren) {
    const t = child.type;
    if (t === "function_definition") {
      const nm = child.childForFieldName("name");
      if (nm) symbols.push(["function", nm.text, isPublic(nm.text), funcSignature(child)]);
    } else if (t === "decorated_definition") {
      const inner = child.childForFieldName("definition") || child.namedChildren[child.namedChildren.length - 1];
      if (inner && (inner.type === "function_definition" || inner.type === "class_definition")) {
        const nm = inner.childForFieldName("name");
        if (nm) {
          if (inner.type === "function_definition") symbols.push(["function", nm.text, isPublic(nm.text), funcSignature(inner)]);
          else { symbols.push(["class", nm.text, isPublic(nm.text), ""]); methods(inner, nm.text); }
        }
      }
    } else if (t === "class_definition") {
      const nm = child.childForFieldName("name");
      if (nm) { symbols.push(["class", nm.text, isPublic(nm.text), ""]); methods(child, nm.text); }
    } else if (t === "expression_statement") {
      for (const c of child.namedChildren) {
        if (c.type === "assignment") {
          const left = c.childForFieldName("left");
          if (left && left.type === "identifier") symbols.push(["const", left.text, isPublic(left.text), ""]);
        }
      }
    }
  }
  return symbols;
}

function extractGo(root) {
  const symbols = [];
  const isExported = (n) => !!n && /[A-Z]/.test(n[0]);
  for (const child of root.namedChildren) {
    const t = child.type;
    if (t === "function_declaration") {
      const nm = child.childForFieldName("name");
      if (nm) symbols.push(["function", nm.text, isExported(nm.text), funcSignature(child, ["result"])]);
    } else if (t === "method_declaration") {
      const nm = child.childForFieldName("name");
      if (nm) {
        const recv = child.childForFieldName("receiver");
        let recvName = "";
        if (recv) { const rt = recv.text.match(/[A-Za-z_][A-Za-z0-9_]*/g); if (rt) recvName = rt[rt.length - 1] + "."; }
        symbols.push(["method", recvName + nm.text, isExported(nm.text), funcSignature(child, ["result"])]);
      }
    } else if (t === "type_declaration") {
      for (const spec of child.namedChildren) {
        if (spec.type === "type_spec") {
          const nm = spec.childForFieldName("name");
          if (nm) symbols.push(["type", nm.text, isExported(nm.text), ""]);
        }
      }
    } else if (t === "var_declaration" || t === "const_declaration") {
      for (const spec of child.namedChildren) {
        if (spec.type === "var_spec" || spec.type === "const_spec") {
          const nm = spec.childForFieldName("name");
          if (nm) symbols.push([t === "var_declaration" ? "var" : "const", nm.text, isExported(nm.text), ""]);
        }
      }
    }
  }
  return symbols;
}

export const LANGS = {
  typescript: { exts: [".ts", ".mts", ".cts"], wasm: "tree-sitter-typescript.wasm", extract: extractTs },
  tsx: { exts: [".tsx"], wasm: "tree-sitter-tsx.wasm", extract: extractTs },
  javascript: { exts: [".js", ".jsx", ".mjs", ".cjs"], wasm: "tree-sitter-tsx.wasm", extract: extractTs },
  python: { exts: [".py", ".pyi"], wasm: "tree-sitter-python.wasm", extract: extractPython },
  go: { exts: [".go"], wasm: "tree-sitter-go.wasm", extract: extractGo },
};
export const REF_FAMILIES = { typescript: "tsjs", tsx: "tsjs", javascript: "tsjs", python: "python", go: "go" };
const EXT_TO_LANG = {};
for (const [lang, cfg] of Object.entries(LANGS)) for (const e of cfg.exts) EXT_TO_LANG[e] = lang;
export const extOf = (p) => path.extname(p).toLowerCase();
export const langOf = (p) => EXT_TO_LANG[extOf(p)] || null;

// Load a tree-sitter parser per language; returns [{lang: Parser}, {lang: err}].
export async function loadParsers(langs) {
  await Parser.init();
  const parsers = {}, skipped = {};
  for (const lang of langs) {
    try {
      const L = await Parser.Language.load(path.join(WASM_DIR, LANGS[lang].wasm));
      const p = new Parser(); p.setLanguage(L); parsers[lang] = p;
    } catch (e) { skipped[lang] = String(e.message || e); }
  }
  return [parsers, skipped];
}

// --- import-aware reference resolution ---

const TS_RESOLVE_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs"];
const TS_FROM_RE = /\b(?:import|export)\b([^;{}]*(?:\{[^}]*\})?[^;{}]*?)\bfrom\s*["']([^"']+)["']/g;
const TS_REQUIRE_RE = /\brequire\(\s*["']([^"']+)["']\s*\)/g;
const PY_FROM_RE = /^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+(.+)$/gm;
const PY_IMPORT_RE = /^[ \t]*import[ \t]+([\w. ,]+?(?:[ \t]+as[ \t]+\w+)?)[ \t]*$/gm;
const GO_BLOCK_RE = /\bimport\s*\(\s*([\s\S]*?)\s*\)/g;
const GO_SINGLE_RE = /\bimport\s+((?:[A-Za-z_.]\w*\s+)?"[^"]+")/g;
const GO_ENTRY_RE = /(?:([A-Za-z_.]\w*)\s+)?"([^"]+)"/;
const GO_ENTRY_RE_G = /(?:([A-Za-z_.]\w*)\s+)?"([^"]+)"/g;

const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
function usesQualified(text, ns, sym) {
  return new RegExp(`(?<![\\w.])${reEsc(ns)}\\s*\\.\\s*${reEsc(sym)}\\b`).test(text);
}

function tsBindings(bindingText) {
  const named = new Set(), namespaces = new Set();
  let star = false;
  for (const m of bindingText.matchAll(/\*\s+as\s+([A-Za-z_$][\w$]*)/g)) namespaces.add(m[1]);
  const brace = bindingText.match(/\{([^}]*)\}/);
  if (brace) {
    for (let part of brace[1].split(",")) {
      part = part.trim();
      if (!part) continue;
      const name = part.split(" as ")[0].trim().replace(/^type\s+/, "");
      if (name) named.add(name);
    }
  } else if (namespaces.size === 0 && /(^|[\s,])\*(\s|$)/.test(bindingText)) {
    star = true;
  }
  return { named, namespaces, star };
}

function parseImports(text, lang) {
  const records = [];
  if (lang === "typescript" || lang === "tsx" || lang === "javascript") {
    for (const m of text.matchAll(TS_FROM_RE)) {
      const { named, namespaces, star } = tsBindings(m[1]);
      records.push({ module: m[2], named, namespaces, star });
    }
    for (const m of text.matchAll(TS_REQUIRE_RE)) {
      records.push({ module: m[1], named: new Set(), namespaces: new Set(), star: true });
    }
  } else if (lang === "python") {
    for (const m of text.matchAll(PY_FROM_RE)) {
      const namesPart = m[3].trim().replace(/^\(|\)$/g, "");
      const star = namesPart.includes("*");
      const named = new Set();
      for (const part of namesPart.split(",")) {
        const nm = part.trim().split(" as ")[0].trim();
        if (nm && nm !== "*") named.add(nm);
      }
      records.push({ kind: "py_from", dots: m[1], module: m[2], named, star });
    }
    for (const m of text.matchAll(PY_IMPORT_RE)) {
      for (let entry of m[1].split(",")) {
        entry = entry.trim();
        if (!entry) continue;
        const parts = entry.split(" as ");
        const mod = parts[0].trim();
        records.push({ kind: "py_import", dots: "", module: mod, alias: parts[1] ? parts[1].trim() : mod });
      }
    }
  } else if (lang === "go") {
    const entries = [];
    for (const blk of text.matchAll(GO_BLOCK_RE)) {
      for (const em of blk[1].matchAll(GO_ENTRY_RE_G)) entries.push(em);
    }
    for (const s of text.matchAll(GO_SINGLE_RE)) {
      const em = s[1].match(GO_ENTRY_RE);
      if (em) entries.push(em);
    }
    for (const m of entries) {
      const p = m[2];
      records.push({ module: p, alias: m[1] || p.replace(/\/+$/, "").split("/").pop() });
    }
  }
  return records;
}

function resolveTsModule(spec, importerAbs) {
  if (!(spec.startsWith("./") || spec.startsWith("../") || spec === ".")) return null;
  const base = path.normalize(path.join(path.dirname(importerAbs), spec));
  const cands = [...TS_RESOLVE_EXTS.map((e) => base + e),
    ...TS_RESOLVE_EXTS.map((e) => path.join(base, "index" + e)), base];
  for (const c of cands) if (existsSync(c) && statSync(c).isFile()) return path.resolve(c);
  return path.resolve(base);
}

function resolvePyModule(dots, mod, importerAbs, root) {
  let base;
  if (dots) {
    let pkg = path.dirname(importerAbs);
    for (let i = 0; i < dots.length - 1; i++) pkg = path.dirname(pkg);
    base = mod ? path.join(pkg, ...mod.split(".")) : pkg;
  } else {
    base = path.join(root, ...mod.split("."));
  }
  for (const c of [base + ".py", path.join(base, "__init__.py"), base + ".pyi"]) {
    if (existsSync(c) && statSync(c).isFile()) return path.resolve(c);
  }
  return path.resolve(base + ".py");
}

function fileReferences(text, candAbs, defAbs, symName, lang, root, cache) {
  let recs = cache.get(candAbs);
  if (!recs) { recs = parseImports(text, lang); cache.set(candAbs, recs); }
  if (lang === "typescript" || lang === "tsx" || lang === "javascript") {
    for (const r of recs) {
      if (resolveTsModule(r.module, candAbs) !== defAbs) continue;
      if (r.named.has(symName) || r.star) return true;
      for (const ns of r.namespaces) if (usesQualified(text, ns, symName)) return true;
    }
    return false;
  }
  if (lang === "python") {
    for (const r of recs) {
      if (r.kind === "py_from") {
        if (resolvePyModule(r.dots, r.module, candAbs, root) !== defAbs) continue;
        if (r.named.has(symName) || r.star) return true;
      } else {
        if (resolvePyModule("", r.module, candAbs, root) !== defAbs) continue;
        if (usesQualified(text, r.alias, symName)) return true;
      }
    }
    return false;
  }
  if (lang === "go") {
    const defDir = path.dirname(defAbs);
    if (path.dirname(candAbs) === defDir) return new RegExp(`(?<![\\w.])${reEsc(symName)}\\b`).test(text);
    const defPkg = path.basename(defDir);
    for (const r of recs) {
      if (path.basename(r.module.replace(/\/+$/, "")) !== defPkg) continue;
      if (usesQualified(text, r.alias, symName)) return true;
    }
    return false;
  }
  return false;
}

function* iterSourceFiles(root, exts) {
  const stack = [root];
  while (stack.length) {
    const dir = stack.pop();
    let entries;
    try { entries = readdirSync(dir, { withFileTypes: true }); } catch { continue; }
    for (const ent of entries) {
      if (ent.isDirectory()) { if (!SKIP_DIRS.has(ent.name)) stack.push(path.join(dir, ent.name)); }
      else if (exts.includes(extOf(ent.name))) yield path.join(dir, ent.name);
    }
  }
}

function integratePriority(l1, symbols) {
  const symsByFile = {};
  for (const s of symbols) (symsByFile[s.file] ||= []).push(s);
  const ranked = [];
  for (const f of l1.files || []) {
    const p = f.path;
    if (!p) continue;
    const l1Score = f.risk_score || 0;
    const pub = (symsByFile[p] || []).filter((s) => s.exported);
    const impact = pub.reduce((s, x) => s + x.blast_radius, 0);
    const combined = l1Score + REF_WEIGHT * impact + (pub.length ? PUBLIC_API_BONUS : 0);
    const reasons = [];
    for (const flag of f.risk_flags || []) reasons.push("L1: " + flag);
    for (const s of [...pub].sort((a, b) => b.blast_radius - a.blast_radius)) {
      if (s.blast_radius > 0) reasons.push(`public ${s.kind} '${s.name}' used by ${s.blast_radius} file(s)`);
      else reasons.push(`public ${s.kind} '${s.name}' changed (no external refs found)`);
    }
    ranked.push({ path: p, combined_score: combined, l1_risk_score: l1Score, impact_score: impact,
      public_api: pub.map((s) => s.name), reasons });
  }
  ranked.sort((a, b) => (b.combined_score - a.combined_score) || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  return [ranked.map((r) => r.path), ranked];
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
  process.stdout.write((compact ? JSON.stringify(stripEmpty(result)) : JSON.stringify(result, null, 2)) + "\n");
}
function fallback(note, extra = {}) {
  process.stdout.write(JSON.stringify({ ok: false, note, ...extra }, null, 2) + "\n");
  process.exit(3);
}

function parseArgs(argv) {
  const a = { root: ".", diffJson: null, files: [], compact: false, maxRefs: MAX_REFS_COMPACT };
  for (let i = 0; i < argv.length; i++) {
    const x = argv[i];
    if (x === "--root") a.root = argv[++i];
    else if (x === "--diff-json") a.diffJson = argv[++i];
    else if (x === "--files") { while (i + 1 < argv.length && !argv[i + 1].startsWith("--")) a.files.push(argv[++i]); }
    else if (x === "--compact") a.compact = true;
    else if (x === "--max-refs") a.maxRefs = +argv[++i];
  }
  return a;
}

async function main() {
  const a = parseArgs(process.argv.slice(2));
  const root = path.resolve(a.root);

  let changed = [];
  let l1 = null;
  if (a.diffJson) {
    try {
      l1 = JSON.parse(readFileSync(a.diffJson, "utf8"));
      for (const f of l1.files || []) if (f.status !== "deleted" && f.path) changed.push(f.path);
    } catch (e) { fallback("could not read --diff-json: " + e.message); }
  }
  changed.push(...a.files);

  const supported = [...new Set(changed.filter((c) => langOf(c)))].sort();
  if (!supported.length) fallback("no supported source files in change set; Layer 1 is sufficient");

  const langsPresent = [...new Set(supported.map(langOf))].sort();
  const [parsers, skipped] = await loadParsers(langsPresent);
  if (!Object.keys(parsers).length) fallback("no grammars loadable", { skipped_languages: skipped });

  const symbols = [];
  const analyzed = [];
  const usedLangs = new Set();
  for (const rel of supported) {
    const lang = langOf(rel);
    const parser = parsers[lang];
    if (!parser) continue;
    const abspath = path.isAbsolute(rel) ? rel : path.join(root, rel);
    if (!existsSync(abspath)) continue;
    let src;
    try { src = readFileSync(abspath, "utf8"); } catch { continue; }
    const tree = parser.parse(src);
    for (const [kind, name, exported, signature] of LANGS[lang].extract(tree.rootNode)) {
      const sym = { file: rel, language: lang, name, kind, exported, referenced_by: [], blast_radius: 0 };
      if (signature) sym.signature = signature;
      symbols.push(sym);
    }
    analyzed.push(rel);
    usedLangs.add(lang);
  }
  if (!analyzed.length) fallback("no analyzable source files found on disk; Layer 1 is sufficient", { skipped_languages: skipped });

  const familyExts = {};
  for (const lang of usedLangs) {
    const fam = REF_FAMILIES[lang];
    familyExts[fam] = familyExts[fam] || new Set();
    for (const e of LANGS[lang].exts) familyExts[fam].add(e);
  }
  const changedAbs = new Set(supported.map((c) => path.resolve(path.isAbsolute(c) ? c : path.join(root, c))));

  const familyTexts = {};
  for (const [fam, exts] of Object.entries(familyExts)) {
    const texts = {};
    for (const fp of iterSourceFiles(root, [...exts])) {
      const ap = path.resolve(fp);
      if (changedAbs.has(ap)) continue;
      try { texts[ap] = readFileSync(fp, "utf8"); } catch { /* skip */ }
    }
    familyTexts[fam] = texts;
  }

  const importsCache = new Map();
  for (const sym of symbols) {
    if (!sym.exported) continue;
    const lang = sym.language;
    const fam = REF_FAMILIES[lang];
    const defAbs = path.resolve(path.join(root, sym.file));
    const searchName = sym.name.split(".").pop();
    const refs = [];
    for (const [ap, text] of Object.entries(familyTexts[fam] || {})) {
      if (!text.includes(searchName)) continue;
      if (fileReferences(text, ap, defAbs, searchName, lang, root, importsCache)) refs.push(path.relative(root, ap));
    }
    sym.referenced_by = refs.sort();
    sym.blast_radius = refs.length;
  }

  const publicApiChanges = [...new Set(symbols.filter((s) => s.exported).map((s) => s.name))].sort();
  const impactFlags = [];
  if (symbols.some((s) => s.exported && s.blast_radius >= WIDE_USE_THRESHOLD)) impactFlags.push("exported_symbol_widely_used");
  if (symbols.some((s) => s.exported && s.blast_radius > 0)) impactFlags.push("public_api_referenced_externally");
  if (publicApiChanges.length) impactFlags.push("public_api_changed");

  if (a.compact && a.maxRefs > 0) {
    for (const sym of symbols) {
      if (sym.referenced_by.length > a.maxRefs) {
        sym.referenced_by = sym.referenced_by.slice(0, a.maxRefs);
        sym.referenced_by_truncated = true;
      }
    }
  }

  const result = {
    ok: true,
    languages: [...usedLangs].sort(),
    analyzed_files: analyzed,
    symbols,
    public_api_changes: publicApiChanges,
    impact_flags: impactFlags,
  };
  if (l1 && (l1.files || []).length) {
    const [reviewOrder, prioritized] = integratePriority(l1, symbols);
    result.review_order = reviewOrder;
    result.prioritized = prioritized;
  }
  if (Object.keys(skipped).length) result.skipped_languages = skipped;
  emit(result, a.compact);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main();
