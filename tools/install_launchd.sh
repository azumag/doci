#!/bin/zsh
# doci: launchd エージェント(定時実行)を「現在のプロジェクト位置」から生成・再ロードする。
# プロジェクトを将来移動した場合、これを1回実行すれば復旧する。
#
# 使い方:
#   tools/install_launchd.sh [StartInterval秒 (デフォルト 10800)]

PROJ="${0:A:h:h}"
INTERVAL="${1:-10800}"
LABEL="com.azumag.doci.generate"
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

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "=== $LABEL 再ロード完了 ==="
launchctl print "gui/$(id -u)/$LABEL" | grep -E "program = |interval = "
