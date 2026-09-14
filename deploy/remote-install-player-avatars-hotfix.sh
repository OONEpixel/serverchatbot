#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This hotfix installer must run as root." >&2
  exit 1
fi

BACKEND_DIR="/opt/matrix-serverchat"
PACKAGE="/tmp/matrix-serverchat-backend-0.4.0-player-avatars-hotfix.tar.gz"
EXPECTED_SHA256="0ADD9992B821D295901E804B6D24249F8F3FA816A42BF0AF97FF0B372F7D4153"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ ! -f "$PACKAGE" ]]; then
  echo "Missing hotfix package: $PACKAGE" >&2
  exit 1
fi
if [[ ! -d "$BACKEND_DIR" || ! -f "$BACKEND_DIR/compose.yaml" ]]; then
  echo "Backend directory is not a valid deployment: $BACKEND_DIR" >&2
  exit 1
fi

ACTUAL_SHA256="$(sha256sum "$PACKAGE" | awk '{print toupper($1)}')"
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  echo "Hotfix package SHA-256 mismatch." >&2
  echo "Expected: $EXPECTED_SHA256" >&2
  echo "Actual:   $ACTUAL_SHA256" >&2
  exit 1
fi

cd "$BACKEND_DIR"
if [[ -f data/bridge.db ]]; then
  BACKUP_PATH="/data/bridge.db.pre-player-avatars-$STAMP"
  if docker compose exec -T matrix-serverchat python -c \
    'import sqlite3,sys; source=sqlite3.connect("/data/bridge.db"); target=sqlite3.connect(sys.argv[1]); source.backup(target); target.close(); source.close()' \
    "$BACKUP_PATH"; then
    echo "SQLite online backup created: $BACKEND_DIR/data/$(basename "$BACKUP_PATH")"
  else
    cp -a -- data/bridge.db "data/bridge.db.pre-player-avatars-$STAMP"
    echo "Container backup was unavailable; a database file backup was created."
  fi
fi

tar -xzf "$PACKAGE" -C "$BACKEND_DIR"
docker compose config --quiet
docker compose up -d --build --no-deps --force-recreate matrix-serverchat
docker compose ps matrix-serverchat

echo
echo "Player-avatar hotfix installed successfully."
echo "Minecraft plugins were not changed or restarted."
echo "Send a Minecraft chat message and check the Matrix timeline to verify it."
