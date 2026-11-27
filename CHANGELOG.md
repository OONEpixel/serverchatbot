# Changelog

## Unreleased

- Added a daily quiet window for unexpected Minecraft-to-bridge connection
  alerts. The backend continues to track connections and alerts at the end of
  the window if the server is still disconnected.

## 0.4.9 - 2026-09-23

- Added Matrix and Minecraft shared mini-games: dice, coin toss, and a
  relay-group-scoped number guessing game.
- Added a light PNG cumulative playtime leaderboard with cached player heads,
  per-server ranking, and online/offline state.
- Expanded `#ping` with live backend-to-plugin round-trip time, player ping,
  one-minute TPS, and MSPT.
- Added graceful Minecraft shutdown/restart announcements and authenticated
  restart recovery notices, while retaining debounced alerts for abrupt outages.
- Persisted reconnect-loop suppression and added player/admin recovery commands.
- Added portable Matrix command threads, client-compatible avatar modes,
  media labels, detailed command help, and server/admin audit improvements.
- Changed the default bridge listener from port 8765 to 8766 to avoid conflicts
  with unrelated services.
- Added systemd/Compose persistence guidance and updated multi-server,
  cross-host, and protocol documentation.

Protocol remains version 2. Upgrade the backend and every Minecraft plugin
together to obtain cumulative statistics, telemetry, and graceful restart
notifications.
