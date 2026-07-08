#!/bin/bash
# FM Snippet Helper (macOS) — part of FM DDR Analyzer
# https://github.com/oogi-io/fm-ddr-analyzer
# OOGI BV - Thomas De Smet <tdesmet@oogi.io> - MIT
#
# Converts fmxmlsnippet XML *text* on the clipboard (e.g. from the web app's
# "Copy FM snippet" button) into real FileMaker clipboard objects (XMSS),
# so they paste straight into Script Workspace.
#
# Install once: unzip, then on first run right-click -> Open (Gatekeeper).
# After that, double-click it (or bind it to a hotkey) after copying snippet XML.

set -e
TEXT=$(pbpaste)

case "$TEXT" in
  '<fmxmlsnippet'*) ;;
  *) echo "Clipboard does not contain fmxmlsnippet XML — copy a snippet first."; exit 1 ;;
esac

# pick the clipboard class from the first object inside the snippet
case "$TEXT" in
  *'<Step '*)            CLASS=XMSS ;;   # script steps
  *'<Script '*)          CLASS=XMSC ;;   # whole scripts
  *'<CustomFunction '*)  CLASS=XMFN ;;   # custom functions
  *'<BaseTable '*)       CLASS=XMTB ;;   # tables
  *'<Layout'*)           CLASS=XML2 ;;   # layout objects (checked before Field: they contain field objects)
  *'<Field '*)           CLASS=XMFD ;;   # field definitions
  *) echo "Unrecognized snippet content."; exit 1 ;;
esac

HEX=$(printf '%s' "$TEXT" | xxd -p | tr -d '\n' | tr 'a-f' 'A-F')

# via a temp file: large scripts can exceed the argv size limit
TMP=$(mktemp /tmp/fm-snippet.XXXXXX.applescript)
printf 'set the clipboard to \302\253data %s%s\302\273\n' "$CLASS" "$HEX" > "$TMP"
osascript "$TMP"
rm -f "$TMP"

echo "Done ($CLASS) — paste into FileMaker."

# close this Terminal window on success (errors above leave it open)
if [ "$TERM_PROGRAM" = "Apple_Terminal" ]; then
  osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 &
fi
