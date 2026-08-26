from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from inhouse_bot.repositories.matches import ActiveMembershipError


T0 = datetime(2032, 1, 1, tzinfo=timezone.utc)


async def _game(service, guild_id: int) -> str:
    key = f"test_{guild_id}"
    async with service.repository.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO games
                   ("key", name, team_size, capacity, default_rating,
                    k_factor, rating_enabled, role_rating_enabled)
               VALUES ($1, '보이스 테스트', 1, 2, 1000, 32, FALSE, FALSE)
               ON CONFLICT ("key") DO NOTHING""",
            key,
        )
    return key


async def _playing(service, guild_id, channel_id, creator_id, member_id, key, category_id):
    match = await service.create_match(
        guild_id, channel_id, creator_id, "보이스 테스트",
        game_key=key, voice_category_id=category_id, now=T0,
    )
    await service.join_match(match.id, member_id, now=T0)
    await service.start_match(match.id, creator_id, now=T0)
    await service.toggle_ready(match.id, creator_id, now=T0)
    return await service.toggle_ready(match.id, member_id, now=T0)


@pytest.mark.asyncio
async def test_active_membership_concurrency_and_guild_isolation(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    key = await _game(service, guild_id)
    first = await service.create_match(
        guild_id, channel_id, 1, "첫 내전", game_key=key,
        voice_category_id=10, now=T0,
    )
    second = await service.create_match(
        guild_id, channel_id + 1, 2, "둘째 내전", game_key=key,
        voice_category_id=11, now=T0,
    )

    results = await asyncio.gather(
        service.join_match(first.id, 99, now=T0),
        service.join_match(second.id, 99, now=T0),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, ActiveMembershipError) for result in results) == 1
    with pytest.raises(ActiveMembershipError, match="번째 내전"):
        await service.create_match(
            guild_id, channel_id + 2, 99, "중복 내전", game_key=key, now=T0
        )

    foreign = await service.create_match(
        guild_id + 1, channel_id + 3, 99, "다른 서버", game_key=key, now=T0
    )
    assert foreign.guild_id == guild_id + 1


@pytest.mark.asyncio
async def test_voice_ids_constraint_cleanup_schedule_and_partial_clear(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    service.voice_cleanup_delay_seconds = 600
    key = await _game(service, guild_id)
    match = await _playing(service, guild_id, channel_id, 1, 2, key, 10)
    match = await service.set_voice_channel_id(match.id, "A", 1001)
    match = await service.set_voice_channel_id(match.id, "B", 1002)
    assert (match.voice_category_id, match.team_a_voice_channel_id, match.team_b_voice_channel_id) == (
        10, 1001, 1002
    )

    with pytest.raises(Exception):
        await service.set_voice_channel_id(match.id, "B", 1001, replace_missing=True)
    assert (await service.get_match(match.id)).team_b_voice_channel_id == 1002

    finished = await service.finish_match(match.id, 1, "A", now=T0)
    assert finished.voice_cleanup_at == T0 + timedelta(seconds=600)
    assert [item.id for item in await service.list_due_voice_cleanups(T0)] == []
    assert [item.id for item in await service.list_due_voice_cleanups(T0 + timedelta(seconds=600))] == [match.id]

    retry_at = T0 + timedelta(seconds=700)
    partial = await service.record_voice_cleanup(
        match.id, clear_team_a=True, clear_team_b=False, retry_at=retry_at
    )
    assert partial.team_a_voice_channel_id is None
    assert partial.team_b_voice_channel_id == 1002
    assert partial.voice_cleanup_at == retry_at


@pytest.mark.asyncio
async def test_empty_voice_claim_is_conditional_and_closed_channel_stays_closed(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    key = await _game(service, guild_id)
    match = await _playing(service, guild_id, channel_id, 1, 2, key, 10)
    match = await service.set_voice_channel_id(match.id, "A", 1001)
    match = await service.set_voice_channel_id(match.id, "B", 1002)

    claims = await asyncio.gather(
        service.claim_empty_voice_channel(guild_id, 1001, now=T0),
        service.claim_empty_voice_channel(guild_id, 1001, now=T0),
    )
    assert sum(claim is not None for claim in claims) == 1
    claimed_match, team = next(claim for claim in claims if claim is not None)
    assert team == "A" and claimed_match.team_a_voice_closed_at == T0
    assert [item.id for item in await service.list_due_voice_cleanups(T0)] == [match.id]

    reopened = await service.reopen_empty_voice_channel(match.id, "A", 1001)
    assert reopened.team_a_voice_closed_at is None
    claimed = await service.claim_empty_voice_channel(guild_id, 1001, now=T0)
    assert claimed is not None
    completed = await service.complete_empty_voice_channel(match.id, "A", 1001)
    assert completed.team_a_voice_channel_id is None
    assert completed.team_a_voice_closed_at == T0
    assert completed.team_b_voice_channel_id == 1002
