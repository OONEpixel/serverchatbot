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
    avatar_directory: Path = Path("/data/avatars")
    avatar_lookup_timeout_seconds: int = 8

    @classmethod
    def from_environment(cls) -> "Settings":
        settings = cls(
            matrix_url=os.getenv("MATRIX_URL", "").rstrip("/"),
            matrix_id=os.getenv("MATRIX_ID", ""),
            matrix_password=_secret("MATRIX_PASSWORD"),
            room_id=os.getenv("ROOM_ID", ""),
            ws_host=os.getenv("WS_HOST", "0.0.0.0"),
            ws_port=_positive_int("WS_PORT", 8765),
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
            avatar_directory=Path(
                os.getenv("AVATAR_DIRECTORY", "/data/avatars")
            ),
            avatar_lookup_timeout_seconds=_positive_int(
                "AVATAR_LOOKUP_TIMEOUT_SECONDS", 8
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
        return settings
