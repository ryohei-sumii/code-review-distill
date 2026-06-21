"""Smoke + behaviour tests for the code-review-distill scripts.

Each test drives a script through its real CLI (subprocess + JSON), the same
way the skill invokes it. Tests that need a tree-sitter grammar skip cleanly
when it isn't installed, so the Layer 1 (language-agnostic) tests always run.

Run:  python -m pytest tests/ -q
"""

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")


# --- grammar availability --------------------------------------------------

def _has(*mods):
    try:
        for m in mods:
            __import__(m)
        return True
    except Exception:
        return False


HAS_TS = _has("tree_sitter", "tree_sitter_typescript")
HAS_PY = _has("tree_sitter", "tree_sitter_python")
HAS_GO = _has("tree_sitter", "tree_sitter_go")

needs_ts = pytest.mark.skipif(not HAS_TS, reason="tree-sitter-typescript not installed")
needs_py = pytest.mark.skipif(not HAS_PY, reason="tree-sitter-python not installed")
needs_go = pytest.mark.skipif(not HAS_GO, reason="tree-sitter-go not installed")


# --- helpers ---------------------------------------------------------------

def run(script, *args, expect=None):
    """Run scripts/<script> with args; return (exit_code, parsed_or_text)."""
    cp = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, script), *map(str, args)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if expect is not None:
        assert cp.returncode == expect, (
            "exit %d != %d\nstderr: %s" % (cp.returncode, expect, cp.stderr))
    out = cp.stdout.strip()
    try:
        return cp.returncode, json.loads(out)
    except ValueError:
        return cp.returncode, out


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init")
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    return path


def write(path, rel, content):
    f = path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    return f


def commit_all(repo, msg):
    git(repo, "add", "-A")
    git(repo, "commit", "-m", msg)


# --- Layer 1: diff_summary (language-agnostic, always runs) ----------------

def test_layer1_code_without_tests(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "src/m.ts", "export function add(a,b){return a+b;}\n")
    commit_all(repo, "init")
    write(repo, "src/m.ts", "export function add(a,b){return a-b;}\n")
    commit_all(repo, "change")

    code, data = run("diff_summary.py", "--range", "HEAD~1..HEAD",
                     "--cwd", repo, expect=0)
    assert "code_changed_without_tests" in data["risk_flags"]
    assert data["totals"]["files"] == 1
    assert data["files"][0]["language"] == "typescript"


def test_layer1_rename_and_delete(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "a.py", "x = 1\n")
    write(repo, "b.py", "y = 2\n")
    commit_all(repo, "init")
    git(repo, "mv", "a.py", "renamed.py")
    git(repo, "rm", "b.py")
    commit_all(repo, "rename+delete")

    code, data = run("diff_summary.py", "--range", "HEAD~1..HEAD",
                     "--cwd", repo, expect=0)
    statuses = {f["path"]: f["status"] for f in data["files"]}
    assert statuses.get("renamed.py") == "renamed"
    assert statuses.get("b.py") == "deleted"
    assert "contains_deletions" in data["risk_flags"]
    # deleted file is highest risk -> sorted first
    assert data["review_order"][0] == "b.py"


def test_layer1_generated_demoted(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "src/app.py", "def f():\n    return 1\n")
    write(repo, "package-lock.json", "{}\n")
    commit_all(repo, "init")
    write(repo, "src/app.py", "def f():\n    return 2\n")
    write(repo, "package-lock.json", '{"v":1}\n')
    commit_all(repo, "change")

    code, data = run("diff_summary.py", "--range", "HEAD~1..HEAD",
                     "--cwd", repo, expect=0)
    gen = next(f for f in data["files"] if f["path"] == "package-lock.json")
    assert gen["is_generated"] is True
    # generated file should rank below real code
    assert data["review_order"][0] == "src/app.py"


# --- Layer 2: symbol_impact ------------------------------------------------

@needs_ts
def test_layer2_ts_blast_radius(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "math.ts", "export function add(a,b){return a+b;}\n")
    write(repo, "x.ts", 'import {add} from "./math"; export const x = add(1,2);\n')
    write(repo, "y.ts", 'import {add} from "./math"; export const y = add(3,4);\n')
    commit_all(repo, "init")

    code, data = run("symbol_impact.py", "--root", repo, "--files", "math.ts",
                     expect=0)
    add = next(s for s in data["symbols"] if s["name"] == "add")
    assert add["exported"] is True
    assert add["blast_radius"] == 2
    assert "public_api_changed" in data["impact_flags"]


@needs_py
def test_layer2_python_public_vs_private(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "calc.py",
          "def add(a,b):\n    return a+b\n"
          "def _helper(x):\n    return x\n")
    commit_all(repo, "init")

    code, data = run("symbol_impact.py", "--root", repo, "--files", "calc.py",
                     expect=0)
    by_name = {s["name"]: s for s in data["symbols"]}
    assert by_name["add"]["exported"] is True
    assert by_name["_helper"]["exported"] is False


@needs_go
def test_layer2_go_exported_by_case(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "m.go",
          "package m\nfunc Add(a,b int) int { return a+b }\n"
          "func helper(x int) int { return x }\n")
    commit_all(repo, "init")

    code, data = run("symbol_impact.py", "--root", repo, "--files", "m.go",
                     expect=0)
    by_name = {s["name"]: s for s in data["symbols"]}
    assert by_name["Add"]["exported"] is True
    assert by_name["helper"]["exported"] is False


def test_layer1_compact_is_smaller_and_valid(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "a.py", "x = 1\n")
    write(repo, "b.py", "y = 2\n")
    commit_all(repo, "init")
    write(repo, "a.py", "x = 3\n")
    write(repo, "b.py", "y = 4\n")
    commit_all(repo, "change")

    _, verbose = run("diff_summary.py", "--range", "HEAD~1..HEAD", "--cwd", repo,
                     expect=0)
    code, compact = run("diff_summary.py", "--range", "HEAD~1..HEAD", "--cwd",
                        repo, "--compact", expect=0)
    # same core data, just no empty fields / whitespace
    assert compact["totals"] == verbose["totals"]
    assert set(compact["review_order"]) == set(verbose["review_order"])
    # empty fields are dropped in compact mode
    for f in compact["files"]:
        assert f.get("risk_flags", ["x"]) != []


@needs_ts
def test_layer2_compact_caps_referenced_by(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "core.ts", "export function p(a){return a;}\n")
    for i in range(10):
        write(repo, "c%d.ts" % i,
              'import {p} from "./core"; export const v%d = p(%d);\n' % (i, i))
    commit_all(repo, "init")

    code, data = run("symbol_impact.py", "--root", repo, "--files", "core.ts",
                     "--compact", expect=0)
    p = next(s for s in data["symbols"] if s["name"] == "p")
    # full count preserved, list trimmed, truncation made explicit
    assert p["blast_radius"] == 10
    assert len(p["referenced_by"]) <= 5
    assert p.get("referenced_by_truncated") is True

    # --max-refs 0 restores the full list with no truncation marker
    code, full = run("symbol_impact.py", "--root", repo, "--files", "core.ts",
                     "--compact", "--max-refs", 0, expect=0)
    pf = next(s for s in full["symbols"] if s["name"] == "p")
    assert pf["blast_radius"] == 10
    assert len(pf["referenced_by"]) == 10
    assert "referenced_by_truncated" not in pf


@needs_ts
def test_layer2_integrates_impact_into_review_order(tmp_path):
    repo = init_repo(tmp_path)
    # a tiny public-API change used by several callers + a quiet self-contained file
    write(repo, "api.ts", "export function shared(a){return a;}\n")
    for i in range(6):
        write(repo, "c%d.ts" % i,
              'import {shared} from "./api"; export const v%d = shared(%d);\n' % (i, i))
    write(repo, "quiet.ts", "function helper(){return 1;}\nexport const z = helper();\n")
    commit_all(repo, "init")
    write(repo, "api.ts", "export function shared(a, b){return a + b;}\n")
    write(repo, "quiet.ts", "function helper(){return 2;}\nexport const z = helper();\n")
    commit_all(repo, "change")

    l1 = tmp_path / "l1.json"
    _, l1data = run("diff_summary.py", "--range", "HEAD~1..HEAD", "--cwd", repo,
                    expect=0)
    l1.write_text(json.dumps(l1data))
    # Layer 1 exposes a risk_score per file now
    assert all("risk_score" in f for f in l1data["files"])

    code, data = run("symbol_impact.py", "--root", repo, "--diff-json", str(l1),
                     expect=0)
    # integrated, impact-aware ordering is present with a transparent breakdown
    assert "review_order" in data and "prioritized" in data
    api = next(r for r in data["prioritized"] if r["path"] == "api.ts")
    assert api["impact_score"] == 6
    assert api["combined_score"] > api["l1_risk_score"]  # impact lifted it
    assert any("used by 6" in reason for reason in api["reasons"])
    # api.ts (6 callers) should outrank the quiet self-contained file
    assert data["review_order"].index("api.ts") < data["review_order"].index("quiet.ts")


@needs_ts
def test_layer2_blast_radius_is_import_resolved(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "math.ts", "export function add(a, b){return a + b;}\n")
    write(repo, "other.ts", "export function add(a){return a;}\n")
    # genuine users of math.add
    write(repo, "good_named.ts", 'import {add} from "./math"; export const a = add(1,2);\n')
    write(repo, "good_ns.ts", 'import * as m from "./math"; export const b = m.add(1,2);\n')
    # NOT users: same name but no import, and import from a different module
    write(repo, "shadow.ts", "function add(x){return x;} export const c = add(5);\n")
    write(repo, "other_user.ts", 'import {add} from "./other"; export const d = add(7);\n')
    commit_all(repo, "init")

    code, data = run("symbol_impact.py", "--root", repo, "--files", "math.ts",
                     expect=0)
    add = next(s for s in data["symbols"] if s["name"] == "add")
    # old identifier matching would report 4 (2 false positives); import
    # resolution reports exactly the two real importers.
    assert add["blast_radius"] == 2
    assert set(add["referenced_by"]) == {"good_named.ts", "good_ns.ts"}


@needs_ts
def test_layer2_import_resolution_no_false_positive_across_statements(tmp_path):
    # A semicolon-less `export { foo }` immediately followed by an import from
    # the defining module must NOT be glued together (regex over-reach).
    repo = init_repo(tmp_path)
    write(repo, "mod.ts", "export function foo(a){return a;}\n")
    write(repo, "mod2.ts", "export function bar(){return 2;}\n")
    write(repo, "tricky.ts",
          "const foo = 1\n"
          "export { foo }\n"
          'import { bar } from "./mod2"\n'
          "export const baz = bar\n")
    commit_all(repo, "init")

    code, data = run("symbol_impact.py", "--root", repo, "--files", "mod.ts",
                     expect=0)
    foo = next(s for s in data["symbols"] if s["name"] == "foo")
    # tricky.ts re-exports its OWN local foo and imports bar (not foo) from mod2
    assert foo["blast_radius"] == 0


@needs_py
def test_layer2_import_resolution_python(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "calc.py", "def add(a, b):\n    return a + b\n")
    write(repo, "good_from.py", "from calc import add\nprint(add(1, 2))\n")
    write(repo, "good_mod.py", "import calc\nprint(calc.add(1, 2))\n")
    write(repo, "shadow.py", "def add(x):\n    return x\nprint(add(5))\n")
    commit_all(repo, "init")

    code, data = run("symbol_impact.py", "--root", repo, "--files", "calc.py",
                     expect=0)
    add = next(s for s in data["symbols"] if s["name"] == "add")
    assert add["blast_radius"] == 2
    assert set(add["referenced_by"]) == {"good_from.py", "good_mod.py"}


@needs_ts
def test_layer2_captures_signature(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "api.ts", "export function f(a: number, b: number): number { return a; }\n")
    commit_all(repo, "init")
    code, data = run("symbol_impact.py", "--root", repo, "--files", "api.ts",
                     expect=0)
    f = next(s for s in data["symbols"] if s["name"] == "f")
    assert "a: number" in f["signature"] and "number" in f["signature"]


def test_layer2_fallback_on_unsupported(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "notes.txt", "hello\n")
    code, data = run("symbol_impact.py", "--root", repo, "--files", "notes.txt")
    assert code == 3
    assert data["ok"] is False


# --- refactor_check --------------------------------------------------------

@needs_ts
def test_refactor_flags_public_api_change(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "api.ts",
          "export function add(a,b){return a+b;}\n"
          "export function sub(a,b){return a-b;}\n")
    commit_all(repo, "init")
    # drop exported sub, add exported mul -> not a pure refactor
    write(repo, "api.ts",
          "export function add(a,b){return a+b;}\n"
          "export function mul(a,b){return a*b;}\n")
    commit_all(repo, "refactor")

    code, data = run("refactor_check.py", "--range", "HEAD~1..HEAD",
                     "--cwd", repo, expect=0)
    assert data["invariants"]["public_api_preserved"] is False
    assert "public_api_changed_during_refactor" in data["flags"]
    assert any("sub" in s for s in data["invariants"]["public_api_removed"])
    assert any("mul" in s for s in data["invariants"]["public_api_added"])


@needs_ts
def test_refactor_pure_internal_change_preserved(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "api.ts",
          "export function add(a,b){return a+b;}\n"
          "function helper(x){return x*2;}\n")
    commit_all(repo, "init")
    # rename only the internal helper -> public API preserved
    write(repo, "api.ts",
          "export function add(a,b){return a+b;}\n"
          "function doubler(x){return x*2;}\n")
    commit_all(repo, "rename internal")

    code, data = run("refactor_check.py", "--range", "HEAD~1..HEAD",
                     "--cwd", repo, expect=0)
    assert data["invariants"]["public_api_preserved"] is True
    assert "public_api_changed_during_refactor" not in data["flags"]


@needs_ts
def test_refactor_detects_signature_breaking_change(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "api.ts",
          "export function process(a: number, b: number): number { return a + b; }\n"
          "export function stable(x: number): number { return x; }\n")
    commit_all(repo, "init")
    # same symbol name, but params reordered + new param + return type changed:
    # a breaking change that the public-api set comparison alone cannot see.
    write(repo, "api.ts",
          "export function process(b: number, a: number, c: number): string { return ''; }\n"
          "export function stable(x: number): number { return x; }\n")
    commit_all(repo, "breaking signature change")

    code, data = run("refactor_check.py", "--range", "HEAD~1..HEAD",
                     "--cwd", repo, expect=0)
    # the symbol set is unchanged, so public_api_preserved stays True ...
    assert data["invariants"]["public_api_preserved"] is True
    # ... but the signature change is caught:
    assert data["invariants"]["signatures_preserved"] is False
    assert "public_signature_changed" in data["flags"]
    assert "function process" in data["invariants"]["public_signatures_changed"]
    changes = [c for f in data["files"] for c in f["signature_changes"]]
    proc = next(c for c in changes if c["name"] == "process")
    assert proc["public"] is True and proc["old"] != proc["new"]
    # the unchanged function is not flagged
    assert all(c["name"] != "stable" for c in changes)


# --- flow_map --------------------------------------------------------------

@needs_ts
def test_flow_flowchart_edges(tmp_path):
    repo = tmp_path
    write(repo, "app.ts",
          "export function main(){ setup(); run(); }\n"
          "function setup(){ configure(); }\n"
          "function configure(){}\n"
          "function run(){ configure(); }\n")
    code, out = run("flow_map.py", "--files", repo / "app.ts", "--root", repo,
                    expect=0)
    assert out.startswith("flowchart TD")
    assert "n_main --> n_setup" in out
    assert "n_run --> n_configure" in out


@needs_ts
def test_flow_sequence_and_json(tmp_path):
    repo = tmp_path
    write(repo, "app.ts",
          "export function main(){ a(); }\n"
          "function a(){ b(); }\n"
          "function b(){}\n")
    code, seq = run("flow_map.py", "--files", repo / "app.ts", "--sequence",
                    "main", expect=0)
    assert seq.startswith("sequenceDiagram")
    assert "main->>a: call" in seq

    code, graph = run("flow_map.py", "--files", repo / "app.ts", "--root", repo,
                      "--json", expect=0)
    assert {"main", "a", "b"} <= set(graph["nodes"])
    assert {"from": "a", "to": "b"} in graph["edges"]


# --- run_loop --------------------------------------------------------------

def test_run_loop_heuristic_recall(tmp_path):
    code, data = run("run_loop.py", "--json", expect=0)
    m = data["metrics"]
    # the default eval set should be fully recalled by the heuristic baseline
    assert m["recall"] == 1.0
    assert 0.0 <= m["precision"] <= 1.0
    assert data["n_cases"] >= 4


def test_run_loop_predictions_perfect(tmp_path):
    evalset = json.load(open(os.path.join(ROOT, "evals", "trigger_evalset.json")))
    preds = {c["prompt"]: ("fire" if c["label"] == "should_fire" else "no_fire")
             for c in evalset["cases"]}
    pfile = tmp_path / "preds.json"
    pfile.write_text(json.dumps(preds))
    code, data = run("run_loop.py", "--predictions", pfile, "--json", expect=0)
    assert data["metrics"]["precision"] == 1.0
    assert data["metrics"]["recall"] == 1.0
    assert data["metrics"]["fp"] == 0


# --- route (auto brief/full switch) ----------------------------------------

def test_route_small_picks_brief(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "a.py", "x = 1\n")
    commit_all(repo, "init")
    write(repo, "a.py", "x = 2\n")
    commit_all(repo, "change")
    code, data = run("route.py", "--range", "HEAD~1..HEAD", "--cwd", repo,
                     "--json", expect=0)
    assert data["mode"] == "brief"


def test_route_many_files_picks_full_for_navigation(tmp_path):
    # >= --large-files (25): ordering helps even without measured blast radius,
    # so this holds with or without a grammar installed.
    repo = init_repo(tmp_path)
    for i in range(26):
        write(repo, "m%d.py" % i, "x = 1\n")
    commit_all(repo, "init")
    for i in range(26):
        write(repo, "m%d.py" % i, "x = 2\n")
    commit_all(repo, "change")
    code, data = run("route.py", "--range", "HEAD~1..HEAD", "--cwd", repo,
                     "--json", expect=0)
    assert data["mode"] == "full"
    # >= --large-files: the large-scale toolkit (patterns/checks/fan-out) is on,
    # and the verbose per-file fields are replaced by the compressed view.
    assert "large_scale" in data
    assert "review_order" not in data


@needs_ts
def test_route_high_blast_picks_full(tmp_path):
    # A medium changeset (below --large-files) routes to full only because a
    # changed public symbol has high blast radius.
    repo = init_repo(tmp_path)
    write(repo, "m0.ts", "export function shared(a){return a;}\n")
    for i in range(1, 6):
        write(repo, "m%d.ts" % i, "export function fn%d(a){return a;}\n" % i)
    for c in range(12):
        write(repo, "c%d.ts" % c,
              'import {shared} from "./m0"; export const v%d = shared(%d);\n' % (c, c))
    commit_all(repo, "init")
    write(repo, "m0.ts", "export function shared(a, b){return a + b;}\n")
    for i in range(1, 6):
        write(repo, "m%d.ts" % i, "export function fn%d(a){return a + 1;}\n" % i)
    commit_all(repo, "change")
    code, data = run("route.py", "--range", "HEAD~1..HEAD", "--cwd", repo,
                     "--json", expect=0)
    assert data["mode"] == "full"


@needs_ts
def test_route_medium_low_blast_picks_brief(tmp_path):
    # Same file count, but low blast radius -> brief (avoids the full-map tax).
    repo = init_repo(tmp_path)
    for i in range(6):
        write(repo, "m%d.ts" % i, "export function fn%d(a){return a;}\n" % i)
    commit_all(repo, "init")
    for i in range(6):
        write(repo, "m%d.ts" % i, "export function fn%d(a){return a + 1;}\n" % i)
    commit_all(repo, "change")
    code, data = run("route.py", "--range", "HEAD~1..HEAD", "--cwd", repo,
                     "--json", expect=0)
    assert data["mode"] == "brief"


def test_route_force_overrides_threshold(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "a.py", "x = 1\n")
    commit_all(repo, "init")
    write(repo, "a.py", "x = 2\n")
    commit_all(repo, "change")
    code, data = run("route.py", "--range", "HEAD~1..HEAD", "--cwd", repo,
                     "--force", "full", "--json", expect=0)
    assert data["mode"] == "full"


# --- diff_patterns (lossless pattern compression) --------------------------

def test_patterns_collapse_codemod(tmp_path):
    repo = init_repo(tmp_path)
    for i in range(20):
        write(repo, "m%d.ts" % i, "export function fn%d(a){ return a; }\n" % i)
    write(repo, "special.ts", "export function special(a){ return a; }\n")
    commit_all(repo, "init")
    for i in range(20):  # identical structural edit
        write(repo, "m%d.ts" % i, "export function fn%d(a){ return a + 1; }\n" % i)
    write(repo, "special.ts", "export function special(a){ return a * 2 - 1; }\n")
    commit_all(repo, "change")

    code, data = run("diff_patterns.py", "--range", "HEAD~1..HEAD", "--cwd", repo,
                     "--json", expect=0)
    assert data["compression"]["changed_files"] == 21
    # the 20 identical edits collapse to one pattern; special is unique
    assert any(p["count"] == 20 for p in data["patterns"])
    assert "special.ts" in data["unique"]
    # lossless: every changed file is covered by a pattern or unique
    covered = set(data["unique"])
    for p in data["patterns"]:
        covered |= set(p["files"])
    assert len(covered) == 21


def test_patterns_do_not_overmerge_distinct_changes(tmp_path):
    repo = init_repo(tmp_path)
    for i in range(6):
        write(repo, "m%d.ts" % i, "export function fn%d(a){ return a; }\n" % i)
    commit_all(repo, "init")
    # two structurally different edits, 3 files each
    for i in range(6):
        if i % 2 == 0:
            write(repo, "m%d.ts" % i, "export function fn%d(a){ return a + 1; }\n" % i)
        else:
            write(repo, "m%d.ts" % i, "export function fn%d(a){ if (a) return a; return 0; }\n" % i)
    commit_all(repo, "change")
    code, data = run("diff_patterns.py", "--range", "HEAD~1..HEAD", "--cwd", repo,
                     "--json", expect=0)
    # not collapsed into one: the two shapes stay separate
    assert len(data["patterns"]) == 2


# --- impact_brief (small-change path) --------------------------------------

def test_brief_layer1_facts_without_grammar(tmp_path):
    # Layer 1 facts always work: file count, no-tests flag.
    repo = init_repo(tmp_path)
    write(repo, "a.py", "x = 1\n")
    commit_all(repo, "init")
    write(repo, "a.py", "x = 2\n")
    commit_all(repo, "change")
    code, data = run("impact_brief.py", "--range", "HEAD~1..HEAD", "--cwd", repo,
                     "--json", expect=0)
    assert data["files"] == 1
    assert "code_changed_without_tests" in data["flags"]


@needs_ts
def test_brief_surfaces_blast_radius_and_breaking(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "api.ts", "export function shared(a){return a;}\n")
    for i in range(4):
        write(repo, "c%d.ts" % i,
              'import {shared} from "./api"; export const v%d = shared(%d);\n' % (i, i))
    commit_all(repo, "init")
    write(repo, "api.ts", "export function shared(a, b){return a + b;}\n")  # breaking
    commit_all(repo, "change")

    code, data = run("impact_brief.py", "--range", "HEAD~1..HEAD", "--cwd", repo,
                     "--json", expect=0)
    assert any("shared" in b for b in data["breaking_changes"])
    hi = next(h for h in data["high_impact"] if h["symbol"] == "shared")
    assert hi["blast_radius"] == 4


@needs_ts
def test_brief_does_not_overclaim_on_internal_change(tmp_path):
    repo = init_repo(tmp_path)
    write(repo, "u.ts", "function helper(x){return x*2;}\nexport const v = helper(3);\n")
    commit_all(repo, "init")
    write(repo, "u.ts", "function helper(x){return x*3;}\nexport const v = helper(3);\n")
    commit_all(repo, "change")
    code, data = run("impact_brief.py", "--range", "HEAD~1..HEAD", "--cwd", repo,
                     "--json", expect=0)
    assert data["breaking_changes"] == []
    assert data["high_impact"] == []


# --- needle_eval (lost-in-the-middle geometry) -----------------------------

def test_needle_geometry_keeps_needle_near_end(tmp_path):
    # Layer 1 only is enough; symbol_impact falls back gracefully if no grammar.
    code, data = run("needle_eval.py", "--files", 20, "--repeats", 1,
                     "--kind", "quiet", "--json", expect=0)
    start = next(r for r in data["by_position"] if r["position"] == "start")
    # In raw review a start-placed needle is far from the end; distilled keeps
    # it at the very end (recency) -> much smaller from_end.
    assert start["B_from_end_tokens"] < start["A_from_end_tokens"]
    assert start["B_rel_pos"] >= 0.9
    # And the recency distance is small regardless of placement.
    for r in data["by_position"]:
        assert r["B_from_end_tokens"] <= 80


def test_needle_emit_cases_hides_marker(tmp_path):
    out = tmp_path / "cases"
    code, text = run("needle_eval.py", "--emit-cases", str(out), "--files", 8,
                     "--kind", "quiet", expect=0)
    manifest = json.loads((out / "manifest.json").read_text())
    assert len(manifest) == 3
    for f in out.glob("*.txt"):
        assert "NEEDLE" not in f.read_text(), "marker leaked into judge input %s" % f

