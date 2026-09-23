# Minecraft–Matrix Bridge Protocol v2

## 1. Terminology

- **Plugin side / frontend**: the Minecraft plugin installed in Leaf/Paper.
- **Bot backend / backend**: the Python bridge service running in Docker.
- **Bot account**: the Matrix user controlled by the backend; it is not the
  backend process itself.
- **Matrix homeserver**: the external Matrix service hosting users and rooms.
- **TLS gateway**: Caddy or Nginx in front of the bot backend.

In this protocol, `plugin` always means the plugin side and `backend` always
means the bot backend. The plugin is a WebSocket client; the bot backend is the
WebSocket server.

## 2. Transport and authentication

Each WebSocket text frame contains one UTF-8 JSON object. The plugin connects to
`/minecraft` with:

```text
Authorization: Bearer <64-hex-character per-server token>
X-Minecraft-Server-Id: <bridge.server-id>
```

The backend looks up that `server-id`, requires `enabled = true`, hashes the
presented 256-bit token with SHA-256, and compares it with `servers.token_hash`.
The authenticated connection is not allowed to send an envelope bearing another
server's ID. Remote connections must use `wss://`.

## 3. Envelope

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

`id` is globally unique. Receivers deduplicate before external side effects.
`serverId` is the authenticated source for plugin-to-backend traffic and the
specific target for backend-to-plugin traffic. Protocol v1 is intentionally
rejected by v2 peers.

## 4. Plugin to backend

`hello` contains `pluginVersion`, `minecraftVersion`, `serverSoftware`,
`serverName`, `relayGroup`, and `gamePort`. An authenticated plugin may update
its configured name and game-port metadata. Matrix administrator overrides take
precedence; the backend remains authoritative for the relay group.

Player event types remain `player_join`, `player_quit`, `player_chat`,
`player_death`, and `player_advancement`. Their payload contains:

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

Only a real, non-cancelled `AsyncChatEvent` produces `player_chat`; commands are
excluded. Backend-originated broadcasts do not synthesize this event.

`bind_confirm` contains `token`, `playerId`, and `playerUuid`.
`query_list_response` contains `onlineCount`, `playerList`, and `requestId`.
Newer plugins also include `playerDetails`, an array of
`{"name": "Steve", "onlineSeconds": 3600, "pingMs": 42}` entries, plus
`tps1m` and `mspt` numeric fields. `onlineSeconds` is the current session
duration as known by the plugin; it restarts if the plugin is reloaded.
These extra fields are optional so older plugins keep working; the bot uses
the same request/response pair for `#list`, `#online`, and `#ping`.
When a request contains `includeLeaderboard: true`, a newer plugin additionally
returns `leaderboardPlayers` entries with `name`, persistent
`totalPlaySeconds`, and current `online` state. The value is derived from the
server's `PLAY_ONE_MINUTE` player statistic. `leaderboardTotal` reports the
number of known ranked players and `leaderboardLimit` reports the response cap.

## 5. Backend to plugin

`matrix_message` contains `roomId`, `eventId`, `sender`, `senderDisplayName`,
and plain-text `body`. For each target server, the backend resolves sender and
mentioned Matrix users to the appropriate game identity before sending.

`query_list_request` contains `requester`. `bind_result` contains `playerId`,
`playerUuid`, `success`, and `message`.

Before a normal Minecraft server shutdown, a newer plugin sends the
`server_stopping` control message with `reason: "server_stopping"`. The backend
uses this signal to distinguish an intentional stop/restart from an unexpected
WebSocket outage. It announces the stop once and announces successful restart
after the same authenticated server ID reconnects and sends `hello`. Abrupt
process termination, host power loss, and network failure cannot send this
signal and continue through the delayed health-alert path.

### Structured cross-server relay

The relay envelope reuses the original `player_chat` global ID:

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

The backend sends this only to other connected, enabled servers in the same
route. The plugin requires `kind = minecraft`, `hop = 1`, a route matching its
configuration, and a source different from itself. It then renders the message
directly, for example `[原版服]<111>234`, without creating a player chat event.

## 6. Reliability and loop prevention

- Plugin player events remain queued in bounded memory until `ack`.
- Duplicate global IDs are acknowledged but not processed twice.
- The backend stores processed IDs for 30 days.
- Source servers are excluded from relays.
- Relays are one hop and are never accepted back as player events.
- The backend ignores Matrix events sent by its own bot account.
- `hello`, `ping`, `pong`, and `ack` are control messages and are not themselves
  acknowledged.
