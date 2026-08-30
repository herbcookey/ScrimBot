"""Regression coverage for match deadlines, waitlists, and MMR boundaries.

These tests use the same PostgreSQL-only fixture as the rest of the repository
tests.  They are skipped automatically when TEST_DATABASE_URL is not set.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from inhouse_bot.repositories.matches import (
    InvalidCapacityError,
    InvalidMatchStateError,
    InvalidRolePreferencesError,
    Match,
)


UTC = timezone.utc
T0 = datetime(2032, 1, 1, tzinfo=UTC)


async def _ensure_game(
    service,
    guild_id: int,
    *,
    team_size: int,
    capacity: int,
    role_rating_enabled: bool = False,
    rating_enabled: bool = True,
) -> str:
    key = f"test_{guild_id}"
    async with service.repository.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO games
                ("key", name, team_size, capacity, default_rating, k_factor,
                 rating_enabled, role_rating_enabled)
            VALUES ($1, $2, $3, $4, 1000, 32, $5, $6)
            ON CONFLICT ("key") DO UPDATE SET
                team_size = EXCLUDED.team_size,
                capacity = EXCLUDED.capacity,
                default_rating = EXCLUDED.default_rating,
                k_factor = EXCLUDED.k_factor,
                rating_enabled = EXCLUDED.rating_enabled,
                role_rating_enabled = EXCLUDED.role_rating_enabled
            """,
            key,
            "State regression game",
            team_size,
            capacity,
            rating_enabled,
            role_rating_enabled,
        )
    return key


async def _recruiting_match(
    service,
    guild_id: int,
    channel_id: int,
    game_key: str,
    user_base: int,
    *,
    now: datetime = T0,
    recruitment_minutes: int = 5,
) -> Match:
    return await service.create_match(
        guild_id,
        channel_id,
        user_base + 1,
        "deadline regression",
        game_key=game_key,
        now=now,
        recruitment_minutes=recruitment_minutes,
    )


async def _fill(
    service,
    match: Match,
    user_base: int,
    capacity: int,
    *,
    now: datetime = T0,
) -> Match:
    for offset in range(1, capacity):
        match = await service.join_match(match.id, user_base + 1 + offset, now=now)
    return match


@pytest.mark.asyncio
async def test_deadline_boundaries_reject_all_mutations_and_due_processing_is_idempotent(
    service_and_scope,
):
    service, guild_id, channel_id = service_and_scope
    game_key = await _ensure_game(service, guild_id, team_size=2, capacity=4)
    deadline = T0 + timedelta(minutes=5)

    # Recruitment-phase mutations: strictly before the deadline are allowed;
    # at and after it are rejected while the row lock is held.
    for index, operation in enumerate(("join", "leave", "preferences", "begin")):
        base = (index + 1) * 1000
        match = await _recruiting_match(
            service, guild_id, channel_id + index, game_key, base
        )
        if operation == "join":
            before = await service.join_match(
                match.id, base + 2, now=deadline - timedelta(seconds=1)
            )
            assert before.participant_count == 2
            with pytest.raises(InvalidMatchStateError):
                await service.join_match(match.id, base + 3, now=deadline)
            with pytest.raises(InvalidMatchStateError):
                await service.join_match(match.id, base + 4, now=deadline + timedelta(seconds=1))
        elif operation == "leave":
            before = await service.leave_match(
                match.id, base + 1, now=deadline - timedelta(seconds=1)
            )
            assert before.participant_count == 0
            # A fresh match is used for the boundary checks because the
            # before-deadline leave intentionally removes the only member.
            match = await _recruiting_match(
                service, guild_id, channel_id + 10 + index, game_key, base + 100
            )
            with pytest.raises(InvalidMatchStateError):
                await service.leave_match(match.id, base + 101, now=deadline)
            with pytest.raises(InvalidMatchStateError):
                await service.leave_match(
                    match.id, base + 101, now=deadline + timedelta(seconds=1)
                )
        elif operation == "preferences":
            before = await service.update_preferences(
                match.id,
                base + 1,
                "TOP",
                "JUNGLE",
                now=deadline - timedelta(seconds=1),
            )
            assert before.participants[0].preferences == ("TOP", "JUNGLE")
            with pytest.raises(InvalidMatchStateError):
                await service.update_preferences(
                    match.id, base + 1, "MID", "ADC", now=deadline
                )
            with pytest.raises(InvalidMatchStateError):
                await service.update_preferences(
                    match.id,
                    base + 1,
                    "MID",
                    "ADC",
                    now=deadline + timedelta(seconds=1),
                )
        else:
            match = await _fill(service, match, base, 4)
            before = await service.start_match(
                match.id, base + 1, now=deadline - timedelta(seconds=1)
            )
            assert before.status == "READY_CHECK"
            boundary_match = await _recruiting_match(
                service, guild_id, channel_id + 20 + index, game_key, base + 100
            )
            boundary_match = await _fill(service, boundary_match, base + 100, 4)
            with pytest.raises(InvalidMatchStateError):
                await service.start_match(
                    boundary_match.id, base + 101, now=deadline
                )
            with pytest.raises(InvalidMatchStateError):
                await service.start_match(
                    boundary_match.id,
                    base + 101,
                    now=deadline + timedelta(seconds=1),
                )

    # Ready-check boundaries use its own deadline and exercise toggle_ready.
    ready_base = 9000
    ready_match = await _recruiting_match(
        service, guild_id, channel_id + 40, game_key, ready_base
    )
    ready_match = await _fill(service, ready_match, ready_base, 4)
    ready_match = await service.start_match(ready_match.id, ready_base + 1, now=T0)
    ready_deadline = T0 + timedelta(seconds=120)
    before = await service.toggle_ready(
        ready_match.id, ready_base + 1, now=ready_deadline - timedelta(seconds=1)
    )
    assert before.ready is True
    await service.cancel_match(ready_match.id, ready_base + 1, now=ready_deadline - timedelta(seconds=2))

    boundary_base = 10000
    boundary = await _recruiting_match(
        service, guild_id, channel_id + 41, game_key, boundary_base
    )
    boundary = await _fill(service, boundary, boundary_base, 4)
    boundary = await service.start_match(boundary.id, boundary_base + 1, now=T0)
    with pytest.raises(InvalidMatchStateError):
        await service.toggle_ready(boundary.id, boundary_base + 1, now=ready_deadline)
    with pytest.raises(InvalidMatchStateError):
        await service.toggle_ready(
            boundary.id,
            boundary_base + 2,
            now=ready_deadline + timedelta(seconds=1),
        )

    # The due worker can safely finish the transition after a rejected action,
    # and a second poll produces no duplicate event.
    events = await service.process_due_matches(now=ready_deadline)
    assert len(events) == 1 and events[0].kind == "ready_expired"
    assert await service.process_due_matches(now=ready_deadline) == []


@pytest.mark.asyncio
async def test_waitlist_preferences_preserve_ready_roster_but_participant_change_resets(
    service_and_scope,
):
    service, guild_id, channel_id = service_and_scope
    game_key = await _ensure_game(service, guild_id, team_size=2, capacity=4)
    base = 20000
    match = await _recruiting_match(service, guild_id, channel_id, game_key, base)
    match = await _fill(service, match, base, 4)
    await service.start_match(match.id, base + 1, now=T0)
    await service.toggle_ready(match.id, base + 1, now=T0)
    await service.toggle_ready(match.id, base + 2, now=T0)
    await service.join_match(match.id, base + 5, now=T0)

    original_deadline = T0 + timedelta(seconds=120)
    async with service.repository.pool.acquire() as conn:
        await conn.execute(
            """UPDATE match_participants
               SET team = 'A', rating_snapshot = 123
               WHERE match_id = $1 AND membership = 'PARTICIPANT' AND user_id = $2""",
            match.id,
            base + 1,
        )
        await conn.execute(
            "UPDATE matches SET ready_deadline_at = $2 WHERE id = $1",
            match.id,
            original_deadline,
        )

    before = await service.get_match(match.id)
    assert before is not None
    before_roster = {
        item.user_id: (item.ready_at, item.team, item.rating_snapshot)
        for item in before.participants
    }
    changed_waitlist = await service.update_preferences(
        match.id,
        base + 5,
        "MID",
        "ADC",
        now=T0 + timedelta(seconds=1),
    )
    assert changed_waitlist.ready_deadline_at == original_deadline
    assert {
        item.user_id: (item.ready_at, item.team, item.rating_snapshot)
        for item in changed_waitlist.participants
    } == before_roster
    assert changed_waitlist.waitlist[0].preferences == ("MID", "ADC")

    changed_participant = await service.update_preferences(
        match.id,
        base + 1,
        "MID",
        "ADC",
        now=T0 + timedelta(seconds=2),
    )
    assert changed_participant.ready_deadline_at == T0 + timedelta(seconds=122)
    assert changed_participant.ready_count == 0
    assert all(
        item.ready_at is None
        and item.team is None
        and item.rating_snapshot is None
        and item.role_rating_snapshot is None
        for item in changed_participant.participants
    )


@pytest.mark.asyncio
async def test_kick_rejects_at_and_after_recruitment_and_ready_deadlines(
    service_and_scope,
):
    service, guild_id, channel_id = service_and_scope
    game_key = await _ensure_game(service, guild_id, team_size=2, capacity=4)

    ready_base = 25000
    ready_match = await _recruiting_match(
        service, guild_id, channel_id, game_key, ready_base
    )
    ready_match = await _fill(service, ready_match, ready_base, 4)
    await service.start_match(ready_match.id, ready_base + 1, now=T0)
    ready_deadline = T0 + timedelta(seconds=120)
    with pytest.raises(InvalidMatchStateError):
        await service.kick_match_member(
            ready_match.id, ready_base + 1, ready_base + 1, now=ready_deadline
        )
    with pytest.raises(InvalidMatchStateError):
        await service.kick_match_member(
            ready_match.id,
            ready_base + 1,
            ready_base + 1,
            now=ready_deadline + timedelta(seconds=1),
        )
    events = await service.process_due_matches(now=ready_deadline)
    assert len(events) == 1 and events[0].kind == "ready_expired"
    assert await service.process_due_matches(now=ready_deadline) == []

    recruitment_base = 26000
    recruitment_match = await _recruiting_match(
        service, guild_id, channel_id + 1, game_key, recruitment_base
    )
    recruitment_deadline = T0 + timedelta(minutes=5)
    with pytest.raises(InvalidMatchStateError):
        await service.kick_match_member(
            recruitment_match.id,
            recruitment_base + 1,
            recruitment_base + 1,
            now=recruitment_deadline,
        )
    with pytest.raises(InvalidMatchStateError):
        await service.kick_match_member(
            recruitment_match.id,
            recruitment_base + 1,
            recruitment_base + 1,
            now=recruitment_deadline + timedelta(seconds=1),
        )
    events = await service.process_due_matches(now=recruitment_deadline)
    assert len(events) == 1 and events[0].kind == "recruitment_expired"
    assert await service.process_due_matches(now=recruitment_deadline) == []


@pytest.mark.asyncio
async def test_role_rating_matches_reject_non_five_v_five_config_early(
    service_and_scope,
):
    service, guild_id, channel_id = service_and_scope
    game_key = await _ensure_game(
        service,
        guild_id,
        team_size=3,
        capacity=6,
        role_rating_enabled=True,
    )
    for index, mode in enumerate(("BALANCED", "DRAFT")):
        with pytest.raises(InvalidCapacityError, match="5대5"):
            await service.create_match(
                guild_id,
                channel_id + index,
                30000 + index,
                "invalid role roster",
                game_key=game_key,
                assignment_mode=mode,
                now=T0,
            )


@pytest.mark.asyncio
async def test_mmr_results_clamp_at_bounds_and_zero_is_ranked(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    game_key = await _ensure_game(service, guild_id, team_size=3, capacity=6)
    base = 40000
    match = await _recruiting_match(service, guild_id, channel_id, game_key, base)
    match = await _fill(service, match, base, 6)
    match = await service.start_match(match.id, base + 1, now=T0)
    for user_id in range(base + 1, base + 7):
        match = await service.toggle_ready(match.id, user_id, now=T0)
    winner = next(item.team for item in match.participants if item.user_id == base + 1)

    async with service.repository.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO player_ratings
                (guild_id, game_id, season_id, user_id, rating, games_played, updated_at)
            SELECT $1, $2, $3, user_id, 10000, 0, $4
            FROM match_participants
            WHERE match_id = $5 AND membership = 'PARTICIPANT'
            ON CONFLICT (guild_id, game_id, season_id, user_id)
            DO UPDATE SET rating = EXCLUDED.rating, games_played = 0, updated_at = EXCLUDED.updated_at
            """,
            guild_id,
            match.game_id,
            match.season_id,
            T0,
            match.id,
        )

    finished = await service.finish_match(match.id, base + 1, winner, now=T0)
    assert finished.status == "FINISHED"
    async with service.repository.pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT user_id, rating FROM player_ratings
               WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
               ORDER BY user_id""",
            guild_id,
            match.game_id,
            match.season_id,
        )
        await conn.execute(
            """UPDATE player_ratings SET rating = 0
               WHERE guild_id = $1 AND game_id = $2 AND season_id = $3 AND user_id = $4""",
            guild_id,
            match.game_id,
            match.season_id,
            base + 1,
        )
    assert all(0 <= int(row["rating"]) <= 10000 for row in rows)
    ranking = await service.ranking(
        guild_id, game_key=game_key, season_id=match.season_id, limit=25
    )
    assert any(item.user_id == base + 1 and item.rating == 0 for item in ranking)

    # The administrative role-rating setter accepts both bounds but rejects
    # values outside them.  Flipping this test game to role MMR is safe after
    # its only match has finished and lets the same season be reused.
    async with service.repository.pool.acquire() as conn:
        await conn.execute(
            'UPDATE games SET role_rating_enabled = TRUE WHERE "key" = $1',
            game_key,
        )
    with pytest.raises(InvalidRolePreferencesError):
        await service.set_role_rating(
            guild_id, base + 1, "TOP", rating=-1, manager_override=True, now=T0
        )
    with pytest.raises(InvalidRolePreferencesError):
        await service.set_role_rating(
            guild_id, base + 1, "TOP", rating=10001, manager_override=True, now=T0
        )
    assert await service.set_role_rating(
        guild_id, base + 1, "TOP", rating=0, manager_override=True, now=T0
    ) == 0
    assert await service.set_role_rating(
        guild_id, base + 1, "JUNGLE", rating=10000, manager_override=True, now=T0
    ) == 10000
    role_ranking = await service.ranking(
        guild_id,
        game_key=game_key,
        season_id=match.season_id,
        role="TOP",
        limit=25,
    )
    assert any(item.user_id == base + 1 and item.rating == 0 for item in role_ranking)
