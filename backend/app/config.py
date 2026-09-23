from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _secret(name: str, default: str = "") -> str:
    direct = os.getenv(name)
    if direct is not None:
        return direct
    secret_file = os.getenv(f"{name}_FILE")
    if secret_file:
        return Path(secret_file).read_text(encoding="utf-8").rstrip("\r\n")
    return default


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, "true" if default else "false").strip().casefold()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class Settings:
    matrix_url: str
    matrix_id: str
    matrix_password: str
    room_id: str
    ws_host: str
    ws_port: int
    database_path: Path
    max_matrix_message_length: int
    bind_token_ttl_seconds: int
    unbound_reminder_cooldown_seconds: int
    matrix_relay_group: str
    binding_mode: str
    admin_power_level: int
    avatar_enabled: bool = True
    avatar_display_mode: str = "text"
    avatar_directory: Path = Path("/data/avatars")
    avatar_lookup_timeout_seconds: int = 8
    reconnect_guard_enabled: bool = True
    reconnect_guard_window_seconds: int = 600
    reconnect_guard_min_session_seconds: int = 60
    reconnect_guard_max_session_seconds: int = 180
    reconnect_guard_threshold: int = 2
    reconnect_guard_quiet_seconds: int = 900
    reconnect_guard_rapid_window_seconds: int = 60
    reconnect_guard_rapid_max_session_seconds: int = 30
    reconnect_guard_rapid_threshold: int = 3
    server_health_enabled: bool = True
    server_health_grace_seconds: int = 30
    server_health_min_notice_interval_seconds: int = 60

    @classmethod
    def from_environment(cls) -> "Settings":
        settings = cls(
            matrix_url=os.getenv("MATRIX_URL", "").rstrip("/"),
            matrix_id=os.getenv("MATRIX_ID", ""),
            matrix_password=_secret("MATRIX_PASSWORD"),
            room_id=os.getenv("ROOM_ID", ""),
            ws_host=os.getenv("WS_HOST", "0.0.0.0"),
            ws_port=_positive_int("WS_PORT", 8766),
            database_path=Path(os.getenv("DATABASE_PATH", "/data/bridge.db")),
            max_matrix_message_length=_positive_int("MAX_MATRIX_MESSAGE_LENGTH", 256),
            bind_token_ttl_seconds=_positive_int("BIND_TOKEN_TTL_SECONDS", 600),
            unbound_reminder_cooldown_seconds=_positive_int(
                "UNBOUND_REMINDER_COOLDOWN_SECONDS", 300
            ),
            matrix_relay_group=os.getenv("MATRIX_RELAY_GROUP", "main").strip(),
            binding_mode=os.getenv("BINDING_MODE", "scoped").strip().casefold(),
            admin_power_level=_positive_int("ADMIN_POWER_LEVEL", 50),
            avatar_enabled=_boolean("AVATAR_ENABLED", True),
            avatar_display_mode=os.getenv(
                "AVATAR_DISPLAY_MODE", "text"
            ).strip().casefold(),
            avatar_directory=Path(
                os.getenv("AVATAR_DIRECTORY", "/data/avatars")
            ),
            avatar_lookup_timeout_seconds=_positive_int(
                "AVATAR_LOOKUP_TIMEOUT_SECONDS", 8
            ),
            reconnect_guard_enabled=_boolean("RECONNECT_GUARD_ENABLED", True),
            reconnect_guard_window_seconds=_positive_int(
                "RECONNECT_GUARD_WINDOW_SECONDS", 600
            ),
            reconnect_guard_min_session_seconds=_positive_int(
                "RECONNECT_GUARD_MIN_SESSION_SECONDS", 60
            ),
            reconnect_guard_max_session_seconds=_positive_int(
                "RECONNECT_GUARD_MAX_SESSION_SECONDS", 180
            ),
            reconnect_guard_threshold=_positive_int(
                "RECONNECT_GUARD_THRESHOLD", 2
            ),
            reconnect_guard_quiet_seconds=_positive_int(
                "RECONNECT_GUARD_QUIET_SECONDS", 900
            ),
            reconnect_guard_rapid_window_seconds=_positive_int(
                "RECONNECT_GUARD_RAPID_WINDOW_SECONDS", 60
            ),
            reconnect_guard_rapid_max_session_seconds=_positive_int(
                "RECONNECT_GUARD_RAPID_MAX_SESSION_SECONDS", 30
            ),
            reconnect_guard_rapid_threshold=_positive_int(
                "RECONNECT_GUARD_RAPID_THRESHOLD", 3
            ),
            server_health_enabled=_boolean("SERVER_HEALTH_ENABLED", True),
            server_health_grace_seconds=_positive_int(
                "SERVER_HEALTH_GRACE_SECONDS", 30
            ),
            server_health_min_notice_interval_seconds=_positive_int(
                "SERVER_HEALTH_MIN_NOTICE_INTERVAL_SECONDS", 60
            ),
        )
        if not settings.matrix_relay_group:
            raise ValueError("MATRIX_RELAY_GROUP must not be blank")
        if not settings.matrix_url.startswith("https://"):
            raise ValueError("MATRIX_URL must be an https:// URL")
        if not settings.matrix_id.startswith("@"):
            raise ValueError("MATRIX_ID must be configured")
        if not settings.room_id.startswith("!"):
            raise ValueError("ROOM_ID must be configured with an internal room ID")
        if settings.binding_mode not in {"scoped", "online_uuid"}:
            raise ValueError("BINDING_MODE must be scoped or online_uuid")
        if settings.admin_power_level > 100:
            raise ValueError("ADMIN_POWER_LEVEL must be at most 100")
        if settings.avatar_display_mode not in {"text", "emote"}:
            raise ValueError("AVATAR_DISPLAY_MODE must be text or emote")
        if (
            settings.reconnect_guard_min_session_seconds
            > settings.reconnect_guard_max_session_seconds
        ):
            raise ValueError(
                "RECONNECT_GUARD_MIN_SESSION_SECONDS must not exceed "
                "RECONNECT_GUARD_MAX_SESSION_SECONDS"
            )
        if (
            settings.reconnect_guard_window_seconds
            < settings.reconnect_guard_max_session_seconds
        ):
            raise ValueError(
                "RECONNECT_GUARD_WINDOW_SECONDS must be at least "
                "RECONNECT_GUARD_MAX_SESSION_SECONDS"
            )
        if (
            settings.reconnect_guard_rapid_window_seconds
            < settings.reconnect_guard_rapid_max_session_seconds
        ):
            raise ValueError(
                "RECONNECT_GUARD_RAPID_WINDOW_SECONDS must be at least "
                "RECONNECT_GUARD_RAPID_MAX_SESSION_SECONDS"
            )
        return settings
