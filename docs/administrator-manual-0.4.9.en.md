# MatrixChatBridge 0.4.9 Administrator Manual

English | [简体中文](管理员操作手册-0.4.9.md)

For a dedicated bot VPS, WSS reverse proxy, or multiple Minecraft instances on one host, also read the [cross-VPS and multi-server guide](deployment-multi-server.en.md).

This manual calls the Leaf/Paper plugin the **plugin side (frontend)** and the Python Matrix bridge in Docker the **bot backend (backend)**. The Matrix homeserver is an external chat platform, and the Matrix user controlled by the backend is the **bot account**.

This document is for Minecraft and Matrix-room administrators. All tokens, passwords, and verification codes are examples. Never post real credentials in rooms, pinned messages, tickets, screenshots, or Git.

## 1. Basic information

| Item | Current value |
| --- | --- |
| Backend SSH host/user/port | Local `deploy.local.psd1` |
| Backend directory | `/opt/matrix-serverchat` |
| Matrix bot/room/homeserver | Backend `.env` |
| Backend protocol | v2 |
| Plugin version | 0.4.9 |
| Java | 21 |

Upgrade the 0.4.9 backend and plugin together. A v2 backend rejects v1 plugins.

## 2. Administrative roles

**Minecraft OP or console administrator:** edits plugin configuration, generates per-server tokens, reloads/restarts Minecraft, and checks plugin connectivity.

**Matrix room administrator:** views server state, changes display metadata and relay groups, enables/disables servers, and reviews audit records.

Bot administration uses two-factor authorization in the policy sense: the sender's complete Matrix ID must be registered locally as `owner` or `admin`, and the sender's room power level must meet `ADMIN_POWER_LEVEL` (default 50). Display names, game-account bindings, typed Matrix IDs, or power level alone do not grant access.

**Backend host administrator:** deploys Docker, registers the first owner and initial server tokens, rotates tokens, backs up the database, and reads logs.

## 3. Plugin configuration

The file is normally:

```text
<minecraft-server>/plugins/MatrixChatBridge/config.yml
```

```yaml
bridge:
  enabled: true
  url: "ws://127.0.0.1:8766/minecraft"
  server-id: "survival"
  server-name: "Survival"
  relay-group: "main"
  game-port: 25565
  token: "this-server's-own-64-hex-token"
```

- `url`: same host may use loopback; a controlled LAN may use unencrypted `ws://192.168.1.10:8766/minecraft`; public or untrusted networks require `wss://` through a TLS gateway.
- `server-id`: stable, unique, and limited to letters, digits, dots, underscores, or hyphens.
- `server-name`: default display name reported by the plugin.
- `relay-group`: servers in the same group exchange chat.
- `game-port`: display metadata, not authentication.
- `token`: independent 256-bit secret for this server.

After editing, run `/matrixbridge reload` in game or `matrixbridge reload` at the console.

## 4. Registering a server for the first time

### Generate the token

Run as an OP:

```text
/matrixbridge token generate
```

or at the server console:

```text
matrixbridge token generate
```

The plugin generates a 64-hex-character token, saves it to `config.yml`, and reconnects. Authentication failures are expected until registration finishes.

### Register it in the backend

```bash
ssh -p <port> <user>@<host>
cd /opt/matrix-serverchat
docker compose exec matrix-serverchat \
  python -m app.admin add-server \
  --id survival --name Survival --group main --port 25565 --prompt-token
```

Paste the token into the hidden prompt. It is not echoed or stored in shell history. The database stores only its SHA-256 hash.

### Verify

```text
matrixbridge status
```

```bash
docker compose exec matrix-serverchat python -m app.admin list-servers
docker compose logs --tail=100 matrix-serverchat
```

```text
!server info survival
```

## 5. Token rotation and leaks

1. Generate a replacement with `matrixbridge token generate`.
2. Run:

```bash
cd /opt/matrix-serverchat
docker compose exec matrix-serverchat \
  python -m app.admin set-token --id survival
```

3. Paste the new token into the hidden prompt.
4. Confirm reconnection with `matrixbridge status`.

The plugin is briefly offline between generation and backend update. The old token becomes invalid. Never send tokens to Matrix, put them in shared documents/screenshots, pass them directly as SSH command arguments, or commit them.

## 6. Multiple Matrix administrators

Both `!` and `#` prefixes are accepted.

### Bootstrap the first owner

The backend host administrator must register the first owner over SSH; users cannot elevate themselves from the room:

```bash
cd /opt/matrix-serverchat
docker compose exec matrix-serverchat \
  python -m app.admin add-admin \
  --matrix-id '@name:matrix.example.org' --role owner
docker compose exec matrix-serverchat python -m app.admin list-admins
```

An `owner` manages servers and administrator identities. An `admin` manages servers but not identities. Register at least two trusted owners; the system refuses to remove or demote the final owner.

### Manage identities from Matrix

```text
!admin list
!admin add @helper:matrix.example.org admin
!admin add @owner2:matrix.example.org owner
!admin remove @helper:matrix.example.org
!admin help
```

Authenticated owners and admins may use `list/help`; only owners may use `add/remove`. Authorization uses the real Matrix event sender, never the display name. Emergency removal over SSH:

```bash
docker compose exec matrix-serverchat \
  python -m app.admin remove-admin \
  --matrix-id '@name:matrix.example.org'
```

### Inspect and modify servers

```text
!server list
!server info survival
!server status survival
!server help
!server help group
```

The whole `server` command family requires both authorization checks. Lists show ID, name, route, port, connection, and enabled state.

```text
!server rename survival New-Survival
!server reset-name survival
!server port survival 25566
!server reset-port survival
!server group survival main
!server disable survival
!server enable survival
!server audit survival
!server audit
```

Name and port overrides take precedence over plugin metadata; reset commands restore plugin values. Display port is not authentication. After changing a group, also update the plugin's `bridge.relay-group` and reload it; the plugin rejects route mismatches. Disabling preserves registration, token, bindings, and audit history but rejects subsequent application traffic. Audits record actor, server, action, old/new values, time, and event deduplication.

## 7. Matrix user features

```text
!help / #help        Show role-sensitive help
!bind / #bind        Obtain a binding code
!list / #list        List online players
#online / #在线榜     Generate the per-server cumulative-playtime PNG
#ping                Live bridge, player ping, TPS, and MSPT status
#game help           Cross-Matrix/Minecraft game help
```

Command replies normally use a Matrix thread to avoid flooding the main timeline. Multi-server `#list` results share that thread. Regular chat and Minecraft events remain on the main timeline; unbound-account reminders deliberately remain there. Older clients may show a fallback.

Binding codes last ten minutes. The player runs `/matrixbind <code>` in game. Do not prefix bot commands with `/`, which is reserved for native Matrix-client commands.

Both room and game chat support `#game roll`, `#game coin`, `#game guess start`, `#game guess <1-100>`, and `#game guess status`. Players in one relay group share a 15-minute guessing game. Results go to the group's room and connected servers; help/errors reply only to the requester. Backend restart ends active games. Bound Matrix users are displayed by game ID; otherwise their room name is used.

`#online` waits up to five seconds per registered server, marks disconnected/timed-out/disabled servers, and is rate-limited to once per user per 15 seconds. The light-theme PNG groups by server and ranks up to 60 players by persistent playtime, showing cached head, game ID, total time, and current status. New plugins read the server's persistent player statistic. Old plugins still return current players but cannot provide history or accurate time.

## 8. Offline-mode servers and AuthMe

Even with AuthMe, use:

```yaml
BINDING_MODE: "scoped"
```

AuthMe validates a local password; it does not turn an offline UUID into a Mojang UUID. Require players to `/login` before `/matrixbind`, and block the binding command while unauthenticated.

Use `BINDING_MODE: "online_uuid"` only when every server enforces online authentication and UUIDs are consistent. Change `/opt/matrix-serverchat/compose.yaml`, then run `docker compose up -d`.

## 9. Mentions and duplicate player names

`@Steve please join` in game generates a standard Matrix mention when Steve is bound. If a relay group contains duplicate names, use `@vanilla:Steve` or `@survival:Steve`. In Matrix, use the client's native mention picker. Bound Matrix IDs become game IDs in Minecraft; scoped offline identities may appear as `@vanilla:Steve`.

## 10. One-command deployment and updates

```powershell
cd "C:\path\to\serverchatbot"
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\deploy-0.4.9.ps1
```

The script uploads backend files, backs up SQLite, rebuilds the bot container, preserves `.env`, the Matrix password, and data, and installs/enables the systemd unit where supported. Copy the plugin JAR separately to every server's `plugins` directory, preserve `plugins/MatrixChatBridge/config.yml`, and restart that game server. OpenSSH reads the password interactively; it is not embedded in the script.

## 11. Routine operations

### Boot persistence

Docker `restart: unless-stopped` recovers a failed container. Systemd waits for Docker/network after host reboot and starts the Compose service from `/opt/matrix-serverchat`.

```bash
sudo bash install-0.4.9.sh matrix-serverchat-backend-0.4.9.tar.gz
systemctl is-enabled matrix-serverchat
systemctl is-active matrix-serverchat
systemctl status matrix-serverchat --no-pager
```

`active (exited)` is correct for the `Type=oneshot` unit with `RemainAfterExit=yes`; Docker continues running the container. Verify:

```bash
docker inspect -f '{{.State.Status}} / restart={{.HostConfig.RestartPolicy.Name}}' \
  matrix-serverchat
```

Expected: `running / restart=unless-stopped`. Prefer:

```bash
systemctl start matrix-serverchat
systemctl stop matrix-serverchat
systemctl restart matrix-serverchat
```

After restart, use `#ping`. It measures backend-to-plugin request round trip, player game ping, 1-minute TPS, and MSPT. Round trip includes main-thread scheduling and times out after three seconds; old plugins show unavailable fields.

New plugins send `server_stopping` before a graceful stop/restart. Matrix announces the stop once and successful restart after the same authenticated ID sends `hello`. Graceful stops suppress duplicate outage alerts. Power loss, process kill, and network failure use delayed health alerts. Both sides must be updated.

If systemd reports multiple Compose files, inspect before deleting anything:

```bash
cd /opt/matrix-serverchat
ls -la *compose*.y*ml
systemctl status matrix-serverchat -l --no-pager
```

The persistence unit manages only `matrix-serverchat`; it does not use `--remove-orphans` or alter another bridge on port 8765. After manual `docker compose stop`, restore with `systemctl restart matrix-serverchat`.

### Quiet hours for a daily network restart

Use a backend build that supports daily quiet hours. For a customized deployment,
set these keys in the existing `matrix-serverchat` service's `environment:`
mapping, replacing any existing values. Keep its port mappings and volumes.
This example covers a daily 06:00 network restart for `survival` in UTC+8.

```yaml
      SERVER_HEALTH_QUIET_START: "05:55"
      SERVER_HEALTH_QUIET_END: "06:15"
      SERVER_HEALTH_QUIET_TIMEZONE: "Asia/Shanghai"
      SERVER_HEALTH_QUIET_SERVER_ID: "survival"
```

```bash
cd /opt/matrix-serverchat
docker compose config --quiet && docker compose up -d --build matrix-serverchat
docker compose exec -T matrix-serverchat printenv \
  SERVER_HEALTH_QUIET_START SERVER_HEALTH_QUIET_END \
  SERVER_HEALTH_QUIET_TIMEZONE SERVER_HEALTH_QUIET_SERVER_ID
```

If the connection drops and recovers within the window, Matrix skips both notices.
If the connection is still down at the end of the window, the backend sends an
alert. Automatic reconnection and `#ping` continue to work. After the network
restart, compare the disconnect and reconnect times in
`docker compose logs --since 30m matrix-serverchat` with the Matrix timeline.
To verify suppression, use an outage that exceeds the usual alert delay.

### Status commands

```bash
cd /opt/matrix-serverchat
docker compose ps
docker compose logs --tail=200 matrix-serverchat
docker compose logs -f --tail=200 matrix-serverchat
docker compose exec matrix-serverchat python -m app.admin list-servers
```

At the Minecraft console: `matrixbridge status`.

## 12. Troubleshooting

### `Unauthorized` or close code 4001

The ID may be unregistered, the token may not match, the server may be disabled, or only one end was rotated. Repeat the rotation procedure; do not try to recover the old plaintext token.

### `unsupported protocol version`

Upgrade backend and plugin to 0.4.9/v2 together.

### `address already in use` on 8766

```bash
docker ps --filter publish=8766
ss -lntp | grep 8766
```

Identify the owner before changing/stopping anything. Do not blindly use `--remove-orphans`; another bridge may legitimately use 8765.

### Repeated Matrix sync timeout

Use the correct homeserver URL and test it:

```yaml
MATRIX_URL: "https://matrix.example.org"
```

```bash
curl -fsS --connect-timeout 10 --max-time 20 \
  https://matrix.example.org/_matrix/client/versions
```

### Missing relay chat

Verify that both servers are connected and enabled, backend `relay_group` values match, plugin `bridge.relay-group` values match the backend, and the source is genuine player chat rather than a command or relayed bot message.

## 13. Data and backups

Persistent data is `/opt/matrix-serverchat/data/bridge.db`, containing registrations and token hashes, account bindings, administrator roles, Matrix login/sync state, deduplication IDs, audit records, and avatar metadata. PNG heads are in `data/avatars/`. Do not delete `data/` or `secrets/matrix_password`; container upgrades preserve mounted data.

Use SQLite online backup before major updates (the deployment script does this). Database restoration requires downtime: stop the bot, verify the backup, then let the backend host administrator restore it.

## 14. Player-avatar cache

The backend prepends a small head image to Minecraft-originated Matrix messages; no plugin change or game restart is required.

1. Query Mojang profile and skin by player ID.
2. Combine face and hat layers into a 32×32 PNG.
3. Store PNGs in `data/avatars/`.
4. Store Matrix media URI and refresh state in `bridge.db`.
5. Use the bundled Steve head for first-message lookup failures.
6. Retry failed players on their next game event.
7. Keep a good cached image if a later refresh fails.
8. Refresh seen players every Sunday at 00:00 Beijing time.

```bash
docker compose logs --tail=200 matrix-serverchat | grep -E '头像|avatar'
```

Disable temporarily with `AVATAR_ENABLED: "false"` in `compose.yaml`, then recreate only the bot:

```bash
docker compose up -d --no-deps --force-recreate matrix-serverchat
```

Do not delete avatar data or the database. Plain-text fallback remains for clients that cannot render inline images. Matrix media is never downloaded to Minecraft; game chat receives only `[image]`, `[video]`, `[audio]`, `[file]`, or `[sticker]`-style labels, with the sender mapped to their bound game ID.

## 15. Suggested pinned player guide

```text
🎮 Minecraft ↔ Matrix chat bridge

Normal room messages are synchronized to Minecraft. Servers in the same chat group also share player chat.

Bind your account:
1. Send !bind or #bind in this room.
2. Copy the six-character code returned by the bot.
3. Within ten minutes, run /matrixbind CODE in game.

Online players: !list or #list
Available commands: !help or #help

Mentions:
• In Matrix, use the client's native @ mention.
• In game, use @PlayerID.
• For duplicate names, use @ServerID:PlayerID, e.g. @vanilla:Steve.

Relay example: [Vanilla]<Steve>Hello!

Bot commands begin with ! or #, not /.
```
