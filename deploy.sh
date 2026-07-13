#!/bin/bash
# Deploy fmsonar (the web app) to fmsonar.com (Cloudflare Pages).
#
#   ./deploy.sh
#
# Reads Cloudflare credentials from a local, untracked .env.local (variables
# CLOUDFLARE_API_TOKEN_TDESMET / CLOUDFLARE_ACCOUNT_ID_TDESMET). Maintainer
# script; a self-hoster would point it at their own token/account vars.

set -e
cd "$(dirname "$0")"

# Only push production from a clean main checkout, so the stamped build hash
# actually identifies what is live. A dirty tree is stamped "-dirty".
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "main" ]; then
  echo "deploy.sh: refusing to deploy from branch '$BRANCH' (expected main)." >&2
  exit 1
fi
DIRTY=""
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "deploy.sh: working tree is dirty; stamping build as -dirty." >&2
  DIRTY="-dirty"
fi

# Export ONLY the two Cloudflare vars into wrangler's environment. Sourcing the
# whole .env.local would hand every unrelated secret it may hold to npx and any
# npm lifecycle script it runs.
ENV_FILE=../../.env.local
get_var(){ grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | sed 's/^"//;s/"$//'; }
export CLOUDFLARE_API_TOKEN="$(get_var CLOUDFLARE_API_TOKEN_TDESMET)"
export CLOUDFLARE_ACCOUNT_ID="$(get_var CLOUDFLARE_ACCOUNT_ID_TDESMET)"
if [ -z "$CLOUDFLARE_API_TOKEN" ] || [ -z "$CLOUDFLARE_ACCOUNT_ID" ]; then
  echo "deploy.sh: missing CLOUDFLARE_*_TDESMET in $ENV_FILE" >&2
  exit 1
fi

DIST=$(mktemp -d)
BUILD="$(git rev-parse --short HEAD)$DIRTY · $(date +%Y-%m-%d)"
sed "s/__BUILD__/$BUILD/" fm_ddr/web/index.html > "$DIST/index.html"
# Fail loudly if the build stamp was not substituted (renamed marker) rather
# than silently shipping the literal placeholder.
if ! grep -qF "$BUILD" "$DIST/index.html"; then
  echo "deploy.sh: __BUILD__ marker not found in index.html; aborting." >&2
  rm -rf "$DIST"; exit 1
fi
cp fm_ddr/web/about.html "$DIST/about.html"
cp fm_ddr/web/_headers "$DIST/_headers"
for asset in favicon.svg favicon-32.png apple-touch-icon-180.png og-image.png; do
  cp "fm_ddr/web/$asset" "$DIST/$asset"
done
# Deprecation stub: the retired watcher installer used to live at /install.sh.
# Shipping a harmless stub guarantees any old `curl .../install.sh | bash`
# installs nothing (CF Pages otherwise retains the old asset).
cp helpers/install.sh "$DIST/install.sh"

npx wrangler pages deploy "$DIST" --project-name fmsonar --branch main
rm -rf "$DIST"
echo "Deployed build $BUILD -> https://fmsonar.com"
