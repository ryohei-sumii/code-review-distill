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
    # full count preserved, list trimmed
    assert p["blast_radius"] == 10
    assert len(p["referenced_by"]) <= 5


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
