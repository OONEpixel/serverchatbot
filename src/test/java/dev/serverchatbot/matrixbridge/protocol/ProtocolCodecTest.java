package dev.serverchatbot.matrixbridge.protocol;

import com.google.gson.JsonObject;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class ProtocolCodecTest {
    private final ProtocolCodec codec = new ProtocolCodec();

    @Test
    void roundTripsUnicodePayload() throws Exception {
        JsonObject payload = new JsonObject();
        payload.addProperty("body", "你好，Matrix！");
        BridgeMessage original = codec.create("player_chat", "survival", payload);

        BridgeMessage decoded = codec.decode(codec.encode(original));

        assertEquals(ProtocolCodec.VERSION, decoded.v());
        assertEquals(original.id(), decoded.id());
        assertEquals("player_chat", decoded.type());
        assertEquals("你好，Matrix！", decoded.payload().get("body").getAsString());
    }

    @Test
    void rejectsUnsupportedVersion() {
        String json = """
                {"v":3,"id":"1","type":"ping","timestamp":"now","serverId":"survival","payload":{}}
                """;

        assertThrows(ProtocolException.class, () -> codec.decode(json));
    }
}
