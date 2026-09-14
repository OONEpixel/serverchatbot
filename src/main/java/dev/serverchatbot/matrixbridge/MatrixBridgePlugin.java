package dev.serverchatbot.matrixbridge;

import com.google.gson.JsonObject;
import dev.serverchatbot.matrixbridge.config.BridgeConfig;
import dev.serverchatbot.matrixbridge.net.WebSocketBridge;
import dev.serverchatbot.matrixbridge.protocol.MatrixInboundMessage;
import dev.serverchatbot.matrixbridge.protocol.BridgeMessage;
import dev.serverchatbot.matrixbridge.protocol.ProtocolCodec;
import dev.serverchatbot.matrixbridge.protocol.RelayInboundMessage;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import net.kyori.adventure.text.minimessage.MiniMessage;
import net.kyori.adventure.text.minimessage.tag.resolver.Placeholder;
import org.bukkit.command.PluginCommand;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.List;
import java.util.HexFormat;
import java.security.SecureRandom;
import java.util.UUID;

public final class MatrixBridgePlugin extends JavaPlugin {
    private static final MiniMessage MINI_MESSAGE = MiniMessage.miniMessage();

    private volatile BridgeConfig bridgeConfig;
    private volatile WebSocketBridge bridge;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        try {
            bridgeConfig = BridgeConfig.from(getConfig());
        } catch (IllegalArgumentException exception) {
            getLogger().severe("配置无效，插件已停用: " + exception.getMessage());
            getServer().getPluginManager().disablePlugin(this);
            return;
        }

        getServer().getPluginManager().registerEvents(new ServerEventListener(this), this);
        BridgeCommand bridgeCommand = new BridgeCommand(this);
        PluginCommand command = getCommand("matrixbridge");
        if (command != null) {
            command.setExecutor(bridgeCommand);
            command.setTabCompleter(bridgeCommand);
        }
        PluginCommand bindCommand = getCommand("matrixbind");
        if (bindCommand != null) {
            bindCommand.setExecutor(new BindCommand(this));
        }
        startConfiguredBridge();
    }

    @Override
    public void onDisable() {
        WebSocketBridge current = bridge;
        bridge = null;
        if (current != null) {
            current.close();
        }
    }

    public BridgeConfig bridgeConfig() {
        return bridgeConfig;
    }

    public void publish(String type, JsonObject payload) {
        WebSocketBridge current = bridge;
        if (current != null) {
            current.publish(type, payload);
        }
    }

    public boolean sendControlMessage(String type, JsonObject payload) {
        WebSocketBridge current = bridge;
        return current != null && current.sendControl(type, payload);
    }

    public boolean reconnect() {
        WebSocketBridge current = bridge;
        if (current == null) {
            return false;
        }
        current.reconnectNow();
        return true;
    }

    public String generateAndInstallToken() {
        byte[] bytes = new byte[32];
        new SecureRandom().nextBytes(bytes);
        String token = HexFormat.of().formatHex(bytes);
        getConfig().set("bridge.token", token);
        saveConfig();
        reloadBridge();
        return token;
    }

    public void reloadBridge() {
        reloadConfig();
        BridgeConfig newConfig = BridgeConfig.from(getConfig());
        WebSocketBridge previous = bridge;
        bridge = null;
        bridgeConfig = newConfig;
        if (previous != null) {
            previous.close();
        }
        startConfiguredBridge();
    }

    public String connectionSummary() {
        WebSocketBridge current = bridge;
        if (!bridgeConfig.enabled()) {
            return "Matrix 桥接已在配置中关闭。";
        }
        if (current == null) {
            return "Matrix 桥接未运行；请检查 token 和配置。";
        }
        String state = current.isConnected() ? "已连接" : "正在等待连接";
        return "Matrix 桥接" + state + "，待确认事件 " + current.pendingCount() + " 条。";
    }

    private void startConfiguredBridge() {
        BridgeConfig config = bridgeConfig;
        if (!config.enabled()) {
            getLogger().info("Matrix 桥接已在配置中关闭。");
            return;
        }
        if (config.hasPlaceholderToken()) {
            getLogger().severe("bridge.token 仍为 CHANGE_ME；为避免未认证连接，桥接不会启动。修改后执行 /matrixbridge reload。");
            return;
        }
        warnAboutRemotePlaintext(config);
        WebSocketBridge replacement = new WebSocketBridge(
                this,
                config,
                new ProtocolCodec(),
                this::acceptMatrixMessage,
                this::acceptRelayMessage,
                this::acceptControlMessage
        );
        bridge = replacement;
        replacement.start();
    }

    private void acceptMatrixMessage(MatrixInboundMessage message) {
        BridgeConfig config = bridgeConfig;
        String body = truncate(message.body(), config.maxInboundMessageLength());
        Component rendered;
        try {
            rendered = MINI_MESSAGE.deserialize(
                    config.inboundFormat(),
                    Placeholder.unparsed("sender", message.senderDisplayName()),
                    Placeholder.unparsed("message", body),
                    Placeholder.unparsed("room", message.roomId())
            );
        } catch (RuntimeException exception) {
            getLogger().warning("inbound.format 无法解析，消息未广播: " + exception.getMessage());
            return;
        }

        getServer().getScheduler().runTask(this, () -> {
            getServer().broadcast(rendered);
            if (config.logInboundToConsole()) {
                getLogger().info("[Matrix] " + message.senderDisplayName() + ": " + body);
            }
        });
    }

    private void acceptRelayMessage(RelayInboundMessage message) {
        BridgeConfig config = bridgeConfig;
        String body = truncate(message.body(), config.maxInboundMessageLength());
        Component rendered;
        try {
            rendered = MINI_MESSAGE.deserialize(
                    config.relayFormat(),
                    Placeholder.unparsed("server", message.sourceDisplayName()),
                    Placeholder.unparsed("server-id", message.sourceServerId()),
                    Placeholder.unparsed("author", message.authorDisplayName()),
                    Placeholder.unparsed("author-id", message.authorId()),
                    Placeholder.unparsed("message", body),
                    Placeholder.unparsed("route", message.route())
            );
        } catch (RuntimeException exception) {
            getLogger().warning("relay.format 无法解析，消息未广播: " + exception.getMessage());
            return;
        }

        getServer().getScheduler().runTask(this, () -> {
            getServer().broadcast(rendered);
            if (config.logRelayToConsole()) {
                getLogger().info("[" + message.sourceDisplayName() + "]<"
                        + message.authorDisplayName() + "> " + body);
            }
        });
    }

    private void acceptControlMessage(BridgeMessage message) {
        switch (message.type()) {
            case "query_list_request" -> answerOnlinePlayerQuery(message);
            case "bind_result" -> deliverBindResult(message);
            default -> getLogger().warning("收到未处理的控制消息: " + message.type());
        }
    }

    private void answerOnlinePlayerQuery(BridgeMessage message) {
        getServer().getScheduler().runTask(this, () -> {
            List<String> playerNames = getServer().getOnlinePlayers().stream()
                    .map(Player::getName)
                    .sorted(String.CASE_INSENSITIVE_ORDER)
                    .toList();

            JsonObject payload = new JsonObject();
            payload.addProperty("onlineCount", playerNames.size());
            com.google.gson.JsonArray players = new com.google.gson.JsonArray();
            playerNames.forEach(players::add);
            payload.add("playerList", players);
            // Extra correlation data is ignored by the current bot.py but makes
            // logs and future concurrent request handling unambiguous.
            payload.addProperty("requestId", message.id());

            if (!sendControlMessage("query_list_response", payload)) {
                getLogger().warning("在线玩家查询完成时机器人后台已经断开，响应未发送。");
            }
        });
    }

    private void deliverBindResult(BridgeMessage message) {
        JsonObject payload = message.payload();
        if (!payload.has("success") || !payload.get("success").isJsonPrimitive()
                || !payload.getAsJsonPrimitive("success").isBoolean()) {
            getLogger().warning("bind_result 缺少布尔字段 success");
            return;
        }
        boolean success = payload.get("success").getAsBoolean();
        String resultText = payload.has("message") && payload.get("message").isJsonPrimitive()
                ? payload.get("message").getAsString()
                : (success ? "Matrix 账号绑定成功。" : "Matrix 账号绑定失败。");
        String playerUuid = payload.has("playerUuid") && payload.get("playerUuid").isJsonPrimitive()
                ? payload.get("playerUuid").getAsString()
                : "";
        String playerName = payload.has("playerId") && payload.get("playerId").isJsonPrimitive()
                ? payload.get("playerId").getAsString()
                : "";

        getServer().getScheduler().runTask(this, () -> {
            Player player = null;
            if (!playerUuid.isBlank()) {
                try {
                    player = getServer().getPlayer(UUID.fromString(playerUuid));
                } catch (IllegalArgumentException ignored) {
                    getLogger().warning("bind_result 包含无效的 playerUuid: " + playerUuid);
                }
            }
            if (player == null && !playerName.isBlank()) {
                player = getServer().getPlayerExact(playerName);
            }
            if (player != null) {
                player.sendMessage(Component.text(resultText,
                        success ? NamedTextColor.GREEN : NamedTextColor.RED));
            } else {
                getLogger().info("绑定结果对应的玩家已离线: " + playerName + "；结果: " + resultText);
            }
        });
    }

    private void warnAboutRemotePlaintext(BridgeConfig config) {
        if (!"ws".equalsIgnoreCase(config.url().getScheme())) {
            return;
        }
        String host = config.url().getHost();
        boolean obviousLoopback = host.equalsIgnoreCase("localhost")
                || host.equals("127.0.0.1")
                || host.equals("0:0:0:0:0:0:0:1")
                || host.equals("::1");
        if (!obviousLoopback) {
            getLogger().warning("bridge.url 使用非本机明文 ws://；远程部署时请改用 wss://。");
        }
    }

    private static String truncate(String value, int maxCodePoints) {
        if (value.codePointCount(0, value.length()) <= maxCodePoints) {
            return value;
        }
        int end = value.offsetByCodePoints(0, maxCodePoints);
        return value.substring(0, end) + "…";
    }
}
