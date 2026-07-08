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
