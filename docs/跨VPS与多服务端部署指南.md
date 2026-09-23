# MatrixChatBridge 跨 VPS 与多服务端部署指南

本文面向机器人后端迁移到独立 VPS、同一台 Minecraft 主机运行多个服务端的
场景，适用于当前 0.4.9（协议 v2）。

有关不同 Minecraft 版本及 Paper、Fabric、Forge/NeoForge 的插件端适配路线，
参见 [多平台与多版本适配规划](多平台与多版本适配规划.md)。

## 1. 项目统一名词

后续开发、管理文档和故障沟通统一使用以下名称：

| 正式名称 | 简称 | 指代内容 |
| --- | --- | --- |
| 插件端 | 前端 | 安装在 Leaf/Paper 服务端中的 Minecraft 插件 JAR |
| 机器人后端 | 后端 | Docker 中运行的 Python Matrix 机器人及桥接服务 |
| 机器人账号 | Bot 账号 | Matrix 中的 `@chatbot:matrix.example.org`，不是后端程序本身 |
| Matrix Homeserver | Matrix 服务器 | `https://matrix.example.org` 对应的 Matrix 平台 |
| Minecraft 服务端 | 游戏服 | 一套独立运行的 Leaf/Paper 服务器进程 |
| TLS 反向代理 | 网关 | 部署在后端 VPS 上的 Caddy 或 Nginx |

因此本文中的“前端”专指插件端，不是网页 UI；“后端”专指机器人桥接程序，
不是 Matrix Homeserver。推荐在命令、日志和工单中优先写完整的“插件端”或
“机器人后端”，只在上下文明确时使用“前端/后端”。

职责边界如下：

- 插件端（前端）：监听游戏事件、显示 Matrix/跨服消息、主动连接机器人后端；
- 机器人后端（后端）：身份鉴权、数据库、路由、去重、Matrix 收发和管理指令；
- Matrix Homeserver：提供 Matrix 账号、房间和消息网络；
- TLS 网关：提供公网 HTTPS/WSS 入口并转发到机器人后端。

## 2. 先区分三类端口

| 端口 | 示例 | 用途 | 是否每个 Minecraft 服务端独立 |
| --- | ---: | --- | --- |
| Minecraft 游戏端口 | 25565、25566 | 玩家连接游戏 | 是，同一 IP 上不能重复 |
| 后端内部 WebSocket 端口 | 8766 | 机器人接收所有插件连接 | 否，所有插件共用一个 |
| 公网 TLS 端口 | 443 | 插件通过 `wss://` 连接机器人 VPS | 否，所有插件共用一个 |

插件不是监听服务器，而是 WebSocket 客户端。因此，一台机器上即使运行十个
Minecraft 服务端，也不需要给插件分配十个桥接监听端口。所有插件都连接同一个
机器人地址，后端依靠每台服不同的 `server-id` 和 token 识别来源。

插件配置中的 `game-port` 是 Minecraft 游戏端口的管理元数据，不是机器人连接
端口，也不参与身份认证。建议它与该服 `server.properties` 的 `server-port`
保持一致。

## 3. 推荐网络结构

```text
Minecraft 主机 A
├─ 原版服   :25565 ─┐
├─ 生存服   :25566 ─┼─ 出站 WSS/443 ─┐
└─ 创造服   :25567 ─┘                │
                                      ▼
                           机器人 VPS 公网 :443
                           ├─ Caddy/Nginx（TLS）
                           └─ 127.0.0.1:8766
                                      │
                                      ▼
                              Matrix Homeserver :443
```

VPS 防火墙只需允许公网访问 SSH 管理端口和 TCP 443。`8766` 应继续绑定
`127.0.0.1`，只允许同机反向代理访问。

Minecraft 主机通常只需要允许到机器人域名 TCP 443 的出站连接，不需要为了
桥接功能开放新的入站端口。

## 4. 多个 Minecraft 服务端如何配置

假设机器人公网域名为 `bridge.example.com`，三个插件都使用同一个 URL：

### 原版服

```yaml
bridge:
  url: "wss://bridge.example.com/minecraft"
  server-id: "vanilla"
  server-name: "原版服"
  relay-group: "main"
  game-port: 25565
  token: "原版服自己的64位token"
```

### 生存服

```yaml
bridge:
  url: "wss://bridge.example.com/minecraft"
  server-id: "survival"
  server-name: "生存服"
  relay-group: "main"
  game-port: 25566
  token: "生存服自己的64位token"
```

### 创造服

```yaml
bridge:
  url: "wss://bridge.example.com/minecraft"
  server-id: "creative"
  server-name: "创造服"
  relay-group: "main"
  game-port: 25567
  token: "创造服自己的64位token"
```

必须保证：

- `server-id` 全局唯一且长期不变；
- 每台服使用独立 token，不能共用；
- `game-port` 在同一 IP 上不重复；
- 属于同一 `relay-group` 的服务器才互相转发；
- 后台登记的 ID、路由组与插件配置一致。

即使不同 Minecraft 服务端运行在不同物理机上，只要能访问机器人 VPS 的
TCP 443，也使用相同方式连接。

## 5. 为什么使用 WSS，而不是直接开放 HTTP 或 WS

当前插件与机器人需要双向实时推送，使用的是 WebSocket。`wss://` 本质上是
通过 HTTPS/TLS 建立连接后升级为 WebSocket，适合跨公网长期连接。

不要在公网使用：

```text
ws://机器人公网IP:8766/minecraft
```

明文 `ws://` 会暴露聊天内容、服务器 ID 和 Bearer token。推荐地址是：

```text
wss://bridge.example.com/minecraft
```

域名的 TLS 证书还可以让插件验证连接的确是自己的机器人服务器，降低中间人
攻击风险。

## 6. VPS 上的 Docker 端口设置

当前 Compose 保持以下映射即可：

```yaml
services:
  matrix-serverchat:
    environment:
      WS_HOST: "0.0.0.0"
      WS_PORT: "8766"
    ports:
      - "127.0.0.1:8766:8766"
```

容器内部监听 `0.0.0.0:8766`，但宿主机只发布到
`127.0.0.1:8766`。不要改成 `0.0.0.0:8766:8766`，也不需要为每个插件
增加一个 Docker 端口映射。

如果同一台 VPS 将来运行多个完全独立的机器人后端实例，每个实例才需要不同的
宿主机内部端口，例如 8766、8767；单个后端管理多台 Minecraft 服不需要这样做。

## 7. 使用 Caddy 提供 WSS

Caddy 配置较短，并能自动申请和续期证书。示例 `Caddyfile`：

```caddyfile
bridge.example.com {
    handle /minecraft {
        reverse_proxy 127.0.0.1:8766
    }

    handle {
        respond 404
    }
}
```

使用前应完成：

1. 将 `bridge.example.com` 的 A/AAAA 记录指向机器人 VPS；
2. 放行 VPS 的 TCP 80 和 443，证书申请成功后可以按需要限制 80；
3. 确认 8766 仍只绑定环回地址；
4. 让 Caddy 加载配置并检查日志。

Caddy 会自动处理 WebSocket 升级。反向代理会保留插件发送的 Authorization 和
服务器 ID 请求头。

## 8. 使用 Nginx 提供 WSS

示例配置：

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name bridge.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name bridge.example.com;

    ssl_certificate     /etc/letsencrypt/live/bridge.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bridge.example.com/privkey.pem;

    location = /minecraft {
        proxy_pass http://127.0.0.1:8766;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header X-Minecraft-Server-Id $http_x_minecraft_server_id;
        proxy_read_timeout 75s;
        proxy_send_timeout 75s;
    }

    location / {
        return 404;
    }
}
```

证书路径需按实际环境修改。配置完成后先运行 Nginx 配置检查，再平滑重载。
不要在反向代理访问日志中主动记录 `Authorization` 请求头。

## 9. 防火墙与代理平台注意事项

建议规则：

- 机器人 VPS：允许 TCP 443；SSH 只开放实际管理端口并尽量限制来源；
- 机器人 VPS：不允许公网直接访问 TCP 8766；
- Minecraft 主机：允许到机器人域名 TCP 443 的出站连接；
- Matrix Homeserver：机器人 VPS 能访问其 HTTPS 443；
- 若配置 IPv6 AAAA 记录，必须同时正确配置 IPv6 防火墙和 TLS；否则先只配置 A
  记录。

如果域名使用 Cloudflare 等代理平台，应确认启用 WebSocket，并使用严格 TLS
验证。排查问题时可以临时改为仅 DNS 解析，先确认插件到 VPS 的直连链路正常。

不要依赖隐藏路径作为安全措施。真正的认证依据仍是每台服的独立 256 位 token。

## 10. 从旧机器迁移机器人后端

推荐顺序：

1. 提前准备新 VPS、域名、TLS 反向代理和 Docker；
2. 停止旧机器人，避免两个实例同时读取同一个 Matrix 账号和房间；
3. 使用 SQLite 在线备份，或在容器停止后复制
   `/opt/matrix-serverchat/data/bridge.db`；
4. 复制后端文件、数据库和 `secrets/matrix_password` 到新 VPS；
5. 在新 VPS 启动后端并确认 Matrix 登录正常；
6. 确认 `wss://新域名/minecraft` 的证书和反向代理可用；
7. 逐台修改插件 `bridge.url`，执行 `/matrixbridge reload`；
8. 通过 `matrixbridge status`、`!server list` 和双方聊天测试验收；
9. 确认全部服务稳定后再关闭旧 VPS 的相关入口。

完整复制 `bridge.db` 后，服务器登记、token 哈希、游戏账号绑定和机器人管理员
身份都会保留，通常不需要重新绑定或重新生成 token。若选择建立全新的空数据库，
则必须重新登记 owner、服务器和 token。

修改 URL 时不必同时重启所有 Minecraft 服务端，可以一台一台重载，减少整体
中断时间。

## 11. 验证与故障排查

后端与代理状态：

```bash
cd /opt/matrix-serverchat
docker compose ps
docker compose logs --tail=200 matrix-serverchat
```

确认 8766 没有暴露到所有网卡：

```bash
ss -lntp | grep 8766
```

预期宿主机地址为 `127.0.0.1:8766`。直接用浏览器打开 `/minecraft` 出现错误并
不代表故障，因为普通 HTTP 请求没有执行 WebSocket 握手，也没有携带插件认证
头。

常见现象：

| 现象 | 优先检查 |
| --- | --- |
| TLS 证书错误 | 域名、系统时间、证书链、AAAA 记录 |
| HTTP 404 | 插件 URL 是否包含 `/minecraft` |
| HTTP 502 | 后端容器是否运行，代理能否访问 127.0.0.1:8766 |
| WebSocket 4001 | server-id、token、服务器 enabled 状态 |
| WebSocket 4003 | server-id 字符格式是否合法 |
| WebSocket 4004 | 请求路径不是 `/minecraft` |
| 连接约一分钟后反复断开 | 代理读写超时、网络设备空闲连接策略 |
| 只有某一台服无法连接 | 该服独立 token、ID、DNS 和出站防火墙 |

不要把完整 token 发到聊天室或粘贴到公开日志中。

## 12. 未来支持普通 HTTPS API 的方案

技术上可行，但迁移到其他 VPS 并不要求把当前 WebSocket 改成普通 HTTP；直接
使用 WSS 已经是安全、实时且改动最小的方案。

如果以后确实需要兼容不允许 WebSocket 的网络，可以在保留现有协议数据结构的
基础上增加可选 HTTPS 传输：

```text
POST /api/v1/minecraft/events       插件上传玩家事件
GET  /api/v1/minecraft/messages     长轮询获取 Matrix/跨服消息
POST /api/v1/minecraft/ack          确认消息已经处理
GET  /healthz                       不含敏感信息的健康检查
```

也可以用 SSE 代替长轮询作为后台到插件的下行通道，但插件仍需用 HTTPS POST
上传事件和确认。纯粹只有一个 POST 接口无法满足 Matrix 向游戏实时推送。

HTTP 版本仍应具备：

- 每台服务器独立 Bearer token 和稳定 `server-id`；
- HTTPS 强制加密，禁止公网明文 HTTP；
- 全局消息 ID、幂等处理、ACK、重试和最近 ID 去重；
- 请求体大小限制、速率限制和连接超时；
- 下行游标或序列号，防止短暂断线丢消息；
- token 轮换、服务器禁用和完整审计；
- 可选时间戳、随机数或 HMAC 签名，用于进一步降低重放风险。

建议未来把它作为与 WebSocket 并存的可选传输，而不是立即替换 WebSocket。
机器人内部继续使用同一套结构化消息和路由逻辑，只有网络适配层不同。

## 13. 上线检查清单

- [ ] 域名只指向正确的新 VPS；
- [ ] TLS 证书有效且自动续期；
- [ ] 插件使用 `wss://域名/minecraft`；
- [ ] 8766 只绑定 `127.0.0.1`；
- [ ] 每台服的 server-id 和 token 均不同；
- [ ] 同机 Minecraft 游戏端口不重复；
- [ ] 数据库、Matrix 密码和管理员身份已迁移；
- [ ] 新旧机器人没有同时运行；
- [ ] `!server list` 显示的服务器均正常连接；
- [ ] Matrix 到游戏、游戏到 Matrix、跨服聊天和提及均已测试。
