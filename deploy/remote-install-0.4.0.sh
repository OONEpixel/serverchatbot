#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "此脚本必须由 root 执行。" >&2
  exit 1
fi
if [[ "$#" -ne 1 ]]; then
  echo "缺少经过 base64 编码的 Minecraft 服务端目录。" >&2
  exit 1
fi

MC_INPUT="$(printf '%s' "$1" | base64 --decode)"
MC_DIR="$(realpath -e -- "$MC_INPUT")"
BACKEND_DIR="/opt/matrix-serverchat"
BACKEND_PACKAGE="/tmp/matrix-serverchat-backend-0.4.0.tar.gz"
PLUGIN_PACKAGE="/tmp/matrix-chat-bridge-0.4.0.jar"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

case "$MC_DIR" in
  /|/opt|/opt/matrix-serverchat)
    echo "拒绝使用过宽或与机器人目录冲突的 Minecraft 路径：$MC_DIR" >&2
    exit 1
    ;;
esac
if [[ ! -d "$MC_DIR/plugins" ]]; then
  echo "目录不存在：$MC_DIR/plugins；请确认 Minecraft 服务端目录。" >&2
  exit 1
fi
if [[ ! -f "$BACKEND_PACKAGE" || ! -f "$PLUGIN_PACKAGE" ]]; then
  echo "缺少上传到 /tmp 的 0.4.0 部署文件。" >&2
  exit 1
fi

install -d -m 0755 "$BACKEND_DIR"
if [[ -f "$BACKEND_DIR/data/bridge.db" ]]; then
  cd "$BACKEND_DIR"
  BACKUP_PATH="/data/bridge.db.pre-0.4.0-$STAMP"
  if docker compose exec -T matrix-serverchat python -c \
    'import sqlite3,sys; source=sqlite3.connect("/data/bridge.db"); target=sqlite3.connect(sys.argv[1]); source.backup(target); target.close(); source.close()' \
    "$BACKUP_PATH"; then
    echo "SQLite 在线备份已创建：$BACKEND_DIR/data/$(basename "$BACKUP_PATH")"
  else
    cp -a -- "$BACKEND_DIR/data/bridge.db" \
      "$BACKEND_DIR/data/bridge.db.pre-0.4.0-$STAMP"
    echo "容器不可用，已创建文件备份。"
  fi
fi

tar -xzf "$BACKEND_PACKAGE" -C "$BACKEND_DIR"
cd "$BACKEND_DIR"
docker compose config --quiet
docker compose up -d --build --remove-orphans

PLUGIN_DIR="$MC_DIR/plugins"
BACKUP_DIR="$PLUGIN_DIR/.matrixbridge-backup-$STAMP"
install -d -m 0755 "$BACKUP_DIR"
while IFS= read -r -d '' old_jar; do
  mv -- "$old_jar" "$BACKUP_DIR/"
done < <(find "$PLUGIN_DIR" -maxdepth 1 -type f \
  -name 'matrix-chat-bridge-*.jar' -print0)
install -m 0644 "$PLUGIN_PACKAGE" \
  "$PLUGIN_DIR/matrix-chat-bridge-0.4.0.jar"

echo
echo "后端和插件文件部署完成。旧插件 JAR（如存在）位于：$BACKUP_DIR"
echo "插件配置目录通常为：$PLUGIN_DIR/MatrixChatBridge/config.yml"
echo "接下来："
echo "1. 检查 server-id、server-name、relay-group 和 game-port。"
echo "2. 重启 Minecraft 服务端。"
echo "3. 游戏 OP 执行 /matrixbridge token generate；控制台不写斜杠。"
echo "4. 回到 $BACKEND_DIR 执行 app.admin add-server --prompt-token 完成登记。"
