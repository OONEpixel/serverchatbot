from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
import re
import secrets
import shlex
import string
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from nio import (
    AsyncClient,
    LoginResponse,
    MatrixRoom,
    RoomMessageAudio,
    RoomMessageFile,
    RoomMessageImage,
    RoomMessageText,
    RoomMessageVideo,
    RoomSendResponse,
    StickerEvent,
    SyncResponse,
    UploadResponse,
    WhoamiResponse,
)
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .avatars import AvatarManager
from .config import Settings
from .database import Binding, Database, Server
from .games import GameManager
from .online_board import BoardPlayer, BoardServer, render_online_board
from .mentions import (
    MentionTarget,
    avatar_message_content,
    bind_token_notice_content,
    game_chat_content,
    multiline_text_content,
    replace_matrix_mentions,
    user_notice_content,
)
from .protocol import (
    ProtocolError,
    decode,
    encode,
    extract_plain_text,
    extract_player_name,
    make_envelope,
)


LOGGER = logging.getLogger("matrix_bridge")
SERVER_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MATRIX_ID_PATTERN = re.compile(r"@[^\s:]+:\S+")
TOKEN_ALPHABET = string.ascii_letters + string.digits
PLAYER_EVENT_TYPES = {
    "player_join",
    "player_quit",
    "player_chat",
    "player_death",
    "player_advancement",
}
MATRIX_COMMAND_PREFIXES = frozenset({"!", "#"})
PING_QUERY_TIMEOUT_SECONDS = 3.0


def matrix_command(text: str) -> str | None:
    if len(text) < 2 or text[0] not in MATRIX_COMMAND_PREFIXES:
        return None
    command = text[1:].split(maxsplit=1)[0].casefold()
    return command if command in {
        "bind", "list", "help", "server", "admin", "avatar", "reconnect", "ping",
        "game", "online", "在线榜",
    } else None


@dataclass(frozen=True, slots=True)
class BindToken:
    matrix_id: str
    expires_at: float
    thread_root_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class ThreadTarget:
    root_event_id: str
    reply_to_event_id: str


@dataclass(slots=True)
class ReconnectActivity:
    joined_at: float | None = None
    qualifying_quits: list[float] = field(default_factory=list)
    rapid_quits: list[float] = field(default_factory=list)


@dataclass(slots=True)
class ServerHealthState:
    connected: bool = False
    outage_started_at: float | None = None
    offline_notice_sent: bool = False
    generation: int = 0
    pending_task: asyncio.Task[None] | None = None
    last_notice_at: float = 0.0
    planned_shutdown: bool = False


@dataclass(slots=True)
class PendingOnlineBoard:
    expected: set[str]
    results: dict[str, tuple[int, tuple[BoardPlayer, ...]]] = field(default_factory=dict)
    complete: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, slots=True)
class PingSample:
    round_trip_ms: int
    online_count: int
    players: tuple[tuple[str, int | None], ...]
    tps_1m: float | None
    mspt: float | None


class MatrixBridgeService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database = Database(settings.database_path)
        self.matrix_client = AsyncClient(settings.matrix_url, settings.matrix_id)
        self.servers: dict[str, ServerConnection] = {}
        self.connection_token_hashes: dict[str, str] = {}
        self.bind_tokens: dict[str, BindToken] = {}
        self.reminder_times: dict[str, float] = {}
        self.online_query_threads: dict[str, tuple[ThreadTarget, float]] = {}
        self.ping_queries: dict[str, tuple[str, float, asyncio.Future[PingSample]]] = {}
        self.expired_ping_queries: dict[str, float] = {}
        self.board_queries: dict[str, tuple[PendingOnlineBoard, str]] = {}
        self.expired_board_queries: dict[str, float] = {}
        self.board_last_request: dict[str, float] = {}
        self.games = GameManager()
        self.command_thread: ContextVar[ThreadTarget | None] = ContextVar(
            f"matrix_command_thread_{id(self)}", default=None
        )
        self.avatar_manager = AvatarManager(
            self.database,
            self.matrix_client,
            settings.avatar_directory,
            settings.avatar_lookup_timeout_seconds,
        )
        saved_avatar_mode = self.database.get_setting("avatar_display_mode")
        self.avatar_display_mode = (
            saved_avatar_mode
            if saved_avatar_mode in {"text", "emote"}
            else settings.avatar_display_mode
        )
        self.avatar_mode_lock = asyncio.Lock()
        self.reconnect_activity: dict[tuple[str, str], ReconnectActivity] = {}
        self.reconnect_guard_task: asyncio.Task[None] | None = None
        self.server_health: dict[str, ServerHealthState] = {}
        self.shutting_down = False

    async def run(self) -> None:
        try:
            await self._authenticate_matrix()
            if self._avatar_emotes_enabled():
                await self.avatar_manager.start()
            if self.settings.reconnect_guard_enabled:
                self.database.expire_reconnect_mutes(
                    self.settings.reconnect_guard_quiet_seconds
                )
                self.reconnect_guard_task = asyncio.create_task(
                    self._reconnect_guard_cleanup_loop(),
                    name="reconnect-guard-cleanup",
                )
            sync_token = await self._prepare_sync()
            self._register_callbacks()
            LOGGER.info(
                "启动 WebSocket 服务端 %s:%d/minecraft",
                self.settings.ws_host,
                self.settings.ws_port,
            )
            async with serve(
                self._websocket_handler,
                self.settings.ws_host,
                self.settings.ws_port,
                max_size=65_536,
                max_queue=32,
                ping_interval=20,
                ping_timeout=20,
            ):
                await self.matrix_client.sync_forever(
                    timeout=30_000,
                    since=sync_token,
                    full_state=True,
                    set_presence="online",
                )
        finally:
            self.shutting_down = True
            for state in self.server_health.values():
                if state.pending_task is not None:
                    state.pending_task.cancel()
            await asyncio.gather(
                *(
                    state.pending_task
                    for state in self.server_health.values()
                    if state.pending_task is not None
                ),
                return_exceptions=True,
            )
            if self.reconnect_guard_task is not None:
                self.reconnect_guard_task.cancel()
                await asyncio.gather(
                    self.reconnect_guard_task, return_exceptions=True
                )
            await self.avatar_manager.close()
            await self.matrix_client.close()
            self.database.close()

    async def _authenticate_matrix(self) -> None:
        saved_token = self.database.get_setting("matrix_access_token")
        saved_device = self.database.get_setting("matrix_device_id")
        if saved_token:
            self.matrix_client.access_token = saved_token
            self.matrix_client.user_id = self.settings.matrix_id
            self.matrix_client.device_id = saved_device or ""
            whoami = await self.matrix_client.whoami()
            if isinstance(whoami, WhoamiResponse) and whoami.user_id == self.settings.matrix_id:
                LOGGER.info("已恢复 Matrix 登录会话，设备 ID: %s", saved_device or "unknown")
                return
            LOGGER.warning("保存的 Matrix access token 已失效，将重新登录")
            self.database.delete_setting("matrix_access_token")
            self.database.delete_setting("matrix_device_id")
            self.matrix_client.access_token = ""

        if not self.settings.matrix_password:
            raise RuntimeError("MATRIX_PASSWORD 未配置，且没有可恢复的登录会话")
        response = await self.matrix_client.login(
            self.settings.matrix_password,
            device_name="Minecraft Matrix Bridge",
        )
        if not isinstance(response, LoginResponse):
            raise RuntimeError(f"Matrix 登录失败: {response}")
        self.database.set_setting("matrix_access_token", response.access_token)
        self.database.set_setting("matrix_device_id", response.device_id)
        LOGGER.info("Matrix 登录成功，设备 ID: %s", response.device_id)

    async def _prepare_sync(self) -> str | None:
        saved_token = self.database.get_setting("matrix_sync_token")
        if saved_token:
            return saved_token

        # Bootstrap room state without replaying old timeline messages into the
        # game when this service is started for the first time.
        response = await self.matrix_client.sync(timeout=0, full_state=True)
        if not isinstance(response, SyncResponse):
            raise RuntimeError(f"Matrix 初始同步失败: {response}")
        self.database.set_setting("matrix_sync_token", response.next_batch)
        LOGGER.info("首次同步完成；已有历史消息不会被重新转发")
        return response.next_batch

    def _register_callbacks(self) -> None:
        self.matrix_client.add_event_callback(self._on_matrix_text, RoomMessageText)
        self.matrix_client.add_event_callback(self._on_matrix_media, RoomMessageImage)
        self.matrix_client.add_event_callback(self._on_matrix_media, RoomMessageVideo)
        self.matrix_client.add_event_callback(self._on_matrix_media, RoomMessageAudio)
        self.matrix_client.add_event_callback(self._on_matrix_media, RoomMessageFile)
        self.matrix_client.add_event_callback(self._on_matrix_media, StickerEvent)
        self.matrix_client.add_response_callback(self._on_sync_response, SyncResponse)

    async def _on_sync_response(self, response: SyncResponse) -> None:
        self.database.set_setting("matrix_sync_token", response.next_batch)

    async def _on_matrix_text(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if not self._accept_matrix_event(room, event):
            return
        thread_token: Token[ThreadTarget | None] | None = None
        try:
            text = event.body.strip()
            if not text:
                return

            command = matrix_command(text)
            if command is not None:
                thread_token = self.command_thread.set(
                    self._thread_target_for_command(event)
                )
            if command == "server":
                await self._handle_server_command(room, event, text)
            elif command == "admin":
                await self._handle_admin_command(room, event, text)
            elif command == "avatar":
                await self._handle_avatar_command(room, event, text)
            elif command == "reconnect":
                await self._handle_reconnect_command(room, event, text)
            elif command == "ping":
                await self._send_ping_status(event.sender)
            elif command == "game":
                binding = self.database.get_binding_by_matrix(event.sender)
                actor_name = (
                    binding.player_id if binding is not None else
                    room.user_name(event.sender)
                    or event.sender.split(":", 1)[0].lstrip("@")
                )
                await self._handle_game_command(
                    self.settings.matrix_relay_group, f"matrix:{event.sender}",
                    actor_name, text,
                    event.sender,
                )
            elif command in {"online", "在线榜"}:
                await self._request_online_board(event.sender)
            elif command == "help":
                await self._send_command_help(room, event.sender)
            elif command == "bind":
                await self._create_bind_token(event.sender)
            else:
                await self._maybe_remind_unbound(event.sender)
                if command == "list":
                    await self._request_online_players(event.sender)
                elif len(text) > self.settings.max_matrix_message_length:
                    await self._send_user_notice(
                        event.sender,
                        f"消息超过 {self.settings.max_matrix_message_length} 个字符，未发送到服务器。",
                    )
                else:
                    LOGGER.info("[Matrix -> MC] %s: %s", event.sender, text)
                    await self._push_to_minecraft(room.room_id, event, text)
        except Exception:
            LOGGER.exception("处理 Matrix 文本事件失败: %s", event.event_id)
            return
        finally:
            if thread_token is not None:
                self.command_thread.reset(thread_token)
        self.database.mark_processed("matrix", event.event_id)

    async def _on_matrix_media(self, room: MatrixRoom, event: Any) -> None:
        if not self._accept_matrix_event(room, event):
            return
        media_names = {
            RoomMessageImage: "[图片]",
            RoomMessageVideo: "[视频]",
            RoomMessageAudio: "[语音]",
            RoomMessageFile: "[文件]",
            StickerEvent: "[贴纸]",
        }
        body = next(
            (name for event_type, name in media_names.items() if isinstance(event, event_type)),
            "[媒体]",
        )
        try:
            await self._maybe_remind_unbound(event.sender)
            LOGGER.info("[Matrix -> MC] %s 发送了 %s", event.sender, body)
            await self._push_to_minecraft(room.room_id, event, body)
        except Exception:
            LOGGER.exception("处理 Matrix 媒体事件失败: %s", event.event_id)
            return
        self.database.mark_processed("matrix", event.event_id)

    def _accept_matrix_event(self, room: MatrixRoom, event: Any) -> bool:
        return (
            room.room_id == self.settings.room_id
            and event.sender != self.settings.matrix_id
            and bool(getattr(event, "event_id", ""))
            and not self.database.is_processed("matrix", event.event_id)
        )

    async def _create_bind_token(self, matrix_id: str) -> None:
        self._remove_expired_bind_tokens()
        token = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(6))
        self.bind_tokens[token] = BindToken(
            matrix_id=matrix_id,
            expires_at=time.time() + self.settings.bind_token_ttl_seconds,
            thread_root_event_id=(
                self.command_thread.get().root_event_id
                if self.command_thread.get() is not None
                else None
            ),
        )
        current = self.database.get_binding_by_matrix(matrix_id)
        await self._send_room_content(
            bind_token_notice_content(
                matrix_id,
                token,
                self.settings.bind_token_ttl_seconds,
                current.player_id if current else None,
            )
        )

    async def _maybe_remind_unbound(self, matrix_id: str) -> None:
        if self.database.get_binding_by_matrix(matrix_id) is not None:
            return
        now = time.monotonic()
        last_time = self.reminder_times.get(matrix_id, 0.0)
        if now - last_time < self.settings.unbound_reminder_cooldown_seconds:
            return
        self.reminder_times[matrix_id] = now
        await self._send_user_notice(
            matrix_id,
            "你还没有绑定 Minecraft 玩家 ID。请发送 !bind 或 #bind 获取验证码，再到游戏中输入 /matrixbind <验证码>。",
            use_command_thread=False,
        )

    async def _request_online_players(self, requester: str) -> None:
        targets = self._connected_servers_in_group(self.settings.matrix_relay_group)
        if not targets:
            await self._send_user_notice(requester, "目前没有 Minecraft 服务器连接到总线。")
            return
        now = time.monotonic()
        self.online_query_threads = {
            request_id: item
            for request_id, item in self.online_query_threads.items()
            if item[1] > now
        }
        thread_target = self.command_thread.get()
        for server_id, websocket in list(targets.items()):
            request_id = str(uuid.uuid4())
            if thread_target is not None:
                self.online_query_threads[request_id] = (
                    thread_target,
                    now + 60,
                )
            await self._send_to_server(
                server_id,
                websocket,
                "query_list_request",
                {"requester": requester},
                message_id=request_id,
            )
        LOGGER.info("已向 %d 台服务器下发在线玩家查询", len(targets))

    async def _request_online_board(self, requester: str) -> None:
        now = time.monotonic()
        self.expired_board_queries = {
            request_id: expiry for request_id, expiry in self.expired_board_queries.items()
            if expiry > now
        }
        if now - self.board_last_request.get(requester, -1e9) < 15:
            await self._send_user_notice(requester, "⏳ 在线榜每 15 秒可查询一次。")
            return
        self.board_last_request[requester] = now
        servers = self.database.all_servers()
        targets = {
            server.server_id: self.servers[server.server_id]
            for server in servers
            if server.enabled and server.server_id in self.servers
        }
        pending = PendingOnlineBoard(set(targets))
        try:
            for server_id, websocket in targets.items():
                request_id = str(uuid.uuid4())
                self.board_queries[request_id] = (pending, server_id)
                await self._send_to_server(
                    server_id, websocket, "query_list_request",
                    {"requester": requester, "includeLeaderboard": True},
                    message_id=request_id,
                )
            if targets:
                try:
                    await asyncio.wait_for(pending.complete.wait(), timeout=5)
                except TimeoutError:
                    LOGGER.info("在线榜查询超时，已收到 %d/%d 台回复", len(pending.results), len(targets))
            entries = [
                BoardServer(
                    server.server_id, server.display_name,
                    "disabled" if not server.enabled else (
                        "online" if server.server_id in pending.results else "offline"
                    ),
                    *(pending.results.get(server.server_id, (0, ()))),
                )
                for server in servers
            ]
            png, width, height = await asyncio.to_thread(render_online_board, entries)
            response, _ = await asyncio.wait_for(
                self.matrix_client.upload(
                    io.BytesIO(png), content_type="image/png",
                    filename="server-online-board.png", filesize=len(png),
                ),
                timeout=20,
            )
            if not isinstance(response, UploadResponse):
                raise RuntimeError(f"上传在线榜失败: {response}")
            await self._send_room_content({
                "msgtype": "m.image", "body": "服务器在线榜.png",
                "url": response.content_uri,
                "info": {"mimetype": "image/png", "size": len(png), "w": width, "h": height},
            })
        except Exception:
            self.board_last_request.pop(requester, None)
            LOGGER.exception("生成或发送在线榜失败")
            await self._send_user_notice(requester, "⚠️ 在线榜暂时生成失败，请稍后重试。")
        finally:
            for request_id, item in self.board_queries.items():
                if item[0] is pending:
                    self.expired_board_queries[request_id] = time.monotonic() + 120
            self.board_queries = {
                request_id: item for request_id, item in self.board_queries.items()
                if item[0] is not pending
            }

    async def _handle_game_command(
        self, group: str, actor_id: str, actor_name: str, text: str,
        requester: str | None = None, source_server: str | None = None,
    ) -> None:
        response, broadcast = self.games.play(group, actor_id, actor_name, text)
        if broadcast:
            if group == self.settings.matrix_relay_group:
                await self._send_room_content(
                    multiline_text_content(response), use_command_thread=False
                )
            event_id = str(uuid.uuid4())
            for server_id, websocket in self._connected_servers_in_group(group).items():
                await self._send_to_server(server_id, websocket, "matrix_message", {
                    "roomId": self.settings.room_id,
                    "eventId": event_id,
                    "sender": self.settings.matrix_id,
                    "senderDisplayName": "游戏助手",
                    "body": response,
                })
        elif requester is not None:
            await self._send_user_notice(requester, response)
        elif source_server is not None and source_server in self.servers:
            await self._send_to_server(
                source_server, self.servers[source_server], "matrix_message", {
                    "roomId": self.settings.room_id,
                    "eventId": str(uuid.uuid4()),
                    "sender": self.settings.matrix_id,
                    "senderDisplayName": "游戏助手",
                    "body": response,
                },
            )

    async def _push_to_minecraft(self, room_id: str, event: Any, body: str) -> None:
        mentioned_ids = self._matrix_mentioned_user_ids(event)
        targets = self._connected_servers_in_group(self.settings.matrix_relay_group)
        for server_id, websocket in targets.items():
            display_names = {
                matrix_id: self._matrix_display_for_server(matrix_id, server_id)
                for matrix_id in mentioned_ids
            }
            payload = {
                "roomId": room_id,
                "eventId": event.event_id,
                "sender": event.sender,
                "senderDisplayName": self._matrix_display_for_server(
                    event.sender, server_id
                ),
                "body": replace_matrix_mentions(body, mentioned_ids, display_names),
            }
            await self._send_to_server(
                server_id, websocket, "matrix_message", payload
            )

    async def _send_to_server(
        self,
        server_id: str,
        websocket: ServerConnection,
        message_type: str,
        payload: dict[str, Any],
        message_id: str | None = None,
    ) -> None:
        try:
            await websocket.send(
                encode(make_envelope(message_type, payload, server_id, message_id))
            )
        except ConnectionClosed:
            if self.servers.get(server_id) is websocket:
                self.servers.pop(server_id, None)
                self.connection_token_hashes.pop(server_id, None)
                self._mark_server_disconnected(server_id)
            LOGGER.warning("向 Minecraft 服务器 %s 发送消息失败：连接已关闭", server_id)

    async def _websocket_handler(self, websocket: ServerConnection) -> None:
        request = websocket.request
        auth_header = request.headers.get("Authorization", "")
        if request.path != "/minecraft":
            await websocket.close(code=4004, reason="Not Found")
            return
        server_id = request.headers.get("X-Minecraft-Server-Id", "")
        if not SERVER_ID_PATTERN.fullmatch(server_id):
            await websocket.close(code=4003, reason="Invalid server id")
            return
        server = self.database.get_server(server_id)
        provided_token = (
            auth_header.removeprefix("Bearer ")
            if auth_header.startswith("Bearer ")
            else ""
        )
        provided_hash = hashlib.sha256(provided_token.encode("utf-8")).hexdigest()
        if (
            server is None
            or not server.enabled
            or not secrets.compare_digest(provided_hash, server.token_hash)
        ):
            await websocket.close(code=4001, reason="Unauthorized")
            return

        previous = self.servers.get(server_id)
        self.servers[server_id] = websocket
        self.connection_token_hashes[server_id] = provided_hash
        if previous is not None and previous is not websocket:
            await previous.close(code=4002, reason="Replaced by a new connection")
        LOGGER.info("Minecraft 服务器 %s 已连接", server_id)
        self.database.touch_server(server_id)

        try:
            async for raw_message in websocket:
                if not isinstance(raw_message, str):
                    continue
                await self._process_minecraft_message(websocket, raw_message, server_id)
        except ConnectionClosed:
            pass
        finally:
            if self.servers.get(server_id) is websocket:
                self.servers.pop(server_id, None)
                self.connection_token_hashes.pop(server_id, None)
                self._mark_server_disconnected(server_id)
            LOGGER.info("Minecraft 服务器 %s 已断开", server_id)

    def _mark_server_connected(self, server_id: str) -> None:
        if not self.settings.server_health_enabled or self.shutting_down:
            return
        state = self.server_health.get(server_id)
        if state is None:
            self.server_health[server_id] = ServerHealthState(connected=True)
            return
        if state.connected:
            return
        state.connected = True
        state.generation += 1
        self._cancel_health_task(state)
        if state.offline_notice_sent:
            self._schedule_health_notice(
                server_id,
                expected_connected=True,
                immediate=state.planned_shutdown,
            )

    def _mark_server_disconnected(self, server_id: str) -> None:
        if not self.settings.server_health_enabled or self.shutting_down:
            return
        state = self.server_health.setdefault(
            server_id, ServerHealthState(connected=True)
        )
        if not state.connected:
            return
        state.connected = False
        state.outage_started_at = time.monotonic()
        state.generation += 1
        self._cancel_health_task(state)
        if not state.offline_notice_sent:
            self._schedule_health_notice(server_id, expected_connected=False)

    @staticmethod
    def _cancel_health_task(state: ServerHealthState) -> None:
        if state.pending_task is not None and not state.pending_task.done():
            state.pending_task.cancel()
        state.pending_task = None

    def _schedule_health_notice(
        self, server_id: str, expected_connected: bool, immediate: bool = False
    ) -> None:
        state = self.server_health[server_id]
        generation = state.generation
        task = asyncio.create_task(
            self._delayed_health_notice(
                server_id, expected_connected, generation, immediate
            ),
            name=f"server-health-{server_id}",
        )
        state.pending_task = task

    async def _delayed_health_notice(
        self,
        server_id: str,
        expected_connected: bool,
        generation: int,
        immediate: bool = False,
    ) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(
                0 if immediate else self.settings.server_health_grace_seconds
            )
            while not self.shutting_down:
                state = self.server_health.get(server_id)
                if (
                    state is None
                    or state.generation != generation
                    or state.connected != expected_connected
                ):
                    return
                quiet_window = self.settings.server_health_quiet_window
                if not immediate and quiet_window is not None:
                    quiet_remaining = quiet_window.remaining_seconds(server_id)
                    if quiet_remaining > 0:
                        # Recheck local time in case it changes while waiting.
                        await asyncio.sleep(min(quiet_remaining, 60))
                        continue
                remaining = 0 if immediate else (
                    self.settings.server_health_min_notice_interval_seconds
                    - (time.monotonic() - state.last_notice_at)
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                try:
                    await self._send_server_health_notice(
                        server_id, expected_connected, state
                    )
                except Exception:
                    LOGGER.exception(
                        "发送服务器 %s 连接状态通知失败，将稍后重试",
                        server_id,
                    )
                    await asyncio.sleep(30)
                    continue
                state.last_notice_at = time.monotonic()
                if expected_connected:
                    state.offline_notice_sent = False
                    state.outage_started_at = None
                    state.planned_shutdown = False
                else:
                    state.offline_notice_sent = True
                return
        finally:
            state = self.server_health.get(server_id)
            if state is not None and state.pending_task is current_task:
                state.pending_task = None

    async def _send_server_health_notice(
        self,
        server_id: str,
        connected: bool,
        state: ServerHealthState,
    ) -> None:
        server = self.database.get_server(server_id)
        if (
            server is None
            or not server.enabled
            or server.relay_group != self.settings.matrix_relay_group
        ):
            return
        if connected:
            outage_seconds = max(
                0,
                int(
                    time.monotonic()
                    - (state.outage_started_at or time.monotonic())
                ),
            )
            title = (
                "✅ Minecraft 服务器重启成功"
                if state.planned_shutdown
                else "✅ Minecraft 连接已恢复"
            )
            text = (
                f"{title}\n"
                f"服务器：{server.display_name} ({server.server_id})\n"
                f"中断时长：约 {self._format_duration(outage_seconds)}\n"
                "插件已重新连接机器人后台，消息转发恢复正常。"
            )
        else:
            text = (
                "⚠️ Minecraft 连接异常\n"
                f"服务器：{server.display_name} ({server.server_id})\n"
                f"插件与机器人后台已持续断开至少 {self._format_duration(self.settings.server_health_grace_seconds)}。\n"
                "可发送 #ping 查看实时连接状态。"
            )
        await self._send_room_content(
            multiline_text_content(text), use_command_thread=False
        )

    async def _handle_server_stopping(self, server_id: str) -> None:
        if not self.settings.server_health_enabled or self.shutting_down:
            return
        server = self.database.get_server(server_id)
        if (
            server is None
            or not server.enabled
            or server.relay_group != self.settings.matrix_relay_group
        ):
            return
        state = self.server_health.setdefault(
            server_id, ServerHealthState(connected=True)
        )
        if state.planned_shutdown:
            return
        state.planned_shutdown = True
        state.outage_started_at = time.monotonic()
        state.offline_notice_sent = True
        state.generation += 1
        self._cancel_health_task(state)
        state.last_notice_at = time.monotonic()
        await self._send_room_content(
            multiline_text_content(
                "🔄 Minecraft 服务器正在关闭或重启\n"
                f"服务器：{server.display_name} ({server.server_id})\n"
                "插件已发出正常停服信号，聊天转发将暂时停止。\n"
                "若服务器正在重启，重新连接成功后会再次通知。"
            ),
            use_command_thread=False,
        )

    async def _process_minecraft_message(
        self, websocket: ServerConnection, raw_message: str, server_id: str
    ) -> None:
        registered = self.database.get_server(server_id)
        if (
            registered is None
            or not registered.enabled
            or not secrets.compare_digest(
                self.connection_token_hashes.get(server_id, ""),
                registered.token_hash,
            )
        ):
            await websocket.close(code=4001, reason="Authorization changed")
            return
        try:
            data = decode(raw_message, server_id)
        except ProtocolError as error:
            LOGGER.warning("忽略来自 %s 的无效协议消息: %s", server_id, error)
            return
        if data["serverId"] != server_id:
            LOGGER.warning(
                "忽略连接 %s 冒充服务器 %s 的消息",
                server_id,
                data["serverId"],
            )
            return

        message_id = data["id"]
        message_type = data["type"]
        payload = data["payload"]

        if message_type == "hello":
            self._validate_hello(server_id, payload)
            self._mark_server_connected(server_id)
            return
        if message_type == "ping":
            await self._send_envelope(
                websocket, "pong", {"replyTo": message_id}, server_id
            )
            return
        if message_type in {"ack", "pong"}:
            return

        source = (
            "minecraft"
            if message_type in PLAYER_EVENT_TYPES
            else f"minecraft:{server_id}"
        )
        if self.database.is_processed(source, message_id):
            if message_type in PLAYER_EVENT_TYPES:
                await self._send_ack(websocket, message_id, server_id)
            return

        try:
            if message_type in PLAYER_EVENT_TYPES:
                server = self.database.get_server(server_id)
                if server is None or not server.enabled:
                    return
                await self._forward_player_event(server, message_type, payload, message_id)
                self.database.mark_processed(source, message_id)
                self.database.touch_server(server_id)
                await self._send_ack(websocket, message_id, server_id)
            elif message_type == "query_list_response":
                await self._send_online_player_response(server_id, payload, message_id)
                self.database.mark_processed(source, message_id)
            elif message_type == "bind_confirm":
                await self._confirm_binding(websocket, server_id, payload)
                self.database.mark_processed(source, message_id)
            elif message_type == "server_stopping":
                await self._handle_server_stopping(server_id)
                self.database.mark_processed(source, message_id)
            else:
                LOGGER.warning("忽略来自 %s 的未知消息类型: %s", server_id, message_type)
        except Exception:
            LOGGER.exception("处理来自 %s 的 %s 消息失败", server_id, message_type)

    async def _forward_player_event(
        self,
        server: Server,
        message_type: str,
        payload: dict[str, Any],
        message_id: str,
    ) -> None:
        player_name = extract_player_name(payload)
        plain_text = extract_plain_text(payload)
        if message_type == "player_chat" and matrix_command(plain_text.strip()) == "game":
            await self._handle_game_command(
                server.relay_group, f"minecraft:{server.server_id}:{player_name}",
                player_name, plain_text.strip(), source_server=server.server_id,
            )
            return
        if message_type in {"player_join", "player_quit"}:
            if await self._handle_reconnect_guard(
                server, message_type, player_name
            ):
                return
        avatar_mxc_uri = (
            self.avatar_manager.avatar_for(player_name)
            if self._avatar_emotes_enabled()
            else None
        )
        if message_type == "player_join":
            content = avatar_message_content(
                f"[{server.display_name}]{player_name}上线了",
                player_name,
                avatar_mxc_uri,
                self.avatar_display_mode,
            )
        elif message_type == "player_quit":
            content = avatar_message_content(
                f"[{server.display_name}]{player_name}下线了",
                player_name,
                avatar_mxc_uri,
                self.avatar_display_mode,
            )
        elif message_type == "player_chat":
            content = game_chat_content(
                server.display_name,
                player_name,
                plain_text,
                self._mention_targets(server.relay_group),
                avatar_mxc_uri,
                self.avatar_display_mode,
            )
        else:
            fallback = f"{player_name} 触发了 {message_type}"
            content = avatar_message_content(
                f"[{server.display_name}]{plain_text or fallback}",
                player_name,
                avatar_mxc_uri,
                self.avatar_display_mode,
            )
        # Reusing the Minecraft envelope ID as Matrix transaction ID makes a
        # retry idempotent at the homeserver as well as in our SQLite ledger.
        if server.relay_group == self.settings.matrix_relay_group:
            await self._send_room_content(content, transaction_id=message_id)
        if message_type == "player_chat":
            await self._relay_player_chat(
                server, payload, player_name, plain_text, message_id
            )

    def _avatar_emotes_enabled(self) -> bool:
        return (
            self.settings.avatar_enabled
            and self.avatar_display_mode == "emote"
        )

    async def _handle_reconnect_guard(
        self,
        server: Server,
        message_type: str,
        player_name: str,
        now: float | None = None,
    ) -> bool:
        if not self.settings.reconnect_guard_enabled:
            return False
        key = (server.server_id, player_name.casefold())
        if self.database.refresh_reconnect_mute(
            server.server_id,
            player_name,
            self.settings.reconnect_guard_quiet_seconds,
        ):
            return True

        current_time = time.monotonic() if now is None else now
        activity = self.reconnect_activity.setdefault(
            key, ReconnectActivity()
        )
        cutoff = current_time - self.settings.reconnect_guard_window_seconds
        activity.qualifying_quits = [
            item for item in activity.qualifying_quits if item >= cutoff
        ]
        rapid_cutoff = (
            current_time - self.settings.reconnect_guard_rapid_window_seconds
        )
        activity.rapid_quits = [
            item for item in activity.rapid_quits if item >= rapid_cutoff
        ]

        if message_type == "player_join":
            activity.joined_at = current_time
            return False
        if activity.joined_at is None:
            return False

        session_seconds = current_time - activity.joined_at
        activity.joined_at = None
        is_auth_timeout_session = (
            self.settings.reconnect_guard_min_session_seconds
            <= session_seconds
            <= self.settings.reconnect_guard_max_session_seconds
        )
        is_rapid_session = (
            0
            <= session_seconds
            <= self.settings.reconnect_guard_rapid_max_session_seconds
        )
        if not is_auth_timeout_session and not is_rapid_session:
            activity.qualifying_quits.clear()
            activity.rapid_quits.clear()
            return False

        if is_auth_timeout_session:
            activity.qualifying_quits.append(current_time)
        if is_rapid_session:
            activity.rapid_quits.append(current_time)
        auth_loop = (
            len(activity.qualifying_quits)
            >= self.settings.reconnect_guard_threshold
        )
        rapid_loop = (
            len(activity.rapid_quits)
            >= self.settings.reconnect_guard_rapid_threshold
        )
        if not auth_loop and not rapid_loop:
            return False

        newly_muted = self.database.mute_reconnect_player(
            server.server_id, player_name
        )
        self.reconnect_activity.pop(key, None)
        if newly_muted:
            await self._send_reconnect_warning(server, player_name)
        return True

    async def _send_reconnect_warning(
        self, server: Server, player_name: str
    ) -> None:
        if server.relay_group != self.settings.matrix_relay_group:
            return
        binding = self.database.get_binding_by_player(
            player_name, server.server_id
        )
        body = (
            "⚠️ 检测到疑似频繁断线或自动重连认证循环\n"
            f"服务器：{server.display_name} ({server.server_id})\n"
            f"玩家：{player_name}\n"
            "该玩家的上线/下线动态已暂时屏蔽，聊天、死亡和成就消息不受影响。\n"
        )
        quiet_text = self._format_duration(
            self.settings.reconnect_guard_quiet_seconds
        )
        if binding is not None:
            await self._send_user_notice(
                binding.matrix_id,
                body
                + "回到电脑并处理登录后，请在聊天室输入：#reconnect clear\n"
                + f"若连续 {quiet_text} 没有新的上下线事件，保护也会自动解除。",
                use_command_thread=False,
            )
            return
        await self._send_room_content(
            multiline_text_content(
                body
                + "该玩家尚未绑定 Matrix 账号，请管理员处理后输入："
                + f"#reconnect clear {server.server_id} {player_name}\n"
                + f"若连续 {quiet_text} 没有新的上下线事件，保护也会自动解除。"
            ),
            use_command_thread=False,
        )

    async def _reconnect_guard_cleanup_loop(self) -> None:
        interval = min(60, self.settings.reconnect_guard_quiet_seconds)
        while True:
            await asyncio.sleep(interval)
            expired = self.database.expire_reconnect_mutes(
                self.settings.reconnect_guard_quiet_seconds
            )
            if expired:
                LOGGER.info("自动解除 %d 项已静默的重连保护", expired)

    async def _send_online_player_response(
        self, server_id: str, payload: dict[str, Any], message_id: str
    ) -> None:
        raw_players = payload.get("playerList", [])
        players = [str(player) for player in raw_players] if isinstance(raw_players, list) else []
        online_count = int(payload.get("onlineCount", len(players)))
        if online_count == 0:
            server = self.database.get_server(server_id)
            label = server.display_name if server else server_id
            body = f"🎮 [{label}] 在线玩家\n• 当前无人在线"
        else:
            server = self.database.get_server(server_id)
            label = server.display_name if server else server_id
            body = f"🎮 [{label}] 在线玩家（{online_count} 人）\n" + "\n".join(
                f"• {player}" for player in players
            )
        request_id = str(payload.get("requestId", ""))
        ping_item = self.ping_queries.pop(request_id, None)
        if ping_item is not None:
            expected_server_id, sent_at, future = ping_item
            if expected_server_id == server_id and not future.done():
                reported_pings: dict[str, int] = {}
                raw_details = payload.get("playerDetails", [])
                if isinstance(raw_details, list):
                    for detail in raw_details:
                        if not isinstance(detail, dict):
                            continue
                        name = detail.get("name")
                        ping_ms = detail.get("pingMs")
                        if isinstance(name, str) and isinstance(ping_ms, int) and not isinstance(ping_ms, bool):
                            reported_pings[name.casefold()] = max(0, ping_ms)
                def metric(name: str) -> float | None:
                    value = payload.get(name)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        number = float(value)
                        if math.isfinite(number) and number >= 0:
                            return number
                    return None
                future.set_result(PingSample(
                    max(0, round((time.monotonic() - sent_at) * 1000)),
                    online_count,
                    tuple((name, reported_pings.get(name.casefold())) for name in players),
                    metric("tps1m"),
                    metric("mspt"),
                ))
            return
        if request_id in self.expired_ping_queries:
            self.expired_ping_queries.pop(request_id, None)
            return
        if request_id in self.expired_board_queries:
            self.expired_board_queries.pop(request_id, None)
            return
        board_item = self.board_queries.pop(request_id, None)
        if board_item is not None:
            pending, expected_server_id = board_item
            if expected_server_id == server_id:
                board_players = []
                raw_leaderboard = payload.get("leaderboardPlayers", [])
                if isinstance(raw_leaderboard, list):
                    for detail in raw_leaderboard:
                        if not isinstance(detail, dict):
                            continue
                        name = detail.get("name")
                        seconds = detail.get("totalPlaySeconds")
                        online = detail.get("online")
                        if (
                            not isinstance(name, str) or not name.strip()
                            or not isinstance(seconds, int) or isinstance(seconds, bool)
                        ):
                            continue
                        avatar = self.database.get_player_avatar(name)
                        board_players.append(BoardPlayer(
                            name.strip(), max(0, seconds), online is True,
                            avatar.local_path if avatar and avatar.local_path else None,
                        ))
                # Backward compatibility: old plugins can still produce a board,
                # but their current player list is not mislabeled as cumulative time.
                if not board_players:
                    for name in players:
                        avatar = self.database.get_player_avatar(name)
                        board_players.append(BoardPlayer(
                            name, None, True,
                            avatar.local_path if avatar and avatar.local_path else None,
                        ))
                pending.results[server_id] = (online_count, tuple(board_players))
                if pending.expected.issubset(pending.results):
                    pending.complete.set()
            return
        thread_item = self.online_query_threads.pop(request_id, None)
        thread_target = thread_item[0] if thread_item is not None else None
        await self._send_room_content(
            multiline_text_content(body),
            transaction_id=message_id,
            thread_root_event_id=(
                thread_target.root_event_id if thread_target is not None else None
            ),
            reply_to_event_id=(
                thread_target.reply_to_event_id if thread_target is not None else None
            ),
        )

    async def _confirm_binding(
        self,
        websocket: ServerConnection,
        server_id: str,
        payload: dict[str, Any],
    ) -> None:
        token = str(payload.get("token", ""))
        player_id = str(payload.get("playerId", "")).strip()
        player_uuid = str(payload.get("playerUuid", "")).strip() or None
        self._remove_expired_bind_tokens()
        bind_token = self.bind_tokens.get(token)
        if bind_token is None or not player_id:
            await self._send_bind_result(
                websocket,
                server_id,
                player_id,
                player_uuid,
                False,
                "绑定验证码无效或已经过期，请在 Matrix 房间重新发送 !bind 或 #bind。",
            )
            return

        self.database.save_binding(
            bind_token.matrix_id,
            server_id,
            player_id,
            player_uuid,
            self.settings.binding_mode,
        )
        self.bind_tokens.pop(token, None)
        LOGGER.info("账号绑定成功: %s -> %s", bind_token.matrix_id, player_id)
        await self._send_bind_result(
            websocket,
            server_id,
            player_id,
            player_uuid,
            True,
            f"Matrix 账号 {bind_token.matrix_id} 已成功绑定。",
        )
        await self._send_user_notice(
            bind_token.matrix_id,
            f"已成功绑定 Minecraft 玩家 {player_id}。",
            thread_root_event_id=bind_token.thread_root_event_id,
        )

    async def _relay_player_chat(
        self,
        source_server: Server,
        original_payload: dict[str, Any],
        player_name: str,
        body: str,
        message_id: str,
    ) -> None:
        targets = self._connected_servers_in_group(source_server.relay_group)
        targets.pop(source_server.server_id, None)
        if not targets:
            return
        relay_payload = {
            "source": {
                "kind": "minecraft",
                "serverId": source_server.server_id,
                "displayName": source_server.display_name,
            },
            "author": {
                "id": str(original_payload.get("playerUuid", "")),
                "displayName": player_name,
            },
            "body": body,
            "route": source_server.relay_group,
            "hop": 1,
        }
        for target_id, websocket in targets.items():
            await self._send_to_server(
                target_id,
                websocket,
                "relay_message",
                relay_payload,
                message_id=message_id,
            )

    def _connected_servers_in_group(
        self, relay_group: str
    ) -> dict[str, ServerConnection]:
        result: dict[str, ServerConnection] = {}
        for server_id, websocket in list(self.servers.items()):
            server = self.database.get_server(server_id)
            if server and server.enabled and server.relay_group == relay_group:
                result[server_id] = websocket
        return result

    def _mention_targets(self, relay_group: str) -> list[MentionTarget]:
        servers = {
            server.server_id: server
            for server in self.database.all_servers()
            if server.enabled and server.relay_group == relay_group
        }
        candidates: dict[str, dict[str, str]] = {}

        def add(reference: str, matrix_id: str) -> None:
            candidates.setdefault(reference.casefold(), {})[matrix_id] = reference

        for binding in self.database.all_bindings():
            if binding.scope_id == "*":
                add(binding.player_id, binding.matrix_id)
                for server_id in servers:
                    add(f"{server_id}:{binding.player_id}", binding.matrix_id)
            elif binding.server_id in servers:
                add(binding.player_id, binding.matrix_id)
                add(
                    f"{binding.server_id}:{binding.player_id}",
                    binding.matrix_id,
                )
        targets: list[MentionTarget] = []
        for by_matrix in candidates.values():
            if len(by_matrix) == 1:
                matrix_id, reference = next(iter(by_matrix.items()))
                targets.append(MentionTarget(reference, matrix_id))
        return targets

    def _matrix_display_for_server(self, matrix_id: str, server_id: str) -> str:
        binding = self.database.get_binding_by_matrix(matrix_id, server_id)
        if binding is not None:
            return binding.player_id
        target = self.database.get_server(server_id)
        group_servers = {
            server.server_id
            for server in self.database.all_servers()
            if target and server.enabled and server.relay_group == target.relay_group
        }
        alternatives = [
            item
            for item in self.database.get_bindings_by_matrix(matrix_id)
            if item.scope_id == "*" or item.server_id in group_servers
        ]
        if alternatives:
            item = alternatives[0]
            return item.player_id if item.scope_id == "*" else f"{item.server_id}:{item.player_id}"
        return matrix_id

    @staticmethod
    def _matrix_mentioned_user_ids(event: Any) -> list[str]:
        source = getattr(event, "source", {})
        content = source.get("content", {}) if isinstance(source, dict) else {}
        mentions = content.get("m.mentions", {}) if isinstance(content, dict) else {}
        user_ids = mentions.get("user_ids", []) if isinstance(mentions, dict) else []
        if not isinstance(user_ids, list):
            return []
        return [item for item in user_ids if isinstance(item, str)]

    def _validate_hello(self, server_id: str, payload: dict[str, Any]) -> None:
        server = self.database.get_server(server_id)
        if server is None:
            return
        claimed_name = str(payload.get("serverName", "")).strip()
        claimed_group = str(payload.get("relayGroup", ""))
        raw_port = payload.get("gamePort")
        claimed_port = (
            int(raw_port)
            if isinstance(raw_port, int) and 1 <= raw_port <= 65535
            else None
        )
        if claimed_name:
            self.database.update_server_hello(
                server_id, claimed_name, claimed_port
            )
            server = self.database.get_server(server_id) or server
        if server.display_name_override and claimed_name != server.display_name:
            LOGGER.warning(
                "服务器 %s 使用管理员覆盖名称 %r（插件配置为 %r）",
                server_id,
                server.display_name,
                claimed_name,
            )
        if claimed_group and claimed_group != server.relay_group:
            LOGGER.warning(
                "服务器 %s 的插件路由组 %r 与后台登记路由组 %r 不一致",
                server_id,
                claimed_group,
                server.relay_group,
            )
        LOGGER.info("服务器 %s 握手信息: %s", server_id, payload)

    async def _handle_server_command(
        self, room: MatrixRoom, event: RoomMessageText, text: str
    ) -> None:
        try:
            parts = shlex.split(text[1:])
        except ValueError:
            await self._send_user_notice(event.sender, "指令中的引号不完整。")
            return
        if self._admin_role(room, event.sender) is None:
            await self._send_admin_denied(event.sender)
            return
        action = parts[1].casefold() if len(parts) > 1 else "help"
        if action in {"help", "帮助"}:
            if len(parts) > 3:
                await self._send_user_notice(
                    event.sender,
                    "用法：#server help [子命令]\n"
                    "示例：#server help group",
                )
                return
            topic = parts[2].casefold() if len(parts) == 3 else None
            await self._send_user_notice(
                event.sender, self._server_command_help(topic)
            )
            return
        if action == "list":
            servers = self.database.all_servers()
            if not servers:
                await self._send_user_notice(event.sender, "尚未登记任何服务器。")
                return
            lines = [
                f"{item.server_id} | {item.display_name} | "
                f"{item.relay_group} | {item.game_port or '-'} | "
                f"{'在线' if item.server_id in self.servers else '离线'} | "
                f"{'启用' if item.enabled else '禁用'}"
                for item in servers
            ]
            await self._send_user_notice(
                event.sender,
                "🖥️ 服务器列表\n" + "\n".join(f"• {line}" for line in lines),
            )
            return
        server_id = parts[2] if len(parts) > 2 else ""
        if action in {"info", "status"}:
            await self._send_server_info(event.sender, server_id)
            return
        if action == "audit":
            entries = self.database.recent_audit(server_id or None)
            body = "\n".join(
                f"• {item.created_at} | {item.actor_matrix_id} | {item.server_id} | "
                f"{item.action}: {item.old_value or '-'} → {item.new_value or '-'}"
                for item in entries
            ) or "• 暂无管理记录"
            await self._send_user_notice(event.sender, "📋 服务器管理审计\n" + body)
            return
        if not server_id:
            await self._send_user_notice(event.sender, self._server_command_help())
            return

        field: str
        value: str | int | bool | None
        if action == "rename" and len(parts) >= 4:
            field, value = "display_name_override", " ".join(parts[3:]).strip()
        elif action == "reset-name":
            field, value = "display_name_override", None
        elif action == "port" and len(parts) == 4:
            try:
                port = int(parts[3])
            except ValueError:
                port = 0
            if not 1 <= port <= 65535:
                await self._send_user_notice(event.sender, "端口必须在 1 到 65535 之间。")
                return
            field, value = "game_port_override", port
        elif action == "reset-port":
            field, value = "game_port_override", None
        elif action == "group" and len(parts) == 4:
            if not SERVER_ID_PATTERN.fullmatch(parts[3]):
                await self._send_user_notice(event.sender, "路由组名称格式无效。")
                return
            field, value = "relay_group", parts[3]
        elif action in {"enable", "disable"}:
            field, value = "enabled", action == "enable"
        else:
            await self._send_user_notice(event.sender, self._server_command_help())
            return
        if isinstance(value, str) and not value:
            await self._send_user_notice(event.sender, "名称不能为空。")
            return
        updated = self.database.admin_update_server(
            server_id,
            field,
            value,
            event.sender,
            event.event_id,
            action,
        )
        if not updated:
            await self._send_user_notice(event.sender, f"未知服务器：{server_id}")
            return
        await self._send_user_notice(event.sender, f"已完成：{action} {server_id}")

    async def _send_command_help(
        self, room: MatrixRoom, requester: str
    ) -> None:
        role = self._admin_role(room, requester)
        lines = [
            "📖 MatrixChatBridge 指令帮助",
            "# 与 ! 前缀等价",
            "",
            "【公共指令】",
            "#help  查看本帮助",
            "#bind  获取游戏账号绑定验证码",
            "#list  查看已连接服务器的在线玩家",
            "#online / #在线榜  生成各服累计游玩时长 PNG 榜单",
            "#game help  查看跨聊天室和游戏内的小游戏玩法",
            "#ping  实测机器人↔插件延迟、玩家 ping、TPS 与 MSPT",
            "#reconnect status  查看自己的重连保护状态",
            "#reconnect clear  处理登录后解除自己的上下线屏蔽",
            "#reconnect help  查看重连保护说明",
        ]
        if role is not None:
            lines.extend(
                [
                    "",
                    f"【服务器管理 · 当前角色 {role}】",
                    "#server list  查看服务器列表",
                    "#server info <id>  查看服务器详情",
                    "#server rename <id> <名称>  覆盖显示名称",
                    "#server reset-name <id>  恢复插件端名称",
                    "#server port <id> <端口>  覆盖展示端口",
                    "#server reset-port <id>  恢复插件端端口",
                    "#server group <id> <组>  修改路由组",
                    "#server enable|disable <id>  启用或禁用服务器",
                    "#server audit [id]  查看服务器管理审计",
                    "#server help [子命令]  查看服务器管理帮助或某项详细说明",
                    "#avatar status  查看玩家头像显示模式",
                    "#avatar on|off  开关实验性内嵌头像",
                    "#reconnect list  查看被重连保护屏蔽的玩家",
                    "#reconnect clear <服务器> <玩家>  管理员解除指定玩家",
                    "#admin list  查看机器人管理员名单",
                ]
            )
        if role == "owner":
            lines.extend(
                [
                    "",
                    "【管理员身份管理 · 仅 owner】",
                    "#admin add <Matrix ID> <owner|admin>  登记或修改管理员",
                    "#admin remove <Matrix ID>  移除管理员",
                    "#admin help  查看管理员身份管理简表",
                ]
            )
        elif role == "admin":
            lines.append("管理员增删仅限 owner 操作。")
        await self._send_user_notice(requester, "\n".join(lines))

    async def _send_ping_status(self, requester: str) -> None:
        servers = self.database.all_servers()
        enabled_servers = [server for server in servers if server.enabled]
        now = time.monotonic()
        self.expired_ping_queries = {
            request_id: expiry for request_id, expiry in self.expired_ping_queries.items()
            if expiry > now
        }
        requests: dict[str, tuple[str, asyncio.Future[PingSample]]] = {}
        try:
            for server in enabled_servers:
                websocket = self.servers.get(server.server_id)
                if websocket is None:
                    continue
                request_id = str(uuid.uuid4())
                future: asyncio.Future[PingSample] = asyncio.get_running_loop().create_future()
                self.ping_queries[request_id] = (
                    server.server_id, time.monotonic(), future,
                )
                requests[server.server_id] = (request_id, future)
                await self._send_to_server(
                    server.server_id, websocket, "query_list_request",
                    {"requester": requester}, message_id=request_id,
                )
            if requests:
                await asyncio.wait(
                    [future for _, future in requests.values()],
                    timeout=PING_QUERY_TIMEOUT_SECONDS,
                )
        finally:
            for server_id, (request_id, future) in requests.items():
                if not future.done():
                    self.expired_ping_queries[request_id] = time.monotonic() + 120
                self.ping_queries.pop(request_id, None)
        connected_count = sum(
            server.server_id in self.servers for server in enabled_servers
        )
        lines = [
            "🏓 MatrixChatBridge 实时状态",
            "机器人后台：正常",
            "Matrix 连接：正常（本指令已实时收到并处理）",
            f"WebSocket：监听端口 {self.settings.ws_port}，路径 /minecraft",
            f"Minecraft：{connected_count}/{len(enabled_servers)} 台已启用服务器在线",
            "延迟说明：机器人↔插件为本次查询往返耗时；玩家→服务器为游戏端报告的 ping",
        ]
        if not servers:
            lines.append("• 尚未登记任何服务器")
        else:
            lines.append("【服务器连接】")
            for server in servers:
                if not server.enabled:
                    state = "已禁用"
                elif server.server_id in self.servers:
                    state = "连接正常"
                else:
                    state = "连接异常或未连接"
                lines.append(
                    f"• {server.server_id} / {server.display_name} / {state} / "
                    f"最后连接 {server.last_seen or '-'}"
                )
                item = requests.get(server.server_id)
                if not server.enabled:
                    lines.append("  机器人↔插件：已禁用；玩家→服务器：不可用")
                elif item is None:
                    lines.append("  机器人↔插件：未连接；玩家→服务器：不可用")
                elif not item[1].done():
                    lines.append(
                        f"  机器人↔插件：查询超时（>{PING_QUERY_TIMEOUT_SECONDS * 1000:.0f} ms）；"
                        "玩家→服务器：不可用"
                    )
                else:
                    sample = item[1].result()
                    lines.append(f"  机器人↔插件：{sample.round_trip_ms} ms")
                    tps = f"{sample.tps_1m:.2f}" if sample.tps_1m is not None else "未提供"
                    mspt = f"{sample.mspt:.2f} ms" if sample.mspt is not None else "未提供"
                    lines.append(f"  服务器性能：TPS(1分钟) {tps} / MSPT {mspt}")
                    if sample.online_count == 0:
                        lines.append("  玩家→服务器：当前无人在线")
                    elif not sample.players:
                        lines.append("  玩家→服务器：插件未提供玩家列表")
                    else:
                        lines.append("  玩家→服务器：")
                        for player_name, ping_ms in sample.players:
                            value = f"{ping_ms} ms" if ping_ms is not None else "未提供"
                            lines.append(f"    {player_name}：{value}")
        await self._send_user_notice(requester, "\n".join(lines))

    async def _handle_reconnect_command(
        self, room: MatrixRoom, event: RoomMessageText, text: str
    ) -> None:
        try:
            parts = shlex.split(text[1:])
        except ValueError:
            await self._send_user_notice(event.sender, "指令中的引号不完整。")
            return
        action = parts[1].casefold() if len(parts) > 1 else "help"
        role = self._admin_role(room, event.sender)
        self.database.expire_reconnect_mutes(
            self.settings.reconnect_guard_quiet_seconds
        )

        if action in {"help", "帮助"} and len(parts) == 2:
            await self._send_user_notice(
                event.sender, self._reconnect_command_help(role is not None)
            )
            return
        if action == "status" and len(parts) == 2:
            bindings = self.database.get_bindings_by_matrix(event.sender)
            if not bindings:
                await self._send_user_notice(
                    event.sender,
                    "尚未绑定游戏账号。请先使用 #bind 获取验证码并在游戏内完成绑定。",
                )
                return
            muted = self.database.all_reconnect_mutes()
            lines: list[str] = []
            for binding in bindings:
                matches = [
                    item for item in muted
                    if item.player_id.casefold() == binding.player_id.casefold()
                    and (
                        binding.scope_id == "*"
                        or item.server_id == binding.server_id
                    )
                ]
                scope = "全部服务器" if binding.scope_id == "*" else binding.server_id
                state = (
                    "已屏蔽：" + ", ".join(item.server_id for item in matches)
                    if matches else "正常"
                )
                lines.append(f"• {scope} / {binding.player_id} / {state}")
            await self._send_user_notice(
                event.sender,
                "🔁 我的重连保护状态\n" + "\n".join(lines),
            )
            return
        if action == "clear" and len(parts) == 2:
            bindings = self.database.get_bindings_by_matrix(event.sender)
            if not bindings:
                await self._send_user_notice(
                    event.sender,
                    "尚未绑定游戏账号，无法确认需要解除的玩家。请联系管理员。",
                )
                return
            removed = 0
            for binding in bindings:
                removed += self.database.clear_reconnect_mutes_for_binding(binding)
                self._clear_reconnect_activity_for_binding(binding)
            message = (
                f"已解除 {removed} 项重连保护屏蔽，可以正常显示上下线动态。"
                if removed
                else "你的绑定账号当前没有被重连保护屏蔽。"
            )
            await self._send_user_notice(event.sender, message)
            return
        if action == "list" and len(parts) == 2:
            if role is None:
                await self._send_admin_denied(event.sender)
                return
            muted = self.database.all_reconnect_mutes()
            lines = [
                f"• {item.server_id} / {item.player_id} / 最近事件 {item.last_event_at}"
                for item in muted
            ] or ["• 当前没有被屏蔽的玩家"]
            await self._send_user_notice(
                event.sender,
                "🔁 重连保护屏蔽列表\n" + "\n".join(lines),
            )
            return
        if action == "clear" and len(parts) == 4:
            if role is None:
                await self._send_admin_denied(event.sender)
                return
            server_id, player_name = parts[2], parts[3]
            removed = self.database.clear_reconnect_mute(
                server_id, player_name
            )
            self.reconnect_activity.pop(
                (server_id, player_name.casefold()), None
            )
            message = (
                f"已解除 {server_id} / {player_name} 的重连保护屏蔽。"
                if removed
                else f"未找到 {server_id} / {player_name} 的重连保护记录。"
            )
            await self._send_user_notice(event.sender, message)
            return
        await self._send_user_notice(
            event.sender, self._reconnect_command_help(role is not None)
        )

    def _clear_reconnect_activity_for_binding(self, binding: Binding) -> None:
        for key in list(self.reconnect_activity):
            server_id, player_id = key
            if player_id != binding.player_id.casefold():
                continue
            if binding.scope_id == "*" or server_id == binding.server_id:
                self.reconnect_activity.pop(key, None)

    def _reconnect_command_help(self, is_admin: bool) -> str:
        quiet_text = self._format_duration(
            self.settings.reconnect_guard_quiet_seconds
        )
        lines = [
            "🔁 自动重连保护",
            "机器人会检测两类异常：短时间内多次快速上线后下线，以及反复上线后约两分钟未认证被踢出。",
            "默认在 60 秒内出现 3 次不超过 30 秒的短会话，或在 10 分钟内出现 2 次 60～180 秒的认证超时会话时触发。",
            "聊天、死亡、成就和账号绑定不受影响。",
            f"屏蔽期间连续 {quiet_text} 没有新的上下线事件时会自动解除。",
            "",
            "【玩家指令】",
            "#reconnect status  查看自己绑定账号的保护状态",
            "#reconnect clear  回到电脑并处理登录后解除自己的屏蔽",
            "#reconnect help  查看本说明",
        ]
        if is_admin:
            lines.extend(
                [
                    "",
                    "【管理员指令】",
                    "#reconnect list  查看所有被屏蔽玩家",
                    "#reconnect clear <服务器> <玩家>  解除指定玩家的屏蔽",
                    "示例：#reconnect clear vanilla Steve",
                ]
            )
        lines.extend(
            [
                "",
                "【注意】",
                "解除前应先确认玩家已经回到电脑并完成密码认证，否则自动重连循环可能再次触发保护。",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _format_duration(seconds: int) -> str:
        if seconds % 60 == 0:
            return f"{seconds // 60} 分钟"
        return f"{seconds} 秒"

    async def _handle_avatar_command(
        self, room: MatrixRoom, event: RoomMessageText, text: str
    ) -> None:
        if self._admin_role(room, event.sender) is None:
            await self._send_admin_denied(event.sender)
            return
        try:
            parts = shlex.split(text[1:])
        except ValueError:
            await self._send_user_notice(event.sender, "指令中的引号不完整。")
            return
        action = parts[1].casefold() if len(parts) > 1 else "status"
        if (action in {"status", "状态"} and len(parts) == 2) or len(parts) == 1:
            await self._send_avatar_status(event.sender)
            return
        if action not in {"on", "off"} or len(parts) != 2:
            await self._send_user_notice(
                event.sender,
                "🖼️ 玩家头像实验功能\n"
                "#avatar status  查看当前状态\n"
                "#avatar on  开启实验性内嵌头像\n"
                "#avatar off  关闭并使用跨客户端兼容文本",
            )
            return
        if action == "on" and not self.settings.avatar_enabled:
            await self._send_user_notice(
                event.sender,
                "头像功能已被服务器环境变量 AVATAR_ENABLED=false 禁用。",
            )
            return

        async with self.avatar_mode_lock:
            requested_mode = "emote" if action == "on" else "text"
            if self.avatar_display_mode == requested_mode:
                await self._send_avatar_status(event.sender)
                return
            if requested_mode == "emote":
                await self.avatar_manager.start()
                self.avatar_display_mode = "emote"
            else:
                self.avatar_display_mode = "text"
                await self.avatar_manager.close()
            self.database.set_setting("avatar_display_mode", requested_mode)

        if requested_mode == "emote":
            await self._send_user_notice(
                event.sender,
                "已开启实验性玩家头像。支持 Matrix 自定义表情的客户端会显示头像；"
                "部分 Element X 版本仍可能显示降级文字。设置已持久保存。",
            )
        else:
            await self._send_user_notice(
                event.sender,
                "已关闭实验性玩家头像，恢复跨客户端兼容文本。设置已持久保存。",
            )

    async def _send_avatar_status(self, requester: str) -> None:
        if not self.settings.avatar_enabled:
            status = "不可用（AVATAR_ENABLED=false）"
        elif self._avatar_emotes_enabled():
            status = "开启（实验性自定义表情模式）"
        else:
            status = "关闭（跨客户端兼容文本模式）"
        await self._send_user_notice(
            requester,
            "🖼️ 玩家头像显示\n"
            f"状态：{status}\n"
            "切换：#avatar on 或 #avatar off",
        )

    async def _handle_admin_command(
        self, room: MatrixRoom, event: RoomMessageText, text: str
    ) -> None:
        role = self._admin_role(room, event.sender)
        if role is None:
            await self._send_admin_denied(event.sender)
            return
        try:
            parts = shlex.split(text[1:])
        except ValueError:
            await self._send_user_notice(event.sender, "指令中的引号不完整。")
            return
        action = parts[1].casefold() if len(parts) > 1 else "help"
        if action in {"help", "帮助"}:
            await self._send_user_notice(event.sender, self._admin_command_help())
            return
        if action == "list":
            admins = self.database.all_matrix_admins()
            body = "\n".join(
                f"• {admin.matrix_id} | {admin.role}" for admin in admins
            ) or "• 尚未登记管理员"
            await self._send_user_notice(event.sender, "🛡️ 管理员列表\n" + body)
            return
        if role != "owner":
            await self._send_user_notice(
                event.sender, "只有 owner 可以增加、修改或移除管理员身份。"
            )
            return
        if action == "add" and len(parts) == 4:
            target, target_role = parts[2], parts[3].casefold()
            if not MATRIX_ID_PATTERN.fullmatch(target):
                await self._send_user_notice(
                    event.sender, "Matrix ID 格式无效，应类似 @name:example.org。"
                )
                return
            if target_role not in {"owner", "admin"}:
                await self._send_user_notice(event.sender, "角色只能是 owner 或 admin。")
                return
            try:
                self.database.set_matrix_admin(
                    target, target_role, event.sender, event.event_id
                )
            except ValueError as error:
                await self._send_user_notice(event.sender, str(error))
                return
            await self._send_user_notice(
                event.sender, f"已登记管理员：{target} ({target_role})"
            )
            return
        if action == "remove" and len(parts) == 3:
            target = parts[2]
            try:
                removed = self.database.remove_matrix_admin(
                    target, event.sender, event.event_id
                )
            except ValueError as error:
                await self._send_user_notice(event.sender, str(error))
                return
            message = (
                f"已移除管理员：{target}"
                if removed
                else f"未找到管理员：{target}"
            )
            await self._send_user_notice(event.sender, message)
            return
        await self._send_user_notice(event.sender, self._admin_command_help())

    def _admin_role(self, room: MatrixRoom, matrix_id: str) -> str | None:
        identity = self.database.get_matrix_admin(matrix_id)
        if identity is None:
            return None
        if (
            room.power_levels.get_user_level(matrix_id)
            < self.settings.admin_power_level
        ):
            return None
        return identity.role

    async def _send_admin_denied(self, requester: str) -> None:
        await self._send_user_notice(
            requester,
            "无机器人管理权限。需要先在后端登记 Matrix ID，"
            f"并具有房间权限等级 {self.settings.admin_power_level} 或以上。",
        )

    async def _send_server_info(self, requester: str, server_id: str) -> None:
        server = self.database.get_server(server_id)
        if server is None:
            await self._send_user_notice(requester, f"未知服务器：{server_id or '-'}")
            return
        body = (
            f"🖥️ 服务器详情 · {server.server_id}\n"
            f"显示名称：{server.display_name}\n"
            f"插件名称：{server.configured_name}\n"
            f"游戏端口：{server.game_port or '-'}\n"
            f"路由组：{server.relay_group}\n"
            f"状态：{'启用' if server.enabled else '禁用'} / "
            f"{'在线' if server.server_id in self.servers else '离线'}\n"
            f"最后连接：{server.last_seen or '-'}\n版本：{server.revision}"
        )
        await self._send_user_notice(requester, body)

    @staticmethod
    def _server_command_help(topic: str | None = None) -> str:
        overview = (
            "🖥️ 服务器管理指令\n"
            "# 与 ! 前缀等价；<参数> 必填，[参数] 可选。\n"
            "【查询】\n"
            "#server list  查看全部已登记服务器\n"
            "#server info <id>  查看单台服务器详情\n"
            "#server audit [id]  查看全部或指定服务器的管理审计\n"
            "【修改】\n"
            "#server rename <id> <名称>  覆盖聊天室显示名称\n"
            "#server reset-name <id>  恢复插件上报的名称\n"
            "#server port <id> <端口>  覆盖展示用游戏端口\n"
            "#server reset-port <id>  恢复插件上报的端口\n"
            "#server group <id> <组>  修改跨服聊天路由组\n"
            "#server enable <id>  启用服务器\n"
            "#server disable <id>  禁用服务器\n"
            "【详细帮助】\n"
            "#server help <子命令>  查看用途、参数、原理、示例和注意事项\n"
            "可查询：list、info、audit、rename、reset-name、port、"
            "reset-port、group、enable、disable\n"
            "示例：#server help group"
        )
        if topic is None:
            return overview

        aliases = {"status": "info", "帮助": "help"}
        topic = aliases.get(topic, topic)
        details = {
            "list": (
                "🖥️ server list 详细帮助\n"
                "【用途】\n列出机器人数据库中已经登记的所有 Minecraft 服务器。\n"
                "【语法】\n#server list  显示服务器列表\n"
                "【输出字段】\n"
                "依次显示 server-id、显示名称、路由组、游戏端口、连接状态和启用状态。\n"
                "【状态区别】\n"
                "在线/离线表示插件当前是否连接机器人；启用/禁用表示机器人是否允许该服务器参与转发。\n"
                "【注意】\n服务器离线不代表已被禁用，两者含义不同。"
            ),
            "info": (
                "🖥️ server info 详细帮助\n"
                "【用途】\n查看一台服务器的完整登记信息和当前状态。\n"
                "【语法】\n#server info <id>  查看指定服务器\n"
                "【参数】\nid：插件配置中的唯一 server-id，不是显示名称。\n"
                "【示例】\n#server info vanilla  查看 vanilla 的详情\n"
                "【输出说明】\n"
                "显示名称可能是管理员覆盖值；插件名称和插件端口是服务器最近一次握手上报的值。"
            ),
            "audit": (
                "📋 server audit 详细帮助\n"
                "【用途】\n查询服务器名称、端口、路由组及启用状态的管理修改记录。\n"
                "【语法】\n"
                "#server audit  查看所有服务器最近的审计记录\n"
                "#server audit <id>  只查看指定服务器\n"
                "【示例】\n#server audit survival  查看 survival 的修改记录\n"
                "【输出字段】\n时间、操作者 Matrix ID、server-id、动作、旧值和新值。"
            ),
            "rename": (
                "✏️ server rename 详细帮助\n"
                "【用途】\n覆盖服务器在聊天室和跨服消息中显示的名称。\n"
                "【语法】\n#server rename <id> <名称>  设置显示名称\n"
                "【参数】\nid：唯一 server-id；名称：允许包含空格。\n"
                "【示例】\n#server rename vanilla 1.21 主服  显示为“1.21 主服”\n"
                "【工作原理】\n只改变机器人数据库中的显示覆盖值，不修改插件 config.yml，也不改变 server-id。\n"
                "【撤销】\n使用 #server reset-name <id> 恢复插件上报名称。"
            ),
            "reset-name": (
                "↩️ server reset-name 详细帮助\n"
                "【用途】\n删除管理员设置的名称覆盖，重新采用插件上报的服务器名称。\n"
                "【语法】\n#server reset-name <id>  恢复插件端名称\n"
                "【示例】\n#server reset-name vanilla  取消 vanilla 的名称覆盖\n"
                "【注意】\n不会修改插件配置；插件下次重连或握手后仍会继续更新其上报名称。"
            ),
            "port": (
                "🔌 server port 详细帮助\n"
                "【用途】\n覆盖机器人展示给管理员看的 Minecraft 游戏端口。\n"
                "【语法】\n#server port <id> <端口>  设置展示端口\n"
                "【参数】\n端口必须是 1 到 65535 的整数。\n"
                "【示例】\n#server port survival 25566  将展示端口设为 25566\n"
                "【工作原理】\n这是元数据覆盖，不会修改 server.properties，不会打开防火墙端口，也不会改变插件连接机器人的 WebSocket 端口。\n"
                "【撤销】\n使用 #server reset-port <id> 恢复插件上报端口。"
            ),
            "reset-port": (
                "↩️ server reset-port 详细帮助\n"
                "【用途】\n删除管理员设置的端口覆盖，重新显示插件最近上报的游戏端口。\n"
                "【语法】\n#server reset-port <id>  恢复插件端口\n"
                "【示例】\n#server reset-port survival  取消 survival 的端口覆盖\n"
                "【注意】\n本指令不会重启服务器，也不会修改任何网络监听。"
            ),
            "group": (
                "🔀 server group 详细帮助\n"
                "【用途】\n决定哪些 Minecraft 服务器属于同一个跨服聊天转发范围。\n"
                "【语法】\n#server group <id> <组>  修改服务器的路由组\n"
                "【参数】\n"
                "id：要修改的唯一 server-id。\n"
                "组：1 到 64 位，只能使用英文字母、数字、下划线、点和连字符。\n"
                "【工作原理】\n"
                "同组服务器会互相收到真实玩家聊天；来源服不会收到自己的回传消息。不同组之间不会跨服转发。\n"
                "只有与 MATRIX_RELAY_GROUP 相同的组会把 Minecraft 消息发送到当前 Matrix 房间，并接收该房间消息。\n"
                "【示例】\n"
                "#server group vanilla main  将 vanilla 加入 main 组\n"
                "#server group minigame games  将 minigame 隔离到 games 组\n"
                "【注意】\n"
                "修改立即写入数据库并影响后续消息，无需重启；不会自动修改插件 config.yml 中的 relay-group，配置不一致时日志会给出警告。"
            ),
            "enable": (
                "✅ server enable 详细帮助\n"
                "【用途】\n允许已登记服务器连接后参与 Matrix 与跨服消息转发。\n"
                "【语法】\n#server enable <id>  启用服务器\n"
                "【示例】\n#server enable creative  启用 creative\n"
                "【注意】\n启用不会启动 Minecraft 进程；服务器插件仍需正常运行并使用正确 token 连接。"
            ),
            "disable": (
                "⛔ server disable 详细帮助\n"
                "【用途】\n阻止指定服务器继续参与机器人消息处理和跨服转发。\n"
                "【语法】\n#server disable <id>  禁用服务器\n"
                "【示例】\n#server disable creative  禁用 creative\n"
                "【影响】\n后续来自该服务器的玩家事件不会被转发；它也不会再作为跨服消息目标。\n"
                "【注意】\n不会关闭 Minecraft 服务器进程，也不会删除服务器、token、绑定或审计数据。"
            ),
        }
        detail = details.get(topic)
        if detail is not None:
            return detail
        return (
            f"未知的 server 子命令：{topic}\n"
            "可查询：list、info、audit、rename、reset-name、port、"
            "reset-port、group、enable、disable\n"
            "示例：#server help group"
        )

    @staticmethod
    def _admin_command_help() -> str:
        return (
            "🛡️ 管理员身份指令\n"
            "!admin list  查看管理员名单\n"
            "!admin add <Matrix ID> <owner|admin>  登记或修改管理员（仅 owner）\n"
            "!admin remove <Matrix ID>  移除管理员（仅 owner）"
        )

    async def _send_bind_result(
        self,
        websocket: ServerConnection,
        server_id: str,
        player_id: str,
        player_uuid: str | None,
        success: bool,
        message: str,
    ) -> None:
        await self._send_envelope(
            websocket,
            "bind_result",
            {
                "playerId": player_id,
                "playerUuid": player_uuid or "",
                "success": success,
                "message": message,
            },
            server_id,
        )

    async def _send_ack(
        self, websocket: ServerConnection, acknowledged_id: str, server_id: str
    ) -> None:
        await self._send_envelope(
            websocket, "ack", {"ackFor": acknowledged_id}, server_id
        )

    async def _send_envelope(
        self,
        websocket: ServerConnection,
        message_type: str,
        payload: dict[str, Any],
        server_id: str,
    ) -> None:
        await websocket.send(encode(make_envelope(message_type, payload, server_id)))

    async def _send_user_notice(
        self,
        matrix_id: str,
        text: str,
        thread_root_event_id: str | None = None,
        use_command_thread: bool = True,
    ) -> None:
        await self._send_room_content(
            user_notice_content(matrix_id, text),
            thread_root_event_id=thread_root_event_id,
            use_command_thread=use_command_thread,
        )

    async def _send_room_content(
        self,
        content: dict[str, object],
        transaction_id: str | None = None,
        thread_root_event_id: str | None = None,
        reply_to_event_id: str | None = None,
        use_command_thread: bool = True,
    ) -> None:
        target = self.command_thread.get() if use_command_thread else None
        root_event_id = thread_root_event_id or (
            target.root_event_id if target is not None else None
        )
        reply_event_id = reply_to_event_id or (
            target.reply_to_event_id if target is not None else root_event_id
        )
        if root_event_id:
            content = dict(content)
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": root_event_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": reply_event_id or root_event_id},
            }
        response = await self.matrix_client.room_send(
            room_id=self.settings.room_id,
            message_type="m.room.message",
            content=content,
            tx_id=transaction_id,
        )
        if not isinstance(response, RoomSendResponse):
            raise RuntimeError(f"Matrix 发送失败: {response}")

    @staticmethod
    def _thread_target_for_command(event: RoomMessageText) -> ThreadTarget:
        source = getattr(event, "source", {})
        content = source.get("content", {}) if isinstance(source, dict) else {}
        relation = (
            content.get("m.relates_to", {}) if isinstance(content, dict) else {}
        )
        existing_root = (
            relation.get("event_id")
            if isinstance(relation, dict)
            and relation.get("rel_type") == "m.thread"
            else None
        )
        return ThreadTarget(
            root_event_id=(
                existing_root if isinstance(existing_root, str) else event.event_id
            ),
            reply_to_event_id=event.event_id,
        )

    def _remove_expired_bind_tokens(self) -> None:
        now = time.time()
        self.bind_tokens = {
            token: binding
            for token, binding in self.bind_tokens.items()
            if binding.expires_at > now
        }
