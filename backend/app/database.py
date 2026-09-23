from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Server:
    server_id: str
    display_name: str
    configured_name: str
    display_name_override: str | None
    game_port: int | None
    configured_port: int | None
    game_port_override: int | None
    token_hash: str
    enabled: bool
    relay_group: str
    last_seen: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class AuditEntry:
    actor_matrix_id: str
    server_id: str
    action: str
    old_value: str | None
    new_value: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class MatrixAdmin:
    matrix_id: str
    role: str
    added_by: str
    added_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Binding:
    matrix_id: str
    server_id: str
    scope_id: str
    player_id: str
    player_uuid: str | None


@dataclass(frozen=True, slots=True)
class PlayerAvatar:
    player_id: str
    mojang_uuid: str | None
    texture_hash: str | None
    local_path: str | None
    mxc_uri: str | None
    refresh_failed: bool
    last_attempt: str | None
    last_success: str | None


@dataclass(frozen=True, slots=True)
class ReconnectMute:
    server_id: str
    player_id: str
    muted_at: str
    last_event_at: str


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS servers (
                server_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                configured_name TEXT NOT NULL DEFAULT '',
                display_name_override TEXT,
                configured_port INTEGER,
                game_port_override INTEGER,
                token_hash TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                relay_group TEXT NOT NULL,
                last_seen TEXT,
                revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS server_admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matrix_event_id TEXT NOT NULL UNIQUE,
                actor_matrix_id TEXT NOT NULL,
                server_id TEXT NOT NULL,
                action TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS matrix_admins (
                matrix_id TEXT PRIMARY KEY,
                role TEXT NOT NULL CHECK(role IN ('owner', 'admin')),
                added_by TEXT NOT NULL,
                added_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS matrix_admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matrix_event_id TEXT NOT NULL UNIQUE,
                actor_matrix_id TEXT NOT NULL,
                target_matrix_id TEXT NOT NULL,
                action TEXT NOT NULL,
                old_role TEXT,
                new_role TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_events (
                source TEXT NOT NULL,
                event_id TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (source, event_id)
            );
            CREATE TABLE IF NOT EXISTS player_avatars (
                player_id TEXT PRIMARY KEY COLLATE NOCASE,
                mojang_uuid TEXT,
                texture_hash TEXT,
                local_path TEXT,
                mxc_uri TEXT,
                refresh_failed INTEGER NOT NULL DEFAULT 0,
                last_attempt TEXT,
                last_success TEXT
            );
            CREATE TABLE IF NOT EXISTS reconnect_mutes (
                server_id TEXT NOT NULL,
                player_id TEXT NOT NULL COLLATE NOCASE,
                muted_at TEXT NOT NULL,
                last_event_at TEXT NOT NULL,
                PRIMARY KEY (server_id, player_id)
            );
            """
        )
        server_columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(servers)")
        }
        additions = {
            "configured_name": "TEXT NOT NULL DEFAULT ''",
            "display_name_override": "TEXT",
            "configured_port": "INTEGER",
            "game_port_override": "INTEGER",
            "revision": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name not in server_columns:
                self._connection.execute(
                    f"ALTER TABLE servers ADD COLUMN {name} {declaration}"
                )
        reconnect_columns = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(reconnect_mutes)"
            )
        }
        if reconnect_columns and "last_event_at" not in reconnect_columns:
            self._connection.execute(
                "ALTER TABLE reconnect_mutes ADD COLUMN last_event_at TEXT"
            )
            self._connection.execute(
                "UPDATE reconnect_mutes SET last_event_at = muted_at "
                "WHERE last_event_at IS NULL"
            )
        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(bindings)")
        }
        if columns and "scope_id" not in columns:
            self._connection.executescript(
                """
                ALTER TABLE bindings RENAME TO bindings_v1;
                CREATE TABLE bindings (
                    matrix_id TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    player_id TEXT NOT NULL COLLATE NOCASE,
                    player_uuid TEXT,
                    bound_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (matrix_id, scope_id),
                    UNIQUE (scope_id, player_id)
                );
                INSERT INTO bindings(
                    matrix_id, server_id, scope_id, player_id, player_uuid,
                    bound_at, updated_at
                )
                SELECT matrix_id, '*', '*', player_id, player_uuid,
                       bound_at, updated_at
                FROM bindings_v1;
                DROP TABLE bindings_v1;
                """
            )
        elif not columns:
            self._connection.execute(
                """
                CREATE TABLE bindings (
                    matrix_id TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    player_id TEXT NOT NULL COLLATE NOCASE,
                    player_uuid TEXT,
                    bound_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (matrix_id, scope_id),
                    UNIQUE (scope_id, player_id)
                )
                """
            )
        self._connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS bindings_scope_uuid_unique
            ON bindings(scope_id, player_uuid)
            WHERE player_uuid IS NOT NULL AND player_uuid <> ''
            """
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        self._connection.execute(
            "DELETE FROM processed_events WHERE processed_at < ?", (cutoff,)
        )
        self._connection.commit()

    def get_matrix_admin(self, matrix_id: str) -> MatrixAdmin | None:
        row = self._connection.execute(
            """
            SELECT matrix_id, role, added_by, added_at, updated_at
            FROM matrix_admins WHERE matrix_id = ?
            """,
            (matrix_id,),
        ).fetchone()
        return self._matrix_admin(row)

    def all_matrix_admins(self) -> list[MatrixAdmin]:
        rows = self._connection.execute(
            """
            SELECT matrix_id, role, added_by, added_at, updated_at
            FROM matrix_admins
            ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END,
                     matrix_id COLLATE NOCASE
            """
        ).fetchall()
        return [self._matrix_admin(row) for row in rows if row is not None]

    def set_matrix_admin(
        self,
        matrix_id: str,
        role: str,
        actor_matrix_id: str,
        audit_event_id: str,
    ) -> bool:
        if role not in {"owner", "admin"}:
            raise ValueError("role must be owner or admin")
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT role FROM matrix_admins WHERE matrix_id = ?",
                (matrix_id,),
            ).fetchone()
            old_role = str(row["role"]) if row else None
            admin_count = self._connection.execute(
                "SELECT COUNT(*) FROM matrix_admins"
            ).fetchone()[0]
            if admin_count == 0 and role != "owner":
                self._connection.rollback()
                raise ValueError("the first administrator must be an owner")
            if old_role == "owner" and role != "owner":
                owners = self._connection.execute(
                    "SELECT COUNT(*) FROM matrix_admins WHERE role = 'owner'"
                ).fetchone()[0]
                if owners <= 1:
                    self._connection.rollback()
                    raise ValueError("cannot demote the last owner")
            self._connection.execute(
                """
                INSERT INTO matrix_admins(
                    matrix_id, role, added_by, added_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(matrix_id) DO UPDATE SET
                    role = excluded.role,
                    updated_at = excluded.updated_at
                """,
                (matrix_id, role, actor_matrix_id, now, now),
            )
            self._connection.execute(
                """
                INSERT INTO matrix_admin_audit(
                    matrix_event_id, actor_matrix_id, target_matrix_id,
                    action, old_role, new_role, created_at
                ) VALUES (?, ?, ?, 'set-admin', ?, ?, ?)
                """,
                (
                    audit_event_id,
                    actor_matrix_id,
                    matrix_id,
                    old_role,
                    role,
                    now,
                ),
            )
            self._connection.commit()
            return old_role != role
        except sqlite3.IntegrityError:
            self._connection.rollback()
            return False
        except Exception:
            self._connection.rollback()
            raise

    def remove_matrix_admin(
        self,
        matrix_id: str,
        actor_matrix_id: str,
        audit_event_id: str,
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT role FROM matrix_admins WHERE matrix_id = ?",
                (matrix_id,),
            ).fetchone()
            if row is None:
                self._connection.rollback()
                return False
            old_role = str(row["role"])
            if old_role == "owner":
                owners = self._connection.execute(
                    "SELECT COUNT(*) FROM matrix_admins WHERE role = 'owner'"
                ).fetchone()[0]
                if owners <= 1:
                    self._connection.rollback()
                    raise ValueError("cannot remove the last owner")
            self._connection.execute(
                "DELETE FROM matrix_admins WHERE matrix_id = ?", (matrix_id,)
            )
            self._connection.execute(
                """
                INSERT INTO matrix_admin_audit(
                    matrix_event_id, actor_matrix_id, target_matrix_id,
                    action, old_role, new_role, created_at
                ) VALUES (?, ?, ?, 'remove-admin', ?, NULL, ?)
                """,
                (audit_event_id, actor_matrix_id, matrix_id, old_role, now),
            )
            self._connection.commit()
            return True
        except sqlite3.IntegrityError:
            self._connection.rollback()
            return False
        except Exception:
            self._connection.rollback()
            raise

    def save_server(
        self,
        server_id: str,
        display_name: str,
        token_hash: str,
        relay_group: str,
        enabled: bool = True,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO servers(
                server_id, display_name, configured_name, token_hash,
                enabled, relay_group
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id) DO UPDATE SET
                display_name = excluded.display_name,
                configured_name = excluded.configured_name,
                token_hash = excluded.token_hash,
                enabled = excluded.enabled,
                relay_group = excluded.relay_group,
                revision = servers.revision + 1
            """,
            (
                server_id, display_name, display_name, token_hash,
                int(enabled), relay_group,
            ),
        )
        self._connection.commit()

    def get_server(self, server_id: str) -> Server | None:
        row = self._connection.execute(
            """
            SELECT *, COALESCE(
                NULLIF(display_name_override, ''),
                NULLIF(configured_name, ''),
                display_name
            ) AS effective_name,
            COALESCE(game_port_override, configured_port) AS effective_port
            FROM servers WHERE server_id = ?
            """,
            (server_id,),
        ).fetchone()
        return self._server(row)

    def all_servers(self) -> list[Server]:
        rows = self._connection.execute(
            """
            SELECT *, COALESCE(
                NULLIF(display_name_override, ''),
                NULLIF(configured_name, ''),
                display_name
            ) AS effective_name,
            COALESCE(game_port_override, configured_port) AS effective_port
            FROM servers ORDER BY server_id COLLATE NOCASE
            """
        ).fetchall()
        return [self._server(row) for row in rows if row is not None]

    def set_server_enabled(self, server_id: str, enabled: bool) -> bool:
        cursor = self._connection.execute(
            "UPDATE servers SET enabled = ? WHERE server_id = ?",
            (int(enabled), server_id),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def rotate_server_token(self, server_id: str, token_hash: str) -> bool:
        cursor = self._connection.execute(
            """
            UPDATE servers SET token_hash = ?, revision = revision + 1
            WHERE server_id = ?
            """,
            (token_hash, server_id),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def update_server_hello(
        self, server_id: str, configured_name: str, configured_port: int | None
    ) -> None:
        self._connection.execute(
            """
            UPDATE servers
            SET configured_name = ?, configured_port = ?,
                revision = revision + CASE
                    WHEN configured_name <> ?
                      OR configured_port IS NOT ? THEN 1 ELSE 0 END
            WHERE server_id = ?
            """,
            (
                configured_name,
                configured_port,
                configured_name,
                configured_port,
                server_id,
            ),
        )
        self._connection.commit()

    def admin_update_server(
        self,
        server_id: str,
        field: str,
        value: str | int | bool | None,
        actor_matrix_id: str,
        matrix_event_id: str,
        action: str,
    ) -> bool:
        allowed = {
            "display_name_override",
            "game_port_override",
            "relay_group",
            "enabled",
        }
        if field not in allowed:
            raise ValueError(f"unsupported server field: {field}")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                f"SELECT {field} AS value FROM servers WHERE server_id = ?",
                (server_id,),
            ).fetchone()
            if row is None:
                self._connection.rollback()
                return False
            old_value = None if row["value"] is None else str(row["value"])
            stored_value = int(value) if isinstance(value, bool) else value
            self._connection.execute(
                f"""
                UPDATE servers SET {field} = ?, revision = revision + 1
                WHERE server_id = ?
                """,
                (stored_value, server_id),
            )
            self._connection.execute(
                """
                INSERT INTO server_admin_audit(
                    matrix_event_id, actor_matrix_id, server_id, action,
                    old_value, new_value, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    matrix_event_id,
                    actor_matrix_id,
                    server_id,
                    action,
                    old_value,
                    None if stored_value is None else str(stored_value),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._connection.commit()
            return True
        except sqlite3.IntegrityError:
            self._connection.rollback()
            return True
        except Exception:
            self._connection.rollback()
            raise

    def recent_audit(
        self, server_id: str | None = None, limit: int = 10
    ) -> list[AuditEntry]:
        if server_id:
            rows = self._connection.execute(
                """
                SELECT actor_matrix_id, server_id, action, old_value,
                       new_value, created_at
                FROM server_admin_audit WHERE server_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (server_id, limit),
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT actor_matrix_id, server_id, action, old_value,
                       new_value, created_at
                FROM server_admin_audit ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            AuditEntry(
                actor_matrix_id=str(row["actor_matrix_id"]),
                server_id=str(row["server_id"]),
                action=str(row["action"]),
                old_value=str(row["old_value"]) if row["old_value"] else None,
                new_value=str(row["new_value"]) if row["new_value"] else None,
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def touch_server(self, server_id: str) -> None:
        self._connection.execute(
            "UPDATE servers SET last_seen = ? WHERE server_id = ?",
            (datetime.now(timezone.utc).isoformat(), server_id),
        )
        self._connection.commit()

    def get_bindings_by_matrix(self, matrix_id: str) -> list[Binding]:
        rows = self._connection.execute(
            """
            SELECT matrix_id, server_id, scope_id, player_id, player_uuid
            FROM bindings WHERE matrix_id = ? ORDER BY server_id COLLATE NOCASE
            """,
            (matrix_id,),
        ).fetchall()
        return [self._binding(row) for row in rows if row is not None]

    def get_binding_by_matrix(
        self, matrix_id: str, server_id: str | None = None
    ) -> Binding | None:
        if server_id is None:
            row = self._connection.execute(
                """
                SELECT matrix_id, server_id, scope_id, player_id, player_uuid
                FROM bindings WHERE matrix_id = ?
                ORDER BY CASE WHEN scope_id = '*' THEN 0 ELSE 1 END, server_id
                LIMIT 1
                """,
                (matrix_id,),
            ).fetchone()
        else:
            row = self._connection.execute(
                """
                SELECT matrix_id, server_id, scope_id, player_id, player_uuid
                FROM bindings
                WHERE matrix_id = ? AND scope_id IN (?, '*')
                ORDER BY CASE WHEN scope_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (matrix_id, server_id, server_id),
            ).fetchone()
        return self._binding(row)

    def get_binding_by_player(
        self, player_id: str, server_id: str | None = None
    ) -> Binding | None:
        if server_id is None:
            rows = self._connection.execute(
                """
                SELECT matrix_id, server_id, scope_id, player_id, player_uuid
                FROM bindings WHERE player_id = ? COLLATE NOCASE
                """,
                (player_id,),
            ).fetchall()
            matrix_ids = {str(row["matrix_id"]) for row in rows}
            row = rows[0] if len(matrix_ids) == 1 and rows else None
        else:
            row = self._connection.execute(
                """
                SELECT matrix_id, server_id, scope_id, player_id, player_uuid
                FROM bindings
                WHERE player_id = ? COLLATE NOCASE AND scope_id IN (?, '*')
                ORDER BY CASE WHEN scope_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (player_id, server_id, server_id),
            ).fetchone()
        return self._binding(row)

    def all_bindings(self) -> list[Binding]:
        rows = self._connection.execute(
            """
            SELECT matrix_id, server_id, scope_id, player_id, player_uuid
            FROM bindings
            """
        ).fetchall()
        return [self._binding(row) for row in rows if row is not None]

    def mute_reconnect_player(self, server_id: str, player_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO reconnect_mutes(
                server_id, player_id, muted_at, last_event_at
            ) VALUES (?, ?, ?, ?)
            """,
            (server_id, player_id, now, now),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def is_reconnect_muted(self, server_id: str, player_id: str) -> bool:
        row = self._connection.execute(
            """
            SELECT 1 FROM reconnect_mutes
            WHERE server_id = ? AND player_id = ? COLLATE NOCASE
            """,
            (server_id, player_id),
        ).fetchone()
        return row is not None

    def refresh_reconnect_mute(
        self,
        server_id: str,
        player_id: str,
        quiet_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(timezone.utc)
        row = self._connection.execute(
            """
            SELECT muted_at, last_event_at FROM reconnect_mutes
            WHERE server_id = ? AND player_id = ? COLLATE NOCASE
            """,
            (server_id, player_id),
        ).fetchone()
        if row is None:
            return False
        raw_last_event = str(row["last_event_at"] or row["muted_at"])
        last_event = datetime.fromisoformat(raw_last_event)
        if current - last_event >= timedelta(seconds=quiet_seconds):
            self.clear_reconnect_mute(server_id, player_id)
            return False
        self._connection.execute(
            """
            UPDATE reconnect_mutes SET last_event_at = ?
            WHERE server_id = ? AND player_id = ? COLLATE NOCASE
            """,
            (current.isoformat(), server_id, player_id),
        )
        self._connection.commit()
        return True

    def expire_reconnect_mutes(
        self, quiet_seconds: int, now: datetime | None = None
    ) -> int:
        current = now or datetime.now(timezone.utc)
        cutoff = (current - timedelta(seconds=quiet_seconds)).isoformat()
        cursor = self._connection.execute(
            "DELETE FROM reconnect_mutes WHERE last_event_at <= ?",
            (cutoff,),
        )
        self._connection.commit()
        return cursor.rowcount

    def clear_reconnect_mute(self, server_id: str, player_id: str) -> bool:
        cursor = self._connection.execute(
            """
            DELETE FROM reconnect_mutes
            WHERE server_id = ? AND player_id = ? COLLATE NOCASE
            """,
            (server_id, player_id),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def clear_reconnect_mutes_for_binding(self, binding: Binding) -> int:
        if binding.scope_id == "*":
            cursor = self._connection.execute(
                "DELETE FROM reconnect_mutes WHERE player_id = ? COLLATE NOCASE",
                (binding.player_id,),
            )
        else:
            cursor = self._connection.execute(
                """
                DELETE FROM reconnect_mutes
                WHERE server_id = ? AND player_id = ? COLLATE NOCASE
                """,
                (binding.server_id, binding.player_id),
            )
        self._connection.commit()
        return cursor.rowcount

    def all_reconnect_mutes(self) -> list[ReconnectMute]:
        rows = self._connection.execute(
            """
            SELECT server_id, player_id, muted_at, last_event_at
            FROM reconnect_mutes
            ORDER BY muted_at DESC
            """
        ).fetchall()
        return [
            ReconnectMute(
                server_id=str(row["server_id"]),
                player_id=str(row["player_id"]),
                muted_at=str(row["muted_at"]),
                last_event_at=str(row["last_event_at"] or row["muted_at"]),
            )
            for row in rows
        ]

    def save_binding(
        self,
        matrix_id: str,
        server_id: str,
        player_id: str,
        player_uuid: str | None,
        binding_mode: str,
    ) -> None:
        scope_id = "*" if binding_mode == "online_uuid" else server_id
        normalized_uuid = player_uuid or None
        if binding_mode == "online_uuid" and normalized_uuid is None:
            raise ValueError("online_uuid binding requires player UUID")
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """
                DELETE FROM bindings
                WHERE scope_id = ? AND matrix_id <> ?
                  AND (player_id = ? COLLATE NOCASE
                       OR (? IS NOT NULL AND player_uuid = ?))
                """,
                (scope_id, matrix_id, player_id, normalized_uuid, normalized_uuid),
            )
            self._connection.execute(
                """
                INSERT INTO bindings(
                    matrix_id, server_id, scope_id, player_id, player_uuid,
                    bound_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(matrix_id, scope_id) DO UPDATE SET
                    server_id = excluded.server_id,
                    player_id = excluded.player_id,
                    player_uuid = excluded.player_uuid,
                    updated_at = excluded.updated_at
                """,
                (
                    matrix_id, server_id, scope_id, player_id, normalized_uuid,
                    now, now,
                ),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def get_setting(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._connection.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self._connection.commit()

    def delete_setting(self, key: str) -> None:
        self._connection.execute("DELETE FROM settings WHERE key = ?", (key,))
        self._connection.commit()

    def is_processed(self, source: str, event_id: str) -> bool:
        row = self._connection.execute(
            """
            SELECT 1 FROM processed_events
            WHERE source = ? AND event_id = ?
            """,
            (source, event_id),
        ).fetchone()
        return row is not None

    def mark_processed(self, source: str, event_id: str) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO processed_events(source, event_id, processed_at)
            VALUES (?, ?, ?)
            """,
            (source, event_id, datetime.now(timezone.utc).isoformat()),
        )
        self._connection.commit()

    def get_player_avatar(self, player_id: str) -> PlayerAvatar | None:
        row = self._connection.execute(
            """
            SELECT player_id, mojang_uuid, texture_hash, local_path, mxc_uri,
                   refresh_failed, last_attempt, last_success
            FROM player_avatars WHERE player_id = ? COLLATE NOCASE
            """,
            (player_id,),
        ).fetchone()
        return self._player_avatar(row)

    def all_player_avatars(self) -> list[PlayerAvatar]:
        rows = self._connection.execute(
            """
            SELECT player_id, mojang_uuid, texture_hash, local_path, mxc_uri,
                   refresh_failed, last_attempt, last_success
            FROM player_avatars ORDER BY player_id COLLATE NOCASE
            """
        ).fetchall()
        return [self._player_avatar(row) for row in rows if row is not None]

    def save_player_avatar(
        self,
        player_id: str,
        mojang_uuid: str,
        texture_hash: str,
        local_path: str,
        mxc_uri: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._connection.execute(
            """
            INSERT INTO player_avatars(
                player_id, mojang_uuid, texture_hash, local_path, mxc_uri,
                refresh_failed, last_attempt, last_success
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
                player_id = excluded.player_id,
                mojang_uuid = excluded.mojang_uuid,
                texture_hash = excluded.texture_hash,
                local_path = excluded.local_path,
                mxc_uri = excluded.mxc_uri,
                refresh_failed = 0,
                last_attempt = excluded.last_attempt,
                last_success = excluded.last_success
            """,
            (
                player_id,
                mojang_uuid,
                texture_hash,
                local_path,
                mxc_uri,
                now,
                now,
            ),
        )
        self._connection.commit()

    def mark_player_avatar_failure(self, player_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._connection.execute(
            """
            INSERT INTO player_avatars(
                player_id, refresh_failed, last_attempt
            ) VALUES (?, 1, ?)
            ON CONFLICT(player_id) DO UPDATE SET
                player_id = excluded.player_id,
                refresh_failed = 1,
                last_attempt = excluded.last_attempt
            """,
            (player_id, now),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _server(row: sqlite3.Row | None) -> Server | None:
        if row is None:
            return None
        return Server(
            server_id=str(row["server_id"]),
            display_name=str(row["effective_name"]),
            configured_name=str(row["configured_name"] or row["display_name"]),
            display_name_override=(
                str(row["display_name_override"])
                if row["display_name_override"]
                else None
            ),
            game_port=int(row["effective_port"]) if row["effective_port"] else None,
            configured_port=(
                int(row["configured_port"])
                if row["configured_port"] is not None
                else None
            ),
            game_port_override=(
                int(row["game_port_override"])
                if row["game_port_override"] is not None
                else None
            ),
            token_hash=str(row["token_hash"]),
            enabled=bool(row["enabled"]),
            relay_group=str(row["relay_group"]),
            last_seen=str(row["last_seen"]) if row["last_seen"] else None,
            revision=int(row["revision"]),
        )

    @staticmethod
    def _binding(row: sqlite3.Row | None) -> Binding | None:
        if row is None:
            return None
        return Binding(
            matrix_id=str(row["matrix_id"]),
            server_id=str(row["server_id"]),
            scope_id=str(row["scope_id"]),
            player_id=str(row["player_id"]),
            player_uuid=str(row["player_uuid"]) if row["player_uuid"] else None,
        )

    @staticmethod
    def _matrix_admin(row: sqlite3.Row | None) -> MatrixAdmin | None:
        if row is None:
            return None
        return MatrixAdmin(
            matrix_id=str(row["matrix_id"]),
            role=str(row["role"]),
            added_by=str(row["added_by"]),
            added_at=str(row["added_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _player_avatar(row: sqlite3.Row | None) -> PlayerAvatar | None:
        if row is None:
            return None
        return PlayerAvatar(
            player_id=str(row["player_id"]),
            mojang_uuid=(str(row["mojang_uuid"]) if row["mojang_uuid"] else None),
            texture_hash=(
                str(row["texture_hash"]) if row["texture_hash"] else None
            ),
            local_path=str(row["local_path"]) if row["local_path"] else None,
            mxc_uri=str(row["mxc_uri"]) if row["mxc_uri"] else None,
            refresh_failed=bool(row["refresh_failed"]),
            last_attempt=(
                str(row["last_attempt"]) if row["last_attempt"] else None
            ),
            last_success=(
                str(row["last_success"]) if row["last_success"] else None
            ),
        )
