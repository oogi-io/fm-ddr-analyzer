#!/bin/bash
# The fmsonar clipboard "watcher" (a resident background LaunchAgent) has been
# RETIRED. fmsonar now converts the clipboard on demand instead of watching it.
#
# After copying a snippet from https://fmsonar.com, convert it once with:
#     fm-ddr clip
# Install the CLI with:
#     pipx install git+https://github.com/oogi-io/fm-ddr-analyzer
# No CLI? Use the one-shot helper offered in the app's "Copy FM snippet" popover.
#
# This script intentionally installs nothing.
echo "The fmsonar background clipboard watcher has been retired (nothing was installed)."
echo "Convert a copied snippet on demand instead:  fm-ddr clip"
echo "Install the CLI:  pipx install git+https://github.com/oogi-io/fm-ddr-analyzer"
exit 0
