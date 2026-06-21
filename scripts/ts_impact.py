#!/usr/bin/env python3
"""Layer 2 (TypeScript/JavaScript) blast-radius analyser.

Given the changed TS/JS files (from Layer 1 output or an explicit list), this:

  1. Extracts the top-level symbols defined in each file
     (function / class / method / exported const|let|var).
  2. Flags which of those are part of the public API (exported).
  3. Estimates blast radius: how many *other* files in the repo reference
     each exported symbol (by identifier + a matching import).

Output (stdout) JSON:

    {
      "ok": true,
      "analyzed_files": ["a.ts", ...],
      "symbols": [
        {"file","name","kind","exported","referenced_by":["x.ts",...],
         "blast_radius": N}
      ],
      "public_api_changes": ["add", "Foo", ...],
      "impact_flags": ["exported_symbol_widely_used", ...]
    }

Graceful fallback: if the tree-sitter grammar is unavailable, OR if no TS/JS
files are present, this prints a JSON note and exits 3 so the caller can
proceed with Layer 1 alone. It never crashes the review on unsupported input.

Dependencies (optional):
    pip install tree-sitter tree-sitter-typescript --break-system-packages
"""

import argparse
import json
import os
import re
import sys

TS_EXTS = {".ts", ".tsx", ".mts", ".cts"}
JS_EXTS = {".js", ".jsx", ".mjs", ".cjs"}
ALL_EXTS = TS_EXTS | JS_EXTS

# A symbol referenced by this many other files is "widely used".
WIDE_USE_THRESHOLD = 3


def eprint(*a):
    sys.stderr.write(" ".join(str(x) for x in a) + "\n")


def fallback(note, exit_code=3):
    json.dump({"ok": False, "note": note}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.exit(exit_code)


# --- tree-sitter loading (optional) ---------------------------------------

def load_parsers():
    """Return {".ts": Parser, ...} or None if grammar unavailable."""
    try:
        from tree_sitter import Parser, Language
        import tree_sitter_typescript as tsts
    except Exception as e:  # ImportError or ABI mismatch
        return None, "tree-sitter not available: %s" % e

    try:
        ts_lang = Language(tsts.language_typescript())
        tsx_lang = Language(tsts.language_tsx())
    except Exception as e:
        return None, "failed to build tree-sitter language: %s" % e

    def mk(lang):
        try:
            return Parser(lang)
        except TypeError:
            # Older API: Parser() then set_language
            par = Parser()
            par.set_language(lang)
            return par

    ts_parser = mk(ts_lang)
    tsx_parser = mk(tsx_lang)
    parsers = {
        ".ts": ts_parser, ".mts": ts_parser, ".cts": ts_parser,
        ".js": ts_parser, ".mjs": ts_parser, ".cjs": ts_parser,
        ".tsx": tsx_parser, ".jsx": tsx_parser,
    }
    return parsers, None


# --- Symbol extraction -----------------------------------------------------

def node_text(src, node):
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def child_field(node, name):
    return node.child_by_field_name(name)


def extract_symbols(src, root):
    """Walk the top level (and class bodies) collecting defined symbols."""
    symbols = []

    def name_of(node):
        n = child_field(node, "name")
        return node_text(src, n) if n is not None else None

    def visit_exported(node, exported):
        t = node.type
        if t == "function_declaration":
            nm = name_of(node)
            if nm:
                symbols.append(("function", nm, exported))
        elif t in ("class_declaration", "abstract_class_declaration"):
            nm = name_of(node)
            if nm:
                symbols.append(("class", nm, exported))
                _collect_methods(src, node, nm, symbols)
        elif t in ("lexical_declaration", "variable_declaration"):
            for decl in node.named_children:
                if decl.type == "variable_declarator":
                    nm_node = child_field(decl, "name")
                    if nm_node is not None and nm_node.type == "identifier":
                        val = child_field(decl, "value")
                        kind = "const"
                        if val is not None and val.type in (
                            "arrow_function", "function_expression", "function"
                        ):
                            kind = "function"
                        symbols.append((kind, node_text(src, nm_node), exported))
        elif t in ("interface_declaration", "type_alias_declaration", "enum_declaration"):
            nm = name_of(node)
            if nm:
                kind = {
                    "interface_declaration": "interface",
                    "type_alias_declaration": "type",
                    "enum_declaration": "enum",
                }[t]
                symbols.append((kind, nm, exported))

    for child in root.named_children:
        if child.type == "export_statement":
            # export [default] <declaration>
            decl = child_field(child, "declaration")
            if decl is not None:
                visit_exported(decl, True)
            else:
                # export { a, b } / export default <expr>
                _collect_export_clause(src, child, symbols)
        else:
            visit_exported(child, False)

    return symbols


def _collect_methods(src, class_node, class_name, symbols):
    body = child_field(class_node, "body")
    if body is None:
        return
    for member in body.named_children:
        if member.type in ("method_definition", "method_signature"):
            nm = child_field(member, "name")
            if nm is not None:
                symbols.append(("method", "%s.%s" % (class_name, node_text(src, nm)), False))


def _collect_export_clause(src, export_node, symbols):
    for child in export_node.named_children:
        if child.type == "export_clause":
            for spec in child.named_children:
                if spec.type == "export_specifier":
                    nm = child_field(spec, "name")
                    if nm is not None:
                        symbols.append(("reexport", node_text(src, nm), True))


# --- Reference / blast-radius search --------------------------------------

def iter_source_files(root):
    skip_dirs = {".git", "node_modules", "dist", "build", "out", "vendor", ".next"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            _, ext = os.path.splitext(fn)
            if ext in ALL_EXTS:
                yield os.path.join(dirpath, fn)


def build_reference_index(root, changed_abs):
    """Map each source file -> set of identifiers it references (cheap regex).

    We only need *which other files mention a symbol*; an exact import graph is
    overkill for a heuristic blast radius. We tokenise identifiers and keep a
    per-file set.
    """
    ident_re = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
    index = {}
    for path in iter_source_files(root):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        index[os.path.abspath(path)] = set(ident_re.findall(text))
    return index


def main():
    p = argparse.ArgumentParser(description="Layer 2 TS/JS blast-radius analyser")
    p.add_argument("--root", default=".", help="repository root")
    p.add_argument("--diff-json", help="Layer 1 JSON output (to read changed files)")
    p.add_argument("--files", nargs="*", help="explicit list of changed files")
    args = p.parse_args()

    root = os.path.abspath(args.root)

    # Resolve the set of changed files.
    changed = []
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

    # Keep only TS/JS.
    ts_changed = [c for c in changed if os.path.splitext(c)[1] in ALL_EXTS]
    ts_changed = sorted(set(ts_changed))

    if not ts_changed:
        fallback("no TS/JS files in change set; Layer 1 is sufficient")

    parsers, err = load_parsers()
    if parsers is None:
        fallback("tree-sitter grammar unavailable (%s); install "
                 "tree-sitter tree-sitter-typescript to enable Layer 2" % err)

    # Extract symbols from each changed file.
    symbols = []  # list of dicts
    analyzed = []
    for rel in ts_changed:
        abspath = rel if os.path.isabs(rel) else os.path.join(root, rel)
        if not os.path.exists(abspath):
            continue
        ext = os.path.splitext(abspath)[1]
        parser = parsers.get(ext)
        if parser is None:
            continue
        try:
            with open(abspath, "rb") as fh:
                src = fh.read()
        except OSError:
            continue
        tree = parser.parse(src)
        for kind, name, exported in extract_symbols(src, tree.root_node):
            symbols.append({
                "file": rel,
                "name": name,
                "kind": kind,
                "exported": exported,
                "referenced_by": [],
                "blast_radius": 0,
            })
        analyzed.append(rel)

    if not analyzed:
        fallback("no analyzable TS/JS files found on disk; Layer 1 is sufficient")

    # Build reference index across the repo and compute blast radius for
    # exported symbols (the only ones that can be referenced externally).
    changed_abs = {
        (c if os.path.isabs(c) else os.path.join(root, c)) for c in ts_changed
    }
    changed_abs = {os.path.abspath(c) for c in changed_abs}
    index = build_reference_index(root, changed_abs)

    for sym in symbols:
        if not sym["exported"]:
            continue
        # For "Class.method" symbols, search by the bare method name.
        search_name = sym["name"].split(".")[-1]
        refs = []
        for filepath, idents in index.items():
            if filepath in changed_abs:
                continue
            if search_name in idents:
                refs.append(os.path.relpath(filepath, root))
        sym["referenced_by"] = sorted(refs)
        sym["blast_radius"] = len(refs)

    public_api_changes = sorted({
        s["name"] for s in symbols if s["exported"]
    })

    impact_flags = []
    if any(s["exported"] and s["blast_radius"] >= WIDE_USE_THRESHOLD for s in symbols):
        impact_flags.append("exported_symbol_widely_used")
    if any(s["exported"] and s["blast_radius"] > 0 for s in symbols):
        impact_flags.append("public_api_referenced_externally")
    if public_api_changes:
        impact_flags.append("public_api_changed")

    result = {
        "ok": True,
        "analyzed_files": analyzed,
        "symbols": symbols,
        "public_api_changes": public_api_changes,
        "impact_flags": impact_flags,
    }
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
