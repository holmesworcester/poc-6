#!/usr/bin/env bash
set -euo pipefail

RECREATE=0
if [[ ${1-} == "--recreate" ]]; then
  RECREATE=1
fi

VENV_DIR=venv

if [[ $RECREATE -eq 1 && -d "$VENV_DIR" ]]; then
  rm -rf "$VENV_DIR"
fi

if [[ ! -d "$VENV_DIR" ]]; then
  python -m venv "$VENV_DIR"
fi

if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
  echo "No activate script found in $VENV_DIR/bin/activate"
  exit 1
fi

source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt

echo "Done. To activate the venv in your shell run: source $VENV_DIR/bin/activate"
