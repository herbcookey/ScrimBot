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
_WAITLIST_DISPLAY_LIMIT = 25


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
        preferences = tuple(
            role for role in (_get(item, "preferences", ()) or ())
            if role is not None and str(role).strip()
        )
        if not preferences:
            preferences = tuple(
                value for value in (
                    _get(item, "preferred_role_1"), _get(item, "preferred_role_2"),
                    _get(item, "preferred_role_3"),
                ) if value is not None and str(value).strip()
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
        shown = waiting[:_WAITLIST_DISPLAY_LIMIT]
        omitted = len(waiting) - len(shown)
        value = f"{len(waiting)}명\n{_roster(shown, show_preferences=role_enabled)}"
        if omitted:
            value += f"\n외 {omitted}명"
        if len(value) > 1024:
            value = f"{len(waiting)}명\n{_roster(shown)}"
            if omitted:
                value += f"\n외 {omitted}명"
        embed.add_field(
            name="대기자",
            value=value,
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
        deadline = _get(match, "draft_deadline_at")
        if deadline is not None:
            embed.add_field(name="지명 마감", value=_deadline_value(deadline), inline=False)
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


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "-")
    return text if len(text) <= limit else text[:max(0, limit - 1)] + "…"


def _panel_link(value: Any) -> str | None:
    ids = tuple(_get(value, name) for name in ("guild_id", "channel_id", "message_id"))
    try:
        guild_id, channel_id, message_id = (int(item) for item in ids)
    except (TypeError, ValueError):
        return None
    if min(guild_id, channel_id, message_id) <= 0:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _rating_change(value: Any, *, show_role: bool) -> str:
    before = _get(value, "rating_before")
    after = _get(value, "rating_after")
    delta = _get(value, "rating_delta")
    if before is None or after is None or delta is None:
        return "변동 기록 없음"
    prefix = ""
    role = _get(value, "assigned_role")
    if show_role and role:
        prefix = f"{ROLE_LABELS.get(str(role), str(role))} · "
    delta_text = f"+{int(delta):,}" if int(delta) > 0 else f"{int(delta):,}"
    return f"{prefix}{int(before):,} → {int(after):,} ({delta_text})"


def _add_bounded_field(
    embed: discord.Embed, name: Any, value: Any, *, inline: bool = False
) -> bool:
    if len(embed.fields) >= 25:
        return False
    field_name = _truncate(name, 256)
    remaining = 5900 - len(embed) - len(field_name)
    if remaining <= 0:
        return False
    field_value = _truncate(value, min(1024, remaining))
    embed.add_field(name=field_name, value=field_value, inline=inline)
    return True


def render_match_history(page: Any, user_id: int) -> discord.Embed:
    """사용자 종료 경기 페이지를 Discord 제한 안에서 표시한다."""

    scope_name = str(_get(page, "scope_name", "전체 시즌"))
    embed = discord.Embed(
        title=_truncate(f"<@{int(user_id)}>님의 경기 기록 · {scope_name}", 256),
        colour=0x57F287,
    )
    entries = tuple(_get(page, "entries", ()) or ())
    if not entries:
        embed.description = "조건에 맞는 종료 경기 기록이 없습니다."
    for entry in entries:
        ended = _deadline_value(_get(entry, "ended_at"))
        team = str(_get(entry, "team"))
        winner = str(_get(entry, "winner_team"))
        mode = "균형 배정" if str(_get(entry, "assignment_mode")) == "BALANCED" else "드래프트"
        if bool(_get(entry, "role_rating_enabled")):
            rating = _rating_change(entry, show_role=True)
        elif _get(entry, "rating_before") is None and not bool(_get(entry, "rating_enabled", True)):
            rating = "MMR 미사용"
        else:
            rating = _rating_change(entry, show_role=False)
        lines = [
            f"{_get(entry, 'game_name')} · {_get(entry, 'season_name')}",
            f"종료: {ended}",
            f"배정: {mode} · 내 팀: {team}팀 · {'승리' if team == winner else '패배'}",
            f"승리팀: {winner}팀",
        ]
        if bool(_get(entry, "role_rating_enabled")) and _get(entry, "assigned_role"):
            lines.append(f"배정 라인: {ROLE_LABELS.get(str(_get(entry, 'assigned_role')), str(_get(entry, 'assigned_role')))}")
        lines.append(f"MMR: {rating}")
        link = _panel_link(entry)
        if link:
            lines.append(f"[원본 모집 패널]({link})")
        _add_bounded_field(
            embed,
            f"#{int(_get(entry, 'id'))} · {_get(entry, 'title')}",
            "\n".join(lines),
        )
    embed.set_footer(
        text=_truncate(
            f"요청 페이지 {int(_get(page, 'page', 1))} · 전체 {int(_get(page, 'total_pages', 1))}페이지",
            2048,
        )
    )
    return embed


def _participant_history_line(item: Any, *, role_enabled: bool, rating_enabled: bool) -> str:
    prefix = _mention(_get(item, "user_id"))
    if role_enabled and _get(item, "assigned_role"):
        prefix = f"{ROLE_LABELS.get(str(_get(item, 'assigned_role')), str(_get(item, 'assigned_role')))} · {prefix}"
    if role_enabled or rating_enabled:
        prefix += f" · {_rating_change(item, show_role=False)}"
    return prefix


def _add_team_fields(
    embed: discord.Embed,
    team: str,
    participants: list[Any],
    *,
    role_enabled: bool,
    rating_enabled: bool,
    winner_team: str,
) -> None:
    lines = [
        _participant_history_line(
            item, role_enabled=role_enabled, rating_enabled=rating_enabled
        ) for item in participants
    ] or ["-"]
    value_limit = 900  # 두 팀의 최소 field와 결과 메모 예산을 남긴다.
    shown = 0
    for index in range(2):
        name = f"{team}팀{' · 승리' if team == winner_team else ''}"
        if index:
            name += f" ({index + 1})"
        value_lines: list[str] = []
        while shown < len(lines):
            candidate = "\n".join((*value_lines, lines[shown]))
            if len(candidate) > value_limit:
                break
            value_lines.append(lines[shown])
            shown += 1
        omitted = len(lines) - shown
        if index == 1 and omitted:
            suffix = f"외 {omitted}명"
            while value_lines and len("\n".join((*value_lines, suffix))) > value_limit:
                value_lines.pop()
                shown -= 1
                omitted += 1
                suffix = f"외 {omitted}명"
            value_lines.append(suffix)
        if not _add_bounded_field(embed, name, "\n".join(value_lines)):
            break
        if shown == len(lines):
            break


def render_match_history_detail(detail: Any) -> discord.Embed:
    """종료 경기와 참가자별 이력만으로 상세 Embed를 만든다."""

    embed = discord.Embed(
        title=_truncate(f"경기 #{int(_get(detail, 'id'))} · {_get(detail, 'title')}", 256),
        colour=0x5865F2,
    )
    mode = "균형 배정" if str(_get(detail, "assignment_mode")) == "BALANCED" else "드래프트"
    lines = [
        f"게임: {_truncate(_get(detail, 'game_name'), 256)}",
        f"시즌: {_truncate(_get(detail, 'season_name'), 100)}",
        f"배정: {mode}",
        f"생성자: {_mention(_get(detail, 'creator_id'))}",
        f"생성: {_deadline_value(_get(detail, 'created_at'))}",
        f"시작: {_deadline_value(_get(detail, 'started_at'))}",
        f"종료: {_deadline_value(_get(detail, 'ended_at'))}",
        f"승리팀: {_get(detail, 'winner_team')}팀",
    ]
    link = _panel_link(detail)
    if link:
        lines.append(f"[원본 모집 패널]({link})")
    embed.description = _truncate("\n".join(lines), 1024)
    role_enabled = bool(_get(detail, "role_rating_enabled"))
    rating_enabled = bool(_get(detail, "rating_enabled"))
    if not role_enabled and not rating_enabled:
        _add_bounded_field(embed, "MMR", "MMR 미사용")
    participants = tuple(_get(detail, "participants", ()) or ())
    winner = str(_get(detail, "winner_team"))
    for team in ("A", "B"):
        _add_team_fields(
            embed,
            team,
            [item for item in participants if str(_get(item, "team")) == team],
            role_enabled=role_enabled,
            rating_enabled=rating_enabled,
            winner_team=winner,
        )
    memo = _get(detail, "memo")
    if memo is not None and str(memo).strip():
        _add_bounded_field(embed, "결과 메모", str(memo).strip())
    return embed


__all__ = ["render_match", "render_match_history", "render_match_history_detail"]
