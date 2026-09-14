#!/usr/bin/env bash
set -euo pipefail

TARGET_DIR="/opt/matrix-serverchat"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 sudo bash install.sh 运行。" >&2
  exit 1
fi

cd "$TARGET_DIR"
install -d -m 0700 data secrets
chown -R 10001:10001 data

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 0600 .env
  echo "已生成 $TARGET_DIR/.env；请填写 Matrix 地址、机器人 ID 和房间 ID 后重新运行。" >&2
  exit 1
fi

if [[ ! -s secrets/matrix_password ]]; then
  read -r -s -p "Matrix 机器人密码: " matrix_password
  echo
  if [[ -z "$matrix_password" ]]; then
    echo "Matrix 密码不能为空。" >&2
    exit 1
  fi
  printf '%s' "$matrix_password" > secrets/matrix_password
  unset matrix_password
fi

chown 10001:10001 secrets/matrix_password
chmod 0400 secrets/matrix_password
docker compose config --quiet
docker compose up -d --build

echo
echo "部署完成。请在游戏/服务器控制台执行 /matrixbridge token generate，"
echo "再使用 app.admin add-server --prompt-token 完成首次登记。"
echo "还必须使用 app.admin add-admin --role owner 登记第一名 Matrix 管理员。"
echo "查看日志：cd $TARGET_DIR && sudo docker compose logs -f --tail=200"
