#!/usr/bin/env bash
# One-shot setup on a fresh Ubuntu VPS. Run from the repo:  bash deploy/setup-vps.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

# Open the firewall for HTTPS (Ubuntu images on Oracle Cloud block it by default).
if command -v iptables >/dev/null; then
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT || true
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT || true
  command -v netfilter-persistent >/dev/null && sudo netfilter-persistent save || true
fi

IP=$(curl -fsS https://api.ipify.org)
DOMAIN="${IP//./-}.sslip.io"   # free hostname that points at this server

if [ ! -f .env ]; then
  SECRET=$(openssl rand -hex 24)
  cat > .env <<ENV
MCP_SECRET=$SECRET
TZ_NAME=Europe/Lisbon
DOMAIN=$DOMAIN
ENV
  chmod 600 .env
fi
set -a; . ./.env; set +a
mkdir -p data

if [ ! -f data/garmin_tokens.json ] && [ -z "${GARMIN_TOKENS:-}" ]; then
  echo
  echo "=== Garmin login (one time) ==="
  sudo docker compose --env-file .env build garmin
  until sudo docker compose --env-file .env run --rm garmin python login.py --save /data/garmin_tokens.json; do
    echo "Login failed — let's try again (Ctrl+C to stop)."
  done
fi

sudo docker compose --env-file .env up -d --build
echo
echo "Done. Add this URL in claude.ai → Settings → Connectors → Add custom connector:"
echo "  https://$DOMAIN/mcp/$MCP_SECRET"
echo "Health check: https://$DOMAIN/health"
