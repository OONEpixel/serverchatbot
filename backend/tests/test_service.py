import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nio import MatrixRoom, RoomSendResponse, StickerEvent, UploadResponse

from app.config import ServerHealthQuietWindow, Settings
from app.protocol import encode, make_envelope
from app.service import BindToken, MatrixBridgeService, matrix_command
from app.online_board import render_online_board


class FakeMatrixClient:
    def __init__(self) -> None:
        self.sent = []
        self.uploads = []

    async def room_send(self, **kwargs):
        self.sent.append(kwargs)
        return RoomSendResponse("$event", kwargs["room_id"])

    async def upload(self, data, **kwargs):
        self.uploads.append((data.read(), kwargs))
        return UploadResponse("mxc://example.org/online-board"), None


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def close(self, **kwargs) -> None:
        self.closed = kwargs


class FakeAvatarManager:
    def __init__(self) -> None:
        self.start_count = 0
        self.close_count = 0

    async def start(self) -> None:
        self.start_count += 1

    async def close(self) -> None:
        self.close_count += 1


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        settings = Settings(
            matrix_url="https://matrix.example.org",
            matrix_id="@bot:example.org",
            matrix_password="password",
            room_id="!room:example.org",
            ws_host="127.0.0.1",
            ws_port=8766,
            database_path=Path(self.temporary_directory.name) / "bridge.db",
            max_matrix_message_length=256,
            bind_token_ttl_seconds=600,
            unbound_reminder_cooldown_seconds=300,
            matrix_relay_group="main",
            binding_mode="scoped",
            admin_power_level=50,
            avatar_enabled=False,
        )
        self.service = MatrixBridgeService(settings)
        self.fake_matrix = FakeMatrixClient()
        self.service.matrix_client = self.fake_matrix
        self.service.database.save_server(
            "vanilla", "原版服", "hash-a", "main"
        )
        self.service.database.save_server(
            "survival", "生存服", "hash-b", "main"
        )

    def tearDown(self) -> None:
        self.service.database.close()
        self.temporary_directory.cleanup()

    async def test_chat_uses_stable_id_mentions_and_relays_without_loop(self) -> None:
        self.service.database.save_binding(
            "@alice:example.org",
            "vanilla",
            "Steve",
            "uuid-steve",
            "scoped",
        )
        vanilla_socket = FakeWebSocket()
        survival_socket = FakeWebSocket()
        self.service.servers = {
            "vanilla": vanilla_socket,
            "survival": survival_socket,
        }
        server = self.service.database.get_server("vanilla")
        await self.service._forward_player_event(
            server,
            "player_chat",
            {
                "playerName": "Alex",
                "playerUuid": "uuid-alex",
                "message": {"plain": "@Steve 上线"},
            },
            "minecraft-event-id",
        )

        sent = self.fake_matrix.sent[0]
        self.assertEqual("minecraft-event-id", sent["tx_id"])
        self.assertEqual(
            ["@alice:example.org"], sent["content"]["m.mentions"]["user_ids"]
        )
        self.assertEqual([], vanilla_socket.sent)
        relay = survival_socket.sent[0]
        self.assertEqual("relay_message", relay["type"])
        self.assertEqual("minecraft-event-id", relay["id"])
        self.assertEqual(1, relay["payload"]["hop"])
        self.assertEqual("原版服", relay["payload"]["source"]["displayName"])

    async def test_matrix_mentions_are_game_ids_for_each_server(self) -> None:
        self.service.database.save_binding(
            "@alice:example.org", "vanilla", "Steve", "uuid", "scoped"
        )
        vanilla_socket = FakeWebSocket()
        survival_socket = FakeWebSocket()
        self.service.servers = {
            "vanilla": vanilla_socket,
            "survival": survival_socket,
        }
        event = SimpleNamespace(
            sender="@bob:example.org",
            event_id="$matrix-event",
            source={
                "content": {
                    "m.mentions": {"user_ids": ["@alice:example.org"]}
                }
            },
        )
        await self.service._push_to_minecraft(
            "!room:example.org", event, "你好 @alice:example.org"
        )
        self.assertEqual("你好 @Steve", vanilla_socket.sent[0]["payload"]["body"])
        self.assertEqual(
            "你好 @vanilla:Steve", survival_socket.sent[0]["payload"]["body"]
        )

    async def test_matrix_sticker_is_forwarded_as_sticker_label(self) -> None:
        self.service.database.save_binding(
            "@alice:example.org", "vanilla", "Steve", "uuid", "scoped"
        )
        vanilla_socket = FakeWebSocket()
        self.service.servers = {"vanilla": vanilla_socket}
        room = MatrixRoom("!room:example.org", "Test room")
        event = StickerEvent.from_dict(
            {
                "content": {
                    "body": "azukisan_img7",
                    "info": {"mimetype": "image/webp", "size": 13060},
                    "url": "mxc://example.org/sticker",
                },
                "event_id": "$sticker-event",
                "origin_server_ts": 1789385086690,
                "sender": "@alice:example.org",
                "type": "m.sticker",
            }
        )

        await self.service._on_matrix_media(room, event)

        self.assertEqual(1, len(vanilla_socket.sent))
        payload = vanilla_socket.sent[0]["payload"]
        self.assertEqual("Steve", payload["senderDisplayName"])
        self.assertEqual("[贴纸]", payload["body"])
        self.assertTrue(
            self.service.database.is_processed("matrix", "$sticker-event")
        )

    async def test_global_message_id_is_deduplicated_across_servers(self) -> None:
        vanilla_socket = FakeWebSocket()
        survival_socket = FakeWebSocket()
        self.service.servers = {
            "vanilla": vanilla_socket,
            "survival": survival_socket,
        }
        self.service.connection_token_hashes = {
            "vanilla": "hash-a",
            "survival": "hash-b",
        }
        payload = {
            "playerName": "Alex",
            "playerUuid": "uuid-alex",
            "message": {"plain": "hello"},
        }
        first = make_envelope(
            "player_chat", payload, "vanilla", "global-message-id"
        )
        duplicate = make_envelope(
            "player_chat", payload, "survival", "global-message-id"
        )
        await self.service._process_minecraft_message(
            vanilla_socket, encode(first), "vanilla"
        )
        await self.service._process_minecraft_message(
            survival_socket, encode(duplicate), "survival"
        )
        self.assertEqual(1, len(self.fake_matrix.sent))

    async def test_binding_is_server_scoped_and_result_returns_to_player(self) -> None:
        websocket = FakeWebSocket()
        self.service.bind_tokens["AbC123"] = BindToken(
            "@alice:example.org", time.time() + 600
        )
        await self.service._confirm_binding(
            websocket,
            "survival",
            {
                "token": "AbC123",
                "playerId": "Steve",
                "playerUuid": "uuid-steve",
            },
        )
        binding = self.service.database.get_binding_by_matrix(
            "@alice:example.org", "survival"
        )
        self.assertEqual("survival", binding.scope_id)
        self.assertEqual("bind_result", websocket.sent[0]["type"])
        self.assertTrue(websocket.sent[0]["payload"]["success"])

    async def test_unbound_reminder_is_rate_limited(self) -> None:
        await self.service._maybe_remind_unbound("@alice:example.org")
        await self.service._maybe_remind_unbound("@alice:example.org")
        self.assertEqual(1, len(self.fake_matrix.sent))

    def test_matrix_commands_use_bang_or_hash_prefix(self) -> None:
        self.assertEqual("bind", matrix_command("!bind"))
        self.assertEqual("bind", matrix_command("#BIND"))
        self.assertEqual("list", matrix_command("!list"))
        self.assertEqual("list", matrix_command("#list"))
        self.assertEqual("help", matrix_command("#help"))
        self.assertEqual("help", matrix_command("!HELP"))
        self.assertEqual("server", matrix_command("!server list"))
        self.assertEqual("admin", matrix_command("#admin list"))
        self.assertEqual("avatar", matrix_command("#avatar status"))
        self.assertEqual("avatar", matrix_command("!AVATAR on"))
        self.assertEqual("reconnect", matrix_command("#reconnect clear"))
        self.assertEqual("reconnect", matrix_command("!RECONNECT status"))
        self.assertEqual("ping", matrix_command("#ping"))
        self.assertEqual("ping", matrix_command("!PING"))
        self.assertIsNone(matrix_command("/bind"))

    async def test_ping_reports_realtime_server_connections(self) -> None:
        socket = FakeWebSocket()
        self.service.servers["vanilla"] = socket
        task = asyncio.create_task(self.service._send_ping_status("@alice:example.org"))
        for _ in range(50):
            if socket.sent:
                break
            await asyncio.sleep(0.01)
        self.assertEqual("query_list_request", socket.sent[0]["type"])
        self.assertNotIn("includeLeaderboard", socket.sent[0]["payload"])
        await self.service._send_online_player_response(
            "vanilla", {
                "requestId": socket.sent[0]["id"],
                "onlineCount": 1,
                "playerList": ["Steve"],
                "playerDetails": [{"name": "Steve", "pingMs": 42}],
                "tps1m": 19.83,
                "mspt": 34.5,
            }, "ping-response",
        )
        await task
        body = self.fake_matrix.sent[-1]["content"]["body"]
        self.assertIn("监听端口 8766", body)
        self.assertIn("Minecraft：1/2 台已启用服务器在线", body)
        self.assertIn("vanilla / 原版服 / 连接正常", body)
        self.assertIn("survival / 生存服 / 连接异常或未连接", body)
        self.assertRegex(body, r"机器人↔插件：\d+ ms")
        self.assertIn("Steve：42 ms", body)
        self.assertIn("TPS(1分钟) 19.83 / MSPT 34.50 ms", body)

    async def test_ping_handles_older_plugin_without_telemetry(self) -> None:
        socket = FakeWebSocket()
        self.service.servers["vanilla"] = socket
        task = asyncio.create_task(self.service._send_ping_status("@alice:example.org"))
        for _ in range(50):
            if socket.sent:
                break
            await asyncio.sleep(0.01)
        await self.service._send_online_player_response(
            "vanilla", {"requestId": socket.sent[0]["id"], "onlineCount": 1,
                        "playerList": ["Steve"]}, "old-ping-response",
        )
        await task
        body = self.fake_matrix.sent[-1]["content"]["body"]
        self.assertIn("Steve：未提供", body)
        self.assertIn("TPS(1分钟) 未提供 / MSPT 未提供", body)

    async def test_ping_query_timeout_is_reported_without_fake_zero_latency(self) -> None:
        self.service.servers["vanilla"] = FakeWebSocket()
        with patch("app.service.PING_QUERY_TIMEOUT_SECONDS", 0.01):
            await self.service._send_ping_status("@alice:example.org")
        body = self.fake_matrix.sent[-1]["content"]["body"]
        self.assertIn("查询超时（>10 ms）", body)
        self.assertIn("玩家→服务器：不可用", body)

    async def test_server_health_notices_are_debounced_and_recover_once(self) -> None:
        self.service.settings = replace(
            self.service.settings,
            server_health_grace_seconds=0.01,
            server_health_min_notice_interval_seconds=0.01,
        )
        self.service._mark_server_connected("vanilla")
        self.service._mark_server_disconnected("vanilla")
        await asyncio.sleep(0.04)
        self.assertEqual(1, len(self.fake_matrix.sent))
        self.assertIn("连接异常", self.fake_matrix.sent[-1]["content"]["body"])

        self.service._mark_server_connected("vanilla")
        await asyncio.sleep(0.05)
        self.assertEqual(2, len(self.fake_matrix.sent))
        self.assertIn("连接已恢复", self.fake_matrix.sent[-1]["content"]["body"])

    async def test_short_disconnect_does_not_send_health_notice(self) -> None:
        self.service.settings = replace(
            self.service.settings,
            server_health_grace_seconds=0.05,
            server_health_min_notice_interval_seconds=0.01,
        )
        self.service._mark_server_connected("vanilla")
        self.service._mark_server_disconnected("vanilla")
        await asyncio.sleep(0.01)
        self.service._mark_server_connected("vanilla")
        await asyncio.sleep(0.07)
        self.assertEqual([], self.fake_matrix.sent)

    def test_server_health_quiet_window_uses_local_time_and_server_scope(self) -> None:
        zone = timezone(timedelta(hours=8))
        window = ServerHealthQuietWindow(clock_time(5, 55), clock_time(6, 15), zone, "vanilla")
        before_start = datetime(2026, 11, 25, 21, 54, 59, tzinfo=UTC)
        start = before_start + timedelta(seconds=1)
        end = start + timedelta(minutes=20)
        self.assertEqual(0, window.remaining_seconds("vanilla", before_start))
        self.assertEqual(1200, window.remaining_seconds("vanilla", start))
        self.assertEqual(0, window.remaining_seconds("survival", start))
        self.assertEqual(0, window.remaining_seconds("vanilla", end))

        overnight = ServerHealthQuietWindow(clock_time(23, 55), clock_time(0, 5), zone)
        midnight = datetime(2026, 11, 25, 16, tzinfo=UTC)
        self.assertEqual(300, overnight.remaining_seconds("vanilla", midnight))

    async def test_health_quiet_window_discards_recovered_outage(self) -> None:
        window = ServerHealthQuietWindow(clock_time(5, 55), clock_time(6, 15), UTC)
        self.service.settings = replace(
            self.service.settings, server_health_grace_seconds=0,
            server_health_quiet_window=window,
        )
        waiting = asyncio.Event()

        def quiet_remaining(server_id):
            waiting.set()
            return 600

        with patch.object(
            ServerHealthQuietWindow, "remaining_seconds", side_effect=quiet_remaining
        ):
            self.service._mark_server_connected("vanilla")
            self.service._mark_server_disconnected("vanilla")
            state = self.service.server_health["vanilla"]
            task = state.pending_task
            await asyncio.wait_for(waiting.wait(), 1)
            self.assertEqual([], self.fake_matrix.sent)
            self.service._mark_server_connected("vanilla")
            await asyncio.gather(task, return_exceptions=True)
            self.assertIsNone(state.pending_task)
        self.assertEqual([], self.fake_matrix.sent)

    async def test_health_quiet_window_alerts_if_still_offline_at_end(self) -> None:
        window = ServerHealthQuietWindow(clock_time(5, 55), clock_time(6, 15), UTC)
        self.service.settings = replace(
            self.service.settings, server_health_grace_seconds=0,
            server_health_min_notice_interval_seconds=0,
            server_health_quiet_window=window,
        )
        with patch.object(
            ServerHealthQuietWindow, "remaining_seconds", side_effect=[0.01, 0]
        ) as remaining:
            self.service._mark_server_connected("vanilla")
            self.service._mark_server_disconnected("vanilla")
            await asyncio.wait_for(self.service.server_health["vanilla"].pending_task, 1)
        self.assertEqual(2, remaining.call_count)
        self.assertEqual(1, len(self.fake_matrix.sent))
        self.assertIn("连接异常", self.fake_matrix.sent[-1]["content"]["body"])

    async def test_planned_server_stop_and_restart_are_announced_once(self) -> None:
        self.service._mark_server_connected("vanilla")
        await self.service._handle_server_stopping("vanilla")
        await self.service._handle_server_stopping("vanilla")
        self.assertEqual(1, len(self.fake_matrix.sent))
        self.assertIn("正在关闭或重启", self.fake_matrix.sent[-1]["content"]["body"])

        self.service._mark_server_disconnected("vanilla")
        await asyncio.sleep(0)
        self.assertEqual(1, len(self.fake_matrix.sent))

        self.service._mark_server_connected("vanilla")
        await asyncio.sleep(0.02)
        self.assertEqual(2, len(self.fake_matrix.sent))
        recovery = self.fake_matrix.sent[-1]["content"]["body"]
        self.assertIn("服务器重启成功", recovery)
        self.assertIn("消息转发恢复正常", recovery)

    async def test_reconnect_loop_is_muted_and_bound_player_is_mentioned(self) -> None:
        self.service.database.save_binding(
            "@alice:example.org", "vanilla", "Steve", "uuid", "scoped"
        )
        server = self.service.database.get_server("vanilla")

        self.assertFalse(
            await self.service._handle_reconnect_guard(
                server, "player_join", "Steve", now=0
            )
        )
        self.assertFalse(
            await self.service._handle_reconnect_guard(
                server, "player_quit", "Steve", now=120
            )
        )
        self.assertFalse(
            await self.service._handle_reconnect_guard(
                server, "player_join", "Steve", now=130
            )
        )
        self.assertTrue(
            await self.service._handle_reconnect_guard(
                server, "player_quit", "Steve", now=250
            )
        )
        self.assertTrue(
            self.service.database.is_reconnect_muted("vanilla", "steve")
        )
        warning = self.fake_matrix.sent[-1]["content"]
        self.assertIn("频繁断线或自动重连认证循环", warning["body"])
        self.assertEqual(
            ["@alice:example.org"], warning["m.mentions"]["user_ids"]
        )
        sent_count = len(self.fake_matrix.sent)
        self.assertTrue(
            await self.service._handle_reconnect_guard(
                server, "player_join", "Steve", now=260
            )
        )
        self.assertEqual(sent_count, len(self.fake_matrix.sent))

    async def test_rapid_disconnect_loop_is_detected_within_seconds(self) -> None:
        server = self.service.database.get_server("vanilla")
        timeline = [
            ("player_join", 0, False),
            ("player_quit", 2, False),
            ("player_join", 5, False),
            ("player_quit", 7, False),
            ("player_join", 10, False),
            ("player_quit", 12, True),
        ]
        for message_type, now, expected in timeline:
            self.assertEqual(
                expected,
                await self.service._handle_reconnect_guard(
                    server, message_type, "Alex", now=now
                ),
            )
        self.assertTrue(
            self.service.database.is_reconnect_muted("vanilla", "Alex")
        )
        self.assertIn(
            "频繁断线", self.fake_matrix.sent[-1]["content"]["body"]
        )

    async def test_bound_player_can_clear_own_reconnect_mute(self) -> None:
        self.service.database.save_binding(
            "@alice:example.org", "vanilla", "Steve", "uuid", "scoped"
        )
        self.service.database.mute_reconnect_player("vanilla", "Steve")
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        event = SimpleNamespace(
            sender="@alice:example.org", event_id="$reconnect-clear"
        )

        await self.service._handle_reconnect_command(
            room, event, "#reconnect clear"
        )

        self.assertFalse(
            self.service.database.is_reconnect_muted("vanilla", "Steve")
        )
        self.assertIn("已解除 1 项", self.fake_matrix.sent[-1]["content"]["body"])

    async def test_admin_can_toggle_persistent_avatar_mode(self) -> None:
        self.service.settings = replace(
            self.service.settings, avatar_enabled=True
        )
        fake_avatar_manager = FakeAvatarManager()
        self.service.avatar_manager = fake_avatar_manager
        self.service.database.set_matrix_admin(
            "@admin:example.org", "owner", "console", "setup:avatar-admin"
        )
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        room.power_levels.users["@admin:example.org"] = 50
        event = SimpleNamespace(
            sender="@admin:example.org", event_id="$avatar-toggle"
        )

        await self.service._handle_avatar_command(room, event, "#avatar on")
        self.assertEqual("emote", self.service.avatar_display_mode)
        self.assertEqual(
            "emote", self.service.database.get_setting("avatar_display_mode")
        )
        self.assertEqual(1, fake_avatar_manager.start_count)

        await self.service._handle_avatar_command(room, event, "!avatar off")
        self.assertEqual("text", self.service.avatar_display_mode)
        self.assertEqual(
            "text", self.service.database.get_setting("avatar_display_mode")
        )
        self.assertEqual(1, fake_avatar_manager.close_count)

    async def test_unregistered_user_cannot_toggle_avatar_mode(self) -> None:
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        room.power_levels.users["@member:example.org"] = 100
        event = SimpleNamespace(
            sender="@member:example.org", event_id="$avatar-denied"
        )
        await self.service._handle_avatar_command(room, event, "#avatar on")
        self.assertEqual("text", self.service.avatar_display_mode)
        self.assertIn(
            "无机器人管理权限", self.fake_matrix.sent[-1]["content"]["body"]
        )

    async def test_multiple_matrix_admins_can_manage_with_audit(self) -> None:
        self.service.database.set_matrix_admin(
            "@admin-a:example.org", "owner", "console", "setup:admin-a"
        )
        self.service.database.set_matrix_admin(
            "@admin-b:example.org", "admin", "console", "setup:admin-b"
        )
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        room.power_levels.users["@admin-a:example.org"] = 50
        room.power_levels.users["@admin-b:example.org"] = 75
        first = SimpleNamespace(
            sender="@admin-a:example.org", event_id="$admin-1"
        )
        second = SimpleNamespace(
            sender="@admin-b:example.org", event_id="$admin-2"
        )
        await self.service._handle_server_command(
            room, first, "!server rename vanilla 新原版服"
        )
        await self.service._handle_server_command(
            room, second, "#server port vanilla 25566"
        )
        server = self.service.database.get_server("vanilla")
        self.assertEqual("新原版服", server.display_name)
        self.assertEqual(25566, server.game_port)
        self.assertEqual(2, len(self.service.database.recent_audit("vanilla")))

    async def test_server_help_explains_individual_subcommand(self) -> None:
        self.service.database.set_matrix_admin(
            "@owner:example.org", "owner", "console", "setup:help-detail"
        )
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        room.power_levels.users["@owner:example.org"] = 100
        event = SimpleNamespace(
            sender="@owner:example.org", event_id="$server-help-group"
        )

        await self.service._handle_server_command(
            room, event, "#server help group"
        )

        body = self.fake_matrix.sent[-1]["content"]["body"]
        self.assertIn("server group 详细帮助", body)
        self.assertIn("同组服务器会互相收到真实玩家聊天", body)
        self.assertIn("MATRIX_RELAY_GROUP", body)
        self.assertIn("#server group vanilla main", body)

    def test_server_help_reports_unknown_subcommand(self) -> None:
        body = self.service._server_command_help("unknown")
        self.assertIn("未知的 server 子命令：unknown", body)
        self.assertIn("#server help group", body)

    async def test_non_admin_cannot_change_server(self) -> None:
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        room.power_levels.users["@member:example.org"] = 100
        event = SimpleNamespace(
            sender="@member:example.org", event_id="$member-change"
        )
        await self.service._handle_server_command(
            room, event, "!server disable vanilla"
        )
        self.assertTrue(self.service.database.get_server("vanilla").enabled)
        self.assertEqual([], self.service.database.recent_audit("vanilla"))

    async def test_registered_admin_still_needs_room_power_level(self) -> None:
        self.service.database.set_matrix_admin(
            "@admin:example.org", "owner", "console", "setup:admin"
        )
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        room.power_levels.users["@admin:example.org"] = 49
        event = SimpleNamespace(
            sender="@admin:example.org", event_id="$low-power"
        )
        await self.service._handle_server_command(
            room, event, "#server disable vanilla"
        )
        self.assertTrue(self.service.database.get_server("vanilla").enabled)

    async def test_all_server_subcommands_require_bound_admin_identity(self) -> None:
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        room.power_levels.users["@unbound:example.org"] = 100
        event = SimpleNamespace(
            sender="@unbound:example.org", event_id="$server-list"
        )
        await self.service._handle_server_command(
            room, event, "#server list"
        )
        body = self.fake_matrix.sent[-1]["content"]["body"]
        self.assertIn("无机器人管理权限", body)
        self.assertNotIn("服务器列表", body)

    async def test_owner_can_add_admin_but_admin_cannot_add_owner(self) -> None:
        self.service.database.set_matrix_admin(
            "@owner:example.org", "owner", "console", "setup:owner"
        )
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        room.power_levels.users["@owner:example.org"] = 100
        room.power_levels.users["@helper:example.org"] = 50
        owner_event = SimpleNamespace(
            sender="@owner:example.org", event_id="$add-helper"
        )
        await self.service._handle_admin_command(
            room, owner_event, "!admin add @helper:example.org admin"
        )
        self.assertEqual(
            "admin",
            self.service.database.get_matrix_admin("@helper:example.org").role,
        )

        helper_event = SimpleNamespace(
            sender="@helper:example.org", event_id="$add-owner"
        )
        await self.service._handle_admin_command(
            room, helper_event, "#admin add @other:example.org owner"
        )
        self.assertIsNone(
            self.service.database.get_matrix_admin("@other:example.org")
        )

    async def test_help_is_public_and_expands_by_admin_role(self) -> None:
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        await self.service._send_command_help(room, "@member:example.org")
        public_body = self.fake_matrix.sent[-1]["content"]["body"]
        self.assertIn("#bind", public_body)
        self.assertIn("#list", public_body)
        self.assertIn("#ping", public_body)
        self.assertIn("#reconnect clear", public_body)
        self.assertNotIn("#server rename", public_body)
        self.assertNotIn("#admin add", public_body)

        self.service.database.set_matrix_admin(
            "@owner:example.org", "owner", "console", "setup:help-owner"
        )
        room.power_levels.users["@owner:example.org"] = 100
        await self.service._send_command_help(room, "@owner:example.org")
        owner_body = self.fake_matrix.sent[-1]["content"]["body"]
        self.assertIn("#server rename", owner_body)
        self.assertIn("#server help [子命令]", owner_body)
        self.assertIn("#avatar on|off", owner_body)
        self.assertIn("#admin add", owner_body)
        self.assertIn("【服务器管理 · 当前角色 owner】", owner_body)
        owner_html = self.fake_matrix.sent[-1]["content"]["formatted_body"]
        self.assertIn("<code>#help</code> — 查看本帮助", owner_html)
        self.assertIn("<strong>【公共指令】</strong>", owner_html)

    async def test_command_response_is_sent_in_a_matrix_thread(self) -> None:
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        event = SimpleNamespace(
            sender="@member:example.org",
            event_id="$help-command",
            body="#help",
            source={"content": {}},
        )
        await self.service._on_matrix_text(room, event)
        relation = self.fake_matrix.sent[-1]["content"]["m.relates_to"]
        self.assertEqual("m.thread", relation["rel_type"])
        self.assertEqual("$help-command", relation["event_id"])
        self.assertTrue(relation["is_falling_back"])
        self.assertEqual(
            "$help-command", relation["m.in_reply_to"]["event_id"]
        )

    async def test_command_inside_thread_reuses_existing_thread_root(self) -> None:
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        event = SimpleNamespace(
            sender="@member:example.org",
            event_id="$threaded-help",
            body="#help",
            source={
                "content": {
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$existing-root",
                    }
                }
            },
        )
        await self.service._on_matrix_text(room, event)
        relation = self.fake_matrix.sent[-1]["content"]["m.relates_to"]
        self.assertEqual("$existing-root", relation["event_id"])
        self.assertEqual(
            "$threaded-help", relation["m.in_reply_to"]["event_id"]
        )

    async def test_online_player_results_return_to_list_command_thread(self) -> None:
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        socket = FakeWebSocket()
        self.service.servers = {"vanilla": socket}
        event = SimpleNamespace(
            sender="@member:example.org",
            event_id="$list-command",
            body="#list",
            source={"content": {}},
        )
        await self.service._on_matrix_text(room, event)
        reminder_content = self.fake_matrix.sent[-1]["content"]
        self.assertNotIn("m.relates_to", reminder_content)
        request_id = socket.sent[-1]["id"]
        await self.service._send_online_player_response(
            "vanilla",
            {
                "requestId": request_id,
                "onlineCount": 2,
                "playerList": ["Steve", "Alex"],
            },
            "minecraft-list-response",
        )
        content = self.fake_matrix.sent[-1]["content"]
        self.assertEqual("$list-command", content["m.relates_to"]["event_id"])
        self.assertEqual(
            "🎮 [原版服] 在线玩家（2 人）\n• Steve\n• Alex",
            content["body"],
        )

    async def test_online_board_uploads_one_png_in_command_thread(self) -> None:
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        socket = FakeWebSocket()
        self.service.servers = {"vanilla": socket}
        event = SimpleNamespace(
            sender="@member:example.org", event_id="$board", body="#在线榜",
            source={"content": {}},
        )
        task = asyncio.create_task(self.service._on_matrix_text(room, event))
        for _ in range(50):
            if socket.sent:
                break
            await asyncio.sleep(0.01)
        self.assertEqual("query_list_request", socket.sent[0]["type"])
        self.assertTrue(socket.sent[0]["payload"]["includeLeaderboard"])
        await self.service._send_online_player_response(
            "vanilla", {"requestId": socket.sent[0]["id"], "onlineCount": 1,
                        "playerList": ["Steve"]}, "response-board",
        )
        await task
        self.assertTrue(self.fake_matrix.uploads[0][0].startswith(b"\x89PNG\r\n\x1a\n"))
        content = self.fake_matrix.sent[-1]["content"]
        self.assertEqual("m.image", content["msgtype"])
        self.assertEqual("$board", content["m.relates_to"]["event_id"])

    async def test_minecraft_game_command_is_shared_without_relaying_original_chat(self) -> None:
        vanilla, survival = FakeWebSocket(), FakeWebSocket()
        self.service.servers = {"vanilla": vanilla, "survival": survival}
        await self.service._forward_player_event(
            self.service.database.get_server("vanilla"), "player_chat",
            {"playerName": "Steve", "message": {"plain": "#game roll"}}, "roll-id",
        )
        self.assertEqual(1, len(self.fake_matrix.sent))
        self.assertIn("Steve 掷出了", self.fake_matrix.sent[0]["content"]["body"])
        self.assertEqual("matrix_message", vanilla.sent[0]["type"])
        self.assertEqual("matrix_message", survival.sent[0]["type"])

    async def test_matrix_game_uses_bound_game_id(self) -> None:
        self.service.database.save_binding(
            "@member:example.org", "vanilla", "Steve", "uuid-steve", "scoped"
        )
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        event = SimpleNamespace(
            sender="@member:example.org", event_id="$game-bound", body="#game roll",
            source={"content": {}},
        )
        await self.service._on_matrix_text(room, event)
        body = self.fake_matrix.sent[-1]["content"]["body"]
        self.assertIn("Steve 掷出了", body)
        self.assertNotIn("member", body)

    async def test_online_board_accepts_cumulative_leaderboard_from_plugin(self) -> None:
        room = MatrixRoom("!room:example.org", "@bot:example.org")
        socket = FakeWebSocket()
        self.service.servers = {"vanilla": socket}
        event = SimpleNamespace(
            sender="@member:example.org", event_id="$board-duration", body="#online",
            source={"content": {}},
        )
        captured = []

        def capture_board(entries):
            captured.extend(entries)
            return render_online_board(entries)

        with patch("app.service.render_online_board", side_effect=capture_board):
            task = asyncio.create_task(self.service._on_matrix_text(room, event))
            for _ in range(50):
                if socket.sent:
                    break
                await asyncio.sleep(0.01)
            await self.service._send_online_player_response(
                "vanilla", {
                "requestId": socket.sent[0]["id"], "onlineCount": 1,
                "playerList": ["Steve"],
                "leaderboardPlayers": [
                    {"name": "Alex", "totalPlaySeconds": 7200, "online": False},
                    {"name": "Steve", "totalPlaySeconds": 3600, "online": True},
                ],
                }, "response-duration",
            )
            await task
        vanilla = next(item for item in captured if item.server_id == "vanilla")
        self.assertEqual(["Alex", "Steve"], [player.name for player in vanilla.players])
        self.assertEqual(7200, vanilla.players[0].total_seconds)
        self.assertFalse(vanilla.players[0].online)
        self.assertTrue(vanilla.players[1].online)
        self.assertEqual("m.image", self.fake_matrix.sent[-1]["content"]["msgtype"])

    def test_authenticated_hello_updates_plugin_name_and_game_port(self) -> None:
        self.service._validate_hello(
            "vanilla",
            {
                "serverName": "插件原版服",
                "relayGroup": "main",
                "gamePort": 25570,
            },
        )
        server = self.service.database.get_server("vanilla")
        self.assertEqual("插件原版服", server.display_name)
        self.assertEqual(25570, server.game_port)


if __name__ == "__main__":
    unittest.main()
