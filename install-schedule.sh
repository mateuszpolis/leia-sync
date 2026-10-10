#!/bin/sh
# Installs a macOS LaunchAgent that runs the Claude sync task every day.
# Usage: ./install-schedule.sh [hour] [minute]     (default 07:40)
#        ./install-schedule.sh remove
set -eu

repo=$(cd "$(dirname "$0")" && pwd)
label=pl.edu.pw.leia-sync
plist="$HOME/Library/LaunchAgents/$label.plist"

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
if [ "${1:-}" = remove ]; then
  rm -f "$plist"
  echo "removed"
  exit 0
fi

claude=$(command -v claude) || { echo "claude CLI not found"; exit 1; }
uv=$(command -v uv) || { echo "uv not found"; exit 1; }

cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>WorkingDirectory</key><string>$repo</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(dirname "$claude"):$(dirname "$uv"):/usr/bin:/bin</string>
  </dict>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>-c</string>
    <string>exec > last-run.md 2>&amp;1; claude -p --allowedTools "Bash(uv run leia_sync.py:*)" Read Edit &lt; scheduled-task-prompt.md</string>
  </array>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>${1:-7}</integer>
    <key>Minute</key><integer>${2:-40}</integer>
  </dict>
</dict></plist>
EOF

launchctl bootstrap "gui/$(id -u)" "$plist"
echo "installed: runs daily at ${1:-7}:${2:-40}, report in $repo/last-run.md"
echo "run now:   launchctl kickstart gui/$(id -u)/$label"
