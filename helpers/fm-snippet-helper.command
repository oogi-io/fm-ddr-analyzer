#!/bin/bash
# FM Snippet Helper (macOS) — part of FM DDR Analyzer
# https://github.com/oogi-io/fm-ddr-analyzer
#
# Converts fmxmlsnippet XML *text* on the clipboard (e.g. from the web app's
# "Copy FM snippet" button) into real FileMaker clipboard objects (XMSS),
# so they paste straight into Script Workspace.
#
# One-time setup: chmod +x "fm-snippet-helper.command"
# Then double-click it (or bind it to a hotkey) after copying snippet XML.

set -e
TEXT=$(pbpaste)

case "$TEXT" in
  '<fmxmlsnippet'*) ;;
  *) echo "Clipboard does not contain fmxmlsnippet XML — copy a snippet first."; exit 1 ;;
esac

HEX=$(printf '%s' "$TEXT" | xxd -p | tr -d '\n' | tr 'a-f' 'A-F')

# via a temp file: large scripts can exceed the argv size limit
TMP=$(mktemp /tmp/fm-snippet.XXXXXX.applescript)
printf 'set the clipboard to \302\253data XMSS%s\302\273\n' "$HEX" > "$TMP"
osascript "$TMP"
rm -f "$TMP"

echo "Done — paste into FileMaker Script Workspace."
