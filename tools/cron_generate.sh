#!/bin/zsh
# doci: 自動生成→アップロード（launchd から定時実行）。
# cron は最小環境なので PATH/HOME を明示し、VOICEVOX(OrbStack) を起動してから実行する。
# 引数は run_daily へ透過する（--all-channels / --channel <id> 等）。

export HOME="/Users/azumag"
PROJ="${0:A:h:h}"
# 外付けボリューム(/Volumes/satelite=homebrew)のバイナリを「起動」すると背景launchd文脈で
# dyldが固まる。そこで実行時に使うバイナリは全て内蔵に寄せる:
#  - ffmpeg/ffprobe: tools/ffbin(静的・内蔵) を最優先
#  - node: nvm(内蔵) を homebrew より前に
#  - python: 下記 .venv-cron(uv管理standalone・内蔵)
#  - claude/opencode/orb/Chrome は元から内蔵
# /opt/homebrew(外付け) は最後＝フォールバックのみ（基本使わせない）。
export PATH="$PROJ/tools/ffbin:/Users/azumag/.nvm/versions/node/v23.10.0/bin:/Users/azumag/.local/bin:/Users/azumag/.opencode/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

RUN_NAME="default"
if [ "${1:-}" = "--all-channels" ]; then
  RUN_NAME="all"
elif [ "${1:-}" = "--channel" ] && [ -n "${2:-}" ]; then
  RUN_NAME="${2//[^A-Za-z0-9_-]/_}"
fi
LOG="$PROJ/output/cron_${RUN_NAME}.log"
PY="$PROJ/.venv-cron/bin/python"
cd "$PROJ" || exit 1
mkdir -p "$PROJ/output"

ts() { date "+%Y-%m-%d %H:%M:%S"; }
echo "[$(ts)] ===== cron run start =====" >> "$LOG"

# 確認Issueは生成・VOICEVOXの成否と切り離し、既存3時間ジョブのたびに先に取得する。
# 前回の長時間生成が継続中でも実行する。短いoperation lockはPython側で管理する。
"$PY" -m doci.run_daily --reconcile-youtube-reviews >> "$LOG" 2>&1
review_rc=$?
if [ "$review_rc" != "0" ]; then
  echo "[$(ts)] YouTube確認Issue処理失敗 rc=$review_rc。生成は継続。" >> "$LOG"
else
  export DOCI_REVIEW_RECONCILED=1
fi

# 新規生成だけ多重起動を防止する（確認Issue処理は上で完了済み）。
LOCK="$PROJ/output/.cron_generate_${RUN_NAME}.lock"
if [ -e "$LOCK" ]; then
  pid=$(cat "$LOCK" 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "[$(ts)] 前回の実行(pid=$pid)が継続中。生成をスキップ。" >> "$LOG"
    exit 0
  fi
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

# VOICEVOX(OrbStack) を起動して到達を待つ（最大 ~150s）
/usr/local/bin/orb start >> "$LOG" 2>&1
ok=0
for i in $(seq 1 30); do
  if curl -s --max-time 3 http://127.0.0.1:50021/version >/dev/null 2>&1; then ok=1; break; fi
  sleep 5
done
if [ "$ok" != "1" ]; then
  echo "[$(ts)] VOICEVOX 未到達。中止。" >> "$LOG"
  exit 1
fi

# パイプライン実行（生成→チャンネル別公開判定でYouTubeアップロード→履歴記録）
"$PY" -m doci.run_daily "$@" >> "$LOG" 2>&1
rc=$?
echo "[$(ts)] ===== cron run end rc=$rc =====" >> "$LOG"
exit $rc
