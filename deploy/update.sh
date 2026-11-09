#!/usr/bin/env bash
# Pull the latest code from GitHub and restart the server. Run:  bash ~/garmin-claude/deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."

KEY=~/.ssh/github_deploy
if [ ! -f "$KEY" ]; then
  # One-time: a read-only "deploy key" so this server can pull your private repo.
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  ssh-keygen -q -t ed25519 -N "" -f "$KEY" -C "garmin-server"
  cat >> ~/.ssh/config <<CFG
Host github.com
  IdentityFile $KEY
  IdentitiesOnly yes
CFG
  ssh-keyscan -q github.com >> ~/.ssh/known_hosts 2>/dev/null
  echo
  echo "=== One-time GitHub step ==="
  echo "1. Open: https://github.com/gtorrado/garmin-claude/settings/keys/new"
  echo "2. Title: garmin-server   (leave 'Allow write access' UNticked)"
  echo "3. Key: paste this whole line:"
  echo
  cat "$KEY.pub"
  echo
  read -rp "4. Click 'Add key' on GitHub, then press Enter here… " _
fi

git remote set-url origin git@github.com:gtorrado/garmin-claude.git
git pull --ff-only origin main
cd deploy
sudo docker compose --env-file .env up -d --build
echo
echo "Updated to: $(git log -1 --format='%h %s')"
