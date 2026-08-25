from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from inhouse_bot.repositories.matches import (
    ActiveMatchExistsError,
    AlreadyJoinedError,
    InvalidMatchStateError,
    InvalidWinnerTeamError,
    Match,
    MatchFullError,
    NotParticipantError,
    PermissionDeniedError,
    ResultAlreadyRecordedError,
)


UTC = timezone.utc
T0 = datetime(2030, 1, 1, tzinfo=UTC)


async def fill(service, guild_id, channel_id, creator_id=1, *, now=T0):
    match = await service.create_match(guild_id, channel_id, creator_id, "test", now=now)
    for user_id in range(2, 11):
        match = await service.join_match(match.id, user_id, now=now)
    return match


async def ready_all(service, match_id, *, now=T0):
    match = await service.start_match(match_id, 1, now=now)
    assert match.status == "READY_CHECK"
    for user_id in range(1, 11):
        match = await service.toggle_ready(match_id, user_id, now=now)
    return match


async def play(service, guild_id, channel_id, *, creator_id=1, now=T0):
    match = await fill(service, guild_id, channel_id, creator_id, now=now)
    return await ready_all(service, match.id, now=now)


@pytest.mark.asyncio
async def test_create_auto_joins_and_partial_unique(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await service.create_match(guild_id, channel_id, 1, "내전", now=T0)
    assert isinstance(match, Match)
    assert match.status == "RECRUITING"
    assert [item.user_id for item in match.participants] == [1]
    assert match.recruitment_deadline_at == T0 + timedelta(minutes=30)

    with pytest.raises(ActiveMatchExistsError):
        await service.create_match(guild_id, channel_id, 2, "두 번째", now=T0)


@pytest.mark.asyncio
async def test_join_duplicate_and_nine_to_ten_concurrency(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await service.create_match(guild_id, channel_id, 1, "내전", now=T0)
    for user_id in range(2, 10):
        await service.join_match(match.id, user_id, now=T0)
    with pytest.raises(AlreadyJoinedError):
        await service.join_match(match.id, 2, now=T0)

    outcomes = await asyncio.gather(
        service.join_match(match.id, 10, now=T0),
        service.join_match(match.id, 11, now=T0),
    )
    assert all(isinstance(item, Match) for item in outcomes)
    current = await service.get_match(match.id)
    assert current is not None
    assert current.participant_count == 10
    assert current.waitlist_count == 1
    assert sorted(item.user_id for item in current.participants) == list(range(1, 11))
    assert [item.user_id for item in current.waitlist] == [11]


@pytest.mark.asyncio
async def test_leave_only_recruiting_and_join_after_leave(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await fill(service, guild_id, channel_id, now=T0)
    await service.leave_match(match.id, 10, now=T0)
    current = await service.get_match(match.id)
    assert current is not None and current.participant_count == 9
    await service.join_match(match.id, 10, now=T0)
    await service.start_match(match.id, 1, now=T0)
    waitlisted = await service.join_match(match.id, 99, now=T0)
    assert waitlisted.waitlisted is True
    assert [item.user_id for item in waitlisted.waitlist] == [99]
    removed_waitlist = await service.leave_match(match.id, 99, now=T0)
    assert removed_waitlist.removed_membership == "WAITLIST"
    changed = await service.leave_match(match.id, 10, now=T0)
    assert changed.status == "RECRUITING"
    assert changed.participant_count == 9


@pytest.mark.asyncio
async def test_ready_check_requires_full_roster_and_real_participant(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await service.create_match(guild_id, channel_id, 1, "내전", now=T0)
    with pytest.raises(PermissionDeniedError):
        await service.start_match(match.id, 2, now=T0)
    with pytest.raises(MatchFullError):
        await service.start_match(match.id, 1, now=T0)

    match = await fill(service, guild_id, channel_id + 1, now=T0)
    ready = await service.start_match(match.id, 1, now=T0)
    assert ready.status == "READY_CHECK"
    assert ready.ready_deadline_at == T0 + timedelta(seconds=120)
    with pytest.raises(NotParticipantError):
        await service.toggle_ready(match.id, 999, now=T0)


@pytest.mark.asyncio
async def test_ready_toggle_and_all_ready_start_once_with_balanced_teams(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await fill(service, guild_id, channel_id, now=T0)
    await service.start_match(match.id, 1, now=T0)

    first = await service.toggle_ready(match.id, 1, now=T0)
    assert first.ready is True and first.ready_count == 1
    second = await service.toggle_ready(match.id, 1, now=T0)
    assert second.ready is False and second.ready_count == 0

    for user_id in range(1, 9):
        await service.toggle_ready(match.id, user_id, now=T0)
    outcomes = await asyncio.gather(
        service.toggle_ready(match.id, 9, now=T0),
        service.toggle_ready(match.id, 10, now=T0),
    )
    assert all(isinstance(item, Match) for item in outcomes)
    assert sum(item.started for item in outcomes) == 1

    current = await service.get_match(match.id)
    assert current is not None and current.status == "PLAYING"
    assert current.started_at is not None and current.ready_deadline_at is None
    assert current.ready_count == 10
    assert sum(item.team == "A" for item in current.participants) == 5
    assert sum(item.team == "B" for item in current.participants) == 5
    with pytest.raises(InvalidMatchStateError):
        await service.start_match(match.id, 1, now=T0)


@pytest.mark.asyncio
async def test_ready_roster_change_resets_ready_and_promotes_fifo(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await fill(service, guild_id, channel_id, now=T0)
    await service.start_match(match.id, 1, now=T0)
    await service.toggle_ready(match.id, 1, now=T0)
    await service.toggle_ready(match.id, 2, now=T0)
    waitlisted = await service.join_match(match.id, 11, now=T0)
    assert waitlisted.waitlisted is True

    changed = await service.leave_match(
        match.id, 1, now=T0 + timedelta(seconds=1)
    )
    assert changed.removed_user_id == 1
    assert changed.promoted_user_ids == (11,)
    assert changed.status == "READY_CHECK"
    assert changed.ready_count == 0
    assert changed.participant_count == 10
    assert changed.waitlist_count == 0
    assert all(item.ready_at is None for item in changed.participants)
    assert next(item for item in changed.participants if item.user_id == 11).team is None


@pytest.mark.asyncio
async def test_waitlist_fifo_duplicate_cancel_and_leave_kick_promotion(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await fill(service, guild_id, channel_id, now=T0)
    first = await service.join_match(match.id, 11, now=T0)
    second = await service.join_match(match.id, 12, now=T0)
    assert first.waitlisted is True and second.waitlisted is True
    with pytest.raises(AlreadyJoinedError):
        await service.join_match(match.id, 11, now=T0)
    with pytest.raises(AlreadyJoinedError):
        await service.join_match(match.id, 2, now=T0)

    cancelled = await service.leave_match(match.id, 12, now=T0)
    assert cancelled.removed_membership == "WAITLIST"
    assert [item.user_id for item in cancelled.waitlist] == [11]

    left = await service.leave_match(match.id, 10, now=T0)
    assert left.promoted_user_ids == (11,)
    assert 11 in [item.user_id for item in left.participants]

    await service.join_match(match.id, 12, now=T0)
    kicked = await service.kick_match_member(match.id, 9, 1, now=T0)
    assert kicked.removed_membership == "PARTICIPANT"
    assert kicked.promoted_user_ids == (12,)
    assert 12 in [item.user_id for item in kicked.participants]


@pytest.mark.asyncio
async def test_concurrent_leave_and_kick_promote_only_once(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await fill(service, guild_id, channel_id, now=T0)
    await service.join_match(match.id, 11, now=T0)

    outcomes = await asyncio.gather(
        service.leave_match(match.id, 10, now=T0),
        service.kick_match_member(match.id, 10, 1, now=T0),
        return_exceptions=True,
    )
    assert sum(isinstance(item, Match) for item in outcomes) == 1
    assert sum(isinstance(item, NotParticipantError) for item in outcomes) == 1
    current = await service.get_match(match.id)
    assert current is not None
    assert current.participant_count == 10
    assert current.waitlist_count == 0
    assert [item.user_id for item in current.participants].count(11) == 1


@pytest.mark.asyncio
async def test_kick_permissions_state_and_self_kick(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await fill(service, guild_id, channel_id, now=T0)
    with pytest.raises(PermissionDeniedError):
        await service.kick_match_member(match.id, 2, 99, now=T0)

    managed = await service.kick_match_member(
        match.id, 2, 99, manage_guild=True, now=T0
    )
    assert managed.removed_user_id == 2

    creator = await service.kick_match_member(match.id, 1, 1, now=T0)
    assert creator.removed_user_id == 1

    playing = await play(service, guild_id, channel_id + 1, now=T0)
    with pytest.raises(InvalidMatchStateError):
        await service.kick_match_member(playing.id, 2, 1, now=T0)


@pytest.mark.asyncio
async def test_ready_expiry_race_has_one_winner(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await fill(service, guild_id, channel_id, now=T0)
    await service.start_match(match.id, 1, now=T0)
    for user_id in range(1, 10):
        await service.toggle_ready(match.id, user_id, now=T0)

    due = T0 + timedelta(seconds=121)
    toggle, expiry = await asyncio.gather(
        service.toggle_ready(match.id, 10, now=due),
        service.process_due_matches(now=due),
        return_exceptions=True,
    )
    current = await service.get_match(match.id)
    assert current is not None
    assert (current.status == "PLAYING") ^ (current.status == "RECRUITING")
    if current.status == "PLAYING":
        assert isinstance(toggle, Match) and toggle.started is True
        assert expiry == []
    else:
        assert isinstance(toggle, InvalidMatchStateError)
        assert len(expiry) == 1 and expiry[0].kind == "ready_expired"
        assert current.participant_count == 9


@pytest.mark.asyncio
async def test_ready_expiry_removes_unready_promotes_and_restarts_check(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await fill(service, guild_id, channel_id, now=T0)
    for user_id in range(11, 16):
        await service.join_match(match.id, user_id, now=T0)
    await service.start_match(match.id, 1, now=T0)
    for user_id in range(1, 6):
        await service.toggle_ready(match.id, user_id, now=T0)

    events = await service.process_due_matches(now=T0 + timedelta(seconds=121))
    assert len(events) == 1 and events[0].kind == "ready_expired"
    event = events[0]
    assert event.removed_user_ids == (6, 7, 8, 9, 10)
    assert event.promoted_user_ids == (11, 12, 13, 14, 15)
    current = event.match
    assert current.status == "READY_CHECK"
    assert current.participant_count == 10 and current.waitlist_count == 0
    assert current.ready_count == 0
    assert current.ready_deadline_at == T0 + timedelta(seconds=121 + 120)


@pytest.mark.asyncio
async def test_recruitment_reminder_once_and_short_recruitment_skips_it(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await service.create_match(
        guild_id, channel_id, 1, "긴 모집", recruitment_minutes=10, now=T0
    )
    events = await service.process_due_matches(now=T0 + timedelta(minutes=6))
    assert [event.kind for event in events] == ["recruitment_reminder"]
    assert await service.process_due_matches(now=T0 + timedelta(minutes=6)) == []
    current = await service.get_match(match.id)
    assert current is not None and current.recruitment_reminded_at is not None

    short = await service.create_match(
        guild_id, channel_id + 1, 1, "짧은 모집", recruitment_minutes=5, now=T0
    )
    assert await service.process_due_matches(now=T0 + timedelta(minutes=1)) == []
    current = await service.get_match(short.id)
    assert current is not None and current.recruitment_reminded_at is None


@pytest.mark.asyncio
async def test_recruitment_expiry_underfull_cancels_and_full_enters_ready(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    underfull = await service.create_match(
        guild_id, channel_id, 1, "미달", recruitment_minutes=5, now=T0
    )
    events = await service.process_due_matches(now=T0 + timedelta(minutes=5))
    assert len(events) == 1 and events[0].kind == "recruitment_expired"
    cancelled = await service.get_match(underfull.id)
    assert cancelled is not None
    assert cancelled.status == "CANCELLED"
    assert cancelled.ended_at == T0 + timedelta(minutes=5)
    assert cancelled.cancel_reason == "모집 시간 만료"
    assert await service.process_due_matches(now=T0 + timedelta(minutes=6)) == []

    full = await fill(service, guild_id, channel_id + 1, now=T0)
    events = await service.process_due_matches(now=T0 + timedelta(minutes=30))
    assert len(events) == 1 and events[0].kind == "recruitment_expired"
    ready = await service.get_match(full.id)
    assert ready is not None and ready.status == "READY_CHECK"
    assert ready.ready_deadline_at == T0 + timedelta(minutes=30, seconds=120)
    assert ready.ended_at is None


@pytest.mark.asyncio
async def test_finish_cancel_stats_are_guild_scoped_and_zero_safe(service_and_scope):
    service, guild_id, channel_id = service_and_scope

    first = await play(service, guild_id, channel_id, now=T0)
    user_team = next(item.team for item in first.participants if item.user_id == 1)
    await service.finish_match(first.id, 1, user_team, now=T0)

    second = await play(service, guild_id, channel_id + 1, now=T0)
    current_team = next(item.team for item in second.participants if item.user_id == 1)
    other_team = "B" if current_team == "A" else "A"
    await service.finish_match(second.id, 1, other_team, now=T0)

    cancelled = await fill(service, guild_id, channel_id + 2, now=T0)
    await service.cancel_match(cancelled.id, 1, now=T0)

    other_guild = guild_id + 1
    other = await play(service, other_guild, channel_id + 3, now=T0)
    team = next(item.team for item in other.participants if item.user_id == 1)
    await service.finish_match(other.id, 1, team, now=T0)

    stats = await service.stats(guild_id, 1)
    assert stats.games == 2 and stats.wins == 1 and stats.losses == 1
    assert stats.rate == pytest.approx(0.5)
    assert (await service.stats(other_guild, 1)).games == 1
    zero = await service.stats(guild_id, 999)
    assert zero.games == zero.wins == zero.losses == 0
    assert zero.rate == 0


@pytest.mark.asyncio
async def test_finish_result_and_cancel_transitions(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await play(service, guild_id, channel_id, now=T0)
    with pytest.raises(PermissionDeniedError):
        await service.finish_match(match.id, 2, "A", now=T0)
    finished = await service.finish_match(match.id, 1, "A", "gg", now=T0)
    assert finished.status == "FINISHED"
    assert finished.ended_at is not None
    assert finished.result is not None and finished.result.winner_team == "A"
    with pytest.raises(ResultAlreadyRecordedError):
        await service.finish_match(match.id, 1, "B", now=T0)
    with pytest.raises(InvalidWinnerTeamError):
        await service.finish_match(match.id, 1, "C", now=T0)

    recruiting = await service.create_match(
        guild_id, channel_id + 1, 1, "취소", now=T0
    )
    cancelled = await service.cancel_match(recruiting.id, 1, now=T0)
    assert cancelled.status == "CANCELLED" and cancelled.ended_at is not None

    playing = await play(service, guild_id, channel_id + 2, now=T0)
    cancelled_playing = await service.cancel_match(playing.id, 1, now=T0)
    assert cancelled_playing.status == "CANCELLED"
    with pytest.raises(InvalidMatchStateError):
        await service.cancel_match(finished.id, 1, now=T0)
