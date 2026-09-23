# MatrixChatBridge Multi-Platform and Multi-Version Roadmap

English | [简体中文](多平台与多版本适配规划.md)

This document explains how multiple Minecraft servers share one bot backend and outlines support for Paper/Leaf, Fabric, Forge/NeoForge, and multiple Minecraft versions.

## 1. Multiple-server deployment

Every Minecraft server needs one plugin-side build matching its platform and version:

```text
Vanilla (Leaf)    → Paper plugin ─┐
Survival (Paper)  → Paper plugin ─┼→ one bot backend :8766
Modded (Fabric)   → Fabric mod ───┘
```

Port 8766 is the backend's shared WebSocket listener, not a per-server port. Each instance is distinguished by a unique `server-id`, an independent 256-bit token, and its own WebSocket connection. Only one active connection is allowed for a server ID; a new one replaces the old one. Game ports on one host must still differ.

```yaml
bridge:
  url: "ws://127.0.0.1:8766/minecraft"
  server-id: "vanilla"
  server-name: "Vanilla"
  relay-group: "main"
  game-port: 25565
  token: "vanilla-specific-token"
```

Other instances use the same URL but different IDs, tokens, and game ports. Servers in `main` relay chat to each other. After moving the backend to a VPS, all plugins can use the same `wss://bridge.example.com/minecraft` URL. See the [cross-VPS guide](deployment-multi-server.en.md).

## 2. Current support

Version 0.4.9 targets Paper API 1.21.1 and Java 21, using Paper `AsyncChatEvent`, Bukkit events/commands/configuration, and Adventure Component/MiniMessage. Leaf generally remains Paper-compatible but must be tested explicitly.

The current JAR cannot be installed directly on Fabric, Forge/NeoForge, pure Spigot/Bukkit without Paper APIs, or substantially older Minecraft/Java generations. In Fabric/Forge terminology these are server-side mods, but this project still groups them under the plugin side/frontend role.

## 3. Target architecture

```text
matrix-bridge-protocol
└─ JSON protocol, message types, validation, global IDs

matrix-bridge-core
├─ WebSocket/WSS client
├─ authentication, reconnect, ACK, retry, deduplication
├─ configuration and capability negotiation
└─ no Bukkit, Fabric, Forge, or Minecraft classes

matrix-bridge-platform-api
└─ events, players, commands, scheduling, broadcast, text interfaces

platform-paper
platform-fabric
platform-forge
platform-neoforge
└─ platform-specific events and lifecycle adapters
```

The backend continues to consume one protocol regardless of platform. The core must not directly use `org.bukkit.*`, `net.minecraft.*`, loader APIs, Paper-only events, or platform-specific component types.

## 4. Platform adapter contract

```text
PlatformAdapter
├─ platformName / platformVersion / minecraftVersion
├─ registerEventListeners()
├─ registerCommands()
├─ runOnServerThread(task)
├─ listOnlinePlayers()
├─ broadcastBridgeMessage(message)
├─ playerId / playerUuid
├─ serializeGameText / plainGameText
├─ readConfig / saveGeneratedToken
└─ logger
```

Network callbacks must switch to the server thread required by the platform before using game APIs. Plain text remains mandatory in the protocol; rich JSON, colors, and click events are optional capabilities so older or non-Adventure platforms still work.

## 5. Capability negotiation

Extend `hello` compatibly:

```json
{
  "pluginVersion": "0.6.0",
  "minecraftVersion": "1.xx.x",
  "platform": "fabric",
  "platformVersion": "...",
  "serverSoftware": "Fabric",
  "capabilities": [
    "player_chat",
    "player_join",
    "player_quit",
    "player_death",
    "advancement",
    "rich_text",
    "online_players"
  ]
}
```

The backend should use declared capabilities rather than guessing from version strings. A platform may omit `advancement` while keeping chat functional. V2 peers must ignore unknown fields, and new backends should accept older plugins with conservative defaults.

## 6. Paper, Spigot, and version policy

### Paper/Leaf/Purpur

Paper remains the first priority. Paper forks may share a build, but startup, chat, death, join/quit, advancement, and command behavior must be tested before claiming support. Do not promise one JAR for every Paper version; chat APIs, Adventure, and Java requirements change across generations.

### Spigot/Bukkit

Paper's `AsyncChatEvent` is unavailable on Spigot. Supporting pure Spigot requires a separate adapter using available chat events and converting text into the common model.

### Java boundaries

Changing only `api-version` is insufficient for old Minecraft versions. Each artifact must target the Java version supported by its Minecraft release; older targets cannot load higher-bytecode classes. Very old Java may require replacing records and incompatible networking APIs. All builds can still speak the same JSON protocol. Prioritize modern Java versions, then evaluate legacy support separately.

## 7. Fabric adapter

Create a separate loader module that:

- opens and closes the core connection with server lifecycle events;
- registers join, quit, chat, death, and advancement callbacks;
- registers `/matrixbridge` and `/matrixbind` with Brigadier;
- converts Minecraft `Text` to plain and optional rich text;
- executes broadcasts and player queries on the server thread;
- stores URL, ID, token, and route in the Fabric config directory;
- is server-side only and requires no client mod.

Prefer official or Fabric API callbacks. Use Mixins only where no public event exists, and isolate them in version-specific adapters. Start with one version that is actually deployed.

## 8. Forge and NeoForge adapters

Use separate modules for their lifecycle, event buses, commands, configuration, and build tooling instead of forcing one reflective JAR. Each module must initialize/close the core at dedicated-server lifecycle points, listen for player events, register commands, broadcast on the server thread, persist generated tokens, and avoid client-only classes. Build and test separately for each target generation; the backend protocol and binding database remain shared.

## 9. Chat safety and event consistency

Every adapter must preserve loop prevention:

- upload only genuine player chat;
- display backend messages as system/broadcast messages;
- never re-trigger player chat for inbound messages;
- retain global IDs and allow at most one relay hop;
- cache recently handled IDs.

Signed-chat behavior varies by version. Observe the final permitted player message without modifying its signature; show Matrix and relay messages clearly as system messages.

| Event | Common requirement |
| --- | --- |
| Player chat | Do not upload cancelled messages or commands |
| Join/quit | Use the real player ID and UUID when reliable |
| Death | Prefer final server display text; always provide plain text |
| Advancement | Relay only publicly announced events |
| Online list | Read on the server thread and include source ID |
| Inbound message | Broadcast without entering player-chat listeners |

## 10. Configuration compatibility

Physical formats may differ: YAML for Paper/Spigot, JSON/JSON5/TOML for Fabric, and platform-recommended formats for Forge/NeoForge. Logical keys should remain equivalent: `enabled`, `url`, `server-id`, `server-name`, `relay-group`, `game-port`, `token`, timeouts, reconnect settings, pending queue, and event switches. Examples must name their platform. Token-generation commands should feel consistent while each adapter writes its own format.

## 11. Build and release model

Use a Gradle multi-project build and publish separate artifacts:

```text
matrix-chat-bridge-paper-<mc-version>-<project-version>.jar
matrix-chat-bridge-fabric-<mc-version>-<project-version>.jar
matrix-chat-bridge-forge-<mc-version>-<project-version>.jar
matrix-chat-bridge-neoforge-<mc-version>-<project-version>.jar
```

Do not ship an all-loaders JAR; loader scanning creates missing-class and dependency conflicts. Every artifact should document Minecraft range, loader/API range, required Java, SHA-256, configuration migration, tested servers, and missing capabilities.

## 12. Testing plan

Core automation covers protocol compatibility, authentication/reconnect/token rotation, ACK/retry/deduplication/queue limits, relay source exclusion and hop count, mentions/bindings/name ambiguity, and capability compatibility.

Every adapter must pass the same contract tests: player events, command and cancellation exclusion, thread-safe inbound display, online list, token permissions, role-sensitive `#help`, and no inbound loop. Every advertised platform/version also needs a real dedicated-server test; successful compilation alone is not support.

| Platform | Minecraft | Java | Build | Real-server test | Gap |
| --- | --- | --- | --- | --- | --- |
| Paper/Leaf | 1.21.1 | 21 | Available | Continuous verification | Baseline |
| Fabric | To select | Target-dependent | Planned | Not started | To assess |
| Forge | To select | Target-dependent | Planned | Not started | To assess |
| NeoForge | To select | Target-dependent | Planned | Not started | To assess |
| Spigot | To select | Target-dependent | Planned | Not started | Replace Paper chat API |

## 13. Milestones

### A — Stabilize current Paper build

Validate 0.4.x in production, improve reconnect/queue/event tests, record actual Paper/Leaf compatibility, and establish the support matrix.

### B — Extract the platform-independent core

Create protocol, core, platform-api, and paper modules without regressing behavior or configuration. Add platform and capabilities to `hello`; make this the principal 0.5.0 goal.

### C — First Fabric release

Support one required version first: chat, join/quit, death, binding, online list, and inbound broadcast. Add advancement/rich text incrementally and test relays with Paper.

### D — First Forge/NeoForge release

Choose based on an actual modded server, reuse core, implement platform adapters, publish loader-specific JARs, and test mixed Paper/Fabric/modded relays.

### E — Legacy and proxy layers

Select legacy Paper/Spigot versions from real demand, isolate old Java/chat/text handling, and evaluate Velocity/BungeeCord for proxy-wide announcements and online information without weakening modern security.

## 14. Recommended priority

1. Stabilize Paper/Leaf 1.21.1.
2. Extract core and the Paper adapter.
3. Select the Fabric version actually in use.
4. Select the Forge or NeoForge version actually in use.
5. Add legacy Spigot/Paper according to demand.
6. Evaluate proxy and optional HTTPS transports last.

The key decision is the first real target set, not a theoretical version count. Every Minecraft-version/loader combination adds build, test, and support cost.
