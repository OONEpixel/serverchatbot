from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import shlex
import string
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
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
    WhoamiResponse,
)
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .avatars import AvatarManager
from .config import Settings
from .database import Binding, Database, Server
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


def matrix_command(text: str) -> str | None:
    if len(text) < 2 or text[0] not in MATRIX_COMMAND_PREFIXES:
        return None
    command = text[1:].split(maxsplit=1)[0].casefold()
    return command if command in {"bind", "list", "help", "server", "admin"} else None


@dataclass(frozen=True, slots=True)
class BindToken:
    matrix_id: str
    expires_at: float
    thread_root_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class ThreadTarget:
    root_event_id: str
    reply_to_event_id: str


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
        self.command_thread: ContextVar[ThreadTarget | None] = ContextVar(
            f"matrix_command_thread_{id(self)}", default=None
        )
        self.avatar_manager = AvatarManager(
            self.database,
            self.matrix_client,
            settings.avatar_directory,
            settings.avatar_lookup_timeout_seconds,
        )

    async def run(self) -> None:
        try:
            await self._authenticate_matrix()
            if self.settings.avatar_enabled:
                await self.avatar_manager.start()
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
            LOGGER.info("Minecraft 服务器 %s 已断开", server_id)

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
        avatar_mxc_uri = (
            self.avatar_manager.avatar_for(player_name)
            if self.settings.avatar_enabled
            else None
        )
        if message_type == "player_join":
            content = avatar_message_content(
                f"[{server.display_name}]{player_name}上线了",
                player_name,
                avatar_mxc_uri,
            )
        elif message_type == "player_quit":
            content = avatar_message_content(
                f"[{server.display_name}]{player_name}下线了",
                player_name,
                avatar_mxc_uri,
            )
        elif message_type == "player_chat":
            content = game_chat_content(
                server.display_name,
                player_name,
                plain_text,
                self._mention_targets(server.relay_group),
                avatar_mxc_uri,
            )
        else:
            fallback = f"{player_name} 触发了 {message_type}"
            content = avatar_message_content(
                f"[{server.display_name}]{plain_text or fallback}",
                player_name,
                avatar_mxc_uri,
            )
        # Reusing the Minecraft envelope ID as Matrix transaction ID makes a
        # retry idempotent at the homeserver as well as in our SQLite ledger.
        if server.relay_group == self.settings.matrix_relay_group:
            await self._send_room_content(content, transaction_id=message_id)
        if message_type == "player_chat":
            await self._relay_player_chat(
                server, payload, player_name, plain_text, message_id
            )

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
            await self._send_user_notice(event.sender, self._server_command_help())
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
                    "#server help  查看服务器管理简表",
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
    def _server_command_help() -> str:
        return (
            "🖥️ 服务器管理指令\n"
            "【查询】\n"
            "!server list  查看服务器列表\n"
            "!server info <id>  查看服务器详情\n"
            "【修改】\n"
            "!server rename <id> <名称>  覆盖显示名称\n"
            "!server reset-name <id>  恢复插件端名称\n"
            "!server port <id> <端口>  覆盖展示端口\n"
            "!server reset-port <id>  恢复插件端端口\n"
            "!server group <id> <组>  修改路由组\n"
            "!server enable <id>  启用服务器\n"
            "!server disable <id>  禁用服务器\n"
            "!server audit [id]  查看服务器管理审计"
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
