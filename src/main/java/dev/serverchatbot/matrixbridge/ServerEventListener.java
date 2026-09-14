package dev.serverchatbot.matrixbridge;

import com.google.gson.JsonObject;
import io.papermc.paper.event.player.AsyncChatEvent;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.serializer.gson.GsonComponentSerializer;
import net.kyori.adventure.text.serializer.plain.PlainTextComponentSerializer;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.player.PlayerAdvancementDoneEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;

public final class ServerEventListener implements Listener {
    private static final PlainTextComponentSerializer PLAIN = PlainTextComponentSerializer.plainText();
    private static final GsonComponentSerializer COMPONENT_JSON = GsonComponentSerializer.gson();

    private final MatrixBridgePlugin plugin;

    public ServerEventListener(MatrixBridgePlugin plugin) {
        this.plugin = plugin;
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPlayerJoin(PlayerJoinEvent event) {
        if (!plugin.bridgeConfig().events().playerJoin()) {
            return;
        }
        JsonObject payload = playerPayload(event.getPlayer());
        addMessage(payload, event.joinMessage());
        plugin.publish("player_join", payload);
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPlayerQuit(PlayerQuitEvent event) {
        if (!plugin.bridgeConfig().events().playerQuit()) {
            return;
        }
        JsonObject payload = playerPayload(event.getPlayer());
        addMessage(payload, event.quitMessage());
        plugin.publish("player_quit", payload);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onPlayerChat(AsyncChatEvent event) {
        if (!plugin.bridgeConfig().events().playerChat()) {
            return;
        }
        String plainMessage = PLAIN.serialize(event.message());
        // Commands have their own preprocess event and do not normally reach
        // AsyncChatEvent. Keep this guard for plugins that synthesize chat events.
        if (plainMessage.stripLeading().startsWith("/")) {
            return;
        }
        Player player = event.getPlayer();
        Component message = event.message();
        Runnable publish = () -> {
            if (!plugin.isEnabled()) {
                return;
            }
            JsonObject payload = playerPayload(player);
            addMessage(payload, message);
            plugin.publish("player_chat", payload);
        };
        if (event.isAsynchronous()) {
            // Player/world/display-name reads stay on the server thread. The
            // WebSocket send itself remains asynchronous inside WebSocketBridge.
            plugin.getServer().getScheduler().runTask(plugin, publish);
        } else {
            publish.run();
        }
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPlayerDeath(PlayerDeathEvent event) {
        if (!plugin.bridgeConfig().events().playerDeath()) {
            return;
        }
        JsonObject payload = playerPayload(event.getEntity());
        addMessage(payload, event.deathMessage());
        plugin.publish("player_death", payload);
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onAdvancement(PlayerAdvancementDoneEvent event) {
        if (!plugin.bridgeConfig().events().advancement()) {
            return;
        }
        Component announcement = event.message();
        // Null covers recipes, hidden advancements, and advancements configured
        // not to announce in chat.
        if (announcement == null) {
            return;
        }
        JsonObject payload = playerPayload(event.getPlayer());
        payload.addProperty("advancementKey", event.getAdvancement().getKey().asString());
        addMessage(payload, announcement);
        plugin.publish("player_advancement", payload);
    }

    private static JsonObject playerPayload(Player player) {
        JsonObject payload = new JsonObject();
        payload.addProperty("playerUuid", player.getUniqueId().toString());
        payload.addProperty("playerName", player.getName());
        payload.addProperty("playerDisplayName", PLAIN.serialize(player.displayName()));
        payload.addProperty("world", player.getWorld().getName());
        return payload;
    }

    private static void addMessage(JsonObject payload, Component message) {
        if (message == null) {
            return;
        }
        JsonObject serialized = new JsonObject();
        serialized.addProperty("plain", PLAIN.serialize(message));
        serialized.addProperty("componentJson", COMPONENT_JSON.serialize(message));
        payload.add("message", serialized);
    }
}
