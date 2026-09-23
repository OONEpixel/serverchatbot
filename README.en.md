# MatrixChatBridge

[简体中文](README.md) | English

## Documentation

- [Administrator Manual 0.4.9](docs/administrator-manual-0.4.9.en.md)
- [Cross-VPS and Multi-Server Deployment Guide](docs/deployment-multi-server.en.md)
- [Multi-Platform and Multi-Version Roadmap](docs/multi-platform-roadmap.en.md)
- [WebSocket Protocol v2](docs/protocol.md)

Project terminology is consistent throughout the documentation: the Minecraft plugin is the **plugin side (frontend)**, while the Python Matrix bridge running in Docker is the **bot backend (backend)**. The Matrix homeserver and bot account refer to the external Matrix service and its user, not to the backend process.

MatrixChatBridge is a server plugin for Minecraft 1.21 Leaf/Paper. It sends player joins, quits, chat, deaths, and advancements to the Matrix bot backend, and broadcasts Matrix-room messages received by the backend into the game.

Leaf is compatible with the Paper plugin API, so the project depends directly on Paper API without NMS or Leaf internals. It targets Java 21 and Minecraft 1.21.x.

## Architecture

```text
Minecraft / Leaf
      │ Bukkit/Paper events
      ▼
MatrixChatBridge ── WebSocket + JSON ──► Matrix bot backend ──► Matrix room
      ▲                 │     │
      └─ matrix_message ┘     └─ relay_message ─► other servers in the group
```

The plugin is a **WebSocket client** and the bot backend is a **WebSocket server**. A same-host deployment may listen only on `127.0.0.1:8766`; cross-host deployments must use `wss://` through a TLS reverse proxy. WebSocket provides one full-duplex connection, so the plugin does not open another listening port.

A trusted LAN may use a private `ws://` address. Public or untrusted networks must use `wss://`.

Messages use JSON; version 0.4.9 speaks protocol `v: 2`. Events have global UUIDs and are confirmed with `ack`. While disconnected, the plugin keeps up to `max-pending-events` unacknowledged events in memory and resends them after reconnecting. See the [complete protocol](docs/protocol.md).

## Implemented features

- Player joins and quits.
- Player chat; cancelled messages and `/` commands are excluded.
- Player deaths.
- Publicly announced advancements; recipe unlocks and hidden advancements are suppressed.
- Plain-text Matrix messages broadcast into Minecraft.
- Matrix `!list` or `#list` queries across registered servers.
- Matrix `#online` or `#在线榜` generates a PNG cumulative-playtime leaderboard.
- Matrix `#ping` measures backend-to-plugin round trip and displays player ping, TPS, and MSPT.
- Graceful server stop/restart announcements and a success notice after reconnection; unexpected failures retain debounced health alerts.
- Leaderboards grouped by server with cached heads, game IDs, persistent playtime, and online state.
- Shared Matrix/Minecraft games with `#game`: dice, coin toss, and time-limited number guessing.
- Matrix `!bind` or `#bind` creates a code completed with `/matrixbind <code>` or `/bind <code>`.
- Independent 256-bit Bearer token per server; only hashes are stored by the backend.
- Structured cross-server chat by `relay-group`, with source exclusion, global deduplication, and one-hop enforcement.
- Global UUID bindings for online-mode servers and server-scoped bindings for offline-mode servers.
- `@vanilla:Steve` name disambiguation and conversion of Matrix mentions to game IDs.
- Exponential-backoff reconnect, bounded in-memory queue, application acknowledgements, and inbound deduplication.
- `/matrixbridge status|reconnect|reload` administration commands.
- `/matrixbridge token generate` for game OPs and the server console.

## Build

```powershell
.\gradlew.bat clean test shadowJar
```

The artifact is written to `build/libs/matrix-chat-bridge-0.4.9.jar`. The project uses a Java 21 toolchain; Minecraft 1.21.x Leaf also requires at least Java 21.

## Local deployment settings

The public repository does not store production SSH details. Before using the PowerShell deployment script, copy the example and fill in your own host, port, and user:

```powershell
Copy-Item .\deploy.local.example.psd1 .\deploy.local.psd1
```

`deploy.local.psd1` is ignored by Git, as are local release/hotfix artifacts in `dist/`. Deployment scripts read this local configuration automatically.

## Installation

1. Put the non-`plain` JAR in the Minecraft server's `plugins/` directory.
2. Start the server once to generate `plugins/MatrixChatBridge/config.yml`.
3. As a game OP or at the console, run `/matrixbridge token generate`; the plugin creates and stores an independent token.
4. In the backend, run `app.admin add-server` with `--prompt-token` and paste the token into the hidden prompt.
5. Confirm that the backend listens at `ws://127.0.0.1:8766/minecraft`, or update the plugin's `bridge.url`.
6. Run `/matrixbridge status` to verify the connection.

## Binding and online queries

1. A Matrix user sends `!bind` or `#bind`; the bot returns a copy-friendly six-character code.
2. Within ten minutes, the Minecraft player runs `/matrixbind ABC123` or `/bind ABC123`.
3. The plugin sends `bind_confirm` with the current Minecraft name and UUID. The backend then labels that Matrix user's game messages with the bound name.

When a Matrix user sends `!list` or `#list`, the backend sends `query_list_request` to every connected server. Each plugin returns its `serverId`, online count, and name-sorted player list.

The persistent backend lives in [backend](backend/README.md). Its Docker-mounted SQLite database replaces the old in-memory dictionary and stores bindings, Matrix login state, sync position, and deduplication records. Ten-minute verification codes remain ephemeral and are lost on container restart.

The default token is `CHANGE_ME`. Until it is replaced, the plugin registers event listeners but does not open a WebSocket connection.

## Configuration

The main configuration is `src/main/resources/config.yml`:

- `bridge.url`: backend WebSocket URL. Use loopback on the same host, or `wss://` across untrusted networks.
- `bridge.server-id`: unique stable ID matching backend registration.
- `bridge.server-name`: display name, for example `Vanilla`.
- `bridge.relay-group`: cross-server route such as `main`.
- `bridge.game-port`: Minecraft game port used only for display and administration.
- `bridge.token`: independent 256-bit Bearer credential.
- `bridge.max-pending-events`: maximum unacknowledged events retained in memory.
- `events.*`: switches for individual event categories.
- `inbound.format`: MiniMessage format for Matrix-to-game messages. `<sender>`, `<message>`, and `<room>` values are inserted as plain text, preventing Matrix users from injecting tags.
- `relay.format`: cross-server format with `<server>`, `<server-id>`, `<author>`, `<author-id>`, `<message>`, and `<route>`.

Matrix administration requires both a registered full Matrix ID with role `owner` or `admin` and a room power level meeting `ADMIN_POWER_LEVEL` (default 50). Owners can maintain multiple administrators with `!admin`; the final owner cannot be removed. Every change is audited. Plaintext server tokens travel only between the Minecraft console and the backend's hidden prompt, never through an unencrypted room.

## Backend responsibilities

The backend performs Matrix login, room filtering, and sending, while implementing [protocol v2](docs/protocol.md):

- Authenticate each WebSocket handshake against the server table using its Bearer token and `X-Minecraft-Server-Id`.
- Deduplicate incoming `player_*` by envelope ID, send it to the correct Matrix room, then return `ack`.
- Convert Matrix content to plain text and send `matrix_message`, retrying until acknowledged when appropriate.
- Ignore events from its own bot account to prevent loops.
- Convert only genuine `player_chat` into structured `relay_message`, exclude the source, and route by group.

The plugin never stores a Matrix access token or calls the homeserver directly. Matrix credentials and SDK dependencies remain isolated in the backend.

## Current reliability boundary

The plugin's unacknowledged queue exists only in memory. A Minecraft process crash or restart therefore loses pending events. If persistence across process restarts becomes necessary, SQLite/WAL can be added to the plugin without changing the protocol. Delivery is at least once, so the backend must deduplicate by message `id`.

References: [Paper project setup](https://docs.papermc.io/paper/dev/project-setup/), [Paper 1.21.1 API](https://jd.papermc.io/paper/1.21.1/), and [Leaf](https://github.com/Winds-Studio/Leaf).
