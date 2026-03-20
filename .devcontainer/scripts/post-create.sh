#!/usr/bin/env bash
set -euo pipefail

VERIFY_ONLY="${1:-}"
DEVCONTAINER_HOME="${AINATIVE_DEVCONTAINER_HOME:-/home/vscode}"
HOST_CONFIG_ROOT="${AINATIVE_DEVCONTAINER_HOST_CONFIG:-/mnt/host-config}"
HOST_COPILOT_ROOT="${AINATIVE_DEVCONTAINER_HOST_COPILOT:-/mnt/host-copilot}"
HOST_CODEX_ROOT="${AINATIVE_DEVCONTAINER_HOST_CODEX:-/mnt/host-codex}"

declare -a REQUIRED_FILES=(
  "${DEVCONTAINER_HOME}/.gitconfig"
)

declare -a REQUIRED_DIRS=(
  "${DEVCONTAINER_HOME}/.ssh"
)

declare -a OPTIONAL_DIRS=(
  "${DEVCONTAINER_HOME}/.config/gh"
  "${DEVCONTAINER_HOME}/.copilot"
)

declare -a OPTIONAL_FILES=(
  "${DEVCONTAINER_HOME}/.codex/auth.json"
  "${DEVCONTAINER_HOME}/.codex/config.toml"
  "${DEVCONTAINER_HOME}/.copilot/config.json"
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

link_optional_file() {
  local source_path="$1"
  local target_path="$2"

  if [[ -f "${source_path}" ]] && [[ ! -e "${target_path}" ]]; then
    mkdir -p "$(dirname "${target_path}")"
    ln -s "${source_path}" "${target_path}"
    echo "[linked] ${target_path} -> ${source_path}"
  fi
}

link_optional_dir "${HOST_CONFIG_ROOT}/gh" "${DEVCONTAINER_HOME}/.config/gh"
link_optional_dir "${HOST_COPILOT_ROOT}" "${DEVCONTAINER_HOME}/.copilot"
link_optional_file "${HOST_CODEX_ROOT}/auth.json" "${DEVCONTAINER_HOME}/.codex/auth.json"
link_optional_file "${HOST_CODEX_ROOT}/config.toml" "${DEVCONTAINER_HOME}/.codex/config.toml"

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

for path in "${OPTIONAL_FILES[@]}"; do
  if [[ -f "${path}" ]]; then
    echo "[ok] ${path}"
  else
    echo "[optional-missing] ${path}"
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
  echo "Check .devcontainer/compose.yaml and confirm ~/.ssh and ~/.gitconfig exist on the host." >&2
  echo "Codex and Copilot credentials are optional provider mounts." >&2
  echo "When ainative.yaml is missing, AI Native auto-detects a ready provider and prefers Codex when both are available." >&2
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
