package dev.serverchatbot.matrixbridge.protocol;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParseException;

import java.time.Instant;
import java.util.Objects;
import java.util.UUID;

public final class ProtocolCodec {
    public static final int VERSION = 2;

    private final Gson gson = new GsonBuilder().disableHtmlEscaping().create();

    public BridgeMessage create(String type, String serverId, JsonObject payload) {
        return new BridgeMessage(
                VERSION,
                UUID.randomUUID().toString(),
                requireText(type, "type"),
                Instant.now().toString(),
                requireText(serverId, "serverId"),
                Objects.requireNonNull(payload, "payload")
        );
    }

    public String encode(BridgeMessage message) {
        return gson.toJson(message);
    }

    public BridgeMessage decode(String json) throws ProtocolException {
        try {
            JsonElement root = gson.fromJson(json, JsonElement.class);
            if (root == null || !root.isJsonObject()) {
                throw new ProtocolException("message must be a JSON object");
            }
            JsonObject object = root.getAsJsonObject();
            int version = requiredInt(object, "v");
            if (version != VERSION) {
                throw new ProtocolException("unsupported protocol version: " + version);
            }
            return new BridgeMessage(
                    version,
                    requiredString(object, "id"),
                    requiredString(object, "type"),
                    requiredString(object, "timestamp"),
                    requiredString(object, "serverId"),
                    requiredObject(object, "payload")
            );
        } catch (JsonParseException | IllegalStateException exception) {
            throw new ProtocolException("invalid JSON message", exception);
        }
    }

    public static String requiredPayloadString(JsonObject payload, String name) throws ProtocolException {
        return requiredString(payload, name);
    }

    public static JsonObject requiredPayloadObject(JsonObject payload, String name) throws ProtocolException {
        return requiredObject(payload, name);
    }

    public static int requiredPayloadInt(JsonObject payload, String name) throws ProtocolException {
        return requiredInt(payload, name);
    }

    private static String requiredString(JsonObject object, String name) throws ProtocolException {
        JsonElement element = object.get(name);
        if (element == null || !element.isJsonPrimitive() || !element.getAsJsonPrimitive().isString()) {
            throw new ProtocolException("field '" + name + "' must be a string");
        }
        String value = element.getAsString();
        if (value.isBlank()) {
            throw new ProtocolException("field '" + name + "' must not be blank");
        }
        return value;
    }

    private static int requiredInt(JsonObject object, String name) throws ProtocolException {
        JsonElement element = object.get(name);
        if (element == null || !element.isJsonPrimitive() || !element.getAsJsonPrimitive().isNumber()) {
            throw new ProtocolException("field '" + name + "' must be a number");
        }
        return element.getAsInt();
    }

    private static JsonObject requiredObject(JsonObject object, String name) throws ProtocolException {
        JsonElement element = object.get(name);
        if (element == null || !element.isJsonObject()) {
            throw new ProtocolException("field '" + name + "' must be an object");
        }
        return element.getAsJsonObject();
    }

    private static String requireText(String value, String name) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(name + " must not be blank");
        }
        return value;
    }
}
