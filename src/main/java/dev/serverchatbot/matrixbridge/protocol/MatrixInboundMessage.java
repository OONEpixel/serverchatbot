package dev.serverchatbot.matrixbridge.protocol;

public record MatrixInboundMessage(
        String bridgeMessageId,
        String roomId,
        String matrixEventId,
        String sender,
        String senderDisplayName,
        String body
) {
}
