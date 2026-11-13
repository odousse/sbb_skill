#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="sbb-trip-planner"
SRC_DIR="${REPO_DIR}/skills/${SKILL_NAME}"
DIST_DIR="${REPO_DIR}/dist"

mkdir -p "${DIST_DIR}"

python3 - "${SRC_DIR}" "${DIST_DIR}" "${SKILL_NAME}" <<'PY'
import os
import sys
import zipfile
from pathlib import Path

src_dir = Path(sys.argv[1]).resolve()
dist_dir = Path(sys.argv[2]).resolve()
skill_name = sys.argv[3]


def should_skip(rel_parts, filename, include_scripts=True):
    if any(p.startswith(".") or p == "__pycache__" for p in rel_parts):
        return True
    if filename.startswith(".") or filename.endswith((".pyc", ".pyo")):
        return True
    if not include_scripts and ("scripts" in rel_parts or filename.endswith(".py")):
        return True
    return False


def build_zip(out_path: Path, include_scripts: bool) -> int:
    count = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
            rel_dir = Path(root).relative_to(src_dir)
            rel_parts = rel_dir.parts
            for fname in sorted(files):
                if should_skip(rel_parts, fname, include_scripts=include_scripts):
                    continue
                full_path = Path(root) / fname
                arcname = Path(skill_name) / rel_dir / fname
                zf.write(full_path, arcname.as_posix())
                count += 1
    return count


full_zip = dist_dir / f"{skill_name}.zip"
gemini_zip = dist_dir / f"{skill_name}-gemini.zip"

n_full = build_zip(full_zip, include_scripts=True)
n_gemini = build_zip(gemini_zip, include_scripts=False)

print(f"Built {full_zip} ({n_full} files — full skill for Claude.ai / Claude Desktop)")
print(f"Built {gemini_zip} ({n_gemini} files — Markdown-only skill for Gemini macOS / web app)")
PY
