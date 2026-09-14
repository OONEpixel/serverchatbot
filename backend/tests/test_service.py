import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from nio import MatrixRoom, RoomSendResponse, StickerEvent

from app.config import Settings
from app.protocol import encode, make_envelope
from app.service import BindToken, MatrixBridgeService, matrix_command


class FakeMatrixClient:
    def __init__(self) -> None:
        self.sent = []

    async def room_send(self, **kwargs):
        self.sent.append(kwargs)
        return RoomSendResponse("$event", kwargs["room_id"])


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def close(self, **kwargs) -> None:
        self.closed = kwargs


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        settings = Settings(
            matrix_url="https://matrix.example.org",
            matrix_id="@bot:example.org",
            matrix_password="password",
            room_id="!room:example.org",
            ws_host="127.0.0.1",
            ws_port=8765,
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
        self.assertIsNone(matrix_command("/bind"))

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
        self.assertNotIn("#server rename", public_body)
        self.assertNotIn("#admin add", public_body)

        self.service.database.set_matrix_admin(
            "@owner:example.org", "owner", "console", "setup:help-owner"
        )
        room.power_levels.users["@owner:example.org"] = 100
        await self.service._send_command_help(room, "@owner:example.org")
        owner_body = self.fake_matrix.sent[-1]["content"]["body"]
        self.assertIn("#server rename", owner_body)
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
