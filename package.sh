#!/usr/bin/env bash
# Build a distributable code-review-distill.skill (a zip of the skill folder).
#
# The archive contains a top-level `code-review-distill/` directory, so:
#     cd ~/.claude/skills && unzip code-review-distill.skill
# installs it as ~/.claude/skills/code-review-distill/.
#
# Usage: ./package.sh [output_dir]   (default: dist/)
set -euo pipefail

SKILL_NAME="code-review-distill"
ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="${1:-$ROOT/dist}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# Validate the required frontmatter is present before packaging.
python3 - "$ROOT/SKILL.md" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
m = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
assert m, "SKILL.md is missing YAML frontmatter"
block = m.group(1)
for key in ("name", "description"):
    assert re.search(r"(?m)^%s:\s*\S" % key, block), "frontmatter missing: %s" % key
print("frontmatter OK")
PY

# Stage only the files that belong in the distributed skill.
DEST="$STAGE/$SKILL_NAME"
mkdir -p "$DEST/js" "$DEST/scripts" "$DEST/evals"
cp "$ROOT/SKILL.md" "$DEST/"
cp "$ROOT/README.md" "$DEST/"
cp "$ROOT/package.json" "$DEST/"
cp "$ROOT"/js/*.mjs "$DEST/js/"
cp "$ROOT"/scripts/*.py "$DEST/scripts/"   # Python reference
cp "$ROOT"/evals/*.json "$DEST/evals/"
# Node deps (web-tree-sitter + WASM grammars) are installed on the target with
# `npm install`; node_modules is intentionally not bundled.

mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/$SKILL_NAME.skill"
rm -f "$OUT"
( cd "$STAGE" && zip -q -r "$OUT" "$SKILL_NAME" -x '*/__pycache__/*' '*.pyc' )

echo "built: $OUT"
unzip -l "$OUT"
