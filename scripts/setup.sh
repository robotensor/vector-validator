#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
if command -v uv >/dev/null 2>&1; then
  uv venv --allow-existing .venv --python "${PYTHON:-3.12}"
  uv pip install --python .venv/bin/python --require-hashes -r requirements.lock
  uv pip install --python .venv/bin/python --no-deps -e .
else
  [[ -x .venv/bin/python ]] || "${PYTHON:-python3}" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install --require-hashes -r requirements.lock
  .venv/bin/pip install --no-deps -e .
fi
if [[ ! -f config.yaml ]]; then
  install -m 600 vector_validator/config.example.yaml config.yaml
  echo "wrote config.yaml: fill in api, netuid, weights_pubkey and wallet"
fi
echo "ready: .venv/bin/vector-validator --config config.yaml config show | run --dry-run --once"
