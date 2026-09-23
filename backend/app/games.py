"""Small, shared games for Matrix and Minecraft chat."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


HELP = (
    "🎲 跨服小游戏\n"
    "#game roll  掷一枚六面骰\n"
    "#game coin  抛硬币\n"
    "#game guess start  开始一局 1–100 猜数字\n"
    "#game guess <数字>  参与猜数字\n"
    "#game guess status  查看本组进度\n"
    "聊天室用 #game，游戏里也可直接输入 #game；两边参与同一局。"
)


@dataclass(slots=True)
class GuessRound:
    answer: int
    started_at: float
    attempts: int = 0


class GameManager:
    def __init__(self) -> None:
        self.rounds: dict[str, GuessRound] = {}
        self.last_action: dict[str, float] = {}

    def play(self, group: str, actor_id: str, actor_name: str, text: str) -> tuple[str, bool]:
        """Return (response, broadcast). Help/errors are private to the caller."""
        parts = text.strip().split()
        if len(parts) < 2 or parts[1].casefold() in {"help", "帮助"}:
            return HELP, False
        action = parts[1].casefold()
        now = time.monotonic()
        key = f"{group}:{actor_id}"
        if action == "guess" and len(parts) >= 3 and parts[2].casefold() == "status":
            round_ = self._active_round(group, now)
            return (
                f"🎯 本组正在猜 1–100，已猜 {round_.attempts} 次。" if round_
                else "🎯 本组暂无猜数字游戏，发送 #game guess start 开局。",
                False,
            )
        if now - self.last_action.get(key, -1e9) < 3:
            return "⏳ 操作太快，请 3 秒后再试。", False
        if action == "roll" and len(parts) == 2:
            self.last_action[key] = now
            return f"🎲 {actor_name} 掷出了 {secrets.randbelow(6) + 1} 点。", True
        if action == "coin" and len(parts) == 2:
            self.last_action[key] = now
            side = ("正面", "反面")[secrets.randbelow(2)]
            return f"🪙 {actor_name} 抛出硬币：{side}。", True
        if action == "guess" and len(parts) == 3:
            if parts[2].casefold() == "start":
                if self._active_round(group, now):
                    return "🎯 本组已有一局进行中，发送 #game guess status 查看。", False
                self.last_action[key] = now
                self.rounds[group] = GuessRound(secrets.randbelow(100) + 1, now)
                return f"🎯 {actor_name} 开始了猜数字！范围 1–100，15 分钟内发送 #game guess <数字>。", True
            if not parts[2].isdigit() or not 1 <= int(parts[2]) <= 100:
                return "请输入 1–100 的整数，例如 #game guess 42。", False
            round_ = self._active_round(group, now)
            if round_ is None:
                return "🎯 本组暂无猜数字游戏，发送 #game guess start 开局。", False
            self.last_action[key] = now
            value = int(parts[2])
            round_.attempts += 1
            if value == round_.answer:
                del self.rounds[group]
                return f"🎉 {actor_name} 猜中了 {value}！本局共猜 {round_.attempts} 次。", True
            hint = "大了" if value > round_.answer else "小了"
            return f"🎯 {actor_name} 猜 {value}：{hint}！", True
        return HELP, False

    def _active_round(self, group: str, now: float) -> GuessRound | None:
        round_ = self.rounds.get(group)
        if round_ and now - round_.started_at >= 900:
            self.rounds.pop(group, None)
            return None
        return round_
