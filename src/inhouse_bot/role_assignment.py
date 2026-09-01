"""라인 MMR 계산과 팀 배정을 DB 없이 처리한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from itertools import combinations, product
from typing import Iterable, Mapping


ROLES = ("TOP", "JUNGLE", "MID", "ADC", "SUPPORT")
ROLE_LABELS = {
    "TOP": "탑",
    "JUNGLE": "정글",
    "MID": "미드",
    "ADC": "원딜",
    "SUPPORT": "서폿",
}
DRAFT_TEAMS = ("A", "B", "B", "A", "A", "B", "B", "A")

_ROLE_INPUTS = {
    **{role: role for role in ROLES},
    "탑": "TOP",
    "정글": "JUNGLE",
    "미드": "MID",
    "원딜": "ADC",
    "서폿": "SUPPORT",
    "서포터": "SUPPORT",
}
_TIER_BASES = {
    "IRON": 0,
    "BRONZE": 400,
    "SILVER": 800,
    "GOLD": 1200,
    "PLATINUM": 1600,
    "EMERALD": 2000,
    "DIAMOND": 2400,
}
_TIER_INPUTS = {
    "아이언": "IRON",
    "브론즈": "BRONZE",
    "실버": "SILVER",
    "골드": "GOLD",
    "플래티넘": "PLATINUM",
    "에메랄드": "EMERALD",
    "다이아": "DIAMOND",
    "마스터": "MASTER",
    "그랜드마스터": "GRANDMASTER",
    "챌린저": "CHALLENGER",
}
_TIER_LABELS = {value: key for key, value in _TIER_INPUTS.items()}
_UPPER_STEPS = {"하": 100, "중": 200, "상": 300, "LOW": 100, "MID": 200, "HIGH": 300}
_UPPER_STEP_LABELS = {"LOW": "하", "MID": "중", "HIGH": "상"}


class RoleAssignmentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RolePlayer:
    user_id: int
    preferences: tuple[str, ...]
    ratings: Mapping[str, int]
    joined_at: datetime | None = None

    def rating_for(self, role: str) -> int:
        return int(self.ratings[role])


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    user_id: int
    team: str
    role: str
    rating: int
    preference_cost: int


def normalize_role(value: str) -> str:
    role = _ROLE_INPUTS.get(str(value).strip().upper()) or _ROLE_INPUTS.get(str(value).strip())
    if role is None:
        raise RoleAssignmentError("라인은 탑, 정글, 미드, 원딜, 서폿 중 하나여야 합니다.")
    return role


def _optional_role_input(value: str | None) -> str | None:
    """빈 입력을 ``None``으로 통일한다.

    Discord 모달과 저장소에서 오는 값은 빈 문자열 또는 공백 문자열일 수
    있다. 선택 지망은 이런 값을 실제 지망으로 취급하지 않아야 하며, 1지망은
    빈 값이면 별도의 필수 입력 오류를 내야 한다.
    """

    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def validate_preferences(
    first: str | None,
    second: str | None = None,
    third: str | None = None,
) -> tuple[str, ...]:
    first = _optional_role_input(first)
    second = _optional_role_input(second)
    third = _optional_role_input(third)
    if first is None:
        raise RoleAssignmentError("1지망 라인은 반드시 입력해야 합니다.")
    if third is not None and second is None:
        raise RoleAssignmentError("3지망을 입력하려면 2지망 라인도 입력해야 합니다.")
    roles = tuple(
        normalize_role(value)
        for value in (first, second, third)
        if value is not None
    )
    if len(set(roles)) != len(roles):
        raise RoleAssignmentError("지망 라인은 서로 달라야 합니다.")
    return roles


def _supplied_preferences(player: RolePlayer) -> tuple[str, ...]:
    """Return supplied preferences for defensive handling of legacy rows."""

    return tuple(
        role for role in player.preferences
        if role is not None and str(role).strip()
    )


def _preference_cost(player: RolePlayer, role: str) -> int:
    """Return the rank among supplied preferences, ignoring null placeholders."""

    return _supplied_preferences(player).index(role)


def initial_role_rating(tier: str, detail: str | int | None = None) -> int:
    canonical = str(tier).strip().upper()
    canonical = _TIER_INPUTS.get(str(tier).strip(), canonical)
    if canonical in _TIER_BASES:
        try:
            division = int(detail)
        except (TypeError, ValueError) as exc:
            raise RoleAssignmentError("아이언부터 다이아까지는 세부 단계를 1~4로 입력해야 합니다.") from exc
        if division not in (1, 2, 3, 4):
            raise RoleAssignmentError("세부 단계는 1~4만 사용할 수 있습니다.")
        return _TIER_BASES[canonical] + (4 - division) * 100 + 50
    if canonical == "CHALLENGER":
        return 3800
    if canonical in ("MASTER", "GRANDMASTER"):
        step = _UPPER_STEPS.get(str(detail).strip().upper()) or _UPPER_STEPS.get(str(detail).strip())
        if step is None:
            raise RoleAssignmentError("상위 티어 세부 단계는 하, 중, 상 중 하나여야 합니다.")
        return (2800 if canonical == "MASTER" else 3200) + step
    raise RoleAssignmentError("지원하지 않는 티어입니다.")


def parse_compact_tier(value: str) -> tuple[str, int | str | None]:
    """통합 티어 문자열을 표준 티어와 단계로 나눈다."""

    compact = "".join(str(value).split())
    if not compact:
        raise RoleAssignmentError("티어를 입력해 주세요.")
    normalized = compact.upper()
    aliases = {
        **{alias.upper(): canonical for alias, canonical in _TIER_INPUTS.items()},
        **{canonical: canonical for canonical in (*_TIER_BASES, "MASTER", "GRANDMASTER", "CHALLENGER")},
    }
    matched = next(
        ((alias, canonical) for alias, canonical in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True)
         if normalized.startswith(alias)),
        None,
    )
    if matched is None:
        raise RoleAssignmentError("지원하지 않는 티어입니다.")
    prefix, tier = matched
    suffix = normalized[len(prefix):]

    if tier in _TIER_BASES:
        if suffix not in {"1", "2", "3", "4"}:
            raise RoleAssignmentError("아이언부터 다이아까지는 세부 단계를 1~4로 입력해야 합니다.")
        division: int | str | None = int(suffix)
    elif tier in ("MASTER", "GRANDMASTER"):
        division = {"하": "LOW", "중": "MID", "상": "HIGH"}.get(suffix, suffix)
        if division not in ("LOW", "MID", "HIGH"):
            raise RoleAssignmentError("상위 티어 세부 단계는 하, 중, 상 중 하나여야 합니다.")
    else:
        division = None
        if suffix:
            raise RoleAssignmentError("챌린저는 세부 단계 없이 입력해야 합니다.")

    # 점수표와 유효성 검사는 기존 계산 함수 하나를 사용한다.
    initial_role_rating(tier, division)
    return tier, division


def compact_tier_label(tier: str, division: int | str | None = None) -> str:
    """표준 티어와 단계를 사용자용 한국어 통합 문자열로 만든다."""

    canonical = str(tier).strip().upper()
    label = _TIER_LABELS.get(canonical, canonical)
    if canonical in _TIER_BASES:
        return f"{label}{int(division)}"
    if canonical in ("MASTER", "GRANDMASTER"):
        step = str(division).strip().upper() if division is not None else ""
        return f"{label}{_UPPER_STEP_LABELS.get(step, step)}"
    return label


def positive_rating_average(ratings: Iterable[int]) -> int:
    placed = [int(rating) for rating in ratings if int(rating) > 0]
    return sum(placed) // len(placed) if placed else 0


def _tie_value(match_id: int, assignments: Iterable[tuple[int, str, str]]) -> bytes:
    text = f"{int(match_id)}:" + ";".join(
        f"{user_id}:{team}:{role}" for user_id, team, role in sorted(assignments)
    )
    return sha256(text.encode("ascii")).digest()


def _best_team_roles(
    players: tuple[RolePlayer, ...], team: str, match_id: int
) -> tuple[RoleAssignment, ...] | None:
    if len(players) != 5:
        return None
    preference_options = tuple(_supplied_preferences(player) for player in players)
    if any(not preferences for preferences in preference_options):
        return None
    best: tuple[tuple[object, ...], tuple[RoleAssignment, ...]] | None = None
    for chosen_roles in product(*preference_options):
        if set(chosen_roles) != set(ROLES) or len(set(chosen_roles)) != 5:
            continue
        assignments = tuple(
            RoleAssignment(
                player.user_id,
                team,
                role,
                player.rating_for(role),
                _preference_cost(player, role),
            )
            for player, role in zip(players, chosen_roles, strict=True)
        )
        objective = (
            sum(item.preference_cost for item in assignments),
            _tie_value(match_id, ((item.user_id, team, item.role) for item in assignments)),
        )
        if best is None or objective < best[0]:
            best = objective, assignments
    return best[1] if best else None


def balanced_assignment(players: Iterable[RolePlayer], match_id: int) -> tuple[RoleAssignment, ...]:
    roster = tuple(sorted(players, key=lambda player: (len(_supplied_preferences(player)), player.user_id)))
    if len(roster) != 10:
        raise RoleAssignmentError("Balanced 배정은 참가자 10명이 필요합니다.")
    preference_options = tuple(_supplied_preferences(player) for player in roster)
    if any(not preferences for preferences in preference_options):
        raise RoleAssignmentError("라인 지망이 없는 참가자가 있어 배정할 수 없습니다.")
    if any(
        any(role not in ROLES for role in preferences)
        for preferences in preference_options
    ):
        raise RoleAssignmentError("지원하지 않는 라인 지망이 있어 배정할 수 없습니다.")
    best: tuple[tuple[object, ...], tuple[RoleAssignment, ...]] | None = None
    role_counts = {role: 0 for role in ROLES}
    chosen: dict[int, str] = {}

    def search(index: int, cost: int) -> None:
        nonlocal best
        if best is not None and cost > int(best[0][0]):
            return
        if index == len(roster):
            if any(role_counts[role] != 2 for role in ROLES):
                return
            pairs = {role: [player for player in roster if chosen[player.user_id] == role] for role in ROLES}
            for sides in product((0, 1), repeat=5):
                assignments: list[RoleAssignment] = []
                for role, side in zip(ROLES, sides, strict=True):
                    first, second = pairs[role]
                    for player, team in ((first, "A" if side == 0 else "B"), (second, "B" if side == 0 else "A")):
                        assignments.append(
                            RoleAssignment(
                                player.user_id,
                                team,
                                role,
                                player.rating_for(role),
                                _preference_cost(player, role),
                            )
                        )
                total_a = sum(item.rating for item in assignments if item.team == "A")
                total_b = sum(item.rating for item in assignments if item.team == "B")
                line_gaps = [
                    abs(
                        next(item.rating for item in assignments if item.team == "A" and item.role == role)
                        - next(item.rating for item in assignments if item.team == "B" and item.role == role)
                    )
                    for role in ROLES
                ]
                objective = (
                    cost,
                    abs(total_a - total_b),
                    max(line_gaps),
                    sum(line_gaps),
                    _tie_value(match_id, ((item.user_id, item.team, item.role) for item in assignments)),
                )
                result = tuple(sorted(assignments, key=lambda item: item.user_id))
                if best is None or objective < best[0]:
                    best = objective, result
            return
        player = roster[index]
        for preference_cost, role in enumerate(preference_options[index]):
            if role_counts[role] >= 2:
                continue
            role_counts[role] += 1
            chosen[player.user_id] = role
            search(index + 1, cost + preference_cost)
            del chosen[player.user_id]
            role_counts[role] -= 1

    search(0, 0)
    if best is None:
        raise RoleAssignmentError("현재 지망으로는 양 팀 라인을 완성할 수 없습니다. 라인을 변경해 주세요.")
    return best[1]


def select_captains(players: Iterable[RolePlayer]) -> tuple[int, int]:
    roster = sorted(
        players,
        key=lambda player: (
            -positive_rating_average(player.ratings.values()),
            player.joined_at or datetime.max.replace(tzinfo=timezone.utc),
            player.user_id,
        ),
    )
    if len(roster) < 2:
        raise RoleAssignmentError("주장을 정할 참가자가 부족합니다.")
    return roster[0].user_id, roster[1].user_id


def draft_completion_possible(
    players: Iterable[RolePlayer], teams: Mapping[int, str], match_id: int
) -> bool:
    roster = tuple(players)
    if any(not _supplied_preferences(player) for player in roster):
        return False
    team_a = {user_id for user_id, team in teams.items() if team == "A"}
    team_b = {user_id for user_id, team in teams.items() if team == "B"}
    if len(team_a) > 5 or len(team_b) > 5 or team_a & team_b:
        return False
    remaining = [player.user_id for player in roster if player.user_id not in team_a | team_b]
    needed_a = 5 - len(team_a)
    by_id = {player.user_id: player for player in roster}
    for selected in combinations(remaining, needed_a):
        completed_a = team_a | set(selected)
        completed_b = {player.user_id for player in roster} - completed_a
        if team_b - completed_b:
            continue
        if _best_team_roles(tuple(by_id[user_id] for user_id in sorted(completed_a)), "A", match_id) \
                and _best_team_roles(tuple(by_id[user_id] for user_id in sorted(completed_b)), "B", match_id):
            return True
    return False


def finalize_draft(
    players: Iterable[RolePlayer], teams: Mapping[int, str], match_id: int
) -> tuple[RoleAssignment, ...]:
    roster = tuple(players)
    if any(not _supplied_preferences(player) for player in roster):
        raise RoleAssignmentError("라인 지망이 없는 참가자가 있어 배정할 수 없습니다.")
    by_team = {
        team: tuple(sorted((player for player in roster if teams.get(player.user_id) == team), key=lambda item: item.user_id))
        for team in ("A", "B")
    }
    assignments_a = _best_team_roles(by_team["A"], "A", match_id)
    assignments_b = _best_team_roles(by_team["B"], "B", match_id)
    if assignments_a is None or assignments_b is None:
        raise RoleAssignmentError("지명 결과로는 양 팀 라인을 완성할 수 없습니다.")
    return tuple(sorted((*assignments_a, *assignments_b), key=lambda item: item.user_id))


__all__ = [
    "DRAFT_TEAMS",
    "ROLES",
    "ROLE_LABELS",
    "RoleAssignment",
    "RoleAssignmentError",
    "RolePlayer",
    "balanced_assignment",
    "compact_tier_label",
    "draft_completion_possible",
    "finalize_draft",
    "initial_role_rating",
    "normalize_role",
    "parse_compact_tier",
    "positive_rating_average",
    "select_captains",
    "validate_preferences",
]
