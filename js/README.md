# Node port (zero-dependency)

Porting the skill from Python to **plain Node.js** so it runs with no extra
runtime — Node ships with Claude Code, Python may not. Plain `.mjs` (no build
step, no `tsc`), standard library only for the language-agnostic core.

Run tests: `node --test js/*.test.mjs`

## Status — complete

| Script | Node port | Deps |
|---|---|---|
| `diff_summary.mjs` (Layer 1) | ✅ byte-identical to Python | none |
| `diff_patterns.mjs` (pattern compression) | ✅ byte-identical | none |
| `symbol_impact.mjs` (blast radius / signatures) | ✅ byte-identical | web-tree-sitter (WASM) |
| `refactor_check.mjs` (breaking changes) | ✅ byte-identical | web-tree-sitter |
| `impact_brief.mjs` / `route.mjs` (orchestration) | ✅ byte-identical | (orchestrate the above) |
| `flow_map.mjs` (call graph → Mermaid) | ✅ byte-identical | web-tree-sitter |
| `run_loop.mjs` / `needle_eval.mjs` / `benchmark_routing.mjs` (eval) | ✅ value-equivalent* | none |

*eval tools differ only in JSON float formatting (`1` vs `1.0`) and half-rounding
mode on borderline display ratios — values are equivalent.

`web-tree-sitter` loads WASM grammars at runtime: **no native build, no node-gyp**.
The language-agnostic core (`diff_summary` / `diff_patterns`) needs zero deps.
SKILL.md points at the Node scripts; `scripts/*.py` stays as the reference.
