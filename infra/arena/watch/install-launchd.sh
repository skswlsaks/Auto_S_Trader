#!/usr/bin/env bash
# Install a macOS LaunchAgent that submits finished experiments to the Arena every 10 minutes.
# Usage: infra/arena/watch/install-launchd.sh [s3://bucket]   (default: terraform output arena_remote)
# Remove: launchctl bootout gui/$(id -u)/com.strader.arena-watch && rm ~/Library/LaunchAgents/com.strader.arena-watch.plist
set -euo pipefail
repo="$(cd "$(dirname "$0")/../../.." && pwd)"
remote="${1:-$(terraform -chdir="$repo/infra/arena" output -raw arena_remote)}"
uv="$(command -v uv)"
label="com.strader.arena-watch"
plist="$HOME/Library/LaunchAgents/$label.plist"
log="$HOME/Library/Logs/arena-watch.log"
path="$(dirname "$uv"):$(dirname "$(command -v aws)"):$(dirname "$(command -v gh)"):/usr/bin:/bin:/usr/sbin:/sbin"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>$uv</string><string>run</string><string>--directory</string><string>$repo</string><string>--quiet</string>
    <string>python</string><string>-m</string><string>arena</string><string>study</string><string>watch</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>ARENA_REMOTE</key><string>$remote</string>
    <key>PATH</key><string>$path</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$log</string>
  <key>StandardErrorPath</key><string>$log</string>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "installed $label (every 10 min) -> $remote; log: $log"
