#!/bin/bash
# FM Snippet Helper - uninstall (macOS)
# Thomas De Smet <tdesmet@oogi.io>

AGENT="$HOME/Library/LaunchAgents/io.oogi.fm-snippet-helper.plist"
DIR="$HOME/Library/Application Support/FM Snippet Helper"
launchctl unload "$AGENT" 2>/dev/null || true
rm -f "$AGENT"
rm -rf "$DIR"
echo "FM Snippet Helper removed."
if [ "$TERM_PROGRAM" = "Apple_Terminal" ]; then
  sleep 2
  osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 &
fi
