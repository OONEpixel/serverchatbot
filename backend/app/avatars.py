from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp
from nio import AsyncClient, UploadResponse
from PIL import Image, ImageDraw

from .database import Database


LOGGER = logging.getLogger("matrix_bridge.avatars")
PLAYER_ID_PATTERN = re.compile(r"[A-Za-z0-9_]{1,16}")
TEXTURE_PATH_PATTERN = re.compile(r"/texture/([0-9a-fA-F]{32,128})")
BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_SKIN_BYTES = 1_048_576
AVATAR_SIZE = 32


class AvatarManager:
    def __init__(
        self,
        database: Database,
        matrix_client: AsyncClient,
        avatar_directory: Path,
        lookup_timeout_seconds: int,
    ) -> None:
        self.database = database
        self.matrix_client = matrix_client
        self.avatar_directory = avatar_directory
        self.lookup_timeout_seconds = lookup_timeout_seconds
        self.default_mxc_uri = database.get_setting("avatar_default_mxc")
        self._session: aiohttp.ClientSession | None = None
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._weekly_task: asyncio.Task[None] | None = None
        self._default_upload_task: asyncio.Task[None] | None = None
        self._refresh_semaphore = asyncio.Semaphore(4)

    async def start(self) -> None:
        self.avatar_directory.mkdir(parents=True, exist_ok=True)
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.lookup_timeout_seconds),
            headers={"User-Agent": "MatrixChatBridge/0.4.9"},
        )
        await self._ensure_default_avatar()
        self._weekly_task = asyncio.create_task(
            self._weekly_refresh_loop(), name="weekly-player-avatar-refresh"
        )

    async def close(self) -> None:
        if self._weekly_task is not None:
            self._weekly_task.cancel()
        if self._default_upload_task is not None:
            self._default_upload_task.cancel()
        for task in self._refresh_tasks.values():
            task.cancel()
        tasks = [
            task
            for task in [
                self._weekly_task,
                self._default_upload_task,
                *self._refresh_tasks.values(),
            ]
            if task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_tasks.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None

    def avatar_for(self, player_id: str) -> str | None:
        if not self.default_mxc_uri and (
            self._default_upload_task is None or self._default_upload_task.done()
        ):
            self._default_upload_task = asyncio.create_task(
                self._ensure_default_avatar(), name="default-player-avatar-upload"
            )
        record = self.database.get_player_avatar(player_id)
        if record is None or record.refresh_failed or not record.mxc_uri:
            self.schedule_refresh(player_id)
        return record.mxc_uri if record and record.mxc_uri else self.default_mxc_uri

    def schedule_refresh(self, player_id: str) -> None:
        normalized = player_id.casefold()
        current = self._refresh_tasks.get(normalized)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._refresh_player(player_id),
            name=f"player-avatar-{normalized}",
        )
        self._refresh_tasks[normalized] = task
        task.add_done_callback(
            lambda completed, key=normalized: self._forget_refresh_task(key, completed)
        )

    async def _ensure_default_avatar(self) -> None:
        path = self.avatar_directory / "default-steve.png"
        if not path.is_file():
            png = await asyncio.to_thread(default_steve_head_png)
            await asyncio.to_thread(path.write_bytes, png)
        else:
            png = await asyncio.to_thread(path.read_bytes)
        if self.default_mxc_uri:
            return
        try:
            self.default_mxc_uri = await self._upload_png(png, path.name)
        except Exception:
            LOGGER.exception("上传默认 Steve 头像失败；稍后玩家事件会继续尝试")
            return
        self.database.set_setting("avatar_default_mxc", self.default_mxc_uri)
        LOGGER.info("默认 Steve 头像已上传到 Matrix 媒体库")

    def _forget_refresh_task(
        self, key: str, completed: asyncio.Task[None]
    ) -> None:
        if self._refresh_tasks.get(key) is completed:
            self._refresh_tasks.pop(key, None)

    async def _refresh_player(self, player_id: str) -> None:
        if self._session is None:
            return
        async with self._refresh_semaphore:
            try:
                mojang_uuid, texture_hash, skin_png = await self._fetch_skin(player_id)
                avatar_png = await asyncio.to_thread(render_head_png, skin_png)
                path = self.avatar_directory / f"{texture_hash.casefold()}.png"
                await asyncio.to_thread(_atomic_write, path, avatar_png)

                previous = self.database.get_player_avatar(player_id)
                if (
                    previous is not None
                    and previous.texture_hash == texture_hash
                    and previous.mxc_uri
                ):
                    mxc_uri = previous.mxc_uri
                else:
                    mxc_uri = await self._upload_png(
                        avatar_png, f"minecraft-{player_id}.png"
                    )
                self.database.save_player_avatar(
                    player_id,
                    mojang_uuid,
                    texture_hash,
                    str(path),
                    mxc_uri,
                )
                LOGGER.info("玩家 %s 的头像缓存已更新", player_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.database.mark_player_avatar_failure(player_id)
                LOGGER.warning("获取玩家 %s 的 Mojang 头像失败：%s", player_id, error)

    async def _fetch_skin(self, player_id: str) -> tuple[str, str, bytes]:
        if self._session is None:
            raise RuntimeError("avatar HTTP session is not started")
        if not PLAYER_ID_PATTERN.fullmatch(player_id):
            raise ValueError("玩家 ID 不是可查询的正版用户名格式")

        profile_url = (
            "https://api.mojang.com/users/profiles/minecraft/"
            + quote(player_id, safe="")
        )
        profile = await self._get_json(profile_url)
        mojang_uuid = str(profile.get("id", ""))
        if not re.fullmatch(r"[0-9a-fA-F]{32}", mojang_uuid):
            raise ValueError("Mojang 没有返回有效 UUID")

        session_url = (
            "https://sessionserver.mojang.com/session/minecraft/profile/"
            f"{mojang_uuid}?unsigned=true"
        )
        session_profile = await self._get_json(session_url)
        properties = session_profile.get("properties", [])
        encoded_texture = next(
            (
                item.get("value")
                for item in properties
                if isinstance(item, dict)
                and item.get("name") == "textures"
                and isinstance(item.get("value"), str)
            ),
            None,
        )
        if not encoded_texture:
            raise ValueError("Mojang profile 没有皮肤纹理")
        try:
            texture_data = json.loads(base64.b64decode(encoded_texture))
            skin_url = texture_data["textures"]["SKIN"]["url"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("Mojang 皮肤纹理数据无效") from error

        parsed = urlparse(str(skin_url))
        match = TEXTURE_PATH_PATTERN.fullmatch(parsed.path)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname != "textures.minecraft.net"
            or match is None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Mojang 返回了不受信任的皮肤地址")
        texture_hash = match.group(1).casefold()
        trusted_skin_url = f"https://textures.minecraft.net{parsed.path}"
        skin_png = await self._get_bytes(trusted_skin_url)
        return mojang_uuid.casefold(), texture_hash, skin_png

    async def _get_json(self, url: str) -> dict[str, object]:
        if self._session is None:
            raise RuntimeError("avatar HTTP session is not started")
        async with self._session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            if response.content_length and response.content_length > 131_072:
                raise ValueError("Mojang JSON 响应过大")
            data = await response.json(content_type=None)
            if not isinstance(data, dict):
                raise ValueError("Mojang JSON 响应格式无效")
            return data

    async def _get_bytes(self, url: str) -> bytes:
        if self._session is None:
            raise RuntimeError("avatar HTTP session is not started")
        async with self._session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError(f"皮肤下载 HTTP {response.status}")
            if response.content_length and response.content_length > MAX_SKIN_BYTES:
                raise ValueError("皮肤文件过大")
            data = await response.content.read(MAX_SKIN_BYTES + 1)
            if len(data) > MAX_SKIN_BYTES:
                raise ValueError("皮肤文件过大")
            return data

    async def _upload_png(self, png: bytes, filename: str) -> str:
        response, _ = await asyncio.wait_for(
            self.matrix_client.upload(
                io.BytesIO(png),
                content_type="image/png",
                filename=filename,
                filesize=len(png),
            ),
            timeout=self.lookup_timeout_seconds,
        )
        if not isinstance(response, UploadResponse):
            raise RuntimeError(f"Matrix 头像上传失败: {response}")
        return response.content_uri

    async def _weekly_refresh_loop(self) -> None:
        try:
            while True:
                delay = seconds_until_next_sunday_midnight()
                LOGGER.info("下一次玩家头像库刷新将在 %.0f 秒后开始", delay)
                await asyncio.sleep(delay)
                await self._refresh_all_cached_players()
        except asyncio.CancelledError:
            raise

    async def _refresh_all_cached_players(self) -> None:
        records = self.database.all_player_avatars()
        LOGGER.info("开始每周玩家头像库刷新，共 %d 名玩家", len(records))
        for record in records:
            await self._refresh_player(record.player_id)
        self.database.set_setting(
            "avatar_last_weekly_refresh",
            datetime.now(BEIJING).isoformat(),
        )
        LOGGER.info("每周玩家头像库刷新完成")


def seconds_until_next_sunday_midnight(now: datetime | None = None) -> float:
    current = now.astimezone(BEIJING) if now else datetime.now(BEIJING)
    days_ahead = (6 - current.weekday()) % 7
    target_date = current.date() + timedelta(days=days_ahead)
    target = datetime.combine(target_date, datetime_time.min, tzinfo=BEIJING)
    if target <= current:
        target += timedelta(days=7)
    return max(1.0, (target - current).total_seconds())


def render_head_png(skin_png: bytes) -> bytes:
    with Image.open(io.BytesIO(skin_png)) as source:
        if source.width < 48 or source.height < 16:
            raise ValueError("皮肤图片尺寸无效")
        if source.width > 1024 or source.height > 1024:
            raise ValueError("皮肤图片尺寸过大")
        source.load()
        rgba = source.convert("RGBA")
        face = rgba.crop((8, 8, 16, 16))
        outer = rgba.crop((40, 8, 48, 16))
        face.alpha_composite(outer)
        head = face.resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.NEAREST)
        output = io.BytesIO()
        head.save(output, format="PNG", optimize=True)
        return output.getvalue()


def default_steve_head_png() -> bytes:
    image = Image.new("RGBA", (8, 8), (176, 128, 96, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 7, 1), fill=(62, 39, 28, 255))
    draw.point((0, 2), fill=(62, 39, 28, 255))
    draw.point((7, 2), fill=(62, 39, 28, 255))
    draw.rectangle((1, 3, 2, 3), fill=(255, 255, 255, 255))
    draw.rectangle((5, 3, 6, 3), fill=(255, 255, 255, 255))
    draw.point((2, 3), fill=(55, 78, 173, 255))
    draw.point((5, 3), fill=(55, 78, 173, 255))
    draw.rectangle((3, 4, 4, 5), fill=(139, 91, 70, 255))
    draw.rectangle((2, 6, 5, 6), fill=(77, 45, 35, 255))
    image = image.resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.NEAREST)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
