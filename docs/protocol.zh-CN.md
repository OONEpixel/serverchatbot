# Minecraft–Matrix 桥接协议 v2

> 本文是 [`protocol.md`](protocol.md) 的中文翻译。协议字段名、消息类型和示例值保持英文，以确保实现之间能够互操作；若译文与英文原文存在歧义，以英文原文为准。

## 1. 名词定义

- **插件端 / 前端（Plugin side / frontend）**：安装在 Leaf/Paper 服务端中的 Minecraft 插件。
- **机器人后端 / 后端（Bot backend / backend）**：在 Docker 中运行的 Python 桥接服务。
- **机器人账号（Bot account）**：由后端控制的 Matrix 用户；它并不等同于后端进程本身。
- **Matrix 主服务器（Matrix homeserver）**：承载用户和房间的外部 Matrix 服务。
- **TLS 网关（TLS gateway）**：部署在机器人后端前方的 Caddy 或 Nginx。

在本协议中，`plugin` 始终表示插件端，`backend` 始终表示机器人后端。插件是 WebSocket 客户端，机器人后端是 WebSocket 服务端。

## 2. 传输与身份认证

每个 WebSocket 文本帧都包含一个 UTF-8 编码的 JSON 对象。插件连接到 `/minecraft` 路径时携带：

```text
Authorization: Bearer <每台服务器独立的 64 位十六进制 token>
X-Minecraft-Server-Id: <bridge.server-id>
```

后端使用该 `server-id` 查询服务器记录，要求 `enabled = true`，对提交的 256 位 token 进行 SHA-256 哈希，并将结果与 `servers.token_hash` 比较。通过认证的连接不得发送带有其他服务器 ID 的信封。远程连接必须使用 `wss://`。

## 3. 消息信封

```json
{
  "v": 2,
  "id": "b5aa8ac9-bfe5-49d0-950e-03fa62afc426",
  "type": "player_chat",
  "timestamp": "2026-09-13T08:42:31.319Z",
  "serverId": "vanilla",
  "payload": {}
}
```

`id` 在全局范围内唯一。接收方必须先进行去重，再产生外部副作用。对于插件到后端的流量，`serverId` 表示已经认证的来源服务器；对于后端到插件的流量，它表示明确的目标服务器。v2 对等端会有意拒绝协议 v1。

## 4. 插件到后端

`hello` 包含 `pluginVersion`、`minecraftVersion`、`serverSoftware`、`serverName`、`relayGroup` 和 `gamePort`。通过认证的插件可以更新其配置的名称和游戏端口元数据。Matrix 管理员设置的覆盖值优先；机器人后端仍然是中继组信息的权威来源。

玩家事件类型包括 `player_join`、`player_quit`、`player_chat`、`player_death` 和 `player_advancement`。它们的 `payload` 包含：

```json
{
  "playerUuid": "8667ba71-b85a-4004-af54-457a9734eed7",
  "playerName": "Steve",
  "playerDisplayName": "Steve",
  "world": "world",
  "message": {
    "plain": "hello",
    "componentJson": "{\"text\":\"hello\"}"
  }
}
```

只有真实且未被取消的 `AsyncChatEvent` 才会产生 `player_chat`；命令会被排除。由后端发起的广播不会模拟生成此事件。

`bind_confirm` 包含 `token`、`playerId` 和 `playerUuid`。

`query_list_response` 包含 `onlineCount`、`playerList` 和 `requestId`。较新的插件还会提供 `playerDetails`：由 `{"name": "Steve", "onlineSeconds": 3600, "pingMs": 42}` 对象组成的数组，以及数值字段 `tps1m` 和 `mspt`。`onlineSeconds` 是插件所记录的本次会话在线时长；插件重载后会重新计时。这些附加字段均为可选字段，因此旧版插件仍可继续工作；机器人通过同一组请求与响应处理 `#list`、`#online` 和 `#ping`。

当请求包含 `includeLeaderboard: true` 时，较新的插件还会返回 `leaderboardPlayers` 条目，其中包含 `name`、持久化的 `totalPlaySeconds` 和当前 `online` 状态。该数值来自服务器的 `PLAY_ONE_MINUTE` 玩家统计。`leaderboardTotal` 表示已知排行榜玩家总数，`leaderboardLimit` 表示响应条目上限。

## 5. 后端到插件

`matrix_message` 包含 `roomId`、`eventId`、`sender`、`senderDisplayName` 和纯文本 `body`。后端会针对每台目标服务器，将发送者及消息中提及的 Matrix 用户解析为相应的游戏身份，然后再发送消息。

`query_list_request` 包含 `requester`。`bind_result` 包含 `playerId`、`playerUuid`、`success` 和 `message`。

在 Minecraft 服务器正常关闭前，较新的插件会发送 `server_stopping` 控制消息，其中 `reason: "server_stopping"`。后端使用此信号区分有意停止或重启与意外 WebSocket 中断。后端只通报一次停服，并在同一已认证服务器 ID 重新连接并发送 `hello` 后通报重启成功。进程突然终止、主机断电和网络故障无法发送此信号，因此仍通过延迟健康告警流程处理。

### 结构化跨服中继

中继信封复用原始 `player_chat` 的全局 ID：

```json
{
  "v": 2,
  "id": "b5aa8ac9-bfe5-49d0-950e-03fa62afc426",
  "type": "relay_message",
  "timestamp": "2026-09-13T08:42:31.400Z",
  "serverId": "survival",
  "payload": {
    "source": {
      "kind": "minecraft",
      "serverId": "vanilla",
      "displayName": "原版服"
    },
    "author": {
      "id": "8667ba71-b85a-4004-af54-457a9734eed7",
      "displayName": "111"
    },
    "body": "234",
    "route": "main",
    "hop": 1
  }
}
```

后端只会将此消息发送给同一路由中其他已连接且已启用的服务器。插件要求 `kind = minecraft`、`hop = 1`、路由与自身配置一致，并且来源服务器不能是自身。随后插件直接渲染消息，例如 `[原版服]<111>234`，且不会创建玩家聊天事件。

## 6. 可靠性与回环防护

- 插件产生的玩家事件会保留在有界内存队列中，直到收到 `ack`。
- 对重复的全局 ID 仍会返回确认，但不会重复处理。
- 后端将已处理的 ID 保存 30 天。
- 中继时排除来源服务器。
- 中继消息最多一跳，且绝不会作为玩家事件重新接收。
- 后端忽略由其自身机器人账号发送的 Matrix 事件。
- `hello`、`ping`、`pong` 和 `ack` 属于控制消息，其自身不需要确认。
