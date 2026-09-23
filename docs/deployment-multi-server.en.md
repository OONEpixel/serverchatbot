# MatrixChatBridge Cross-VPS and Multi-Server Deployment Guide

English | [简体中文](跨VPS与多服务端部署指南.md)

This guide covers moving the bot backend to a dedicated VPS and running multiple Minecraft servers on one host. It applies to version 0.4.9 (protocol v2). For platform and Minecraft-version plans, see the [multi-platform roadmap](multi-platform-roadmap.en.md).

## 1. Terminology

| Term | Short form | Meaning |
| --- | --- | --- |
| Plugin side | frontend | The plugin JAR installed in Leaf/Paper |
| Bot backend | backend | The Python Matrix bot and bridge service in Docker |
| Bot account | bot | The Matrix user controlled by the backend, not the process |
| Matrix homeserver | Matrix server | The external Matrix platform hosting users and rooms |
| Minecraft server | game server | One independent Leaf/Paper server process |
| TLS reverse proxy | gateway | Caddy or Nginx in front of the backend |

The plugin listens for game events, displays Matrix and relay messages, and initiates the backend connection. The backend performs authentication, persistence, routing, deduplication, Matrix I/O, and administration. The homeserver provides Matrix accounts and rooms; the gateway provides the public HTTPS/WSS endpoint.

## 2. Distinguish the three port types

| Port | Example | Purpose | Per Minecraft server? |
| --- | ---: | --- | --- |
| Minecraft game port | 25565, 25566 | Player connections | Yes; must be unique on one IP |
| Backend WebSocket port | 8766 | Accepts every plugin connection | No; all plugins share it |
| Public TLS port | 443 | Plugins connect to the VPS with `wss://` | No; all plugins share it |

The plugin is a WebSocket client, not a listening server. Ten Minecraft instances can all connect to one backend endpoint. The backend identifies them by unique `server-id` and token. `game-port` is display metadata matching `server.properties`; it is not a bridge port or credential.

## 3. Recommended topology

```text
Minecraft host A
├─ Vanilla  :25565 ─┐
├─ Survival :25566 ─┼─ outbound WSS/443 ─┐
└─ Creative :25567 ─┘                    │
                                          ▼
                              Bot VPS public :443
                              ├─ Caddy/Nginx (TLS)
                              └─ 127.0.0.1:8766
                                          │
                                          ▼
                                  Matrix homeserver :443
```

Expose only the actual SSH administration port and TCP 443 on the VPS. Keep 8766 bound to `127.0.0.1`. Minecraft hosts normally need only outbound TCP 443 and no new inbound bridge port.

## 4. Configuring multiple Minecraft servers

All plugins use the same URL but different identities:

```yaml
# Vanilla
bridge:
  url: "wss://bridge.example.com/minecraft"
  server-id: "vanilla"
  server-name: "Vanilla"
  relay-group: "main"
  game-port: 25565
  token: "this-server's-own-64-hex-token"
```

For other instances, use IDs such as `survival` and `creative`, their own names and game ports, and separate tokens. Requirements:

- `server-id` is globally unique and stable;
- every server has its own token;
- game ports are unique on the same IP;
- only servers in the same `relay-group` exchange chat;
- backend registration agrees with plugin ID and route.

The same rules apply when Minecraft instances run on different physical hosts.

## 5. Why WSS instead of public HTTP or WS

The bridge needs bidirectional real-time delivery. `wss://` is WebSocket upgraded over HTTPS/TLS. Never expose this over the public Internet:

```text
ws://public-vps-ip:8766/minecraft
```

Plain WS exposes chat, server IDs, and Bearer tokens. Use `wss://bridge.example.com/minecraft`; certificate validation also helps prevent man-in-the-middle attacks.

## 6. Docker port mapping on the VPS

```yaml
services:
  matrix-serverchat:
    environment:
      WS_HOST: "0.0.0.0"
      WS_PORT: "8766"
    ports:
      - "127.0.0.1:8766:8766"
```

The container listens on all container interfaces, while the host publishes only on loopback. Do not change this to `0.0.0.0:8766:8766`, and do not add one mapping per plugin. Only completely independent backend instances need separate host ports such as 8766 and 8767.

## 7. Caddy WSS gateway

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

Point the A/AAAA record at the VPS, allow TCP 80/443 for certificate issuance, keep 8766 on loopback, and reload Caddy. Caddy handles WebSocket upgrades and preserves authentication headers.

## 8. Nginx WSS gateway

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
    location / { return 404; }
}
```

Adjust certificate paths, test the configuration, then reload gracefully. Never log the `Authorization` header.

## 9. Firewall and proxy services

- VPS: allow TCP 443; restrict the real SSH port by source where possible.
- VPS: deny public access to TCP 8766.
- Minecraft hosts: allow outbound TCP 443 to the bridge domain.
- VPS: allow HTTPS 443 to the Matrix homeserver.
- Configure IPv6 firewall and TLS correctly before publishing AAAA records.

With Cloudflare or similar services, enable WebSocket and strict TLS. During diagnosis, DNS-only mode can isolate proxy problems. A hidden path is not security; per-server 256-bit tokens remain the authentication boundary.

## 10. Migrating the backend

1. Prepare the new VPS, domain, TLS gateway, and Docker.
2. Stop the old bot so two instances do not sync the same Matrix account.
3. Use SQLite online backup, or copy `data/bridge.db` while the container is stopped.
4. Copy backend files, the database, and `secrets/matrix_password`.
5. Start the backend and verify Matrix login.
6. Verify the certificate and `wss://new-domain/minecraft` proxy.
7. Change each plugin's `bridge.url` and run `/matrixbridge reload` one server at a time.
8. Validate with `matrixbridge status`, `!server list`, and chat in both directions.
9. Disable the old endpoint only after everything is stable.

Copying the database preserves registrations, token hashes, bindings, and administrators. A new empty database requires registering owners, servers, and tokens again.

## 11. Verification and troubleshooting

```bash
cd /opt/matrix-serverchat
docker compose ps
docker compose logs --tail=200 matrix-serverchat
ss -lntp | grep 8766
```

The expected host listener is `127.0.0.1:8766`. Opening `/minecraft` in a normal browser is not a valid test because it lacks the WebSocket upgrade and authentication headers.

| Symptom | Check first |
| --- | --- |
| TLS certificate error | Domain, system time, chain, AAAA record |
| HTTP 404 | URL includes `/minecraft` |
| HTTP 502 | Container state and proxy access to loopback:8766 |
| WebSocket 4001 | ID, token, and enabled state |
| WebSocket 4003 | `server-id` syntax |
| WebSocket 4004 | Request path |
| Disconnects after about a minute | Proxy timeouts and idle policies |
| Only one server fails | Its token, ID, DNS, and outbound firewall |

Never post a complete token in chat or public logs.

## 12. Optional future HTTPS API

An HTTPS transport is feasible but unnecessary for ordinary VPS migration. WSS is already secure and real-time. For networks that prohibit WebSocket, a future adapter could provide:

```text
POST /api/v1/minecraft/events
GET  /api/v1/minecraft/messages
POST /api/v1/minecraft/ack
GET  /healthz
```

SSE may replace long polling for downstream delivery, but uploads and acknowledgements still need HTTPS POST. The transport must retain per-server credentials, HTTPS-only operation, global IDs, idempotency, ACK/retry/deduplication, body and rate limits, downstream cursors, token rotation, disabling, auditing, and optionally timestamp/nonce/HMAC replay protection. It should coexist with WebSocket and reuse the same internal message model.

## 13. Go-live checklist

- [ ] Domain points only to the new VPS.
- [ ] TLS certificate is valid and renews automatically.
- [ ] Plugins use `wss://domain/minecraft`.
- [ ] Port 8766 binds only to `127.0.0.1`.
- [ ] Every server has a unique ID and token.
- [ ] Game ports do not collide on one host.
- [ ] Database, Matrix password, and administrators are migrated.
- [ ] Old and new bots are not running simultaneously.
- [ ] `!server list` shows healthy connections.
- [ ] Matrix-to-game, game-to-Matrix, relay chat, and mentions are tested.
