#!/bin/zsh
# doci: launchd エージェント(定時実行)を「現在のプロジェクト位置」から生成・再ロードする。
# プロジェクトを将来移動した場合、これを1回実行すれば復旧する。
#
# 使い方:
#   tools/install_launchd.sh [StartInterval秒 (デフォルト 10800)] [channel]
# channel 未指定時は --all-channels を逐次実行する単一ジョブを登録する。

PROJ="${0:A:h:h}"
INTERVAL="${1:-10800}"
CHANNEL="${2:-}"
if [[ ! "$INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid interval: $INTERVAL" >&2
  exit 2
fi
if [[ -n "$CHANNEL" && ! "$CHANNEL" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "invalid channel id: $CHANNEL" >&2
  exit 2
fi
if [[ -n "$CHANNEL" ]]; then
  LABEL="com.azumag.doci.generate.$CHANNEL"
  RUN_ARGS_XML="    <string>--channel</string>
    <string>$CHANNEL</string>"
else
  LABEL="com.azumag.doci.generate"
  RUN_ARGS_XML="    <string>--all-channels</string>"
fi
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>$PROJ/tools/cron_generate.sh</string>
$RUN_ARGS_XML
  </array>
  <key>RunAtLoad</key>
  <false/>
  <key>StartInterval</key>
  <integer>$INTERVAL</integer>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/doci.launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/doci.launchd.err.log</string>
</dict>
</plist>
EOF

if [[ "${DOCI_LAUNCHD_DRY_RUN:-0}" == "1" ]]; then
  echo "=== dry-run: $PLIST ==="
  cat "$PLIST"
  exit 0
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "=== $LABEL 再ロード完了 ==="
launchctl print "gui/$(id -u)/$LABEL" | grep -E "program = |interval = "
