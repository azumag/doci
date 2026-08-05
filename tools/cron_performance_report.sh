#!/bin/zsh
# doci: 実績フィードバックの3日毎レポートissueサイクル（launchdから毎日起動、
# 実際の発行頻度はPython側のPERFORMANCE_REPORT_MIN_INTERVAL_HOURSゲートで
# 3日に1回程度に保つ）。cron_generate.shと違い動画生成は行わないため、
# VOICEVOX(OrbStack)の起動待ちは不要。PATH設定はcron_generate.shに合わせる
# （tactic_issues.py経由のgh呼び出しが同じPATHで既に実績あり）。

export HOME="/Users/azumag"
PROJ="${0:A:h:h}"
nvm_node_bins=(/Users/azumag/.nvm/versions/node/*/bin(N/n[-1]))
NVM_NODE_BIN="${nvm_node_bins[1]}"
export PATH="$PROJ/tools/ffbin:${NVM_NODE_BIN:-/Users/azumag/.nvm/versions/node/v24.18.0/bin}:/Users/azumag/.local/bin:/Users/azumag/.opencode/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

LOG="$PROJ/output/cron_performance_report.log"
PY="$PROJ/.venv-cron/bin/python"
cd "$PROJ" || exit 1
mkdir -p "$PROJ/output"

ts() { date "+%Y-%m-%d %H:%M:%S"; }
echo "[$(ts)] ===== performance_report run start =====" >> "$LOG"

# 多重起動を防止する。
LOCK="$PROJ/output/.cron_performance_report.lock"
if [ -e "$LOCK" ]; then
  pid=$(cat "$LOCK" 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "[$(ts)] 前回の実行(pid=$pid)が継続中。スキップ。" >> "$LOG"
    exit 0
  fi
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

"$PY" -m doci.performance_report --apply >> "$LOG" 2>&1
rc=$?
echo "[$(ts)] ===== performance_report run end rc=$rc =====" >> "$LOG"
exit $rc
