#!/usr/bin/env python3
"""Layer 2 (multi-language) blast-radius analyser.

Given the changed source files (from Layer 1 output or an explicit list), this:

  1. Extracts the symbols defined in each file (function / class / method /
     exported variable / type / interface) plus their signature, per language.
  2. Flags which of those are part of the public API.
       - TS/JS:  `export`-ed symbols.
       - Python: module-level names not prefixed with `_`.
       - Go:     identifiers starting with an upper-case letter.
  3. Estimates blast radius: how many *other* files import each public symbol
     from the file that defines it. This is import-resolved (not bare identifier
     matching), so same-name locals and cross-module collisions are excluded.

Supported languages: typescript, javascript, python, go.
Adding another is a small registry entry (see LANGS below) plus a per-language
`extract_*` walk. Layer 1 (diff_summary.py) needs no changes.

Output (stdout) JSON:

    {
      "ok": true,
      "languages": ["typescript", "python"],
      "analyzed_files": ["a.ts", ...],
      "symbols": [
        {"file","language","name","kind","exported","signature",
         "referenced_by":["x.ts",...],"blast_radius": N}
      ],
      "public_api_changes": ["add", "Foo", ...],
      "impact_flags": ["exported_symbol_widely_used", ...],
      # present only when --diff-json was given: Layer 1's order re-ranked by
      # folding in blast radius, with a transparent score breakdown + reasons.
      "review_order": ["src/api.ts", ...],
      "prioritized": [
        {"path","combined_score","l1_risk_score","impact_score",
         "public_api":[...],"reasons":[...]}
      ]
    }

Graceful fallback: if no supported source files are present, or none of the
required grammars are installed, prints a JSON note and exits 3 so the caller
can proceed with Layer 1 alone. Per-language grammar gaps are tolerated: a
language whose grammar is missing is skipped (recorded in `skipped_languages`),
not fatal.

Dependencies (optional, install only what you need):
    pip install tree-sitter \
        tree-sitter-typescript tree-sitter-python tree-sitter-go \
        --break-system-packages
"""

import argparse
import json
import os
import re
import sys

WIDE_USE_THRESHOLD = 3
# In --compact mode, cap each symbol's referenced_by list to this many entries;
# blast_radius still carries the true total count.
MAX_REFS_COMPACT = 5
# Weights for folding Layer 2 impact into Layer 1's per-file risk_score.
# Each external referrer of a changed public symbol adds REF_WEIGHT; any public
# API change in a file adds PUBLIC_API_BONUS even with zero current callers.
REF_WEIGHT = 5
PUBLIC_API_BONUS = 10
SKIP_DIRS = {".git", "node_modules", "dist", "build", "out", "vendor", ".next",
             "__pycache__", ".venv", "venv"}


def strip_empty(obj):
    """Recursively drop None / empty-list / empty-string values (compact mode).

    Booleans and numbers (incl. 0) are kept. Shared by the companion scripts.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            v = strip_empty(v)
            if v is None or v == [] or v == "":
                continue
            out[k] = v
        return out
    if isinstance(obj, list):
        return [strip_empty(v) for v in obj]
    return obj


def emit(result, compact):
    """Write JSON to stdout: minified+stripped when compact, else indent=2."""
    if compact:
        json.dump(strip_empty(result), sys.stdout, separators=(",", ":"))
    else:
        json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def fallback(note, exit_code=3, **extra):
    obj = {"ok": False, "note": note}
    obj.update(extra)
    json.dump(obj, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.exit(exit_code)


def node_text(src, node):
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def field(node, name):
    return node.child_by_field_name(name)


def _norm(s):
    """Collapse whitespace so signatures compare stably across formatting."""
    return re.sub(r"\s+", " ", s).strip()


def func_signature(src, node, ret_fields=("return_type",)):
    """Normalized "(params) -> ret" signature for a callable node, or ""."""
    params = field(node, "parameters")
    if params is None:
        return ""
    sig = _norm(node_text(src, params))
    for rf in ret_fields:
        r = field(node, rf)
        if r is not None:
            sig += " " + _norm(node_text(src, r))
            break
    return sig


# --- Per-language symbol extraction ---------------------------------------
# Each extractor takes (src: bytes, root_node) and returns a list of
# (kind: str, name: str, exported: bool, signature: str) tuples. signature is
# "" for non-callable kinds (class / type / const / ...).

def extract_ts(src, root):
    symbols = []

    def name_of(node):
        n = field(node, "name")
        return node_text(src, n) if n is not None else None

    def methods(class_node, class_name):
        body = field(class_node, "body")
        if body is None:
            return
        for m in body.named_children:
            if m.type in ("method_definition", "method_signature"):
                nm = field(m, "name")
                if nm is not None:
                    symbols.append(("method", "%s.%s" % (class_name, node_text(src, nm)),
                                    False, func_signature(src, m)))

    def visit(node, exported):
        t = node.type
        if t == "function_declaration":
            nm = name_of(node)
            if nm:
                symbols.append(("function", nm, exported, func_signature(src, node)))
        elif t in ("class_declaration", "abstract_class_declaration"):
            nm = name_of(node)
            if nm:
                symbols.append(("class", nm, exported, ""))
                methods(node, nm)
        elif t in ("lexical_declaration", "variable_declaration"):
            for decl in node.named_children:
                if decl.type == "variable_declarator":
                    nm_node = field(decl, "name")
                    if nm_node is not None and nm_node.type == "identifier":
                        val = field(decl, "value")
                        kind = "const"
                        sig = ""
                        if val is not None and val.type in (
                            "arrow_function", "function_expression", "function"
                        ):
                            kind = "function"
                            sig = func_signature(src, val)
                        symbols.append((kind, node_text(src, nm_node), exported, sig))
        elif t in ("interface_declaration", "type_alias_declaration", "enum_declaration"):
            nm = name_of(node)
            if nm:
                kind = {"interface_declaration": "interface",
                        "type_alias_declaration": "type",
                        "enum_declaration": "enum"}[t]
                symbols.append((kind, nm, exported, ""))

    for child in root.named_children:
        if child.type == "export_statement":
            decl = field(child, "declaration")
            if decl is not None:
                visit(decl, True)
            else:
                for c in child.named_children:
                    if c.type == "export_clause":
                        for spec in c.named_children:
                            if spec.type == "export_specifier":
                                nm = field(spec, "name")
                                if nm is not None:
                                    symbols.append(("reexport", node_text(src, nm), True, ""))
        else:
            visit(child, False)
    return symbols


def extract_python(src, root):
    symbols = []

    def is_public(name):
        return not name.startswith("_")

    def methods(class_node, class_name):
        body = field(class_node, "body")
        if body is None:
            return
        for m in body.named_children:
            if m.type == "function_definition":
                nm = field(m, "name")
                if nm is not None:
                    mname = node_text(src, nm)
                    symbols.append(("method", "%s.%s" % (class_name, mname),
                                    False, func_signature(src, m)))

    for child in root.named_children:
        t = child.type
        if t == "function_definition":
            nm = field(child, "name")
            if nm is not None:
                name = node_text(src, nm)
                symbols.append(("function", name, is_public(name), func_signature(src, child)))
        elif t == "decorated_definition":
            inner = field(child, "definition") or child.named_children[-1]
            if inner is not None and inner.type in ("function_definition", "class_definition"):
                nm = field(inner, "name")
                if nm is not None:
                    name = node_text(src, nm)
                    if inner.type == "function_definition":
                        symbols.append(("function", name, is_public(name),
                                        func_signature(src, inner)))
                    else:
                        symbols.append(("class", name, is_public(name), ""))
                        methods(inner, name)
        elif t == "class_definition":
            nm = field(child, "name")
            if nm is not None:
                name = node_text(src, nm)
                symbols.append(("class", name, is_public(name), ""))
                methods(child, name)
        elif t in ("expression_statement",):
            # module-level assignment: NAME = ...
            for c in child.named_children:
                if c.type == "assignment":
                    left = field(c, "left")
                    if left is not None and left.type == "identifier":
                        name = node_text(src, left)
                        symbols.append(("const", name, is_public(name), ""))
    return symbols


def extract_go(src, root):
    symbols = []

    def is_exported(name):
        return bool(name) and name[0].isupper()

    for child in root.named_children:
        t = child.type
        if t == "function_declaration":
            nm = field(child, "name")
            if nm is not None:
                name = node_text(src, nm)
                symbols.append(("function", name, is_exported(name),
                                func_signature(src, child, ret_fields=("result",))))
        elif t == "method_declaration":
            nm = field(child, "name")
            if nm is not None:
                name = node_text(src, nm)
                # try to qualify with the receiver type for readability
                recv = field(child, "receiver")
                recv_name = ""
                if recv is not None:
                    rt = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node_text(src, recv))
                    if rt:
                        recv_name = rt[-1] + "."
                symbols.append(("method", recv_name + name, is_exported(name),
                                func_signature(src, child, ret_fields=("result",))))
        elif t == "type_declaration":
            for spec in child.named_children:
                if spec.type == "type_spec":
                    nm = field(spec, "name")
                    if nm is not None:
                        name = node_text(src, nm)
                        symbols.append(("type", name, is_exported(name), ""))
        elif t in ("var_declaration", "const_declaration"):
            for spec in child.named_children:
                if spec.type in ("var_spec", "const_spec"):
                    nm = field(spec, "name")
                    if nm is not None:
                        name = node_text(src, nm)
                        kind = "var" if t == "var_declaration" else "const"
                        symbols.append((kind, name, is_exported(name), ""))
    return symbols


# --- Language registry -----------------------------------------------------

LANGS = {
    "typescript": {
        "exts": {".ts", ".mts", ".cts"},
        "extract": extract_ts,
        "loader": ("tree_sitter_typescript", "language_typescript"),
    },
    "tsx": {
        "exts": {".tsx"},
        "extract": extract_ts,
        "loader": ("tree_sitter_typescript", "language_tsx"),
    },
    "javascript": {
        # tree-sitter-typescript parses JS too; reuse the TS grammar/walk.
        "exts": {".js", ".jsx", ".mjs", ".cjs"},
        "extract": extract_ts,
        "loader": ("tree_sitter_typescript", "language_tsx"),
    },
    "python": {
        "exts": {".py", ".pyi"},
        "extract": extract_python,
        "loader": ("tree_sitter_python", "language"),
    },
    "go": {
        "exts": {".go"},
        "extract": extract_go,
        "loader": ("tree_sitter_go", "language"),
    },
}

# Languages that share a reference namespace for blast-radius search.
# (TS/JS/TSX reference each other; py and go are separate.)
REF_FAMILIES = {
    "typescript": "tsjs", "tsx": "tsjs", "javascript": "tsjs",
    "python": "python", "go": "go",
}

EXT_TO_LANG = {}
for _lang, _cfg in LANGS.items():
    for _e in _cfg["exts"]:
        EXT_TO_LANG[_e] = _lang


def ext_of(path):
    return os.path.splitext(path)[1].lower()


def lang_of(path):
    return EXT_TO_LANG.get(ext_of(path))


def load_parser(lang):
    """Return (Parser, None) or (None, reason)."""
    try:
        from tree_sitter import Parser, Language
    except Exception as e:
        return None, "tree-sitter core not available: %s" % e
    module_name, fn_name = LANGS[lang]["loader"]
    try:
        mod = __import__(module_name)
        language_obj = Language(getattr(mod, fn_name)())
    except Exception as e:
        return None, "grammar for %s unavailable: %s" % (lang, e)
    try:
        return Parser(language_obj), None
    except TypeError:
        par = Parser()
        par.set_language(language_obj)
        return par, None


def iter_source_files(root, exts):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if ext_of(fn) in exts:
                yield os.path.join(dirpath, fn)


# --- Import-aware reference resolution -------------------------------------
# Blast radius counts files that actually *import* a changed symbol from the
# file that defines it — not files that merely mention the identifier. This is a
# precision upgrade over bare identifier matching: it drops same-name locals and
# cross-module false positives. Resolution is best-effort and regex-based;
# dynamic imports, default-export aliasing, and deep re-export chains are out of
# scope (precision-biased: such cases are missed rather than over-counted).

TS_RESOLVE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")
# bindings span at most one brace group and never cross a `{`, `}` or `;`, so a
# semicolon-less `export { x }` can't be glued onto a following import's module.
_TS_FROM_RE = re.compile(
    r'\b(?:import|export)\b(?P<bindings>[^;{}]*(?:\{[^}]*\})?[^;{}]*?)\bfrom\s*'
    r'["\'](?P<mod>[^"\']+)["\']')
_TS_REQUIRE_RE = re.compile(r'\brequire\(\s*["\'](?P<mod>[^"\']+)["\']\s*\)')
_PY_FROM_RE = re.compile(
    r'^[ \t]*from[ \t]+(?P<dots>\.*)(?P<mod>[\w.]*)[ \t]+import[ \t]+(?P<names>.+)$', re.M)
_PY_IMPORT_RE = re.compile(r'^[ \t]*import[ \t]+(?P<body>[\w. ,]+?(?:[ \t]+as[ \t]+\w+)?)[ \t]*$', re.M)
_GO_BLOCK_RE = re.compile(r'\bimport\s*\(\s*(?P<body>[\s\S]*?)\s*\)')
_GO_SINGLE_RE = re.compile(r'\bimport\s+(?P<entry>(?:[A-Za-z_.]\w*\s+)?"[^"]+")')
_GO_ENTRY_RE = re.compile(r'(?:(?P<alias>[A-Za-z_.]\w*)\s+)?"(?P<path>[^"]+)"')


def _uses_qualified(text, ns, sym):
    return re.search(r'(?<![\w.])%s\s*\.\s*%s\b' % (re.escape(ns), re.escape(sym)),
                     text) is not None


def _ts_bindings(binding_text):
    """(named:set, namespaces:set, star:bool) from an import binding clause."""
    named, namespaces = set(), set()
    star = False
    for m in re.finditer(r'\*\s+as\s+([A-Za-z_$][\w$]*)', binding_text):
        namespaces.add(m.group(1))
    brace = re.search(r'\{([^}]*)\}', binding_text)
    if brace:
        for part in brace.group(1).split(','):
            part = part.strip()
            if not part:
                continue
            name = re.sub(r'^type\s+', '', part.split(' as ')[0].strip())
            if name:
                named.add(name)
    elif not namespaces and re.search(r'(^|[\s,])\*(\s|$)', binding_text):
        star = True  # export * from "mod"
    return named, namespaces, star


def parse_imports(text, lang):
    """Return import records for a candidate file, by language family."""
    records = []
    if lang in ("typescript", "tsx", "javascript"):
        for m in _TS_FROM_RE.finditer(text):
            named, ns, star = _ts_bindings(m.group("bindings"))
            records.append({"module": m.group("mod"), "named": named,
                            "namespaces": ns, "star": star, "kind": "ts"})
        for m in _TS_REQUIRE_RE.finditer(text):
            records.append({"module": m.group("mod"), "named": set(),
                            "namespaces": set(), "star": True, "kind": "ts"})
    elif lang == "python":
        for m in _PY_FROM_RE.finditer(text):
            names_part = m.group("names").strip().strip("()")
            star = "*" in names_part
            named = set()
            for part in names_part.split(","):
                nm = part.strip().split(" as ")[0].strip()
                if nm and nm != "*":
                    named.add(nm)
            records.append({"dots": m.group("dots"), "module": m.group("mod"),
                            "named": named, "star": star, "kind": "py_from"})
        for m in _PY_IMPORT_RE.finditer(text):
            for entry in m.group("body").split(","):
                entry = entry.strip()
                if not entry:
                    continue
                parts = entry.split(" as ")
                mod = parts[0].strip()
                alias = parts[1].strip() if len(parts) > 1 else mod
                records.append({"dots": "", "module": mod, "alias": alias,
                                "kind": "py_import"})
    elif lang == "go":
        entries = []
        for blk in _GO_BLOCK_RE.finditer(text):
            entries.extend(_GO_ENTRY_RE.finditer(blk.group("body")))
        for s in _GO_SINGLE_RE.finditer(text):
            em = _GO_ENTRY_RE.search(s.group("entry"))
            if em:
                entries.append(em)
        for m in entries:
            path = m.group("path")
            alias = m.group("alias") or path.rstrip("/").split("/")[-1]
            records.append({"module": path, "alias": alias, "kind": "go"})
    return records


def resolve_ts_module(spec, importer_abs):
    if not (spec.startswith("./") or spec.startswith("../") or spec == "."):
        return None  # bare/external specifier — not a local file
    base = os.path.normpath(os.path.join(os.path.dirname(importer_abs), spec))
    cands = [base + e for e in TS_RESOLVE_EXTS]
    cands += [os.path.join(base, "index" + e) for e in TS_RESOLVE_EXTS]
    cands.append(base)
    for c in cands:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return os.path.abspath(base)


def resolve_py_module(dots, mod, importer_abs, root):
    if dots:
        pkg = os.path.dirname(importer_abs)
        for _ in range(len(dots) - 1):
            pkg = os.path.dirname(pkg)
        base = os.path.join(pkg, *mod.split(".")) if mod else pkg
    else:
        base = os.path.join(root, *mod.split("."))
    for c in (base + ".py", os.path.join(base, "__init__.py"), base + ".pyi"):
        if os.path.isfile(c):
            return os.path.abspath(c)
    return os.path.abspath(base + ".py")


def file_references(text, cand_abs, def_abs, sym_name, lang, root, imports_cache):
    """True if candidate file actually imports `sym_name` from `def_abs`."""
    recs = imports_cache.get(cand_abs)
    if recs is None:
        recs = parse_imports(text, lang)
        imports_cache[cand_abs] = recs

    if lang in ("typescript", "tsx", "javascript"):
        for r in recs:
            if resolve_ts_module(r["module"], cand_abs) != def_abs:
                continue
            if sym_name in r["named"] or r["star"]:
                return True
            for ns in r["namespaces"]:
                if _uses_qualified(text, ns, sym_name):
                    return True
        return False
    if lang == "python":
        for r in recs:
            if r["kind"] == "py_from":
                if resolve_py_module(r["dots"], r["module"], cand_abs, root) != def_abs:
                    continue
                if sym_name in r["named"] or r["star"]:
                    return True
            else:  # py_import: import mod [as alias]; usage mod.Sym
                if resolve_py_module("", r["module"], cand_abs, root) != def_abs:
                    continue
                if _uses_qualified(text, r["alias"], sym_name):
                    return True
        return False
    if lang == "go":
        def_dir = os.path.dirname(def_abs)
        # Same package (same directory): the symbol is used unqualified, no
        # import. Package-level names are unique within a package, so a bare
        # occurrence is a real reference.
        if os.path.dirname(cand_abs) == def_dir:
            return re.search(r'(?<![\w.])%s\b' % re.escape(sym_name), text) is not None
        def_pkg = os.path.basename(def_dir)
        for r in recs:
            if os.path.basename(r["module"].rstrip("/")) != def_pkg:
                continue
            if _uses_qualified(text, r["alias"], sym_name):
                return True
        return False
    return False


def integrate_priority(l1, symbols):
    """Re-rank Layer 1 files by folding in Layer 2 blast radius.

    Returns (review_order, prioritized). `prioritized` shows the score
    breakdown and human reasons so the ranking is never a black box. Files Layer
    2 never saw (non-code, deleted, unsupported) keep their Layer 1 score.
    """
    syms_by_file = {}
    for s in symbols:
        syms_by_file.setdefault(s["file"], []).append(s)

    ranked = []
    for f in l1.get("files", []):
        path = f.get("path")
        if not path:
            continue
        l1_score = f.get("risk_score", 0)
        public = [s for s in syms_by_file.get(path, []) if s["exported"]]
        impact = sum(s["blast_radius"] for s in public)
        combined = l1_score + REF_WEIGHT * impact + (PUBLIC_API_BONUS if public else 0)

        reasons = []
        for flag in (f.get("risk_flags") or []):
            reasons.append("L1: %s" % flag)
        for s in sorted(public, key=lambda s: -s["blast_radius"]):
            if s["blast_radius"] > 0:
                reasons.append("public %s '%s' used by %d file(s)"
                               % (s["kind"], s["name"], s["blast_radius"]))
            else:
                reasons.append("public %s '%s' changed (no external refs found)"
                               % (s["kind"], s["name"]))

        ranked.append({
            "path": path,
            "combined_score": combined,
            "l1_risk_score": l1_score,
            "impact_score": impact,
            "public_api": [s["name"] for s in public],
            "reasons": reasons,
        })

    ranked.sort(key=lambda r: (-r["combined_score"], r["path"]))
    return [r["path"] for r in ranked], ranked


def main():
    p = argparse.ArgumentParser(description="Layer 2 multi-language blast-radius analyser")
    p.add_argument("--root", default=".", help="repository root")
    p.add_argument("--diff-json", help="Layer 1 JSON output (to read changed files)")
    p.add_argument("--files", nargs="*", help="explicit list of changed files")
    p.add_argument("--compact", action="store_true",
                   help="minified JSON, empty fields dropped, referenced_by capped "
                        "(blast_radius keeps the full count) — ~half the size")
    p.add_argument("--max-refs", type=int, default=MAX_REFS_COMPACT,
                   help="in --compact, cap referenced_by to this many entries "
                        "(0 = unlimited); blast_radius is always the exact count")
    args = p.parse_args()

    root = os.path.abspath(args.root)

    changed = []
    l1 = None
    if args.diff_json:
        try:
            with open(args.diff_json, "r", encoding="utf-8") as fh:
                l1 = json.load(fh)
            for f in l1.get("files", []):
                if f.get("status") != "deleted" and f.get("path"):
                    changed.append(f["path"])
        except (OSError, ValueError) as e:
            fallback("could not read --diff-json: %s" % e)
    if args.files:
        changed.extend(args.files)

    # Keep only supported source files, grouped by language.
    supported = [c for c in changed if lang_of(c)]
    supported = sorted(set(supported))
    if not supported:
        fallback("no supported source files in change set; Layer 1 is sufficient")

    langs_present = sorted({lang_of(c) for c in supported})

    # Load parsers lazily, tolerating per-language grammar gaps.
    parsers = {}
    skipped = {}
    for lang in langs_present:
        par, err = load_parser(lang)
        if par is None:
            skipped[lang] = err
        else:
            parsers[lang] = par

    if not parsers:
        fallback("no required grammars installed (%s); install tree-sitter "
                 "language packages to enable Layer 2" % "; ".join(skipped.values()),
                 skipped_languages=skipped)

    # Extract symbols from each changed, analyzable file.
    symbols = []
    analyzed = []
    used_langs = set()
    for rel in supported:
        lang = lang_of(rel)
        parser = parsers.get(lang)
        if parser is None:
            continue
        abspath = rel if os.path.isabs(rel) else os.path.join(root, rel)
        if not os.path.exists(abspath):
            continue
        try:
            with open(abspath, "rb") as fh:
                src = fh.read()
        except OSError:
            continue
        tree = parser.parse(src)
        extractor = LANGS[lang]["extract"]
        for kind, name, exported, signature in extractor(src, tree.root_node):
            sym = {
                "file": rel, "language": lang, "name": name, "kind": kind,
                "exported": exported, "referenced_by": [], "blast_radius": 0,
            }
            if signature:
                sym["signature"] = signature
            symbols.append(sym)
        analyzed.append(rel)
        used_langs.add(lang)

    if not analyzed:
        fallback("no analyzable source files found on disk; Layer 1 is sufficient",
                 skipped_languages=skipped)

    # Read candidate files per ref-family (same-language only). Blast radius is
    # import-resolved: a candidate counts only if it imports the symbol from the
    # file that defines it (see file_references), not if it merely mentions it.
    family_exts = {}
    for lang in used_langs:
        fam = REF_FAMILIES[lang]
        family_exts.setdefault(fam, set()).update(LANGS[lang]["exts"])

    changed_abs = {os.path.abspath(c if os.path.isabs(c) else os.path.join(root, c))
                   for c in supported}

    family_texts = {}
    for fam, exts in family_exts.items():
        texts = {}
        for fp in iter_source_files(root, exts):
            ap = os.path.abspath(fp)
            if ap in changed_abs:
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    texts[ap] = fh.read()
            except OSError:
                continue
        family_texts[fam] = texts

    imports_cache = {}
    for sym in symbols:
        if not sym["exported"]:
            continue
        lang = sym["language"]
        fam = REF_FAMILIES[lang]
        def_abs = os.path.abspath(os.path.join(root, sym["file"]))
        search_name = sym["name"].split(".")[-1]
        refs = []
        for ap, text in family_texts.get(fam, {}).items():
            if search_name not in text:
                continue  # cheap pre-filter before the import check
            if file_references(text, ap, def_abs, search_name, lang, root, imports_cache):
                refs.append(os.path.relpath(ap, root))
        sym["referenced_by"] = sorted(refs)
        sym["blast_radius"] = len(refs)

    public_api_changes = sorted({s["name"] for s in symbols if s["exported"]})

    impact_flags = []
    if any(s["exported"] and s["blast_radius"] >= WIDE_USE_THRESHOLD for s in symbols):
        impact_flags.append("exported_symbol_widely_used")
    if any(s["exported"] and s["blast_radius"] > 0 for s in symbols):
        impact_flags.append("public_api_referenced_externally")
    if public_api_changes:
        impact_flags.append("public_api_changed")

    if args.compact and args.max_refs > 0:
        # Keep the true count in blast_radius; trim the (potentially huge) list
        # and mark it explicitly so a partial list is never mistaken for whole.
        for sym in symbols:
            if len(sym["referenced_by"]) > args.max_refs:
                sym["referenced_by"] = sym["referenced_by"][:args.max_refs]
                sym["referenced_by_truncated"] = True

    result = {
        "ok": True,
        "languages": sorted(used_langs),
        "analyzed_files": analyzed,
        "symbols": symbols,
        "public_api_changes": public_api_changes,
        "impact_flags": impact_flags,
    }

    # Fold blast radius back into Layer 1's ordering when we have the L1 map.
    # This is the impact-aware review order — prefer it over Layer 1's.
    if l1 is not None and l1.get("files"):
        review_order, prioritized = integrate_priority(l1, symbols)
        result["review_order"] = review_order
        result["prioritized"] = prioritized

    if skipped:
        result["skipped_languages"] = skipped
    emit(result, args.compact)


if __name__ == "__main__":
    main()
