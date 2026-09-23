#!/usr/bin/env bash
set -euo pipefail

VERSION="0.4.9"
TARGET_DIR="/opt/matrix-serverchat"
PACKAGE="${1:-./matrix-serverchat-backend-${VERSION}.tar.gz}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this installer as root: sudo bash install-${VERSION}.sh" >&2
  exit 1
fi
if [[ ! -f "$PACKAGE" ]]; then
  echo "Backend package not found: $PACKAGE" >&2
  exit 1
fi

install -d -m 0755 "$TARGET_DIR"
if [[ -f "$TARGET_DIR/data/bridge.db" ]]; then
  cd "$TARGET_DIR"
  BACKUP_NAME="bridge.db.pre-${VERSION}-${STAMP}"
  if [[ -f compose.yaml ]] && docker compose ps --status running matrix-serverchat \
      --format '{{.Name}}' 2>/dev/null | grep -qx 'matrix-serverchat'; then
    docker compose exec -T matrix-serverchat python -c \
      'import sqlite3,sys; source=sqlite3.connect("/data/bridge.db"); target=sqlite3.connect(sys.argv[1]); source.backup(target); target.close(); source.close()' \
      "/data/$BACKUP_NAME"
  else
    cp -a -- data/bridge.db "data/$BACKUP_NAME"
  fi
  echo "Database backup: $TARGET_DIR/data/$BACKUP_NAME"
fi

tar -xzf "$PACKAGE" -C "$TARGET_DIR"
cd "$TARGET_DIR"
bash install.sh

echo
echo "Backend ${VERSION} installed in $TARGET_DIR."
echo "Install matrix-chat-bridge-${VERSION}.jar in every Minecraft server's plugins directory,"
echo "preserve each existing plugins/MatrixChatBridge/config.yml, then restart each server."
