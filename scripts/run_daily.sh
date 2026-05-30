#!/usr/bin/env bash
# doci 日次実行ラッパー（launchd / cron から呼ぶ）
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

# venv があれば使う
if [ -x "$HERE/.venv/bin/python" ]; then
  PY="$HERE/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

LOG_DIR="$HERE/output/logs"
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d_%H%M%S)"

# 引数はそのまま run_daily へ（例: --no-upload, --corner communism）
exec "$PY" -m doci.run_daily "$@" >>"$LOG_DIR/run_${TS}.log" 2>&1
