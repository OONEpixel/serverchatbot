package dev.serverchatbot.matrixbridge.net;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import dev.serverchatbot.matrixbridge.config.BridgeConfig;
import dev.serverchatbot.matrixbridge.protocol.BridgeMessage;
import dev.serverchatbot.matrixbridge.protocol.MatrixInboundMessage;
import dev.serverchatbot.matrixbridge.protocol.ProtocolCodec;
import dev.serverchatbot.matrixbridge.protocol.ProtocolException;
import dev.serverchatbot.matrixbridge.protocol.RelayInboundMessage;
import org.bukkit.plugin.java.JavaPlugin;

import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Consumer;

public final class WebSocketBridge implements AutoCloseable {
    private static final int MAX_PROTOCOL_MESSAGE_LENGTH = 65_536;
    private static final int MAX_SEEN_INBOUND_IDS = 2_048;

    private final JavaPlugin plugin;
    private final BridgeConfig config;
    private final ProtocolCodec codec;
    private final Consumer<MatrixInboundMessage> inboundConsumer;
    private final Consumer<RelayInboundMessage> relayConsumer;
    private final Consumer<BridgeMessage> controlConsumer;
    private final ScheduledExecutorService executor;
    private final HttpClient httpClient;
    private final AtomicReference<WebSocket> socket = new AtomicReference<>();
    private final AtomicBoolean connecting = new AtomicBoolean();
    private final AtomicBoolean reconnectScheduled = new AtomicBoolean();
    private final Object pendingLock = new Object();
    private final LinkedHashMap<String, String> pending = new LinkedHashMap<>();
    private final Object seenLock = new Object();
    private final LinkedHashMap<String, Boolean> seenInbound = new LinkedHashMap<>();

    private volatile boolean stopping;
    private volatile long reconnectDelaySeconds;
    private CompletableFuture<Void> sendChain = CompletableFuture.completedFuture(null);

    public WebSocketBridge(
            JavaPlugin plugin,
            BridgeConfig config,
            ProtocolCodec codec,
            Consumer<MatrixInboundMessage> inboundConsumer,
            Consumer<RelayInboundMessage> relayConsumer,
            Consumer<BridgeMessage> controlConsumer
    ) {
        this.plugin = plugin;
        this.config = config;
        this.codec = codec;
        this.inboundConsumer = inboundConsumer;
        this.relayConsumer = relayConsumer;
        this.controlConsumer = controlConsumer;
        this.reconnectDelaySeconds = config.reconnectInitial().toSeconds();
        this.executor = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "matrix-chat-bridge-io");
            thread.setDaemon(true);
            return thread;
        });
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(config.connectTimeout())
                .executor(executor)
                .build();
    }

    public void start() {
        if (!stopping) {
            executor.execute(this::connect);
        }
    }

    public void publish(String type, JsonObject payload) {
        if (stopping) {
            return;
        }
        BridgeMessage message = codec.create(type, config.serverId(), payload);
        String json = codec.encode(message);
        synchronized (pendingLock) {
            if (pending.size() >= config.maxPendingEvents()) {
                String oldestId = pending.keySet().iterator().next();
                pending.remove(oldestId);
                plugin.getLogger().warning("待确认事件队列已满，已丢弃最旧事件 " + oldestId);
            }
            pending.put(message.id(), json);
        }
        transmitIfConnected(json);
    }

    /**
     * Sends a control-plane response that the current bot implementation does
     * not acknowledge. It is intentionally not added to the reliable event
     * queue, otherwise query and binding responses would remain pending forever.
     */
    public boolean sendControl(String type, JsonObject payload) {
        if (stopping) {
            return false;
        }
        WebSocket current = socket.get();
        if (current == null) {
            return false;
        }
        String json = codec.encode(codec.create(type, config.serverId(), payload));
        transmit(current, json);
        return true;
    }

    public boolean isConnected() {
        return socket.get() != null;
    }

    public int pendingCount() {
        synchronized (pendingLock) {
            return pending.size();
        }
    }

    public void reconnectNow() {
        if (stopping) {
            return;
        }
        reconnectDelaySeconds = config.reconnectInitial().toSeconds();
        WebSocket current = socket.getAndSet(null);
        if (current != null) {
            current.abort();
        }
        reconnectScheduled.set(false);
        executor.execute(this::connect);
    }

    private void connect() {
        if (stopping || socket.get() != null || !connecting.compareAndSet(false, true)) {
            return;
        }

        WebSocket.Builder builder = httpClient.newWebSocketBuilder()
                .connectTimeout(config.connectTimeout())
                .header("Authorization", "Bearer " + config.token())
                .header("X-Minecraft-Server-Id", config.serverId());

        builder.buildAsync(config.url(), new SocketListener())
                .whenComplete((ignored, error) -> {
                    connecting.set(false);
                    if (error != null && !stopping) {
                        plugin.getLogger().warning("无法连接机器人后台: " + rootMessage(error));
                        scheduleReconnect();
                    }
                });
    }

    private void connected(WebSocket webSocket) {
        if (stopping) {
            webSocket.abort();
            return;
        }
        WebSocket previous = socket.getAndSet(webSocket);
        if (previous != null && previous != webSocket) {
            previous.abort();
        }
        reconnectScheduled.set(false);
        reconnectDelaySeconds = config.reconnectInitial().toSeconds();
        plugin.getLogger().info("已连接机器人后台 " + config.url());

        JsonObject hello = new JsonObject();
        hello.addProperty("pluginVersion", plugin.getPluginMeta().getVersion());
        hello.addProperty("minecraftVersion", plugin.getServer().getMinecraftVersion());
        hello.addProperty("serverSoftware", plugin.getServer().getName());
        hello.addProperty("serverName", config.serverName());
        hello.addProperty("relayGroup", config.relayGroup());
        hello.addProperty("gamePort", config.gamePort());
        transmit(webSocket, codec.encode(codec.create("hello", config.serverId(), hello)));

        List<String> toRetry;
        synchronized (pendingLock) {
            toRetry = new ArrayList<>(pending.values());
        }
        toRetry.forEach(json -> transmit(webSocket, json));
    }

    private void transmitIfConnected(String json) {
        WebSocket current = socket.get();
        if (current != null) {
            transmit(current, json);
        }
    }

    private void transmit(WebSocket expectedSocket, String json) {
        executor.execute(() -> sendChain = sendChain
                .handle((ignored, previousError) -> null)
                .thenCompose(ignored -> {
                    if (stopping || socket.get() != expectedSocket) {
                        return CompletableFuture.<Void>completedFuture(null);
                    }
                    return expectedSocket.sendText(json, true).thenApply(sent -> (Void) null);
                })
                .whenComplete((ignored, error) -> {
                    if (error != null) {
                        disconnected(expectedSocket, "发送失败: " + rootMessage(error));
                    }
                }));
    }

    private void receive(String json) {
        BridgeMessage message;
        try {
            message = codec.decode(json);
        } catch (ProtocolException | IllegalArgumentException exception) {
            plugin.getLogger().warning("忽略无效的后台消息: " + exception.getMessage());
            return;
        }

        if (!message.serverId().equals(config.serverId()) && !message.serverId().equals("*")) {
            plugin.getLogger().warning("忽略发往其他服务器的消息: " + message.serverId());
            return;
        }

        try {
            switch (message.type()) {
                case "ack" -> acknowledge(ProtocolCodec.requiredPayloadString(message.payload(), "ackFor"));
                case "ping" -> pong(message.id());
                case "matrix_message" -> receiveMatrixMessage(message);
                case "relay_message" -> receiveRelayMessage(message);
                case "query_list_request" -> controlConsumer.accept(message);
                case "bind_result" -> controlConsumer.accept(message);
                default -> plugin.getLogger().warning("忽略未知后台消息类型: " + message.type());
            }
        } catch (ProtocolException exception) {
            plugin.getLogger().warning("忽略字段不完整的后台消息: " + exception.getMessage());
        }
    }

    private void acknowledge(String acknowledgedId) {
        synchronized (pendingLock) {
            pending.remove(acknowledgedId);
        }
    }

    private void pong(String pingId) {
        JsonObject payload = new JsonObject();
        payload.addProperty("replyTo", pingId);
        transmitIfConnected(codec.encode(codec.create("pong", config.serverId(), payload)));
    }

    private void receiveMatrixMessage(BridgeMessage message) throws ProtocolException {
        JsonObject payload = message.payload();
        String roomId = ProtocolCodec.requiredPayloadString(payload, "roomId");
        String eventId = ProtocolCodec.requiredPayloadString(payload, "eventId");
        String sender = ProtocolCodec.requiredPayloadString(payload, "sender");
        String body = ProtocolCodec.requiredPayloadString(payload, "body");
        String displayName = optionalString(payload, "senderDisplayName", sender);

        if (rememberInbound(message.id())) {
            inboundConsumer.accept(new MatrixInboundMessage(
                    message.id(), roomId, eventId, sender, displayName, body
            ));
        }
        sendAck(message.id());
    }

    private void receiveRelayMessage(BridgeMessage message) throws ProtocolException {
        JsonObject payload = message.payload();
        JsonObject source = ProtocolCodec.requiredPayloadObject(payload, "source");
        JsonObject author = ProtocolCodec.requiredPayloadObject(payload, "author");
        String sourceKind = ProtocolCodec.requiredPayloadString(source, "kind");
        String sourceServerId = ProtocolCodec.requiredPayloadString(source, "serverId");
        String sourceDisplayName = ProtocolCodec.requiredPayloadString(source, "displayName");
        String authorId = ProtocolCodec.requiredPayloadString(author, "id");
        String authorDisplayName = ProtocolCodec.requiredPayloadString(author, "displayName");
        String body = ProtocolCodec.requiredPayloadString(payload, "body");
        String route = ProtocolCodec.requiredPayloadString(payload, "route");
        int hop = ProtocolCodec.requiredPayloadInt(payload, "hop");

        if (!sourceKind.equals("minecraft") || sourceServerId.equals(config.serverId())
                || !route.equals(config.relayGroup()) || hop != 1) {
            plugin.getLogger().warning("忽略路由、来源或跳数无效的 relay_message " + message.id());
            sendAck(message.id());
            return;
        }
        if (rememberInbound(message.id())) {
            relayConsumer.accept(new RelayInboundMessage(
                    message.id(),
                    sourceServerId,
                    sourceDisplayName,
                    authorId,
                    authorDisplayName,
                    body,
                    route
            ));
        }
        sendAck(message.id());
    }

    private boolean rememberInbound(String id) {
        synchronized (seenLock) {
            if (seenInbound.containsKey(id)) {
                return false;
            }
            if (seenInbound.size() >= MAX_SEEN_INBOUND_IDS) {
                String oldest = seenInbound.keySet().iterator().next();
                seenInbound.remove(oldest);
            }
            seenInbound.put(id, Boolean.TRUE);
            return true;
        }
    }

    private void sendAck(String acknowledgedId) {
        JsonObject payload = new JsonObject();
        payload.addProperty("ackFor", acknowledgedId);
        transmitIfConnected(codec.encode(codec.create("ack", config.serverId(), payload)));
    }

    private void disconnected(WebSocket webSocket, String reason) {
        if (socket.compareAndSet(webSocket, null) && !stopping) {
            webSocket.abort();
            plugin.getLogger().warning("与机器人后台断开: " + reason);
            scheduleReconnect();
        }
    }

    private void scheduleReconnect() {
        if (stopping || !reconnectScheduled.compareAndSet(false, true)) {
            return;
        }
        long delay = reconnectDelaySeconds;
        reconnectDelaySeconds = Math.min(
                Math.max(delay * 2, config.reconnectInitial().toSeconds()),
                config.reconnectMax().toSeconds()
        );
        executor.schedule(() -> {
            reconnectScheduled.set(false);
            connect();
        }, delay, TimeUnit.SECONDS);
    }

    private static String optionalString(JsonObject object, String name, String fallback) {
        JsonElement element = object.get(name);
        if (element == null || !element.isJsonPrimitive() || !element.getAsJsonPrimitive().isString()) {
            return fallback;
        }
        String value = element.getAsString();
        return value.isBlank() ? fallback : value;
    }

    private static String rootMessage(Throwable throwable) {
        Throwable current = throwable;
        while (current.getCause() != null) {
            current = current.getCause();
        }
        String message = current.getMessage();
        return message == null || message.isBlank() ? current.getClass().getSimpleName() : message;
    }

    @Override
    public void close() {
        stopping = true;
        WebSocket current = socket.getAndSet(null);
        if (current != null) {
            current.sendClose(WebSocket.NORMAL_CLOSURE, "plugin disabled")
                    .orTimeout(2, TimeUnit.SECONDS)
                    .exceptionally(error -> {
                        current.abort();
                        return null;
                    });
        }
        executor.shutdownNow();
    }

    private final class SocketListener implements WebSocket.Listener {
        private final StringBuilder textBuffer = new StringBuilder();

        @Override
        public void onOpen(WebSocket webSocket) {
            connected(webSocket);
            webSocket.request(1);
        }

        @Override
        public CompletionStage<?> onText(WebSocket webSocket, CharSequence data, boolean last) {
            if (textBuffer.length() + data.length() > MAX_PROTOCOL_MESSAGE_LENGTH) {
                disconnected(webSocket, "收到的消息超过 64 KiB 限制");
                return CompletableFuture.completedFuture(null);
            }
            textBuffer.append(data);
            if (last) {
                String complete = textBuffer.toString();
                textBuffer.setLength(0);
                executor.execute(() -> receive(complete));
            }
            webSocket.request(1);
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public CompletionStage<?> onPing(WebSocket webSocket, ByteBuffer message) {
            webSocket.request(1);
            return webSocket.sendPong(message);
        }

        @Override
        public CompletionStage<?> onClose(WebSocket webSocket, int statusCode, String reason) {
            disconnected(webSocket, "关闭码 " + statusCode + (reason.isBlank() ? "" : " (" + reason + ")"));
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public void onError(WebSocket webSocket, Throwable error) {
            disconnected(webSocket, rootMessage(error));
        }
    }
}
