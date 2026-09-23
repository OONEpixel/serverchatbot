#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This hotfix installer must run as root." >&2
  exit 1
fi

BACKEND_DIR="/opt/matrix-serverchat"
PACKAGE="/tmp/matrix-serverchat-backend-0.4.0-sticker-label-hotfix.tar.gz"
EXPECTED_SHA256="A1B006F4994F6AB34B2D2EA7EC2C3DA0B25F09D7322713E2015B38AC7C01FC9C"
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
  BACKUP_PATH="/data/bridge.db.pre-sticker-label-$STAMP"
  if docker compose exec -T matrix-serverchat python -c \
    'import sqlite3,sys; source=sqlite3.connect("/data/bridge.db"); target=sqlite3.connect(sys.argv[1]); source.backup(target); target.close(); source.close()' \
    "$BACKUP_PATH"; then
    echo "SQLite online backup created: $BACKEND_DIR/data/$(basename "$BACKUP_PATH")"
  else
    cp -a -- data/bridge.db "data/bridge.db.pre-sticker-label-$STAMP"
    echo "Container backup was unavailable; a database file backup was created."
  fi
fi

tar -xzf "$PACKAGE" -C "$BACKEND_DIR"
docker compose config --quiet
docker compose up -d --build --no-deps --force-recreate matrix-serverchat
docker compose ps matrix-serverchat

echo
echo "Sticker-label hotfix installed successfully."
echo "Minecraft server files were not changed or restarted."
echo "Send a Matrix sticker and verify that Minecraft displays [贴纸]."
