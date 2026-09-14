package dev.serverchatbot.matrixbridge.protocol;

public record RelayInboundMessage(
        String bridgeMessageId,
        String sourceServerId,
        String sourceDisplayName,
        String authorId,
        String authorDisplayName,
        String body,
        String route
) {
}
