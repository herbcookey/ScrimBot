"""내전 보이스 채널 생성, 이동, 정리 처리."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Iterable

import discord


logger = logging.getLogger(__name__)
_VOICE_LOCKS: dict[int, asyncio.Lock] = {}


@dataclass(frozen=True, slots=True)
class VoiceMoveSummary:
    """보이스 채널 준비와 참가자 이동 결과."""

    success: int = 0
    skipped: int = 0
    failed: int = 0
    created_channel_ids: tuple[int, ...] = ()
    team_a_channel_id: int | None = None
    team_b_channel_id: int | None = None
    error: str | None = None

    @property
    def moved(self) -> int:
        return self.success

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class VoiceCleanupSummary:
    deleted: int = 0
    failed: int = 0


class VoiceSetupError(RuntimeError):
    pass


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    try:
        return value[name]
    except (KeyError, IndexError, TypeError):
        return getattr(value, name, default)


def match_voice_channel_name(match_id: int, team: str) -> str:
    if team not in {"A", "B"}:
        raise ValueError("보이스 팀은 A 또는 B여야 합니다.")
    return f"{int(match_id)}번째 내전 {1 if team == 'A' else 2}팀"


def voice_move_plan(
    participants: Iterable[Any], connected_user_ids: Iterable[int]
) -> list[tuple[int, str]]:
    connected = {int(user_id) for user_id in connected_user_ids}
    return [
        (int(_value(item, "user_id")), str(_value(item, "team")))
        for item in participants
        if int(_value(item, "user_id")) in connected
        and _value(item, "team") in {"A", "B"}
    ]


def _same_guild(channel: Any, guild: Any) -> bool:
    channel_guild = getattr(channel, "guild", guild)
    return int(getattr(channel_guild, "id", 0)) == int(getattr(guild, "id", 0))


def _voice_channel(guild: Any, channel_id: int | None) -> Any | None:
    if channel_id is None or guild is None:
        return None
    channel = guild.get_channel(int(channel_id))
    return channel if isinstance(channel, discord.VoiceChannel) and _same_guild(channel, guild) else None


def _category_channel(guild: Any, category_id: int | None) -> Any | None:
    if category_id is None or guild is None:
        return None
    channel = guild.get_channel(int(category_id))
    return channel if isinstance(channel, discord.CategoryChannel) and _same_guild(channel, guild) else None


def resolve_voice_category_id(
    guild: Any,
    text_channel: Any,
    configured_category_id: int | None,
    fixed_a_channel_id: int | None,
    fixed_b_channel_id: int | None,
) -> tuple[int | None, str | None]:
    if configured_category_id is not None:
        category = _category_channel(guild, configured_category_id)
        if category is None:
            return None, "설정한 내전 보이스 카테고리를 이 서버에서 찾을 수 없습니다."
        return int(category.id), None
    if fixed_a_channel_id is not None and fixed_b_channel_id is not None:
        return None, None
    category = getattr(text_channel, "category", None)
    if isinstance(category, discord.CategoryChannel) and _same_guild(category, guild):
        return int(category.id), None
    return None, "내전 텍스트 채널에 상위 카테고리가 없어 보이스 채널을 만들 수 없습니다."


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
    state_user = getattr(getattr(guild, "_state", None), "user", None)
    getter = getattr(guild, "get_member", None)
    return getter(getattr(state_user, "id", 0)) if getter and state_user else None


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
    if str(_value(match, "status", "PLAYING")) != "PLAYING":
        return VoiceMoveSummary(error="PLAYING 상태에서만 참가자를 이동합니다.")
    if team_a_voice_channel_id is None or team_b_voice_channel_id is None:
        return VoiceMoveSummary()
    channel_a = _voice_channel(guild, team_a_voice_channel_id)
    channel_b = _voice_channel(guild, team_b_voice_channel_id)
    participants = tuple(_value(match, "participants", ()) or ())
    if channel_a is None or channel_b is None:
        return VoiceMoveSummary(skipped=len(participants))
    if not (_can_move(guild, channel_a) and _can_move(guild, channel_b)):
        return VoiceMoveSummary(
            failed=len(participants), error="보이스 이동에 필요한 Connect 또는 Move Members 권한이 없습니다."
        )

    connected = _connected_members(guild)
    success = failed = 0
    for user_id, team in voice_move_plan(participants, connected):
        member = connected.get(user_id)
        target = channel_a if team == "A" else channel_b
        try:
            await member.move_to(target)
        except Exception:
            failed += 1
            logger.exception("참가자 보이스 이동 실패", extra={"match_id": _value(match, "id"), "user_id": user_id})
        else:
            success += 1
    skipped = sum(
        1 for item in participants
        if int(_value(item, "user_id")) not in connected
        or _value(item, "team") not in {"A", "B"}
    )
    return VoiceMoveSummary(
        success, skipped, failed,
        team_a_channel_id=int(team_a_voice_channel_id),
        team_b_channel_id=int(team_b_voice_channel_id),
    )


def _named_channels(category: Any, name: str) -> list[Any]:
    return [
        channel for channel in getattr(category, "voice_channels", ()) or ()
        if isinstance(channel, discord.VoiceChannel) and str(getattr(channel, "name", "")) == name
    ]


async def _team_overwrites(guild: Any, match: Any, team: str) -> dict[Any, Any]:
    overwrites: dict[Any, Any] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False)
    }
    me = _bot_member(guild)
    if me is not None:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True, connect=True, manage_channels=True, move_members=True
        )
    for participant in tuple(_value(match, "participants", ()) or ()):
        if _value(participant, "team") != team:
            continue
        user_id = int(_value(participant, "user_id"))
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                logger.exception("보이스 권한용 사용자 조회 실패", extra={"match_id": _value(match, "id"), "user_id": user_id})
                continue
        overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True)
    return overwrites


def _validate_saved_channel(guild: Any, category: Any, channel_id: int, team: str) -> Any | None:
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return None
    if not isinstance(channel, discord.VoiceChannel) or not _same_guild(channel, guild):
        raise VoiceSetupError(f"{team}팀에 저장된 채널 ID가 이 서버의 보이스 채널이 아닙니다.")
    if int(getattr(channel, "category_id", 0) or 0) != int(category.id):
        raise VoiceSetupError(f"{team}팀 보이스 채널이 저장된 카테고리와 다릅니다.")
    return channel


async def ensure_match_voice_channels(
    service: Any,
    guild: Any,
    match: Any,
    fixed_a_channel_id: int | None = None,
    fixed_b_channel_id: int | None = None,
) -> VoiceMoveSummary:
    if str(_value(match, "status", "")) != "PLAYING":
        return VoiceMoveSummary(error="PLAYING 상태에서만 보이스 채널을 준비합니다.")
    if (
        _value(match, "voice_category_id") is None
        and fixed_a_channel_id is not None
        and fixed_b_channel_id is not None
    ):
        return await move_match_participants(guild, match, fixed_a_channel_id, fixed_b_channel_id)

    match_id = int(_value(match, "id"))
    lock = _VOICE_LOCKS.setdefault(match_id, asyncio.Lock())
    async with lock:
        latest = await service.get_match(match_id)
        if latest is None or str(_value(latest, "status", "")) != "PLAYING":
            return VoiceMoveSummary(error="진행 중인 내전을 찾을 수 없습니다.")
        category = _category_channel(guild, _value(latest, "voice_category_id"))
        if category is None:
            return VoiceMoveSummary(error="저장된 내전 보이스 카테고리를 찾을 수 없습니다.")

        me = _bot_member(guild)
        permissions_for = getattr(category, "permissions_for", None)
        permissions = permissions_for(me) if me is not None and callable(permissions_for) else None
        if permissions is None or not all(
            bool(getattr(permissions, name, False))
            for name in ("manage_channels", "view_channel", "connect", "move_members")
        ):
            return VoiceMoveSummary(
                error="Manage Channels, Move Members, View Channel, Connect 권한이 필요합니다."
            )

        created: list[int] = []
        channels: dict[str, Any] = {}
        try:
            for team, field, closed_field in (
                ("A", "team_a_voice_channel_id", "team_a_voice_closed_at"),
                ("B", "team_b_voice_channel_id", "team_b_voice_closed_at"),
            ):
                if _value(latest, closed_field) is not None:
                    continue
                stored_id = _value(latest, field)
                channel = _validate_saved_channel(guild, category, int(stored_id), team) if stored_id else None
                if channel is None:
                    name = match_voice_channel_name(match_id, team)
                    found = _named_channels(category, name)
                    if len(found) > 1:
                        raise VoiceSetupError(f"{name} 이름의 채널이 여러 개라 자동 복구하지 않았습니다.")
                    if found:
                        channel = found[0]
                        created_now = False
                    else:
                        channel = await guild.create_voice_channel(
                            name,
                            category=category,
                            overwrites=await _team_overwrites(guild, latest, team),
                            user_limit=int(_value(latest, "team_size", 5)),
                            reason=f"{match_id}번째 내전 팀 보이스 생성",
                        )
                        created_now = True
                    try:
                        latest = await service.set_voice_channel_id(
                            match_id, team, int(channel.id), replace_missing=stored_id is not None
                        )
                    except Exception:
                        if created_now:
                            try:
                                await channel.delete(reason=f"{match_id}번째 내전 DB 저장 실패 정리")
                            except Exception:
                                logger.exception(
                                    "DB 저장에 실패한 보이스 채널 정리 실패",
                                    extra={"match_id": match_id, "channel_id": int(channel.id)},
                                )
                        raise
                    if created_now:
                        created.append(int(channel.id))
                channels[team] = channel
        except Exception as exc:
            logger.exception("내전 보이스 채널 준비 실패", extra={"match_id": match_id})
            return VoiceMoveSummary(
                created_channel_ids=tuple(created),
                team_a_channel_id=_value(latest, "team_a_voice_channel_id"),
                team_b_channel_id=_value(latest, "team_b_voice_channel_id"),
                error=str(exc) or "보이스 채널 생성에 실패했습니다.",
            )

        if "A" not in channels or "B" not in channels:
            return VoiceMoveSummary(
                created_channel_ids=tuple(created),
                team_a_channel_id=_value(latest, "team_a_voice_channel_id"),
                team_b_channel_id=_value(latest, "team_b_voice_channel_id"),
            )
        moved = await move_match_participants(guild, latest, int(channels["A"].id), int(channels["B"].id))
        return VoiceMoveSummary(
            moved.success, moved.skipped, moved.failed, tuple(created),
            int(channels["A"].id), int(channels["B"].id), moved.error,
        )


async def close_empty_match_voice_channel(
    service: Any, channel: Any
) -> VoiceCleanupSummary:
    """DB에 저장된 빈 동적 보이스 채널 하나를 선점해서 삭제한다."""

    guild = getattr(channel, "guild", None)
    channel_id = getattr(channel, "id", None)
    if guild is None or channel_id is None or len(getattr(channel, "members", ()) or ()) != 0:
        return VoiceCleanupSummary()
    claimed = await service.claim_empty_voice_channel(int(guild.id), int(channel_id))
    if claimed is None:
        return VoiceCleanupSummary()
    match, team = claimed
    match_id = int(_value(match, "id"))
    category_id = _value(match, "voice_category_id")
    if (
        not isinstance(channel, discord.VoiceChannel)
        or not _same_guild(channel, guild)
        or int(getattr(channel, "category_id", 0) or 0) != int(category_id or 0)
        or len(getattr(channel, "members", ()) or ()) != 0
    ):
        await service.reopen_empty_voice_channel(match_id, team, int(channel_id))
        return VoiceCleanupSummary()
    try:
        await channel.delete(reason=f"{match_id}번째 내전 빈 보이스 정리")
    except discord.NotFound:
        pass
    except (discord.Forbidden, discord.HTTPException):
        logger.exception(
            "빈 내전 보이스 채널 삭제 실패",
            extra={"match_id": match_id, "channel_id": int(channel_id)},
        )
        return VoiceCleanupSummary(failed=1)
    await service.complete_empty_voice_channel(match_id, team, int(channel_id))
    return VoiceCleanupSummary(deleted=1)


async def close_empty_match_voice_channels(
    service: Any, guild: Any, match: Any
) -> VoiceCleanupSummary:
    deleted = failed = 0
    if guild is None:
        return VoiceCleanupSummary()
    for field in ("team_a_voice_channel_id", "team_b_voice_channel_id"):
        channel_id = _value(match, field)
        channel = guild.get_channel(int(channel_id)) if channel_id is not None else None
        if isinstance(channel, discord.VoiceChannel) and len(channel.members) == 0:
            result = await close_empty_match_voice_channel(service, channel)
            deleted += result.deleted
            failed += result.failed
    return VoiceCleanupSummary(deleted, failed)


async def cleanup_match_voice_channels(
    service: Any, guild: Any, match: Any, *, retry_seconds: int = 60
) -> VoiceCleanupSummary:
    match_id = int(_value(match, "id"))
    category_id = _value(match, "voice_category_id")
    if guild is None:
        await service.record_voice_cleanup(
            match_id,
            clear_team_a=False,
            clear_team_b=False,
            retry_at=datetime.now(timezone.utc) + timedelta(seconds=max(1, int(retry_seconds))),
        )
        return VoiceCleanupSummary(failed=sum(
            _value(match, field) is not None
            for field in ("team_a_voice_channel_id", "team_b_voice_channel_id")
        ))
    deleted = failed = 0
    cleared = {"A": False, "B": False}
    current = datetime.now(timezone.utc)
    scheduled_due = (
        str(_value(match, "status", "")) in {"FINISHED", "CANCELLED"}
        and _value(match, "voice_cleanup_at") is not None
        and _value(match, "voice_cleanup_at") <= current
    )
    for team, field, closed_field in (
        ("A", "team_a_voice_channel_id", "team_a_voice_closed_at"),
        ("B", "team_b_voice_channel_id", "team_b_voice_closed_at"),
    ):
        channel_id = _value(match, field)
        if channel_id is None:
            cleared[team] = True
            continue
        closed_at = _value(match, closed_field)
        if closed_at is None and not scheduled_due:
            continue
        channel = guild.get_channel(int(channel_id)) if guild is not None else None
        if channel is None:
            cleared[team] = True
            deleted += 1
            continue
        if (
            not isinstance(channel, discord.VoiceChannel)
            or not _same_guild(channel, guild)
            or int(getattr(channel, "category_id", 0) or 0) != int(category_id or 0)
        ):
            failed += 1
            continue
        if closed_at is not None and len(getattr(channel, "members", ()) or ()) != 0:
            await service.reopen_empty_voice_channel(match_id, team, int(channel_id))
            continue
        try:
            await channel.delete(reason=f"{match_id}번째 내전 보이스 정리")
        except discord.NotFound:
            cleared[team] = True
            deleted += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
            logger.exception("내전 보이스 채널 정리 실패", extra={"match_id": match_id, "channel_id": int(channel_id)})
        else:
            cleared[team] = True
            deleted += 1

    retry_at = current + timedelta(seconds=max(1, int(retry_seconds))) if failed else None
    await service.record_voice_cleanup(
        match_id,
        clear_team_a=cleared["A"],
        clear_team_b=cleared["B"],
        retry_at=retry_at,
    )
    return VoiceCleanupSummary(deleted, failed)


__all__ = [
    "VoiceCleanupSummary",
    "VoiceMoveSummary",
    "VoiceSetupError",
    "close_empty_match_voice_channel",
    "close_empty_match_voice_channels",
    "cleanup_match_voice_channels",
    "ensure_match_voice_channels",
    "match_voice_channel_name",
    "move_match_participants",
    "resolve_voice_category_id",
    "voice_move_plan",
]
