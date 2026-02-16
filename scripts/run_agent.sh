#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${REPO_ROOT}/.env"
  set +a
fi

PYTHON_BIN="python3"
if [[ -x "${REPO_ROOT}/.venv/bin/python3" ]]; then
  PYTHON_BIN="${REPO_ROOT}/.venv/bin/python3"
fi

export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
cd "${REPO_ROOT}"
exec "${PYTHON_BIN}" -m slackclaw.app "$@"
