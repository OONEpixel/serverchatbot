# MatrixChatBridge

管理与运维请参阅 [管理员操作手册 0.4.0](docs/管理员操作手册-0.4.0.md)。
跨机器部署及一机多服请参阅
[跨 VPS 与多服务端部署指南](docs/跨VPS与多服务端部署指南.md)。
多 Minecraft 服务端接入以及 Paper、Fabric、Forge/NeoForge 的演进路线参阅
[多平台与多版本适配规划](docs/多平台与多版本适配规划.md)。

项目名词统一为：Minecraft 插件是“插件端（前端）”，Docker 中的 Python
桥接机器人是“机器人后端（后端）”；Matrix Homeserver 和机器人账号分别指
外部 Matrix 服务与其账号，不与机器人后端混称。

面向 Minecraft 1.21 Leaf/Paper 的服务器插件。它把玩家加入、退出、聊天、死亡和进度（成就）事件发送给 Matrix 机器人后台，也把后台收到的 Matrix 房间消息广播到游戏内。

Leaf 与 Paper 插件 API 兼容，因此项目直接依赖 Paper API，不使用 NMS 或 Leaf 内部类。编译目标为 Java 21，适合 Minecraft 1.21.x 服务器。

## 通信架构

```text
Minecraft / Leaf
      │ Bukkit/Paper events
      ▼
MatrixChatBridge ── WebSocket + JSON ──► Matrix bot backend ──► Matrix room
      ▲                 │     │
      └─ matrix_message ┘     └─ relay_message ─► 同组的其他服务器
```

插件是 **WebSocket 客户端**，机器人后台是 **WebSocket 服务端**。同机部署可只监听 `127.0.0.1:8765`；跨机器必须通过 TLS 反向代理使用 `wss://`。WebSocket 提供全双工长连接，不需要插件开放额外端口。

消息协议使用 JSON，0.4.0 对应 `v: 2`。事件有全局 UUID，双方通过 `ack` 确认；掉线期间插件最多在内存中保存 `max-pending-events` 条未确认事件，重连后重发。完整协议见 [docs/protocol.md](docs/protocol.md)。

## 已实现

- 玩家加入和退出
- 玩家聊天（已取消的聊天和 `/` 开头的消息不转发；命令事件本身未注册监听）
- 玩家死亡
- 会在游戏聊天中公告的进度/成就；配方解锁和隐藏进度不会产生噪声
- Matrix 房间纯文本消息广播到游戏
- Matrix `!list` 或 `#list` 查询各 Minecraft 服务器的在线人数和玩家列表
- Matrix `!bind` 或 `#bind` 生成验证码后，玩家可用 `/matrixbind <验证码>` 或 `/bind <验证码>` 完成绑定
- 每台服务器独立的 256 位 Bearer Token；后台只保存哈希
- 按 `relay-group` 跨服转发结构化玩家聊天，来源排除、全局去重且最多一跳
- 正版 UUID 全局绑定和离线服按服务器绑定两种模式
- `@vanilla:Steve` 消歧；Matrix 提及进入游戏时显示游戏 ID
- 指数退避重连、有界内存队列、应用层确认和入站去重
- `/matrixbridge status|reconnect|reload` 管理命令
- `/matrixbridge token generate` 由游戏 OP 或服务器控制台生成并安装每服 token

## 构建

```powershell
.\gradlew.bat clean test shadowJar
```

产物位于 `build/libs/matrix-chat-bridge-0.4.0.jar`。项目使用 Java 21 toolchain；运行 Minecraft 1.21.x 的 Leaf 也至少需要 Java 21。

## 本地部署参数

公开仓库不保存生产服务器的 SSH 地址。第一次使用 PowerShell 部署脚本前，复制
示例文件并填写自己的主机、端口和用户：

```powershell
Copy-Item .\deploy.local.example.psd1 .\deploy.local.psd1
```

`deploy.local.psd1` 已加入 `.gitignore`，不会被提交；`dist/` 中的热修压缩包也
属于本地构建产物。各部署脚本会自动读取此配置，因此原来的一键运行方式不变。

## 安装

1. 将构建出的非 `plain` JAR 放入服务器的 `plugins/`。
2. 启动一次服务器，让插件生成 `plugins/MatrixChatBridge/config.yml`。
3. 在游戏 OP 或服务器控制台执行 `/matrixbridge token generate`，插件会生成并自动保存独立 token。
4. 在后台执行带 `--prompt-token` 的 `app.admin add-server`，通过隐藏输入粘贴该 token。
5. 确认后台监听 `ws://127.0.0.1:8765/minecraft`，或修改插件的 `bridge.url`。
6. 用 `/matrixbridge status` 检查连接。

## 绑定与在线查询

新版持久化 Python 后端沿用现有命令：

1. Matrix 用户在房间发送 `!bind` 或 `#bind`，机器人以方便复制的代码块返回 6 位验证码。
2. 对应 Minecraft 玩家在 10 分钟内执行 `/matrixbind ABC123`；也可以使用短别名 `/bind ABC123`。
3. 插件向后台发送 `bind_confirm`，其中 `playerId` 是玩家当前的 Minecraft 名称，后台随后会用这个名称标记该 Matrix 用户发送的游戏消息。

Matrix 用户发送 `!list` 或 `#list` 时，后台向所有已连接服务器发送 `query_list_request`。每台插件实例会返回自己的 `serverId`、在线人数和按名称排序的玩家列表。

新的持久化、提及与未绑定提醒后端实现位于 [backend](backend/README.md)。它使用 Docker 挂载的 SQLite 数据库取代旧 `bot.py` 的内存字典，并保存绑定关系、Matrix 登录状态、同步位置和事件去重记录。验证码仍是十分钟有效的一次性临时数据，容器重启后失效。

默认 token 是 `CHANGE_ME`。在它被修改前，插件会加载事件监听器，但不会建立 WebSocket 连接。

## 配置说明

主要配置位于 `src/main/resources/config.yml`：

- `bridge.url`：机器人后台 WebSocket 地址。同机部署保持 `ws://127.0.0.1:...`；跨机器必须使用 `wss://`。
- `bridge.server-id`：服务器唯一逻辑 ID，必须与后台登记一致。
- `bridge.server-name`：显示名称，例如“原版服”。
- `bridge.relay-group`：跨服路由组，例如 `main`。
- `bridge.game-port`：服务器的 Minecraft 游戏端口，仅用于展示和管理，不作为身份凭据。
- `bridge.token`：该服务器独有的 256 位密钥，通过 Bearer 认证发送。
- `bridge.max-pending-events`：尚未收到后台确认时最多保留的事件数；队列只在内存中。
- `events.*`：分别开关各类事件。
- `inbound.format`：Matrix 消息在游戏内的 MiniMessage 格式。`<sender>`、`<message>` 和 `<room>` 占位值按纯文本插入，Matrix 用户不能注入 MiniMessage 标签。
- `relay.format`：跨服消息格式，可使用 `<server>`、`<server-id>`、`<author>`、`<author-id>`、`<message>` 和 `<route>`。

Matrix 管理采用双重鉴权：完整 Matrix ID 必须登记为 `owner` 或 `admin`，同时
房间权限等级达到后端 `ADMIN_POWER_LEVEL`（默认 50），才能使用 `!server` 或
`#server`。`owner` 可通过 `!admin` 维护多人名单，最后一名 owner 受删除保护。
所有修改都写入审计表；独立 token 只在游戏/服务器控制台与后台隐藏输入之间
传递，不发送到未加密房间。

## 机器人后台的责任边界

后台需要完成 Matrix SDK 登录、房间过滤及消息发送，并实现 [v2 协议](docs/protocol.md)：

- WebSocket 握手时按服务器表验证独立 Bearer Token 和 `X-Minecraft-Server-Id`。
- 收到 `player_*` 后先用外层 `id` 去重，再发送到正确的 Matrix 房间，成功后回 `ack`。
- 从 Matrix 收到消息后去掉 HTML/富文本，发 `matrix_message`；收到插件 `ack` 前可重试。
- 忽略 Matrix 机器人自己发送的事件，防止回环。
- 只把真实 `player_chat` 转成结构化 `relay_message`，排除来源服并按组投递。

插件不会直接保存 Matrix access token，也不会直接调用 Matrix homeserver；这让账号凭据和 Matrix SDK 依赖都留在机器人进程中，服务器插件保持轻量。

## 当前可靠性边界

未确认队列只存在于内存中，因此 Minecraft 进程崩溃或正常重启会丢失尚未确认的事件。若后续需要“进程重启也不丢”，可以在插件侧加入 SQLite/WAL，协议无需改变。应用层语义是“至少一次”，所以机器人后台必须按消息 `id` 去重。

参考：[Paper 插件项目配置](https://docs.papermc.io/paper/dev/project-setup/)、[Paper 1.21.1 API](https://jd.papermc.io/paper/1.21.1/)、[Leaf 项目说明](https://github.com/Winds-Studio/Leaf)。
