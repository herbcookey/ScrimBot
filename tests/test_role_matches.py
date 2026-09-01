from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from inhouse_bot.repositories.matches import (
    InvalidMatchStateError,
    InvalidRolePreferencesError,
    PermissionDeniedError,
    RoleRatingAlreadyExistsError,
    ResultAlreadyRecordedError,
    SeasonNotFoundError,
    UnplacedRoleError,
)
from inhouse_bot.role_assignment import ROLES


T0 = datetime(2031, 1, 1, tzinfo=timezone.utc)


def _balanced_preferences(user_id: int) -> tuple[str, str]:
    index = (user_id - 1) % 5
    return ROLES[index], ROLES[(index + 1) % 5]


async def _place(service, guild_id: int, user_id: int, preferences: tuple[str, ...]) -> None:
    for offset, role in enumerate(preferences):
        await service.set_role_rating(
            guild_id, user_id, role, rating=1500 + user_id * 10 - offset * 5,
            manager_override=True, now=T0,
        )


async def _place_constant(service, guild_id: int, user_id: int, preferences: tuple[str, ...]) -> None:
    for role in preferences:
        await service.set_role_rating(
            guild_id, user_id, role, rating=1500,
            manager_override=True, now=T0,
        )


@pytest.mark.asyncio
async def test_register_role_rating_is_first_write_only(service_and_scope):
    service, guild_id, _channel_id = service_and_scope
    with pytest.raises(SeasonNotFoundError, match="관리자에게 시즌 시작을 요청"):
        await service.register_role_rating(guild_id, 1, "ADC", "플래티넘2", now=T0)
    await service.start_season(guild_id, "등록 테스트", manager_override=True, now=T0)
    assert await service.register_role_rating(guild_id, 1, "ADC", "플래티넘2", now=T0) == 1850
    with pytest.raises(RoleRatingAlreadyExistsError, match="원딜 MMR이 등록"):
        await service.register_role_rating(guild_id, 1, "ADC", "골드1", now=T0)
    async with service.repository.pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT rating, games_played FROM player_role_ratings
               WHERE guild_id = $1 AND user_id = $2 AND role = 'ADC'""",
            guild_id, 1,
        )
    assert (row["rating"], row["games_played"]) == (1850, 0)


@pytest.mark.asyncio
async def test_single_and_optional_role_preferences_round_trip_and_update(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    await service.start_season(guild_id, "선택 지망 테스트", manager_override=True, now=T0)
    await _place(service, guild_id, 1, ("TOP",))
    await _place(service, guild_id, 2, ("JUNGLE", "MID"))

    match = await service.create_match(
        guild_id,
        channel_id,
        1,
        "1지망 내전",
        preferred_role_1=" TOP ",
        preferred_role_2=" ",
        preferred_role_3="",
        now=T0,
    )
    assert match.participants[0].preferences == ("TOP",)

    match = await service.join_match(
        match.id,
        2,
        preferred_role_1="JUNGLE",
        preferred_role_2="MID",
        preferred_role_3=None,
        now=T0,
    )
    joined = next(item for item in match.participants if item.user_id == 2)
    assert joined.preferences == ("JUNGLE", "MID")

    match = await service.update_preferences(
        match.id,
        2,
        "JUNGLE",
        now=T0,
    )
    changed = next(item for item in match.participants if item.user_id == 2)
    assert changed.preferences == ("JUNGLE",)

    queried = await service.get_match(match.id)
    assert queried is not None
    assert next(item for item in queried.participants if item.user_id == 1).preferences == ("TOP",)
    assert next(item for item in queried.participants if item.user_id == 2).preferences == ("JUNGLE",)

    # A legacy all-null row remains readable even in a role-enabled match.
    async with service.repository.pool.acquire() as conn:
        await conn.execute(
            """UPDATE match_participants
               SET preferred_role_1 = NULL, preferred_role_2 = NULL,
                   preferred_role_3 = NULL
               WHERE match_id = $1 AND user_id = $2""",
            match.id,
            1,
        )
    legacy = await service.get_match(match.id)
    assert legacy is not None
    assert next(item for item in legacy.participants if item.user_id == 1).preferences == ()


@pytest.mark.asyncio
async def test_role_preference_validation_rejects_missing_first_dependency_and_each_duplicate_pair(
    service_and_scope,
):
    service, guild_id, channel_id = service_and_scope
    await service.start_season(guild_id, "지망 검증 테스트", manager_override=True, now=T0)
    await _place(service, guild_id, 1, ("TOP", "JUNGLE", "MID"))

    with pytest.raises(InvalidRolePreferencesError, match="1지망"):
        await service.create_match(
            guild_id, channel_id, 1, "첫 지망 없음", preferred_role_1=None, now=T0
        )
    with pytest.raises(InvalidRolePreferencesError, match="3지망"):
        await service.create_match(
            guild_id,
            channel_id,
            1,
            "세 번째 단독",
            preferred_role_1="TOP",
            preferred_role_3="MID",
            now=T0,
        )

    for values in (
        ("TOP", "TOP", None),
        ("TOP", "JUNGLE", "TOP"),
        ("TOP", "JUNGLE", "JUNGLE"),
    ):
        with pytest.raises(InvalidRolePreferencesError, match="서로 달라야"):
            await service.create_match(
                guild_id,
                channel_id,
                1,
                "중복 지망",
                preferred_role_1=values[0],
                preferred_role_2=values[1],
                preferred_role_3=values[2],
                now=T0,
            )


@pytest.mark.asyncio
async def test_mixed_one_two_three_preferences_work_in_balanced_and_draft(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    preferences = {
        101: ("TOP",),
        102: ("TOP", "JUNGLE"),
        103: ("JUNGLE",),
        104: ("JUNGLE", "TOP"),
        105: ("MID",),
        106: ("MID", "TOP"),
        107: ("ADC",),
        108: ("ADC", "TOP"),
        109: ("SUPPORT",),
        110: ("SUPPORT", "TOP", "MID"),
    }
    await service.start_season(guild_id, "혼합 지망 테스트", manager_override=True, now=T0)
    for user_id, roles in preferences.items():
        await _place_constant(service, guild_id, user_id, roles)

    balanced = await service.create_match(
        guild_id, channel_id, 101, "혼합 Balanced",
        preferred_role_1="TOP", now=T0,
    )
    for user_id in range(102, 111):
        balanced = await service.join_match(
            balanced.id,
            user_id,
            preferred_role_1=preferences[user_id][0],
            preferred_role_2=preferences[user_id][1] if len(preferences[user_id]) > 1 else None,
            preferred_role_3=preferences[user_id][2] if len(preferences[user_id]) > 2 else None,
            now=T0,
        )
    await service.start_match(balanced.id, 101, now=T0)
    for user_id in preferences:
        balanced = await service.toggle_ready(balanced.id, user_id, now=T0)
    assert balanced.status == "PLAYING"
    for team in ("A", "B"):
        assert {item.assigned_role for item in balanced.participants if item.team == team} == set(ROLES)
    # Cancel after checking the assignment so the same roster can exercise
    # Draft without changing role ratings or membership availability.
    await service.cancel_match(balanced.id, 101, now=T0)

    drafted = await service.create_match(
        guild_id, channel_id + 1, 101, "혼합 Draft",
        assignment_mode="DRAFT", preferred_role_1="TOP", now=T0,
    )
    for user_id in range(102, 111):
        drafted = await service.join_match(
            drafted.id,
            user_id,
            preferred_role_1=preferences[user_id][0],
            preferred_role_2=preferences[user_id][1] if len(preferences[user_id]) > 1 else None,
            preferred_role_3=preferences[user_id][2] if len(preferences[user_id]) > 2 else None,
            now=T0,
        )
    await service.start_match(drafted.id, 101, now=T0)
    for user_id in preferences:
        drafted = await service.toggle_ready(drafted.id, user_id, now=T0)
    assert drafted.status == "DRAFTING"
    assert (drafted.captain_a_id, drafted.captain_b_id) == (101, 102)
    for actor, target in (
        (101, 104), (102, 103), (102, 106), (101, 105),
        (101, 107), (102, 108), (102, 110), (101, 109),
    ):
        drafted = await service.draft_pick(drafted.id, actor, target, now=T0)
    assert drafted.status == "PLAYING"
    for team in ("A", "B"):
        assert {item.assigned_role for item in drafted.participants if item.team == team} == set(ROLES)


@pytest.mark.asyncio
async def test_role_preferences_waitlist_balanced_finish_stats_and_ranking(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    await service.start_season(guild_id, "3B 테스트", manager_override=True, now=T0)
    await service.set_role_rating(
        guild_id, 1, "TOP", rating=1500, manager_override=True, now=T0
    )
    with pytest.raises(UnplacedRoleError, match="정글"):
        await service.create_match(
            guild_id, channel_id, 1, "미배치 거절",
            preferred_role_1="TOP", preferred_role_2="JUNGLE", now=T0,
        )
    with pytest.raises(InvalidRolePreferencesError, match="서로 달라야"):
        await service.create_match(
            guild_id, channel_id, 1, "중복 거절",
            preferred_role_1="TOP", preferred_role_2="TOP", now=T0,
        )

    for user_id in range(1, 12):
        await _place(service, guild_id, user_id, _balanced_preferences(user_id))
    first, second = _balanced_preferences(1)
    match = await service.create_match(
        guild_id, channel_id, 1, "라인 내전",
        preferred_role_1=first, preferred_role_2=second, now=T0,
    )
    for user_id in range(2, 12):
        first, second = _balanced_preferences(user_id)
        match = await service.join_match(
            match.id, user_id, preferred_role_1=first, preferred_role_2=second, now=T0,
        )
    assert match.waitlist[0].user_id == 11
    expected_preferences = match.waitlist[0].preferences
    match = await service.leave_match(match.id, 10, now=T0)
    promoted = next(item for item in match.participants if item.user_id == 11)
    assert promoted.preferences == expected_preferences

    await service.start_match(match.id, 1, now=T0)
    for user_id in sorted(item.user_id for item in match.participants):
        match = await service.toggle_ready(match.id, user_id, now=T0)
    assert match.status == "PLAYING"
    for team in ("A", "B"):
        members = [item for item in match.participants if item.team == team]
        assert {item.assigned_role for item in members} == set(ROLES)
        assert all(item.role_rating_snapshot is not None for item in members)

    target = next(item for item in match.participants if item.user_id == 1)
    snapshot = target.role_rating_snapshot
    await service.set_role_rating(
        guild_id, 1, target.assigned_role, rating=2200, manager_override=True, now=T0
    )
    assert (await service.get_match(match.id)).participants[0].role_rating_snapshot == snapshot
    finished = await service.finish_match(match.id, 1, "A", now=T0)
    assert finished.status == "FINISHED"
    with pytest.raises(ResultAlreadyRecordedError):
        await service.finish_match(match.id, 1, "B", now=T0)

    stats = await service.stats(guild_id, 1)
    assert stats.games == 1 and len(stats.role_stats) == 5
    assert (await service.stats(guild_id + 1, 1)).games == 0
    assert (await service.ranking(guild_id, role=target.assigned_role))[0].user_id in {
        item.user_id for item in match.participants
    }
    async with service.repository.pool.acquire() as conn:
        game_id = await conn.fetchval('SELECT id FROM games WHERE "key" = $1', "lol")
        global_count = await conn.fetchval(
            "SELECT count(*) FROM player_ratings WHERE guild_id = $1 AND game_id = $2",
            guild_id, game_id,
        )
        changed_roles = await conn.fetch(
            """SELECT role, games_played FROM player_role_ratings
               WHERE guild_id = $1 AND game_id = $2 AND user_id = 1""",
            guild_id, game_id,
        )
    assert global_count == 0
    assert {row["role"] for row in changed_roles if row["games_played"] == 1} == {
        target.assigned_role
    }


@pytest.mark.asyncio
async def test_draft_state_captains_snake_lock_and_recovery(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    preferences = {
        1: ("TOP", "JUNGLE"), 2: ("TOP", "JUNGLE"),
        3: ("JUNGLE", "TOP"), 4: ("JUNGLE", "TOP"),
        5: ("MID", "TOP"), 6: ("MID", "TOP"),
        7: ("ADC", "TOP"), 8: ("ADC", "TOP"),
        9: ("SUPPORT", "TOP"), 10: ("SUPPORT", "TOP"),
    }
    await service.start_season(guild_id, "Draft 테스트", manager_override=True, now=T0)
    for user_id, roles in preferences.items():
        for role in roles:
            await service.set_role_rating(
                guild_id, user_id, role, rating=1500,
                manager_override=True, now=T0,
            )
    match = await service.create_match(
        guild_id, channel_id, 1, "Draft 내전", assignment_mode="DRAFT",
        preferred_role_1="TOP", preferred_role_2="JUNGLE", now=T0,
    )
    for user_id in range(2, 11):
        match = await service.join_match(
            match.id, user_id,
            preferred_role_1=preferences[user_id][0],
            preferred_role_2=preferences[user_id][1], now=T0,
        )
    await service.start_match(match.id, 1, now=T0)
    for user_id in range(1, 11):
        match = await service.toggle_ready(match.id, user_id, now=T0)
    assert (match.status, match.captain_a_id, match.captain_b_id) == ("DRAFTING", 1, 2)
    assert (await service.get_active_match(guild_id, channel_id)).status == "DRAFTING"
    with pytest.raises(PermissionDeniedError):
        await service.draft_pick(match.id, 2, 4, now=T0)

    concurrent = await asyncio.gather(
        service.draft_pick(match.id, 1, 4, now=T0),
        service.draft_pick(match.id, 1, 5, now=T0),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in concurrent) == 1
    match = await service.get_match(match.id)
    first_pick = next(item.user_id for item in match.participants if item.draft_order == 1)
    remaining_a = 5 if first_pick == 4 else 4
    for actor, target in ((2, 3), (2, 6), (1, remaining_a), (1, 7), (2, 8), (2, 10), (1, 9)):
        match = await service.draft_pick(match.id, actor, target, now=T0)
    assert match.status == "PLAYING" and match.draft_pick_index == 8
    for team in ("A", "B"):
        assert {item.assigned_role for item in match.participants if item.team == team} == set(ROLES)


@pytest.mark.asyncio
async def test_draft_timeout_cancels_and_is_idempotent(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    preferences = {
        user_id: (ROLES[(user_id - 1) % 5], ROLES[user_id % 5])
        for user_id in range(60001, 60011)
    }
    await service.start_season(guild_id, "Draft 만료 테스트", manager_override=True, now=T0)
    for user_id, roles in preferences.items():
        for role in roles:
            await service.set_role_rating(
                guild_id, user_id, role, rating=1500,
                manager_override=True, now=T0,
            )
    match = await service.create_match(
        guild_id, channel_id, 60001, "Draft 만료", assignment_mode="DRAFT",
        preferred_role_1=preferences[60001][0],
        preferred_role_2=preferences[60001][1], now=T0,
    )
    for user_id in range(60002, 60011):
        match = await service.join_match(
            match.id, user_id,
            preferred_role_1=preferences[user_id][0],
            preferred_role_2=preferences[user_id][1], now=T0,
        )
    await service.start_match(match.id, 60001, now=T0)
    for user_id in range(60001, 60011):
        match = await service.toggle_ready(match.id, user_id, now=T0)
    assert match.status == "DRAFTING"
    deadline = match.draft_deadline_at
    assert deadline == T0 + timedelta(seconds=120)
    captain = match.captain_a_id
    target = next(item.user_id for item in match.participants if item.team is None)
    with pytest.raises(InvalidMatchStateError):
        await service.draft_pick(match.id, captain, target, now=deadline)

    events = await service.process_due_matches(now=deadline)
    assert len(events) == 1 and events[0].kind == "draft_expired"
    cancelled = await service.get_match(match.id)
    assert cancelled.status == "CANCELLED"
    assert cancelled.cancel_reason == "지명 시간 만료"
    assert cancelled.ended_at == deadline
    assert cancelled.draft_deadline_at is None
    assert await service.process_due_matches(now=deadline) == []
