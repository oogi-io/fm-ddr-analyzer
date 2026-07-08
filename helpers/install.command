#!/bin/bash
# FM Snippet Helper - install (macOS)
# Thomas De Smet <tdesmet@oogi.io> - MIT
#
# Installs a tiny background watcher: from now on, every fmxmlsnippet XML
# you copy becomes instantly paste-ready in FileMaker. Nothing to run again.
# Remove any time with uninstall.command.

set -e
DIR="$HOME/Library/Application Support/FM Snippet Helper"
AGENT="$HOME/Library/LaunchAgents/io.oogi.fm-snippet-helper.plist"
SRC="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$DIR" "$HOME/Library/LaunchAgents"
# the installed name is what macOS shows in Login Items & Extensions
cp "$SRC/fm-snippet-watcher.sh" "$DIR/FM Snippet Helper"
chmod +x "$DIR/FM Snippet Helper"

cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>io.oogi.fm-snippet-helper</string>
  <key>ProgramArguments</key><array><string>$DIR/FM Snippet Helper</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
PLIST

launchctl unload "$AGENT" 2>/dev/null || true
launchctl load "$AGENT"

echo "Installed. Copied FM snippets are now paste-ready automatically."
echo "(Remove any time by running uninstall.command.)"
if [ "$TERM_PROGRAM" = "Apple_Terminal" ]; then
  sleep 2
  osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 &
fi
