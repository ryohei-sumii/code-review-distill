#!/usr/bin/env python3
"""Process-flow distiller: source -> call graph -> Mermaid.

Confirming "how does control flow through this code" usually means reading a
lot of files. This extracts an internal call graph and emits a compact Mermaid
diagram instead, so an agent (or a human) can grasp the structure from a few
lines rather than the whole tree.

Supported languages (via the shared symbol_impact registry):
    typescript / javascript / python / go.

Modes:
    (default)            Mermaid `flowchart TD` of the internal call graph.
    --sequence ENTRY     Mermaid `sequenceDiagram` traced from ENTRY (DFS).
    --json               Emit the raw graph {nodes, edges, external_calls}.

Inputs:
    --files a.ts b.py    explicit files, or
    --dir   src/         walk a directory tree.

Only edges whose callee is a function defined within the analysed set are
drawn (keeps the graph about *this* code); calls to outside functions are
tallied as `external_calls` in --json mode.

Graceful fallback: exits 3 with a JSON note when no supported files are found
or the needed grammar is missing.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import symbol_impact as si  # noqa: E402

# Per-language node-type config for call-graph extraction.
FLOW = {
    "typescript": {
        "func_types": {"function_declaration", "method_definition",
                       "arrow_function", "function_expression", "generator_function_declaration"},
        "named_types": {"function_declaration", "method_definition",
                        "generator_function_declaration"},
        "call_types": {"call_expression"},
    },
    "python": {
        "func_types": {"function_definition"},
        "named_types": {"function_definition"},
        "call_types": {"call"},
    },
    "go": {
        "func_types": {"function_declaration", "method_declaration", "func_literal"},
        "named_types": {"function_declaration", "method_declaration"},
        "call_types": {"call_expression"},
    },
}
# typescript config is reused for tsx/javascript
FLOW["tsx"] = FLOW["typescript"]
FLOW["javascript"] = FLOW["typescript"]


def text(src, node):
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def field(node, name):
    return node.child_by_field_name(name)


def last_identifier(src, node):
    """Rightmost identifier inside a callee expression (heuristic name)."""
    result = [None]

    def walk(n):
        if n.type in ("identifier", "property_identifier", "field_identifier", "type_identifier"):
            result[0] = text(src, n)
        for c in n.children:
            walk(c)
    walk(node)
    return result[0]


def func_name(src, node, lang):
    cfg = FLOW[lang]
    if node.type in cfg["named_types"]:
        nm = field(node, "name")
        if nm is not None:
            return text(src, nm)
    # anonymous function assigned to a variable / property -> borrow that name
    parent = node.parent
    if parent is not None:
        if parent.type == "variable_declarator":
            nm = field(parent, "name")
            if nm is not None:
                return text(src, nm)
        if parent.type in ("pair", "property_signature"):
            key = field(parent, "key") or field(parent, "name")
            if key is not None:
                return text(src, key)
        if parent.type == "assignment":
            left = field(parent, "left")
            if left is not None:
                return last_identifier(src, left)
    return None


def analyze_file(src, root, lang, defined, edges, external):
    """Walk one file, recording defined funcs, internal edges, external calls."""
    cfg = FLOW[lang]
    func_types = cfg["func_types"]
    call_types = cfg["call_types"]

    def walk(node, stack):
        is_func = node.type in func_types
        pushed = False
        if is_func:
            name = func_name(src, node, lang)
            if name:
                defined.add(name)
                stack = stack + [name]
                pushed = True
            else:
                stack = stack + [None]
                pushed = True
        if node.type in call_types:
            callee_node = field(node, "function") or (node.named_children[0]
                                                       if node.named_children else None)
            callee = last_identifier(src, callee_node) if callee_node is not None else None
            caller = next((s for s in reversed(stack) if s), None)
            if callee:
                edges.append((caller or "(top-level)", callee))
        for c in node.children:
            walk(c, stack)
        return pushed

    walk(root, [])
    # Resolve external vs internal after we know the full defined set (caller does).


def build_graph(files_map):
    """files_map: {rel_path: (src_bytes, root_node, lang)} -> graph dict."""
    defined = set()
    raw_edges = []
    for rel, (src, root, lang) in files_map.items():
        analyze_file(src, root, lang, defined, raw_edges, None)

    internal_edges = []
    external_calls = {}
    seen = set()
    for caller, callee in raw_edges:
        if callee in defined:
            key = (caller, callee)
            if key not in seen:
                seen.add(key)
                internal_edges.append({"from": caller, "to": callee})
        else:
            external_calls[callee] = external_calls.get(callee, 0) + 1

    nodes = sorted(defined | {"(top-level)"})
    # only keep (top-level) node if it actually has edges
    used = {e["from"] for e in internal_edges} | {e["to"] for e in internal_edges}
    nodes = [n for n in nodes if n in used or n in defined]
    return {
        "nodes": sorted(set(nodes)),
        "edges": internal_edges,
        "external_calls": dict(sorted(external_calls.items(), key=lambda kv: -kv[1])),
    }


def sanitize(name):
    return "n_" + "".join(c if c.isalnum() else "_" for c in name)


def to_flowchart(graph):
    lines = ["flowchart TD"]
    for n in graph["nodes"]:
        lines.append('    %s["%s"]' % (sanitize(n), n))
    for e in graph["edges"]:
        lines.append("    %s --> %s" % (sanitize(e["from"]), sanitize(e["to"])))
    return "\n".join(lines)


def to_sequence(graph, entry, max_depth=25):
    adj = {}
    for e in graph["edges"]:
        adj.setdefault(e["from"], []).append(e["to"])
    if entry not in graph["nodes"]:
        sys.stderr.write("error: entry '%s' is not a defined function\n" % entry)
        sys.exit(2)
    lines = ["sequenceDiagram"]
    visited_edges = set()

    def dfs(node, depth):
        if depth > max_depth:
            return
        for callee in adj.get(node, []):
            edge = (node, callee)
            lines.append("    %s->>%s: call" % (node, callee))
            if edge in visited_edges:
                continue
            visited_edges.add(edge)
            dfs(callee, depth + 1)

    dfs(entry, 0)
    if len(lines) == 1:
        lines.append("    %% (entry makes no internal calls)")
    return "\n".join(lines)


def collect_files(args):
    paths = []
    if args.files:
        paths.extend(args.files)
    if args.dir:
        for dirpath, dirnames, filenames in os.walk(args.dir):
            dirnames[:] = [d for d in dirnames if d not in si.SKIP_DIRS]
            for fn in filenames:
                paths.append(os.path.join(dirpath, fn))
    return [p for p in paths if si.lang_of(p) in FLOW]


def main():
    p = argparse.ArgumentParser(description="Call-graph -> Mermaid flow distiller")
    p.add_argument("--files", nargs="*", help="explicit source files")
    p.add_argument("--dir", help="walk this directory")
    p.add_argument("--root", default=".", help="base for relative paths")
    p.add_argument("--sequence", metavar="ENTRY", help="emit a sequenceDiagram from ENTRY")
    p.add_argument("--json", action="store_true", help="emit raw graph JSON")
    p.add_argument("--compact", action="store_true",
                   help="with --json, emit minified JSON with empty fields dropped")
    args = p.parse_args()

    if not args.files and not args.dir:
        sys.stderr.write("error: provide --files or --dir\n")
        sys.exit(2)

    src_paths = collect_files(args)
    if not src_paths:
        si.fallback("no supported source files found")

    langs = sorted({si.lang_of(p) for p in src_paths})
    parsers = {}
    skipped = {}
    for lang in langs:
        par, err = si.load_parser(lang)
        if par is None:
            skipped[lang] = err
        else:
            parsers[lang] = par
    if not parsers:
        si.fallback("no required grammars installed (%s)" % "; ".join(skipped.values()),
                    skipped_languages=skipped)

    files_map = {}
    for path in sorted(set(src_paths)):
        lang = si.lang_of(path)
        parser = parsers.get(lang)
        if parser is None:
            continue
        try:
            with open(path, "rb") as fh:
                src = fh.read()
        except OSError:
            continue
        tree = parser.parse(src)
        rel = os.path.relpath(path, args.root)
        files_map[rel] = (src, tree.root_node, lang)

    if not files_map:
        si.fallback("no analyzable source files found", skipped_languages=skipped)

    graph = build_graph(files_map)

    if args.json:
        out = dict(graph)
        out["ok"] = True
        if skipped:
            out["skipped_languages"] = skipped
        si.emit(out, args.compact)
    elif args.sequence:
        sys.stdout.write(to_sequence(graph, args.sequence) + "\n")
    else:
        sys.stdout.write(to_flowchart(graph) + "\n")


if __name__ == "__main__":
    main()
