package dev.serverchatbot.matrixbridge;

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.event.ClickEvent;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabCompleter;
import org.jetbrains.annotations.NotNull;

import java.util.List;
import java.util.Locale;

public final class BridgeCommand implements CommandExecutor, TabCompleter {
    private static final List<String> SUBCOMMANDS = List.of("status", "reconnect", "reload", "token");

    private final MatrixBridgePlugin plugin;

    public BridgeCommand(MatrixBridgePlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(
            @NotNull CommandSender sender,
            @NotNull Command command,
            @NotNull String label,
            @NotNull String[] args
    ) {
        String subcommand = args.length == 0 ? "status" : args[0].toLowerCase(Locale.ROOT);
        switch (subcommand) {
            case "status" -> sender.sendMessage(Component.text(plugin.connectionSummary(), NamedTextColor.AQUA));
            case "reconnect" -> {
                if (plugin.reconnect()) {
                    sender.sendMessage(Component.text("已请求重新连接。", NamedTextColor.GREEN));
                } else {
                    sender.sendMessage(Component.text("桥接当前未启用或配置无效。", NamedTextColor.RED));
                }
            }
            case "reload" -> {
                try {
                    plugin.reloadBridge();
                    sender.sendMessage(Component.text("配置已重载。", NamedTextColor.GREEN));
                } catch (IllegalArgumentException exception) {
                    sender.sendMessage(Component.text("配置重载失败: " + exception.getMessage(), NamedTextColor.RED));
                }
            }
            case "token" -> {
                if (args.length != 2 || !args[1].equalsIgnoreCase("generate")) {
                    sender.sendMessage(Component.text(
                            "用法: /" + label + " token generate", NamedTextColor.YELLOW));
                    return true;
                }
                String token = plugin.generateAndInstallToken();
                sender.sendMessage(Component.text(
                        "已生成新的服务器独立 token，并写入 config.yml。请点击下一行复制：",
                        NamedTextColor.GREEN));
                sender.sendMessage(Component.text(token, NamedTextColor.AQUA)
                        .clickEvent(ClickEvent.copyToClipboard(token)));
                sender.sendMessage(Component.text(
                        "请立即在机器人后台使用隐藏输入完成登记；登记前连接会认证失败。",
                        NamedTextColor.YELLOW));
            }
            default -> {
                sender.sendMessage(Component.text("用法: /" + label
                                + " <status|reconnect|reload|token generate>",
                        NamedTextColor.YELLOW));
                return true;
            }
        }
        return true;
    }

    @Override
    public List<String> onTabComplete(
            @NotNull CommandSender sender,
            @NotNull Command command,
            @NotNull String alias,
            @NotNull String[] args
    ) {
        if (args.length != 1) {
            if (args.length == 2 && args[0].equalsIgnoreCase("token")
                    && "generate".startsWith(args[1].toLowerCase(Locale.ROOT))) {
                return List.of("generate");
            }
            return List.of();
        }
        String prefix = args[0].toLowerCase(Locale.ROOT);
        return SUBCOMMANDS.stream().filter(value -> value.startsWith(prefix)).toList();
    }
}
