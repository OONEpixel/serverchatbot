from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class MentionTarget:
    reference: str
    matrix_id: str


def _avatar_html(player_name: str, avatar_mxc_uri: str | None) -> str:
    if not avatar_mxc_uri:
        return ""
    if not re.fullmatch(r"mxc://[^\s/]+/[^\s/]+", avatar_mxc_uri):
        return ""
    return (
        f'<img src="{html.escape(avatar_mxc_uri, quote=True)}" '
        f'width="24" height="24" alt="[{html.escape(player_name)}头像]" '
        f'title="{html.escape(player_name)}"> '
    )


def avatar_message_content(
    body: str, player_name: str, avatar_mxc_uri: str | None
) -> dict[str, object]:
    content: dict[str, object] = {
        "msgtype": "m.text",
        "body": body,
        "m.mentions": {},
    }
    avatar_html = _avatar_html(player_name, avatar_mxc_uri)
    if avatar_html:
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = avatar_html + html.escape(body)
    return content


def game_chat_content(
    server_name: str,
    player_name: str,
    message: str,
    targets: list[MentionTarget],
    avatar_mxc_uri: str | None = None,
) -> dict[str, object]:
    prefix = f"[{server_name}]<{player_name}> "
    body = prefix + message
    by_reference = {target.reference.casefold(): target for target in targets}
    avatar_html = _avatar_html(player_name, avatar_mxc_uri)
    if not by_reference and not avatar_html:
        return {"msgtype": "m.text", "body": body, "m.mentions": {}}

    alternatives = sorted(
        (re.escape(target.reference) for target in targets),
        key=len,
        reverse=True,
    )
    pattern = (
        re.compile(
            r"@(" + "|".join(alternatives) + r")(?![\w:-])", re.IGNORECASE
        )
        if alternatives
        else None
    )
    mentioned_users: list[str] = []
    html_parts = [avatar_html, html.escape(prefix)]
    cursor = 0
    for match in pattern.finditer(message) if pattern else ():
        target = by_reference.get(match.group(1).casefold())
        if target is None:
            continue
        html_parts.append(html.escape(message[cursor : match.start()]))
        matrix_uri = "https://matrix.to/#/" + quote(target.matrix_id, safe="@:")
        html_parts.append(
            f'<a href="{html.escape(matrix_uri, quote=True)}">'
            f"{html.escape(match.group(0))}</a>"
        )
        cursor = match.end()
        if target.matrix_id not in mentioned_users:
            mentioned_users.append(target.matrix_id)
    html_parts.append(html.escape(message[cursor:]))

    content: dict[str, object] = {
        "msgtype": "m.text",
        "body": body,
        "m.mentions": {"user_ids": mentioned_users} if mentioned_users else {},
    }
    if mentioned_users or avatar_html:
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = "".join(html_parts)
    return content


def replace_matrix_mentions(
    body: str, mentioned_user_ids: list[str], display_names: dict[str, str]
) -> str:
    result = body
    for matrix_id in sorted(mentioned_user_ids, key=len, reverse=True):
        player_name = display_names.get(matrix_id)
        if player_name and player_name != matrix_id:
            result = result.replace(matrix_id, f"@{player_name}")
    return result


def user_notice_content(matrix_id: str, text: str) -> dict[str, object]:
    matrix_uri = "https://matrix.to/#/" + quote(matrix_id, safe="@:")
    formatted_text = _formatted_multiline_text(text)
    return {
        "msgtype": "m.notice",
        "body": f"{matrix_id} {text}",
        "format": "org.matrix.custom.html",
        "formatted_body": (
            f'<a href="{html.escape(matrix_uri, quote=True)}">'
            f"{html.escape(matrix_id)}</a> {formatted_text}"
        ),
        "m.mentions": {"user_ids": [matrix_id]},
    }


def multiline_text_content(text: str) -> dict[str, object]:
    return {
        "msgtype": "m.text",
        "body": text,
        "format": "org.matrix.custom.html",
        "formatted_body": _formatted_multiline_text(text),
        "m.mentions": {},
    }


def _formatted_multiline_text(text: str) -> str:
    lines = text.split("\n")
    rendered: list[str] = []
    for index, line in enumerate(lines):
        escaped = html.escape(line)
        if not line:
            rendered.append("")
        elif index == 0 and len(lines) > 1:
            rendered.append(f"<strong>{escaped}</strong>")
        elif line.startswith("【") and line.endswith("】"):
            rendered.append(f"<strong>{escaped}</strong>")
        elif line.startswith(("#", "!")) and "  " in line:
            command, description = line.split("  ", 1)
            rendered.append(
                f"<code>{html.escape(command)}</code> — {html.escape(description)}"
            )
        else:
            rendered.append(escaped)
    return "<br>\n".join(rendered)


def bind_token_notice_content(
    matrix_id: str,
    token: str,
    ttl_seconds: int,
    current_player: str | None = None,
) -> dict[str, object]:
    matrix_uri = "https://matrix.to/#/" + quote(matrix_id, safe="@:")
    validity = (
        f"{ttl_seconds // 60} 分钟"
        if ttl_seconds % 60 == 0
        else f"{ttl_seconds} 秒"
    )
    current_text = f"当前已绑定 {current_player}。\n" if current_player else ""
    body = (
        f"{matrix_id} {current_text}绑定验证码（{validity}内有效）：\n"
        f"{token}\n"
        f"请在游戏中输入：/matrixbind {token}"
    )
    current_html = (
        f"当前已绑定 {html.escape(current_player)}。<br>"
        if current_player
        else ""
    )
    return {
        "msgtype": "m.notice",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": (
            f'<a href="{html.escape(matrix_uri, quote=True)}">'
            f"{html.escape(matrix_id)}</a> "
            f"{current_html}绑定验证码（{validity}内有效）："
            f"<pre><code>{html.escape(token)}</code></pre>"
            f"请在游戏中输入：<code>/matrixbind {html.escape(token)}</code>"
        ),
        "m.mentions": {"user_ids": [matrix_id]},
    }
