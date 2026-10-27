#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
umask 077

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "Missing .venv. Create it with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt soundfile numpy" >&2
  exit 1
fi

# Triton may compile a tiny CUDA helper at runtime. Find Python.h without
# requiring system-wide package changes on this machine.
python_include="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_config_var("INCLUDEPY"))')"
if [[ ! -f "$python_include/Python.h" ]]; then
  python_include="/home/wowbharat/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/include/python3.12"
fi
if [[ ! -f "$python_include/Python.h" ]]; then
  echo "Python.h was not found. Install the matching Python development headers." >&2
  exit 1
fi
export CPATH="$python_include${CPATH:+:$CPATH}"

export VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.55}"
export VLLM_MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-4096}"
export VLLM_ENFORCE_EAGER="${VLLM_ENFORCE_EAGER:-true}"
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"
export VLLM_LOGGING_LEVEL="${VLLM_LOGGING_LEVEL:-ERROR}"
export SNAC_DEVICE="${SNAC_DEVICE:-cpu}"
export SNAC_COMPILE="${SNAC_COMPILE:-false}"
export ADMIN_API_KEY_FILE="${ADMIN_API_KEY_FILE:-$PWD/.secrets/admin.key}"
export ADMIN_LOGIN_FILE="${ADMIN_LOGIN_FILE:-$PWD/.secrets/admin-login.json}"
export ACCESS_DB_PATH="${ACCESS_DB_PATH:-$PWD/.data/access.sqlite3}"

.venv/bin/python bootstrap_access.py

exec .venv/bin/uvicorn server:app --app-dir api --host "${API_HOST:-127.0.0.1}" --port "${API_PORT:-8088}" --no-proxy-headers --ws-max-size 10485760
