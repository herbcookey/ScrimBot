from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from inhouse_bot.repositories.matches import (
    InvalidRolePreferencesError,
    PermissionDeniedError,
    ResultAlreadyRecordedError,
    UnplacedRoleError,
)
from inhouse_bot.role_assignment import ROLES


T0 = datetime(2031, 1, 1, tzinfo=timezone.utc)


def _balanced_preferences(user_id: int) -> tuple[str, str]:
    index = (user_id - 1) % 5
    return ROLES[index], ROLES[(index + 1) % 5]


async def _place(service, guild_id: int, user_id: int, preferences: tuple[str, str]) -> None:
    for offset, role in enumerate(preferences):
        await service.set_role_rating(
            guild_id, user_id, role, rating=1500 + user_id * 10 - offset * 5,
            manage_guild=True, now=T0,
        )


@pytest.mark.asyncio
async def test_role_preferences_waitlist_balanced_finish_stats_and_ranking(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    await service.start_season(guild_id, "3B 테스트", manage_guild=True, now=T0)
    await service.set_role_rating(
        guild_id, 1, "TOP", rating=1500, manage_guild=True, now=T0
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
        guild_id, 1, target.assigned_role, rating=2200, manage_guild=True, now=T0
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
    await service.start_season(guild_id, "Draft 테스트", manage_guild=True, now=T0)
    for user_id, roles in preferences.items():
        for role in roles:
            await service.set_role_rating(
                guild_id, user_id, role, rating=1500,
                manage_guild=True, now=T0,
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
