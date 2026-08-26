"""저장된 내전 상태를 Discord 임베드로 만든다."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any

import discord


_STATUS_LABELS = {
    "RECRUITING": "모집 중",
    "READY_CHECK": "준비 확인",
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


def _roster(items: list[Any], *, numbered: bool = False) -> str:
    if not items:
        return "-"
    if numbered:
        return "\n".join(
            f"{index}. {_mention(_participant_user_id(item))}"
            for index, item in enumerate(items, 1)
        )
    return "\n".join(_mention(_participant_user_id(item)) for item in items)


def render_match(match: Any) -> discord.Embed:
    """DB 최신 상태로 화면을 만든다. 기준은 Discord가 아니라 DB다."""

    status = str(_get(match, "status", "RECRUITING"))
    game_name = str(_get(match, "game_name", "League of Legends"))
    team_size = int(_get(match, "team_size", 5))
    title = str(_get(match, "title", f"{game_name} 내전"))
    capacity = int(_get(match, "capacity", 10))
    players = _participants(match)
    waiting = _waitlist(match)
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
        embed.add_field(name="대기자", value=f"{len(waiting)}명\n{_roster(waiting)}", inline=True)

    if status == "RECRUITING":
        embed.add_field(name="참가자 명단", value=_roster(players, numbered=True), inline=False)
        deadline = _get(match, "recruitment_deadline_at")
        if deadline is not None:
            embed.add_field(name="모집 마감", value=_deadline_value(deadline), inline=False)
    elif status == "READY_CHECK":
        ready = [item for item in players if _ready(item)]
        unready = [item for item in players if not _ready(item)]
        ready_count = int(_get(match, "ready_count", len(ready)))
        embed.add_field(name="준비", value=f"{ready_count} / {len(players)}", inline=True)
        embed.add_field(name="준비 완료", value=_roster(ready), inline=True)
        embed.add_field(name="미준비", value=_roster(unready), inline=True)
        deadline = _get(match, "ready_deadline_at")
        if deadline is not None:
            embed.add_field(name="준비 마감", value=_deadline_value(deadline), inline=False)
    elif status == "PLAYING":
        for team in ("A", "B"):
            members = [item for item in players if _participant_team(item) == team]
            embed.add_field(name=f"{team}팀", value=_roster(members), inline=True)
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
            embed.add_field(name=f"{team}팀", value=_roster(members), inline=True)
    elif status == "CANCELLED":
        reason = _get(match, "cancel_reason") or "사유 없음"
        embed.add_field(name="취소 사유", value=str(reason), inline=False)

    creator_id = _get(match, "creator_id")
    if creator_id is not None:
        embed.set_footer(text=f"생성자: {_mention(creator_id)}")
    return embed


__all__ = ["render_match"]
