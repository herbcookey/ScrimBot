"""저장된 내전 상태를 Discord 임베드로 만든다."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any

import discord

from inhouse_bot.role_assignment import DRAFT_TEAMS, ROLE_LABELS


_STATUS_LABELS = {
    "RECRUITING": "모집 중",
    "READY_CHECK": "준비 확인",
    "DRAFTING": "주장 지명",
    "PLAYING": "진행 중",
    "FINISHED": "종료",
    "CANCELLED": "취소됨",
}


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mention(user_id: Any) -> str:
    return f"<@{int(user_id)}>" if user_id is not None else "-"


def _participants(match: Any) -> list[Any]:
    return list(_get(match, "participants", ()) or ())


def _waitlist(match: Any) -> list[Any]:
    return list(_get(match, "waitlist", ()) or ())


def _participant_user_id(participant: Any) -> Any:
    return _get(participant, "user_id", _get(participant, "id", participant))


def _participant_team(participant: Any) -> str | None:
    team = _get(participant, "team")
    return str(team) if team is not None else None


def _ready(participant: Any) -> bool:
    return _get(participant, "ready_at") is not None


def _timestamp(value: Any, style: str = "F") -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        epoch = int(parsed.timestamp())
    elif isinstance(value, date):
        epoch = int(datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp())
    else:
        try:
            epoch = int(value)
        except (TypeError, ValueError):
            return None
    return f"<t:{epoch}:{style}>"


def _deadline_value(value: Any) -> str:
    absolute = _timestamp(value, "F")
    relative = _timestamp(value, "R")
    if absolute and relative:
        return f"{absolute} ({relative})"
    return absolute or "-"


def _participant_line(item: Any, *, show_preferences: bool, show_role: bool) -> str:
    line = _mention(_participant_user_id(item))
    if show_role and _get(item, "assigned_role"):
        role = str(_get(item, "assigned_role"))
        rating = _get(item, "role_rating_snapshot")
        line += f" · {ROLE_LABELS.get(role, role)}"
        if rating is not None:
            line += f" {int(rating)}점"
    elif show_preferences:
        preferences = tuple(_get(item, "preferences", ()) or ())
        if not preferences:
            preferences = tuple(
                value for value in (
                    _get(item, "preferred_role_1"), _get(item, "preferred_role_2"),
                    _get(item, "preferred_role_3"),
                ) if value
            )
        labels = "/".join(ROLE_LABELS.get(str(role), str(role)) for role in preferences)
        average = int(_get(item, "average_role_rating", 0) or 0)
        line += f" · {labels or '-'} · {average}점"
    return line


def _roster(
    items: list[Any], *, numbered: bool = False,
    show_preferences: bool = False, show_role: bool = False,
) -> str:
    if not items:
        return "-"
    if numbered:
        return "\n".join(
            f"{index}. {_participant_line(item, show_preferences=show_preferences, show_role=show_role)}"
            for index, item in enumerate(items, 1)
        )
    return "\n".join(
        _participant_line(item, show_preferences=show_preferences, show_role=show_role)
        for item in items
    )


def render_match(match: Any) -> discord.Embed:
    """DB 최신 상태로 화면을 만든다. 기준은 Discord가 아니라 DB다."""

    status = str(_get(match, "status", "RECRUITING"))
    game_name = str(_get(match, "game_name", "League of Legends"))
    team_size = int(_get(match, "team_size", 5))
    title = str(_get(match, "title", f"{game_name} 내전"))
    capacity = int(_get(match, "capacity", 10))
    players = _participants(match)
    waiting = _waitlist(match)
    role_enabled = bool(_get(match, "role_rating_enabled", False))
    embed = discord.Embed(
        title=f"[{game_name} {team_size}:{team_size} 내전]",
        description=title,
        colour=0x5865F2,
    )
    embed.add_field(name="상태", value=_STATUS_LABELS.get(status, status), inline=True)
    embed.add_field(name="참가자", value=f"{len(players)} / {capacity}", inline=True)
    season_name = _get(match, "season_name")
    if season_name:
        embed.add_field(name="시즌", value=str(season_name), inline=True)
    if waiting:
        embed.add_field(
            name="대기자",
            value=f"{len(waiting)}명\n{_roster(waiting, show_preferences=role_enabled)}",
            inline=True,
        )

    if status == "RECRUITING":
        embed.add_field(
            name="참가자 명단",
            value=_roster(players, numbered=True, show_preferences=role_enabled),
            inline=False,
        )
        deadline = _get(match, "recruitment_deadline_at")
        if deadline is not None:
            embed.add_field(name="모집 마감", value=_deadline_value(deadline), inline=False)
    elif status == "READY_CHECK":
        ready = [item for item in players if _ready(item)]
        unready = [item for item in players if not _ready(item)]
        ready_count = int(_get(match, "ready_count", len(ready)))
        embed.add_field(name="준비", value=f"{ready_count} / {len(players)}", inline=True)
        embed.add_field(name="준비 완료", value=_roster(ready, show_preferences=role_enabled), inline=True)
        embed.add_field(name="미준비", value=_roster(unready, show_preferences=role_enabled), inline=True)
        deadline = _get(match, "ready_deadline_at")
        if deadline is not None:
            embed.add_field(name="준비 마감", value=_deadline_value(deadline), inline=False)
    elif status == "DRAFTING":
        pick_index = int(_get(match, "draft_pick_index", 0))
        current_team = DRAFT_TEAMS[pick_index] if pick_index < len(DRAFT_TEAMS) else "-"
        captain_id = _get(match, "captain_a_id" if current_team == "A" else "captain_b_id")
        embed.add_field(
            name="현재 지명",
            value=f"{current_team}팀 {_mention(captain_id)}",
            inline=False,
        )
        for team in ("A", "B"):
            members = [item for item in players if _participant_team(item) == team]
            embed.add_field(
                name=f"{team}팀", value=_roster(members, show_preferences=True), inline=True
            )
        unpicked = [item for item in players if _participant_team(item) is None]
        embed.add_field(
            name="미지명", value=_roster(unpicked, show_preferences=True), inline=False
        )
    elif status == "PLAYING":
        for team in ("A", "B"):
            members = [item for item in players if _participant_team(item) == team]
            embed.add_field(name=f"{team}팀", value=_roster(members, show_role=role_enabled), inline=True)
        voice_a = _get(match, "team_a_voice_channel_id")
        voice_b = _get(match, "team_b_voice_channel_id")
        closed_a = _get(match, "team_a_voice_closed_at")
        closed_b = _get(match, "team_b_voice_closed_at")
        if closed_a is not None or closed_b is not None:
            embed.add_field(
                name="1팀 보이스",
                value=f"<#{int(voice_a)}>" if voice_a is not None else "종료됨",
                inline=True,
            )
            embed.add_field(
                name="2팀 보이스",
                value=f"<#{int(voice_b)}>" if voice_b is not None else "종료됨",
                inline=True,
            )
        elif voice_a is not None and voice_b is not None:
            embed.add_field(name="1팀 보이스", value=f"<#{int(voice_a)}>", inline=True)
            embed.add_field(name="2팀 보이스", value=f"<#{int(voice_b)}>", inline=True)
        elif voice_a is not None or voice_b is not None:
            embed.add_field(name="보이스", value="보이스 채널 생성 실패", inline=False)
        elif _get(match, "voice_category_id") is not None:
            embed.add_field(name="보이스", value="보이스 채널 준비 중", inline=False)
    elif status == "FINISHED":
        result = _get(match, "result")
        winner = _get(result, "winner_team") if result is not None else None
        if winner:
            embed.add_field(name="승리팀", value=f"{winner}팀", inline=True)
        memo = _get(result, "memo") if result is not None else None
        if memo:
            embed.add_field(name="메모", value=str(memo), inline=False)
        for team in ("A", "B"):
            members = [item for item in players if _participant_team(item) == team]
            embed.add_field(name=f"{team}팀", value=_roster(members, show_role=role_enabled), inline=True)
    elif status == "CANCELLED":
        reason = _get(match, "cancel_reason") or "사유 없음"
        embed.add_field(name="취소 사유", value=str(reason), inline=False)

    creator_id = _get(match, "creator_id")
    if creator_id is not None:
        embed.set_footer(text=f"생성자: {_mention(creator_id)}")
    return embed


__all__ = ["render_match"]
