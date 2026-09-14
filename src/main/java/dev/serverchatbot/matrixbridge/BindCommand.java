package dev.serverchatbot.matrixbridge;

import com.google.gson.JsonObject;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.jetbrains.annotations.NotNull;

import java.util.regex.Pattern;

public final class BindCommand implements CommandExecutor {
    private static final Pattern TOKEN_PATTERN = Pattern.compile("[A-Za-z0-9]{6}");

    private final MatrixBridgePlugin plugin;

    public BindCommand(MatrixBridgePlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(
            @NotNull CommandSender sender,
            @NotNull Command command,
            @NotNull String label,
            @NotNull String[] args
    ) {
        if (!(sender instanceof Player player)) {
            sender.sendMessage(Component.text("该命令只能由游戏内玩家执行。", NamedTextColor.RED));
            return true;
        }
        if (args.length != 1 || !TOKEN_PATTERN.matcher(args[0]).matches()) {
            sender.sendMessage(Component.text("用法: /" + label + " <6位绑定验证码>", NamedTextColor.YELLOW));
            return true;
        }

        JsonObject payload = new JsonObject();
        payload.addProperty("token", args[0]);
        // bot.py currently stores this value as the Matrix user's in-game
        // display identity, so it must be the player name rather than the UUID.
        payload.addProperty("playerId", player.getName());
        payload.addProperty("playerUuid", player.getUniqueId().toString());

        if (!plugin.sendControlMessage("bind_confirm", payload)) {
            sender.sendMessage(Component.text("机器人后台当前未连接，请稍后重试。", NamedTextColor.RED));
            return true;
        }

        sender.sendMessage(Component.text("绑定验证码已提交，正在等待机器人后台确认。", NamedTextColor.YELLOW));
        return true;
    }
}
