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
_UPPER_STEPS = {"하": 100, "중": 200, "상": 300, "LOW": 100, "MID": 200, "HIGH": 300}


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


def validate_preferences(first: str, second: str, third: str | None = None) -> tuple[str, ...]:
    if not first or not second:
        raise RoleAssignmentError("1지망과 2지망 라인이 필요합니다.")
    roles = tuple(normalize_role(value) for value in (first, second, third) if value)
    if len(roles) < 2:
        raise RoleAssignmentError("1지망과 2지망 라인이 필요합니다.")
    if len(set(roles)) != len(roles):
        raise RoleAssignmentError("지망 라인은 서로 달라야 합니다.")
    return roles


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
    best: tuple[tuple[object, ...], tuple[RoleAssignment, ...]] | None = None
    for chosen_roles in product(*(player.preferences for player in players)):
        if set(chosen_roles) != set(ROLES) or len(set(chosen_roles)) != 5:
            continue
        assignments = tuple(
            RoleAssignment(
                player.user_id,
                team,
                role,
                player.rating_for(role),
                player.preferences.index(role),
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
    roster = tuple(sorted(players, key=lambda player: (len(player.preferences), player.user_id)))
    if len(roster) != 10:
        raise RoleAssignmentError("Balanced 배정은 참가자 10명이 필요합니다.")
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
                                player.preferences.index(role),
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
        for preference_cost, role in enumerate(player.preferences):
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
    "draft_completion_possible",
    "finalize_draft",
    "initial_role_rating",
    "normalize_role",
    "positive_rating_average",
    "select_captains",
    "validate_preferences",
]
