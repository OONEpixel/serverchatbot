import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.database import Database


class DatabaseTest(unittest.TestCase):
    def test_servers_and_scoped_bindings_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.db"
            database = Database(path)
            database.save_server("vanilla", "原版服", "hash-a", "main")
            database.save_server("survival", "生存服", "hash-b", "main")
            database.save_binding(
                "@alice:example.org", "vanilla", "Steve", "uuid-a", "scoped"
            )
            database.save_binding(
                "@alice:example.org", "survival", "Alex", "uuid-b", "scoped"
            )
            database.close()

            reopened = Database(path)
            self.assertEqual("原版服", reopened.get_server("vanilla").display_name)
            self.assertEqual(
                "Steve",
                reopened.get_binding_by_matrix(
                    "@alice:example.org", "vanilla"
                ).player_id,
            )
            self.assertEqual(2, len(reopened.get_bindings_by_matrix("@alice:example.org")))
            reopened.close()

    def test_online_uuid_binding_is_global_and_one_to_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "bridge.db")
            database.save_binding(
                "@alice:example.org", "vanilla", "Steve", "uuid-steve", "online_uuid"
            )
            database.save_binding(
                "@bob:example.org", "survival", "Steve", "uuid-steve", "online_uuid"
            )
            self.assertIsNone(database.get_binding_by_matrix("@alice:example.org"))
            self.assertEqual(
                "@bob:example.org",
                database.get_binding_by_player("STEVE", "vanilla").matrix_id,
            )
            database.close()

    def test_legacy_bindings_are_migrated_as_global(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE bindings (
                    matrix_id TEXT PRIMARY KEY,
                    player_id TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    player_uuid TEXT,
                    bound_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO bindings VALUES(
                    '@alice:example.org', 'Steve', 'uuid-steve', 'now', 'now'
                );
                """
            )
            connection.close()
            database = Database(path)
            binding = database.get_binding_by_matrix("@alice:example.org", "any")
            self.assertEqual("*", binding.scope_id)
            self.assertEqual("Steve", binding.player_id)
            database.close()

    def test_settings_processed_events_and_server_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "bridge.db")
            database.set_setting("sync", "token")
            database.mark_processed("minecraft", "event-1")
            database.save_server(
                "vanilla", "原版服", hashlib.sha256(b"token").hexdigest(), "main"
            )
            database.touch_server("vanilla")
            self.assertEqual("token", database.get_setting("sync"))
            self.assertTrue(database.is_processed("minecraft", "event-1"))
            self.assertIsNotNone(database.get_server("vanilla").last_seen)
            self.assertTrue(database.set_server_enabled("vanilla", False))
            self.assertFalse(database.get_server("vanilla").enabled)
            database.close()

    def test_matrix_admin_identity_is_persistent_and_last_owner_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.db"
            database = Database(path)
            database.set_matrix_admin(
                "@owner:example.org", "owner", "console", "console:1"
            )
            database.set_matrix_admin(
                "@helper:example.org", "admin", "@owner:example.org", "$event-1"
            )
            with self.assertRaises(ValueError):
                database.remove_matrix_admin(
                    "@owner:example.org", "console", "console:2"
                )
            database.close()

            reopened = Database(path)
            self.assertEqual(
                "owner", reopened.get_matrix_admin("@owner:example.org").role
            )
            self.assertEqual(2, len(reopened.all_matrix_admins()))
            reopened.close()

    def test_first_matrix_admin_must_be_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "bridge.db")
            with self.assertRaises(ValueError):
                database.set_matrix_admin(
                    "@helper:example.org", "admin", "console", "console:1"
                )
            database.close()

    def test_player_avatar_cache_survives_failure_and_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.db"
            database = Database(path)
            database.save_player_avatar(
                "Steve",
                "0123456789abcdef0123456789abcdef",
                "a" * 64,
                "/data/avatars/a.png",
                "mxc://example.org/avatar",
            )
            database.mark_player_avatar_failure("Steve")
            failed = database.get_player_avatar("steve")
            self.assertTrue(failed.refresh_failed)
            self.assertEqual("mxc://example.org/avatar", failed.mxc_uri)
            database.close()

            reopened = Database(path)
            self.assertEqual(1, len(reopened.all_player_avatars()))
            self.assertEqual(
                "a" * 64, reopened.get_player_avatar("STEVE").texture_hash
            )
            reopened.close()


if __name__ == "__main__":
    unittest.main()
