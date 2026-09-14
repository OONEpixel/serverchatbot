# 0.4.0 升级顺序

0.4.0 使用协议 v2，与 0.3.x 不兼容。后端升级到 v2 后，旧插件会暂时断开，
直到替换为 0.4.0 并使用已登记的独立 token。

1. 备份 `/opt/matrix-serverchat/data/bridge.db`。
2. 解压 0.4.0 后端包并重建容器。旧绑定会自动迁移为全局绑定。
3. 从服务器控制台使用 `app.admin add-admin` 登记第一名 `owner`。未登记前，
   所有 Matrix `!server`/`#server` 指令都会被拒绝。
4. 确定 `BINDING_MODE`：离线/混合服用 `scoped`；全部正版服可用
   `online_uuid`。
5. 每台服务器执行 `/matrixbridge token generate`，再运行带
   `--prompt-token` 的 `app.admin add-server` 并通过隐藏输入粘贴 token。
6. 在每台服务器插件配置中填写对应的 ID、名称、组和 token，并替换 0.4.0
   JAR 后重启 Minecraft。
7. 用 `list-servers` 检查 `last_seen`，再测试 Matrix、绑定、`@` 与跨服聊天。

示例：

```bash
cd /opt/matrix-serverchat
sudo cp data/bridge.db data/bridge.db.pre-0.4.0
sudo docker compose up -d --build --remove-orphans

sudo docker compose exec matrix-serverchat \
  python -m app.admin add-server --id vanilla --name 原版服 --group main \
  --port 25565 --prompt-token
sudo docker compose exec matrix-serverchat \
  python -m app.admin add-server --id survival --name 生存服 --group main \
  --port 25566 --prompt-token
sudo docker compose exec matrix-serverchat \
  python -m app.admin add-server --id creative --name 创造服 --group main \
  --port 25567 --prompt-token

sudo docker compose exec matrix-serverchat python -m app.admin list-servers
```

跨机器连接只使用 `wss://`。若后端仍通过 Compose 绑定
`127.0.0.1:8765`，应由同机 Nginx/Caddy 终止 TLS 并反向代理 `/minecraft`。
