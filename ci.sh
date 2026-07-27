#!/usr/bin/env bash
# Local equivalent of the CI job: unit tests, then a MOCK server + gold-set gate
# + injection tests. Any failure aborts (set -e); the server is always killed.
set -euo pipefail

export MOCK_LLM=1
PORT="${PORT:-8000}"
export BASE_URL="${BASE_URL:-http://127.0.0.1:${PORT}}"

cd "$(dirname "$0")"

# Prefer a local .venv, then python, then python3.
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"
elif command -v python >/dev/null 2>&1; then PY="python"
else PY="python3"; fi
echo "using: $PY ($($PY --version 2>&1))"

echo "== unit tests (obs self-check + pytest) =="
"$PY" obs.py
"$PY" -m pytest -q --ignore=test_injection.py

echo "== start MOCK server =="
"$PY" app.py &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

echo "== wait for :${PORT} =="
for i in $(seq 1 60); do
  if curl -sf "${BASE_URL}/graph" >/dev/null 2>&1; then
    echo "server up"; break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "server exited early"; exit 1
  fi
  sleep 1
  [ "$i" = 60 ] && { echo "server never came up"; exit 1; }
done

echo "== gold-set eval (gate) =="
"$PY" eval_gold.py

echo "== injection / safety tests =="
"$PY" -m pytest -q test_injection.py

echo "== ci.sh: all green =="
