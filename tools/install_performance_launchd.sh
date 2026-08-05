#!/bin/zsh
# doci: 実績フィードバックレポートissueサイクル用launchdエージェントを
# 「現在のプロジェクト位置」から生成・再ロードする。プロジェクトを将来
# 移動した場合、これを1回実行すれば復旧する（tools/install_launchd.shと対）。
#
# 起動間隔は毎日(86400秒)固定。スリープ/再起動でStartIntervalタイマーが
# リセットされ3日間隔だと脱落しやすいため、日次起動＋Python側の
# PERFORMANCE_REPORT_MIN_INTERVAL_HOURS(既定72時間)ゲートで実質「3日に
# 1回程度」の頻度に保つ設計（詳細はREADME参照）。
#
# 使い方:
#   tools/install_performance_launchd.sh

PROJ="${0:A:h:h}"
LABEL="com.azumag.doci.performance"
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
    <string>$PROJ/tools/cron_performance_report.sh</string>
  </array>
  <key>RunAtLoad</key>
  <false/>
  <key>StartInterval</key>
  <integer>86400</integer>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/doci.performance.launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/doci.performance.launchd.err.log</string>
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
