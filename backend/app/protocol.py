from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any


PROTOCOL_VERSION = 2


class ProtocolError(ValueError):
    pass


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def make_envelope(
    message_type: str,
    payload: dict[str, Any],
    server_id: str = "*",
    message_id: str | None = None,
) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "id": message_id or str(uuid.uuid4()),
        "type": message_type,
        "timestamp": iso_now(),
        "serverId": server_id,
        "payload": payload,
    }


def encode(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def decode(raw_message: str, default_server_id: str) -> dict[str, Any]:
    try:
        data = json.loads(raw_message)
    except (TypeError, json.JSONDecodeError) as error:
        raise ProtocolError("invalid JSON") from error
    if not isinstance(data, dict):
        raise ProtocolError("envelope must be an object")
    if data.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    for key in ("id", "type"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ProtocolError(f"{key} must be a non-empty string")
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise ProtocolError("payload must be an object")
    server_id = data.get("serverId", default_server_id)
    if not isinstance(server_id, str) or not server_id:
        server_id = default_server_id
    data["serverId"] = server_id
    return data


def extract_player_name(payload: dict[str, Any]) -> str:
    for key in ("playerName", "playerDisplayName", "player_name", "sender"):
        value = payload.get(key)
        if value is not None and str(value).strip() not in {"", "{}", "null", "None"}:
            return str(value).strip()
    return "未知玩家"


def extract_plain_text(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    if isinstance(message, dict):
        value = message.get("plain", "")
    elif isinstance(message, str):
        value = message
    else:
        value = ""
    text = str(value).strip()
    return "" if text in {"{}", "null", "None"} else text
