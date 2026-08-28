#!/usr/bin/env bash
# Install Postiz on a Linux server (Docker Compose). Not for Windows/WSL.
# See scripts/POSTIZ.md for full steps.
set -euo pipefail

INSTALL_DIR="${HOME}/postiz-docker-compose"

sudo systemctl start docker 2>/dev/null || sudo service docker start 2>/dev/null || true

if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  git clone https://github.com/gitroomhq/postiz-docker-compose.git "$INSTALL_DIR"
else
  git -C "$INSTALL_DIR" pull --ff-only || true
fi

cd "$INSTALL_DIR"

# Generate a unique JWT secret if still placeholder
if grep -q "random string that is unique" docker-compose.yaml; then
  JWT="$(openssl rand -hex 32)"
  # Escape for sed
  sed -i "s|JWT_SECRET: 'random string that is unique to every install - just type random characters here!'|JWT_SECRET: '${JWT}'|" docker-compose.yaml
  echo "Set JWT_SECRET"
fi

# Single-user friendly: disable public registration after first signup
# Keep false for first boot so user can register; they can flip later.
# DISABLE_REGISTRATION already false in compose.

echo "Pulling images (this can take several minutes)..."
sudo docker compose pull

echo "Starting Postiz stack..."
sudo docker compose up -d

echo "Waiting for containers..."
sleep 15
sudo docker compose ps
echo "POSTIZ_START_OK"
echo "UI: http://localhost:4007"
echo "Temporal UI: http://localhost:8080"
