#!/usr/bin/env python3
"""Layer 2 (multi-language) blast-radius analyser.

Given the changed source files (from Layer 1 output or an explicit list), this:

  1. Extracts the symbols defined in each file (function / class / method /
     exported variable / type / interface), per language.
  2. Flags which of those are part of the public API.
       - TS/JS:  `export`-ed symbols.
       - Python: module-level names not prefixed with `_`.
       - Go:     identifiers starting with an upper-case letter.
  3. Estimates blast radius: how many *other* files of the same language
     reference each public symbol (identifier match across the repo).

Supported languages: typescript, javascript, python, go.
Adding another is a small registry entry (see LANGS below) plus a per-language
`extract_*` walk. Layer 1 (diff_summary.py) needs no changes.

Output (stdout) JSON:

    {
      "ok": true,
      "languages": ["typescript", "python"],
      "analyzed_files": ["a.ts", ...],
      "symbols": [
        {"file","language","name","kind","exported",
         "referenced_by":["x.ts",...],"blast_radius": N}
      ],
      "public_api_changes": ["add", "Foo", ...],
      "impact_flags": ["exported_symbol_widely_used", ...]
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
IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
SKIP_DIRS = {".git", "node_modules", "dist", "build", "out", "vendor", ".next",
             "__pycache__", ".venv", "venv"}


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


# --- Per-language symbol extraction ---------------------------------------
# Each extractor takes (src: bytes, root_node) and returns a list of
# (kind: str, name: str, exported: bool) tuples.

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
                    symbols.append(("method", "%s.%s" % (class_name, node_text(src, nm)), False))

    def visit(node, exported):
        t = node.type
        if t == "function_declaration":
            nm = name_of(node)
            if nm:
                symbols.append(("function", nm, exported))
        elif t in ("class_declaration", "abstract_class_declaration"):
            nm = name_of(node)
            if nm:
                symbols.append(("class", nm, exported))
                methods(node, nm)
        elif t in ("lexical_declaration", "variable_declaration"):
            for decl in node.named_children:
                if decl.type == "variable_declarator":
                    nm_node = field(decl, "name")
                    if nm_node is not None and nm_node.type == "identifier":
                        val = field(decl, "value")
                        kind = "const"
                        if val is not None and val.type in (
                            "arrow_function", "function_expression", "function"
                        ):
                            kind = "function"
                        symbols.append((kind, node_text(src, nm_node), exported))
        elif t in ("interface_declaration", "type_alias_declaration", "enum_declaration"):
            nm = name_of(node)
            if nm:
                kind = {"interface_declaration": "interface",
                        "type_alias_declaration": "type",
                        "enum_declaration": "enum"}[t]
                symbols.append((kind, nm, exported))

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
                                    symbols.append(("reexport", node_text(src, nm), True))
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
                    symbols.append(("method", "%s.%s" % (class_name, mname), False))

    for child in root.named_children:
        t = child.type
        if t == "function_definition":
            nm = field(child, "name")
            if nm is not None:
                name = node_text(src, nm)
                symbols.append(("function", name, is_public(name)))
        elif t == "decorated_definition":
            inner = field(child, "definition") or child.named_children[-1]
            if inner is not None and inner.type in ("function_definition", "class_definition"):
                nm = field(inner, "name")
                if nm is not None:
                    name = node_text(src, nm)
                    kind = "function" if inner.type == "function_definition" else "class"
                    symbols.append((kind, name, is_public(name)))
                    if inner.type == "class_definition":
                        methods(inner, name)
        elif t == "class_definition":
            nm = field(child, "name")
            if nm is not None:
                name = node_text(src, nm)
                symbols.append(("class", name, is_public(name)))
                methods(child, name)
        elif t in ("expression_statement",):
            # module-level assignment: NAME = ...
            for c in child.named_children:
                if c.type == "assignment":
                    left = field(c, "left")
                    if left is not None and left.type == "identifier":
                        name = node_text(src, left)
                        symbols.append(("const", name, is_public(name)))
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
                symbols.append(("function", name, is_exported(name)))
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
                symbols.append(("method", recv_name + name, is_exported(name)))
        elif t == "type_declaration":
            for spec in child.named_children:
                if spec.type == "type_spec":
                    nm = field(spec, "name")
                    if nm is not None:
                        name = node_text(src, nm)
                        symbols.append(("type", name, is_exported(name)))
        elif t in ("var_declaration", "const_declaration"):
            for spec in child.named_children:
                if spec.type in ("var_spec", "const_spec"):
                    nm = field(spec, "name")
                    if nm is not None:
                        name = node_text(src, nm)
                        kind = "var" if t == "var_declaration" else "const"
                        symbols.append((kind, name, is_exported(name)))
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


def main():
    p = argparse.ArgumentParser(description="Layer 2 multi-language blast-radius analyser")
    p.add_argument("--root", default=".", help="repository root")
    p.add_argument("--diff-json", help="Layer 1 JSON output (to read changed files)")
    p.add_argument("--files", nargs="*", help="explicit list of changed files")
    args = p.parse_args()

    root = os.path.abspath(args.root)

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
        for kind, name, exported in extractor(src, tree.root_node):
            symbols.append({
                "file": rel, "language": lang, "name": name, "kind": kind,
                "exported": exported, "referenced_by": [], "blast_radius": 0,
            })
        analyzed.append(rel)
        used_langs.add(lang)

    if not analyzed:
        fallback("no analyzable source files found on disk; Layer 1 is sufficient",
                 skipped_languages=skipped)

    # Reference index per ref-family, so blast radius only counts same-language refs.
    family_exts = {}
    for lang in used_langs:
        fam = REF_FAMILIES[lang]
        family_exts.setdefault(fam, set()).update(LANGS[lang]["exts"])

    changed_abs = {os.path.abspath(c if os.path.isabs(c) else os.path.join(root, c))
                   for c in supported}

    family_index = {}
    for fam, exts in family_exts.items():
        idx = {}
        for fp in iter_source_files(root, exts):
            ap = os.path.abspath(fp)
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    idx[ap] = set(IDENT_RE.findall(fh.read()))
            except OSError:
                continue
        family_index[fam] = idx

    for sym in symbols:
        if not sym["exported"]:
            continue
        fam = REF_FAMILIES[sym["language"]]
        idx = family_index.get(fam, {})
        search_name = sym["name"].split(".")[-1]
        refs = []
        for fp, idents in idx.items():
            if fp in changed_abs:
                continue
            if search_name in idents:
                refs.append(os.path.relpath(fp, root))
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

    result = {
        "ok": True,
        "languages": sorted(used_langs),
        "analyzed_files": analyzed,
        "symbols": symbols,
        "public_api_changes": public_api_changes,
        "impact_flags": impact_flags,
    }
    if skipped:
        result["skipped_languages"] = skipped
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
