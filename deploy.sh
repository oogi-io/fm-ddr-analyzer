#!/bin/bash
# Deploy FMSonar (the web app) to fmsonar.com (Cloudflare Pages).
#
#   ./deploy.sh
#
# Uses the tdesmet@oogi.io Cloudflare account (CLOUDFLARE_API_TOKEN_TDESMET /
# CLOUDFLARE_ACCOUNT_ID_TDESMET from the workspace root .env.local).

set -e
cd "$(dirname "$0")"

set -a; source ../../.env.local; set +a
export CLOUDFLARE_API_TOKEN="$CLOUDFLARE_API_TOKEN_TDESMET"
export CLOUDFLARE_ACCOUNT_ID="$CLOUDFLARE_ACCOUNT_ID_TDESMET"

DIST=$(mktemp -d)
BUILD="$(git rev-parse --short HEAD) · $(date +%Y-%m-%d)"
sed "s/__BUILD__/$BUILD/" fm_ddr/web/index.html > "$DIST/index.html"
cp helpers/install.sh "$DIST/install.sh"

npx wrangler pages deploy "$DIST" --project-name fmsonar --branch main
rm -rf "$DIST"
echo "Deployed build $BUILD -> https://fmsonar.com"
