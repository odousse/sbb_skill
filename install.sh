#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="sbb-trip-planner"
SRC_DIR="${REPO_DIR}/skills/${SKILL_NAME}"

MODE="symlink"
ALL_AGENTS=0
CUSTOM_TARGET=""

usage() {
  cat <<EOF
Usage: ./install.sh [options]

Installs the '${SKILL_NAME}' skill globally so AI agents can use it in any workspace.

Options:
  --all-agents      Install into Antigravity (~/.gemini/config/skills),
                    Gemini CLI (~/.gemini/skills), Claude Code (~/.claude/skills),
                    and portable ~/.agents/skills.
  --target <dir>    Install into a specific skills directory.
  --copy            Copy files instead of creating a symlink (default: symlink).
  -h, --help        Show this help message.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all-agents)
      ALL_AGENTS=1
      shift
      ;;
    --target)
      CUSTOM_TARGET="${2:-}"
      shift 2
      ;;
    --copy)
      MODE="copy"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

install_into() {
  local base_dir="$1"
  local dest="${base_dir}/${SKILL_NAME}"
  mkdir -p "${base_dir}"
  if [[ "${MODE}" == "copy" ]]; then
    rm -rf "${dest}"
    cp -R "${SRC_DIR}" "${dest}"
    echo "Copied ${SKILL_NAME} -> ${dest}"
  else
    ln -sfn "${SRC_DIR}" "${dest}"
    echo "Linked ${SKILL_NAME} -> ${dest}"
  fi
}

if [[ -n "${CUSTOM_TARGET}" ]]; then
  install_into "${CUSTOM_TARGET}"
elif [[ "${ALL_AGENTS}" -eq 1 ]]; then
  install_into "${HOME}/.gemini/config/skills"
  install_into "${HOME}/.gemini/skills"
  install_into "${HOME}/.claude/skills"
  install_into "${HOME}/.agents/skills"
else
  # Default: Antigravity global customization root
  install_into "${HOME}/.gemini/config/skills"
fi
