"""접속 중인 참가자를 기존 음성 채널로 옮긴다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import discord


@dataclass(frozen=True, slots=True)
class VoiceMoveSummary:
    """음성 채널 배치 결과 인원수."""

    success: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def moved(self) -> int:
        return self.success

    def __getitem__(self, key: str) -> int:
        return getattr(self, key)


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    try:
        return value[name]
    except (KeyError, IndexError, TypeError):
        return getattr(value, name, default)


def voice_move_plan(
    participants: Iterable[Any],
    connected_user_ids: Iterable[int],
) -> list[tuple[int, str]]:
    """접속한 사용자 기준으로 ``(user_id, team)`` 배치 목록을 만든다.

    Gateway 연결 없이도 테스트하려고 참가자 데이터만 받는다.
    """

    connected = {int(user_id) for user_id in connected_user_ids}
    return [
        (int(_value(item, "user_id")), str(_value(item, "team")))
        for item in participants
        if int(_value(item, "user_id")) in connected
        and _value(item, "team") in {"A", "B"}
    ]


def _voice_channel(guild: Any, channel_id: int | None) -> Any | None:
    if channel_id is None or guild is None:
        return None
    channel = guild.get_channel(int(channel_id))
    return channel if isinstance(channel, discord.VoiceChannel) else None


def _connected_members(guild: Any) -> dict[int, Any]:
    connected: dict[int, Any] = {}
    for channel in getattr(guild, "voice_channels", ()) or ():
        for member in getattr(channel, "members", ()) or ():
            member_id = getattr(member, "id", None)
            if member_id is not None:
                connected[int(member_id)] = member
    return connected


def _bot_member(guild: Any) -> Any | None:
    member = getattr(guild, "me", None)
    if member is not None:
        return member
    user = getattr(guild, "_state", None)
    user = getattr(user, "user", None)
    getter = getattr(guild, "get_member", None)
    return getter(getattr(user, "id", 0)) if getter and user else None


def _can_move(guild: Any, channel: Any) -> bool:
    me = _bot_member(guild)
    permissions_for = getattr(channel, "permissions_for", None)
    if me is None or not callable(permissions_for):
        return False
    permissions = permissions_for(me)
    return bool(getattr(permissions, "connect", False) and getattr(permissions, "move_members", False))


async def move_match_participants(
    guild: Any,
    match: Any,
    team_a_voice_channel_id: int | None,
    team_b_voice_channel_id: int | None,
) -> VoiceMoveSummary:
    """접속 중인 참가자를 기존 A/B 음성 채널로 옮긴다.

    설정이나 권한이 없으면 넘어간다. 한 명 이동 실패로 전체를 중단하지 않는다.
    """

    if team_a_voice_channel_id is None or team_b_voice_channel_id is None:
        return VoiceMoveSummary()
    channel_a = _voice_channel(guild, team_a_voice_channel_id)
    channel_b = _voice_channel(guild, team_b_voice_channel_id)
    participants = tuple(_value(match, "participants", ()) or ())
    if channel_a is None or channel_b is None:
        return VoiceMoveSummary(skipped=len(participants))
    if not (_can_move(guild, channel_a) and _can_move(guild, channel_b)):
        return VoiceMoveSummary(failed=len(participants))

    connected = _connected_members(guild)
    success = skipped = failed = 0
    for user_id, team in voice_move_plan(participants, connected):
        member = connected.get(user_id)
        target = channel_a if team == "A" else channel_b
        try:
            await member.move_to(target)
        except Exception:
            failed += 1
        else:
            success += 1
    skipped += sum(
        1
        for item in participants
        if int(_value(item, "user_id")) not in connected
        or _value(item, "team") not in {"A", "B"}
    )
    return VoiceMoveSummary(success, skipped, failed)


__all__ = [
    "VoiceMoveSummary",
    "move_match_participants",
    "voice_move_plan",
]
