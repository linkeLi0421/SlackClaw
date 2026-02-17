#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

PYTHON_BIN="python3"
if [[ -x "${REPO_ROOT}/.venv/bin/python3" ]]; then
  PYTHON_BIN="${REPO_ROOT}/.venv/bin/python3"
fi

"${PYTHON_BIN}" -m pip install -r requirements.txt pyinstaller
"${PYTHON_BIN}" -m PyInstaller \
  packaging/launcher.py \
  --name SlackClaw \
  --onefile \
  --clean \
  --paths src \
  --hidden-import websocket

BIN_NAME="SlackClaw"
if [[ -f "dist/SlackClaw.exe" ]]; then
  BIN_NAME="SlackClaw.exe"
fi

OS_NAME="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "${OS_NAME}" in
  darwin) OS_NAME="macos" ;;
  mingw*|msys*|cygwin*) OS_NAME="windows" ;;
esac

ARCH_NAME="$(uname -m | tr '[:upper:]' '[:lower:]')"
case "${ARCH_NAME}" in
  x86_64|amd64) ARCH_NAME="x64" ;;
  aarch64|arm64) ARCH_NAME="arm64" ;;
esac

EXT=""
if [[ "${BIN_NAME}" == *.exe ]]; then
  EXT=".exe"
fi

RELEASE_NAME="SlackClaw-${OS_NAME}-${ARCH_NAME}${EXT}"
mkdir -p release
find release -mindepth 1 -maxdepth 1 -exec rm -rf {} +
cp "dist/${BIN_NAME}" "release/${RELEASE_NAME}"
if [[ "${EXT}" != ".exe" ]]; then
  chmod +x "release/${RELEASE_NAME}"
fi

echo "Build complete."
echo "Binary: dist/${BIN_NAME}"
echo "Release file: release/${RELEASE_NAME}"
echo "Run first-time setup with: ./release/${RELEASE_NAME} --setup"
