#!/usr/bin/env bash
set -euo pipefail

VERIFY_ONLY="${1:-}"

declare -a REQUIRED_FILES=(
  "/home/vscode/.codex/auth.json"
  "/home/vscode/.codex/config.toml"
  "/home/vscode/.gitconfig"
)

declare -a REQUIRED_DIRS=(
  "/home/vscode/.ssh"
)

declare -a OPTIONAL_DIRS=(
  "/home/vscode/.config/gh"
  "/home/vscode/.copilot"
)

missing=0

link_optional_dir() {
  local source_path="$1"
  local target_path="$2"

  if [[ -d "${source_path}" ]] && [[ ! -e "${target_path}" ]]; then
    mkdir -p "$(dirname "${target_path}")"
    ln -s "${source_path}" "${target_path}"
    echo "[linked] ${target_path} -> ${source_path}"
  fi
}

link_optional_dir "/mnt/host-config/gh" "/home/vscode/.config/gh"
link_optional_dir "/mnt/host-copilot" "/home/vscode/.copilot"

for path in "${REQUIRED_FILES[@]}"; do
  if [[ -f "${path}" ]]; then
    echo "[ok] ${path}"
  else
    echo "[missing] ${path}"
    missing=1
  fi
done

for path in "${REQUIRED_DIRS[@]}"; do
  if [[ -d "${path}" ]]; then
    echo "[ok] ${path}"
  else
    echo "[missing] ${path}"
    missing=1
  fi
done

for path in "${OPTIONAL_DIRS[@]}"; do
  if [[ -d "${path}" ]]; then
    echo "[ok] ${path}"
  else
    echo "[optional-missing] ${path}"
  fi
done

if [[ "${missing}" -eq 1 ]]; then
  echo "Required host credentials were not mounted into the devcontainer." >&2
  echo "Check .devcontainer/compose.yaml and confirm ~/.codex, ~/.ssh, and ~/.gitconfig exist on the host." >&2
fi

if [[ "${VERIFY_ONLY}" == "--verify-only" ]]; then
  exit "${missing}"
fi

if command -v uv >/dev/null 2>&1; then
  if [[ -f "pyproject.toml" ]]; then
    uv sync || true
  fi
fi

exit "${missing}"
