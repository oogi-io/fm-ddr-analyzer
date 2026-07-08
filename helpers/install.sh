#!/bin/bash
# FM Snippet Helper - one-line installer (macOS)
# Thomas De Smet <tdesmet@oogi.io> - MIT
# https://github.com/oogi-io/fm-ddr-analyzer
#
#   curl -fsSL <url>/install.sh | bash
#
# Installs a tiny background watcher: every fmxmlsnippet XML you copy
# becomes instantly paste-ready in FileMaker. Uninstall any time:
#   curl -fsSL <url>/install.sh | bash -s -- --uninstall

set -e
DIR="$HOME/Library/Application Support/FM Snippet Helper"
AGENT="$HOME/Library/LaunchAgents/io.oogi.fm-snippet-helper.plist"

if [ "$1" = "--uninstall" ]; then
  launchctl unload "$AGENT" 2>/dev/null || true
  rm -f "$AGENT"; rm -rf "$DIR"
  echo "FM Snippet Helper removed."
  exit 0
fi

mkdir -p "$DIR" "$HOME/Library/LaunchAgents"
cat > "$DIR/FM Snippet Helper" <<'WATCHER_EOF'
#!/bin/bash
# FM Snippet Watcher (macOS)
# Thomas De Smet <tdesmet@oogi.io> - MIT
# https://github.com/oogi-io/fm-ddr-analyzer
#
# Watches the clipboard; whenever fmxmlsnippet XML text is copied (e.g. from
# the FM DDR Analyzer web app), it adds the matching FileMaker clipboard
# flavor so it pastes straight into FileMaker (replacing the text flavor -
# copy again in the app if you need the XML text back).
# Installed as a LaunchAgent by install.command; removed by uninstall.command.

# LaunchAgents start with a bare C locale, under which pbpaste mangles
# non-ASCII characters - force UTF-8
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

LAST=""
while true; do
  T=$(pbpaste 2>/dev/null)
  if [ "$T" != "$LAST" ]; then
    LAST="$T"
    case "$T" in
      '<fmxmlsnippet'*)
        case "$T" in
          *'<Step '*)            CLASS=XMSS ;;
          *'<Script '*)          CLASS=XMSC ;;
          *'<CustomFunction '*)  CLASS=XMFN ;;
          *'<BaseTable '*)       CLASS=XMTB ;;
          *'<Layout'*)           CLASS=XML2 ;;
          *'<Field '*)           CLASS=XMFD ;;
          *)                     CLASS="" ;;
        esac
        if [ -n "$CLASS" ]; then
          # convert to the FileMaker flavor (UTF-8, byte-exact). The text
          # flavor is consumed - copy again in the app if you need the XML
          # text back. Keeping both flavors is possible but encoding-fragile.
          HEX=$(printf '%s' "$T" | xxd -p | tr -d '\n' | tr 'a-f' 'A-F')
          TMP=$(mktemp /tmp/fm-snip-watch.XXXXXX.applescript)
          printf 'set the clipboard to \302\253data %s%s\302\273\n' "$CLASS" "$HEX" > "$TMP"
          osascript "$TMP" >/dev/null 2>&1
          rm -f "$TMP"
        fi
        ;;
    esac
  fi
  sleep 1
done
WATCHER_EOF
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
