package dev.serverchatbot.matrixbridge.config;

import org.bukkit.configuration.file.FileConfiguration;

import java.net.URI;
import java.time.Duration;

public record BridgeConfig(
        boolean enabled,
        URI url,
        String serverId,
        String serverName,
        String relayGroup,
        int gamePort,
        String token,
        Duration connectTimeout,
        Duration reconnectInitial,
        Duration reconnectMax,
        int maxPendingEvents,
        int maxInboundMessageLength,
        EventSettings events,
        String inboundFormat,
        boolean logInboundToConsole,
        String relayFormat,
        boolean logRelayToConsole
) {
    public static BridgeConfig from(FileConfiguration config) {
        URI url = parseWebSocketUri(config.getString("bridge.url", "ws://127.0.0.1:8765/minecraft"));
        String serverId = requireText(config.getString("bridge.server-id"), "bridge.server-id");
        String serverName = requireText(config.getString("bridge.server-name", serverId), "bridge.server-name");
        String relayGroup = requireText(config.getString("bridge.relay-group", "main"), "bridge.relay-group");
        int gamePort = port(config.getInt("bridge.game-port", 25565), "bridge.game-port");
        String token = requireText(config.getString("bridge.token"), "bridge.token");

        int connectTimeout = positive(config.getInt("bridge.connect-timeout-seconds", 10),
                "bridge.connect-timeout-seconds");
        int reconnectInitial = positive(config.getInt("bridge.reconnect-initial-seconds", 1),
                "bridge.reconnect-initial-seconds");
        int reconnectMax = positive(config.getInt("bridge.reconnect-max-seconds", 30),
                "bridge.reconnect-max-seconds");
        if (reconnectMax < reconnectInitial) {
            throw new IllegalArgumentException("bridge.reconnect-max-seconds must be >= reconnect-initial-seconds");
        }

        EventSettings eventSettings = new EventSettings(
                config.getBoolean("events.player-join", true),
                config.getBoolean("events.player-quit", true),
                config.getBoolean("events.player-chat", true),
                config.getBoolean("events.player-death", true),
                config.getBoolean("events.advancement", true)
        );

        return new BridgeConfig(
                config.getBoolean("bridge.enabled", true),
                url,
                serverId,
                serverName,
                relayGroup,
                gamePort,
                token,
                Duration.ofSeconds(connectTimeout),
                Duration.ofSeconds(reconnectInitial),
                Duration.ofSeconds(reconnectMax),
                positive(config.getInt("bridge.max-pending-events", 1000), "bridge.max-pending-events"),
                positive(config.getInt("bridge.max-inbound-message-length", 500),
                        "bridge.max-inbound-message-length"),
                eventSettings,
                requireText(config.getString("inbound.format"), "inbound.format"),
                config.getBoolean("inbound.log-to-console", true),
                requireText(config.getString("relay.format",
                        "<gold>[<server>]</gold><aqua>\\<<author>></aqua><white><message></white>"),
                        "relay.format"),
                config.getBoolean("relay.log-to-console", true)
        );
    }

    public boolean hasPlaceholderToken() {
        return token.equals("CHANGE_ME");
    }

    private static URI parseWebSocketUri(String value) {
        String text = requireText(value, "bridge.url");
        URI uri;
        try {
            uri = URI.create(text);
        } catch (IllegalArgumentException exception) {
            throw new IllegalArgumentException("bridge.url is not a valid URI", exception);
        }
        if (!"ws".equalsIgnoreCase(uri.getScheme()) && !"wss".equalsIgnoreCase(uri.getScheme())) {
            throw new IllegalArgumentException("bridge.url must use ws:// or wss://");
        }
        if (uri.getHost() == null) {
            throw new IllegalArgumentException("bridge.url must include a host");
        }
        return uri;
    }

    private static int positive(int value, String path) {
        if (value <= 0) {
            throw new IllegalArgumentException(path + " must be greater than zero");
        }
        return value;
    }

    private static int port(int value, String path) {
        if (value < 1 || value > 65535) {
            throw new IllegalArgumentException(path + " must be between 1 and 65535");
        }
        return value;
    }

    private static String requireText(String value, String path) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(path + " must not be blank");
        }
        return value.trim();
    }

    public record EventSettings(
            boolean playerJoin,
            boolean playerQuit,
            boolean playerChat,
            boolean playerDeath,
            boolean advancement
    ) {
    }
}
