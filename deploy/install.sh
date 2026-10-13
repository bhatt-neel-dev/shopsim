#!/usr/bin/env bash
# Install or update ShopSim on a Docker host and (re)start it.
#   curl -fsSL https://raw.githubusercontent.com/bhatt-neel-dev/shopsim/main/deploy/install.sh | bash
# or, from a checkout:  bash deploy/install.sh
set -euo pipefail

REPO="${REPO:-https://github.com/bhatt-neel-dev/shopsim.git}"
DIR="${DIR:-$HOME/shopsim}"
SUDO=""
docker info >/dev/null 2>&1 || SUDO="sudo"

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO" "$DIR"
fi
cd "$DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  # Syslog from the host's primary IP so the receiver sees a real source address.
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$ip" ] && sed -i "s/^SYSLOG_HOST=.*/SYSLOG_HOST=$ip/" .env
fi

log_dir="$(grep -E '^LOG_DIR=' .env | cut -d= -f2-)"
$SUDO mkdir -p "${log_dir:-/var/log/shopsim}"

$SUDO docker compose up -d --build
$SUDO docker compose ps
echo
echo "Control panel: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8180"
