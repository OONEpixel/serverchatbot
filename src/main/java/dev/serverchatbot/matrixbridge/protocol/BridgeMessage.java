package dev.serverchatbot.matrixbridge.protocol;

import com.google.gson.JsonObject;

public record BridgeMessage(
        int v,
        String id,
        String type,
        String timestamp,
        String serverId,
        JsonObject payload
) {
}
