#!/bin/zsh
# doci: 自動生成→アップロード（cron から5時間ごとに実行）。
# cron は最小環境なので PATH/HOME を明示し、VOICEVOX(OrbStack) を起動してから実行する。
# コーナーは指定せず run_daily の自動交互（capitalism/communism）に任せる。

export HOME="/Users/azumag"
# 外付けボリューム(/Volumes/satelite=homebrew)のバイナリを「起動」すると背景launchd文脈で
# dyldが固まる。そこで実行時に使うバイナリは全て内蔵に寄せる:
#  - ffmpeg/ffprobe: tools/ffbin(静的・内蔵) を最優先
#  - node: nvm(内蔵) を homebrew より前に
#  - python: 下記 .venv-cron(uv管理standalone・内蔵)
#  - claude/opencode/orb/Chrome は元から内蔵
# /opt/homebrew(外付け) は最後＝フォールバックのみ（基本使わせない）。
export PATH="/Users/azumag/azumag/work/doci/tools/ffbin:/Users/azumag/.nvm/versions/node/v23.10.0/bin:/Users/azumag/.local/bin:/Users/azumag/.opencode/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

PROJ="/Users/azumag/azumag/work/doci"
LOG="$PROJ/output/cron.log"
PY="$PROJ/.venv-cron/bin/python"
cd "$PROJ" || exit 1
mkdir -p "$PROJ/output"

ts() { date "+%Y-%m-%d %H:%M:%S"; }
echo "[$(ts)] ===== cron run start =====" >> "$LOG"

# 多重起動防止（前回が走っていたらスキップ）
LOCK="$PROJ/output/.cron_generate.lock"
if [ -e "$LOCK" ]; then
  pid=$(cat "$LOCK" 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "[$(ts)] 前回の実行(pid=$pid)が継続中。スキップ。" >> "$LOG"
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

# パイプライン実行（生成→YouTube限定公開アップロード→履歴記録）
"$PY" -m doci.run_daily >> "$LOG" 2>&1
rc=$?
echo "[$(ts)] ===== cron run end rc=$rc =====" >> "$LOG"
exit $rc
