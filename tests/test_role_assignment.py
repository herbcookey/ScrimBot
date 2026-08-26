from __future__ import annotations

import pytest

from inhouse_bot.role_assignment import (
    ROLES,
    RoleAssignmentError,
    RolePlayer,
    balanced_assignment,
    draft_completion_possible,
    finalize_draft,
    initial_role_rating,
    positive_rating_average,
    validate_preferences,
)


def _players() -> tuple[RolePlayer, ...]:
    players = []
    for index, role in enumerate(ROLES):
        players.append(RolePlayer(index + 1, (role, ROLES[(index + 1) % 5]), {role: 1600, ROLES[(index + 1) % 5]: 1400}))
        players.append(RolePlayer(index + 6, (role, ROLES[(index + 1) % 5]), {role: 1500, ROLES[(index + 1) % 5]: 1300}))
    return tuple(players)


def test_initial_role_rating_table():
    assert initial_role_rating("골드", 4) == 1250
    assert initial_role_rating("GOLD", 1) == 1550
    assert initial_role_rating("다이아", 4) == 2450
    assert initial_role_rating("DIAMOND", 1) == 2750
    assert initial_role_rating("마스터", "하") == 2900
    assert initial_role_rating("MASTER", "중") == 3000
    assert initial_role_rating("그랜드마스터", "상") == 3500
    assert initial_role_rating("챌린저") == 3800


def test_positive_average_and_preferences():
    assert positive_rating_average((0, 1680, -1, 1922)) == 1801
    assert positive_rating_average((0, -1)) == 0
    assert validate_preferences("탑", "정글", "미드") == ("TOP", "JUNGLE", "MID")
    with pytest.raises(RoleAssignmentError, match="서로 달라야"):
        validate_preferences("탑", "TOP")


def test_balanced_has_every_role_and_is_deterministic():
    first = balanced_assignment(_players(), 42)
    second = balanced_assignment(reversed(_players()), 42)
    assert first == second
    assert sum(item.preference_cost for item in first) == 0
    for team in ("A", "B"):
        assert {item.role for item in first if item.team == team} == set(ROLES)
        assert len([item for item in first if item.team == team]) == 5


def test_balanced_rejects_impossible_preferences():
    players = tuple(RolePlayer(user_id, ("TOP", "MID"), {"TOP": 1000, "MID": 1000}) for user_id in range(1, 11))
    with pytest.raises(RoleAssignmentError, match="라인을 완성"):
        balanced_assignment(players, 1)


def test_draft_completion_and_final_roles():
    players = _players()
    teams = {1: "A", 6: "B"}
    assert draft_completion_possible(players, teams, 7)
    complete = {index + 1: "A" if index < 5 else "B" for index in range(10)}
    assert draft_completion_possible(players, complete, 7)
    result = finalize_draft(players, complete, 7)
    assert {item.role for item in result if item.team == "A"} == set(ROLES)
    assert {item.role for item in result if item.team == "B"} == set(ROLES)
