#!/usr/bin/env bash
# Save your Hevy API key on the server (Hevy app → Settings → Developer; needs Hevy Pro).
set -euo pipefail
cd "$(dirname "$0")"
read -rsp "Paste your Hevy API key (hidden), then Enter: " KEY; echo
[ -n "$KEY" ] || { echo "No key entered."; exit 1; }
grep -v '^HEVY_API_KEY=' .env > .env.tmp || true
echo "HEVY_API_KEY=$KEY" >> .env.tmp && mv .env.tmp .env && chmod 600 .env
sudo docker compose --env-file .env up -d
echo "Saved. Claude can now read your Hevy routines."
