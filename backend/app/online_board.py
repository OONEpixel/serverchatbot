"""Light-themed, per-server online player ranking image."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .avatars import default_steve_head_png


@dataclass(frozen=True, slots=True)
class BoardPlayer:
    name: str
    total_seconds: int | None = None
    online: bool = False
    avatar_path: str | None = None


@dataclass(frozen=True, slots=True)
class BoardServer:
    server_id: str
    name: str
    status: str
    online_count: int = 0
    players: tuple[BoardPlayer, ...] = ()


FONT_PATHS = (
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
MAX_ROWS_PER_SERVER = 60


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_PATHS:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    minutes = max(0, seconds) // 60
    if minutes == 0:
        return "<1分钟"
    days, remainder = divmod(minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    if days:
        return f"{days}天{hours}小时" if hours else f"{days}天"
    if hours:
        return f"{hours}小时{minutes:02d}分"
    return f"{minutes}分钟"


def _ranked_players(server: BoardServer) -> list[BoardPlayer]:
    return sorted(
        server.players,
        key=lambda player: (
            player.total_seconds is None,
            -(player.total_seconds or 0),
            player.name.casefold(),
        ),
    )


def _load_avatar(player: BoardPlayer, fallback: Image.Image) -> Image.Image:
    if player.avatar_path:
        try:
            with Image.open(player.avatar_path) as source:
                return source.convert("RGBA").resize((44, 44), Image.Resampling.NEAREST)
        except (OSError, ValueError):
            pass
    return fallback


def render_online_board(servers: list[BoardServer]) -> tuple[bytes, int, int]:
    width = 900
    grouped = [(server, _ranked_players(server)) for server in servers]
    heights = [
        112 + (70 * min(len(players), MAX_ROWS_PER_SERVER) if players else 64)
        + (35 if len(players) > MAX_ROWS_PER_SERVER else 0)
        for _, players in grouped
    ]
    height = 290 if not servers else 150 + sum(value + 20 for value in heights) + 28
    image = Image.new("RGB", (width, height), "#f4f7fb")
    draw = ImageDraw.Draw(image)
    title, heading, body, small, rank_font = (
        _font(37), _font(26), _font(21), _font(17), _font(23)
    )
    draw.text((42, 31), "服务器在线榜", font=title, fill="#17243b")
    total = sum(server.online_count for server in servers if server.status == "online")
    draw.text((44, 86), "各服务器 · 累计游玩时长", font=small, fill="#718096")
    draw.text((718, 48), f"在线 {total} 人", font=body, fill="#16845a")
    if not servers:
        draw.rounded_rectangle((38, 140, 862, 250), radius=20, fill="#ffffff")
        draw.text((70, 176), "暂无已登记服务器", font=body, fill="#64748b")
    with Image.open(io.BytesIO(default_steve_head_png())) as default_image:
        fallback = default_image.convert("RGBA").resize(
            (44, 44), Image.Resampling.NEAREST
        )
    top = 140
    for (server, players), card_height in zip(grouped, heights):
        draw.rounded_rectangle(
            (38, top + 4, 862, top + card_height + 4), radius=20, fill="#e3e9f1"
        )
        draw.rounded_rectangle(
            (38, top, 862, top + card_height), radius=20, fill="#ffffff"
        )
        label = server.name[:28] + ("…" if len(server.name) > 28 else "")
        draw.text((64, top + 20), label, font=heading, fill="#17243b")
        draw.text((65, top + 59), server.server_id[:55], font=small, fill="#8190a6")
        status_text = (
            f"{server.online_count} 人在线" if server.status == "online" else
            "已禁用" if server.status == "disabled" else "未连接 / 超时"
        )
        status_color = "#15815a" if server.status == "online" else "#a85656"
        draw.text((704, top + 27), status_text, font=small, fill=status_color)
        draw.line((64, top + 95, 836, top + 95), fill="#e8edf4", width=2)
        if not players:
            empty_text = (
                "当前没有玩家在线" if server.status == "online" else "暂时无法读取在线玩家"
            )
            draw.text((68, top + 113), empty_text, font=body, fill="#8190a6")
        for index, player in enumerate(players[:MAX_ROWS_PER_SERVER], start=1):
            row_top = top + 106 + (index - 1) * 70
            if index > 1:
                draw.line((66, row_top, 834, row_top), fill="#f0f3f8", width=1)
            draw.text((68, row_top + 19), f"#{index}", font=rank_font, fill="#4d607c")
            avatar = _load_avatar(player, fallback)
            image.paste(avatar, (143, row_top + 12), avatar)
            draw.text((207, row_top + 18), player.name[:20], font=body, fill="#1f2e45")
            draw.text((541, row_top + 20), format_duration(player.total_seconds),
                      font=small, fill="#50627d")
            badge_fill = "#e7f8ef" if player.online else "#eef1f5"
            badge_text = "在线" if player.online else "离线"
            badge_color = "#16845a" if player.online else "#718096"
            draw.rounded_rectangle((745, row_top + 16, 819, row_top + 49),
                                   radius=15, fill=badge_fill)
            draw.text((758, row_top + 20), badge_text, font=small, fill=badge_color)
        if len(players) > MAX_ROWS_PER_SERVER:
            draw.text((68, top + card_height - 31),
                      f"还有 {len(players) - MAX_ROWS_PER_SERVER} 位玩家未显示",
                      font=small, fill="#8190a6")
        top += card_height + 20
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue(), width, height
