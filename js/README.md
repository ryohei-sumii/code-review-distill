# Node port (zero-dependency)

Porting the skill from Python to **plain Node.js** so it runs with no extra
runtime — Node ships with Claude Code, Python may not. Plain `.mjs` (no build
step, no `tsc`), standard library only for the language-agnostic core.

Run tests: `node --test js/*.test.mjs`

## Status

| Script | Node port | Deps |
|---|---|---|
| `diff_summary.mjs` (Layer 1) | ✅ output-identical to Python | none |
| `diff_patterns.mjs` (pattern compression) | ✅ output-identical | none |
| `symbol_impact` (blast radius / signatures) | ⏳ pending | web-tree-sitter (WASM, no native build) |
| `refactor_check` (breaking changes) | ⏳ pending | web-tree-sitter |
| `impact_brief` / `route` (orchestration) | ⏳ pending | none (orchestrate the above) |
| `flow_map` (call graph → Mermaid) | ⏳ pending | web-tree-sitter |

The language-agnostic core (Layer 1 + pattern compression + routing) needs
**zero dependencies**. The AST layer will use `web-tree-sitter` (WASM grammars,
loaded at runtime) so there is still no native build or `pip`/`npm install` of
compiled code.

Until the port is complete, `scripts/*.py` remains the reference implementation
and the one SKILL.md points to.
