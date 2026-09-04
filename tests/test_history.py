from __future__ import annotations

from datetime import datetime, timezone
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from inhouse_bot.discord.commands import MatchCommandGroup
from inhouse_bot.discord.renderer import render_match_history, render_match_history_detail
from inhouse_bot.repositories.matches import MatchRepository, SeasonNotFoundError


NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


def _entry(**overrides):
    values = dict(
        id=10, guild_id=123, channel_id=456, message_id=789,
        title="테스트 경기", game_name="League of Legends", season_name="시즌 1",
        ended_at=NOW, assignment_mode="BALANCED", team="A", winner_team="A",
        assigned_role="MID", role_rating_enabled=True, rating_enabled=True,
        rating_before=1240, rating_delta=16, rating_after=1256,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_history_rendering_formats_results_empty_pages_and_limits():
    page = SimpleNamespace(
        entries=(_entry(), _entry(
            id=9, message_id=None, assignment_mode="DRAFT", team="B",
            winner_team="A", role_rating_enabled=False, rating_enabled=False,
            assigned_role=None, rating_before=None, rating_delta=None, rating_after=None,
        )),
        page=3, total_pages=2, scope_name="전체 시즌", total_count=7,
    )
    embed = render_match_history(page, 77)
    text = "\n".join(str(field.value) for field in embed.fields)
    assert "미드 · 1,240 → 1,256 (+16)" in text
    assert "균형 배정" in text and "드래프트" in text
    assert "승리" in text and "패배" in text and "MMR 미사용" in text
    assert text.count("원본 모집 패널") == 1
    assert embed.footer.text == "요청 페이지 3 · 전체 2페이지"
    assert len(embed) <= 6000
    assert len(embed.fields) <= 25
    assert all(len(field.name) <= 256 and len(field.value) <= 1024 for field in embed.fields)

    empty = render_match_history(
        SimpleNamespace(entries=(), page=8, total_pages=1, scope_name="시즌 1"), 77
    )
    assert empty.description == "조건에 맞는 종료 경기 기록이 없습니다."
    assert empty.footer.text == "요청 페이지 8 · 전체 1페이지"


def test_detail_rendering_uses_mentions_history_and_truncates_deterministically():
    participants = tuple(
        SimpleNamespace(
            id=index, user_id=index, team="A" if index % 2 else "B",
            assigned_role=("TOP", "JUNGLE", "MID", "ADC", "SUPPORT")[index % 5],
            rating_before=1000, rating_delta=0, rating_after=1000,
        ) for index in range(1, 501)
    )
    detail = SimpleNamespace(
        id=20, guild_id=123, channel_id=456, message_id=789,
        creator_id=1, title="x" * 500, game_name="g" * 5000, season_name="s" * 5000,
        assignment_mode="BALANCED", role_rating_enabled=True, rating_enabled=True,
        created_at=NOW, started_at=NOW, ended_at=NOW, winner_team="B",
        memo="m" * 3000, participants=participants,
    )
    embed = render_match_history_detail(detail)
    assert embed.to_dict() == render_match_history_detail(detail).to_dict()
    assert len(embed.title) <= 256
    assert len(embed.description) <= 1024
    assert "원본 모집 패널" in embed.description
    assert any(field.name == "결과 메모" for field in embed.fields)
    for team, expected_ids in (("A", list(range(1, 501, 2))), ("B", list(range(2, 501, 2)))):
        fields = [field for field in embed.fields if str(field.name).startswith(f"{team}팀")]
        assert fields
        assert any("승리" in str(field.name) for field in fields) is (team == "B")
        lines = [line for field in fields for line in str(field.value).splitlines()]
        shown_ids = [int(match.group(1)) for line in lines if (match := re.search(r"<@(\d+)>", line))]
        omitted = [int(match.group(1)) for line in lines if (match := re.fullmatch(r"외 (\d+)명", line))]
        assert shown_ids == expected_ids[:len(shown_ids)]
        assert omitted == [len(expected_ids) - len(shown_ids)]
        assert all(
            re.fullmatch(r".+ · <@\d+> · 1,000 → 1,000 \(0\)", line)
            for line in lines if not line.startswith("외 ")
        )
    assert len(embed) <= 6000 and len(embed.fields) <= 25
    assert all(len(field.name) <= 256 and len(field.value) <= 1024 for field in embed.fields)


class _Response:
    def __init__(self):
        self.done = False
        self.defer = AsyncMock(side_effect=self._mark_done)
        self.send_message = AsyncMock()

    def is_done(self):
        return self.done

    def _mark_done(self, **kwargs):
        self.done = True


def _interaction(*, guild=True):
    return SimpleNamespace(
        guild=SimpleNamespace(id=123) if guild else None,
        channel=SimpleNamespace(id=456), user=SimpleNamespace(id=77),
        response=_Response(), followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_history_commands_default_target_validate_positive_and_reply_ephemeral():
    result = SimpleNamespace(entries=(), page=1, total_pages=1, scope_name="전체 시즌")
    detail = SimpleNamespace(
        id=1, guild_id=123, channel_id=456, message_id=789, status="PLAYING"
    )
    service = SimpleNamespace(
        match_history=AsyncMock(return_value=result),
        match_history_detail=AsyncMock(return_value=detail),
    )
    group = MatchCommandGroup(service)
    interaction = _interaction()
    await MatchCommandGroup.history.callback(group, interaction, None, None, None, 1)
    service.match_history.assert_awaited_once_with(
        123, 77, game_key="lol", season_id=None, page=1
    )
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True

    interaction = _interaction()
    await MatchCommandGroup.history_detail.callback(group, interaction, 1)
    sent = interaction.followup.send.await_args.args[0]
    assert "현재 모집 패널" in sent and "/123/456/789" in sent
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True

    interaction = _interaction()
    await MatchCommandGroup.history.callback(group, interaction, None, None, None, 0)
    assert "1 이상" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("detail", "message"),
    [
        (None, "경기를 찾을 수 없습니다."),
        (SimpleNamespace(status="CANCELLED", winner_team=None), "조회할 수 있는 종료 경기 기록이 없습니다."),
        (SimpleNamespace(status="FINISHED", winner_team=None), "조회할 수 있는 종료 경기 기록이 없습니다."),
    ],
)
async def test_history_detail_hides_missing_foreign_and_unavailable_states(detail, message):
    service = SimpleNamespace(match_history_detail=AsyncMock(return_value=detail))
    group = MatchCommandGroup(service)
    interaction = _interaction()
    await MatchCommandGroup.history_detail.callback(group, interaction, 10)
    assert interaction.followup.send.await_args.args[0] == message


def test_history_command_registration_and_game_autocomplete():
    assert MatchCommandGroup.history.name == "기록"
    assert MatchCommandGroup.history_detail.name == "경기조회"
    assert MatchCommandGroup.history._params["user"].required is False
    assert MatchCommandGroup.history._params["game"].autocomplete is not None
    page = MatchCommandGroup.history._params["page"]
    assert page.default == 1 and page.min_value == 1
    assert MatchCommandGroup.history_detail._params["match_id"].min_value == 1


def test_history_models_are_publicly_exported():
    from inhouse_bot.repositories import MatchHistoryEntry, MatchHistoryParticipant
    from inhouse_bot.services import MatchHistoryDetail, MatchHistoryPage

    assert MatchHistoryEntry and MatchHistoryParticipant
    assert MatchHistoryDetail and MatchHistoryPage


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_repository_history_is_batched_scoped_and_deterministic():
    row = vars(_entry())
    conn = SimpleNamespace(
        fetchrow=AsyncMock(side_effect=[{"id": 1, "role_rating_enabled": False}]),
        fetchval=AsyncMock(return_value=1),
        fetch=AsyncMock(return_value=[row]),
    )
    page = await MatchRepository(_Pool(conn)).match_history(123, 77, page=2)
    assert page.total_count == 1 and page.page == 2 and page.total_pages == 1
    sql = conn.fetch.await_args.args[0]
    assert "m.guild_id = $1" in sql
    assert "ORDER BY m.ended_at DESC NULLS LAST, m.id DESC" in sql
    assert "LIMIT $5 OFFSET $6" in sql
    assert "player_ratings" not in sql and "player_role_ratings" not in sql
    assert conn.fetch.await_args.args[-2:] == (5, 5)

    no_season_conn = SimpleNamespace(
        fetchrow=AsyncMock(side_effect=[{"id": 1, "role_rating_enabled": True}, None]),
        fetchval=AsyncMock(), fetch=AsyncMock(),
    )
    empty = await MatchRepository(_Pool(no_season_conn)).match_history(123, 77)
    assert empty.entries == () and empty.total_pages == 1
    no_season_conn.fetchval.assert_not_awaited()
    no_season_conn.fetch.assert_not_awaited()

    wrong_scope_conn = SimpleNamespace(
        fetchrow=AsyncMock(side_effect=[{"id": 1, "role_rating_enabled": False}, None])
    )
    with pytest.raises(SeasonNotFoundError):
        await MatchRepository(_Pool(wrong_scope_conn)).match_history(
            123, 77, season_id=999
        )


@pytest.mark.asyncio
async def test_repository_detail_forces_guild_scope_and_batches_participant_history():
    header = dict(
        id=20, guild_id=123, channel_id=456, message_id=789, creator_id=1,
        title="경기", status="FINISHED", game_name="LoL", season_name="시즌 1",
        assignment_mode="BALANCED", role_rating_enabled=True, rating_enabled=True,
        created_at=NOW, started_at=NOW, ended_at=NOW, winner_team="A", memo=None,
    )
    participant = dict(
        id=1, user_id=77, team="A", joined_at=NOW, assigned_role="MID",
        rating_before=1000, rating_delta=16, rating_after=1016,
    )
    conn = SimpleNamespace(
        fetchrow=AsyncMock(return_value=header), fetch=AsyncMock(return_value=[participant])
    )
    detail = await MatchRepository(_Pool(conn)).match_history_detail(123, 20)
    assert detail is not None and detail.participants[0].rating_after == 1016
    assert "WHERE m.guild_id = $1 AND m.id = $2" in conn.fetchrow.await_args.args[0]
    sql = conn.fetch.await_args.args[0]
    assert "m.guild_id = $1" in sql and "m.id = $2" in sql
    assert "player_ratings" not in sql and "player_role_ratings" not in sql
    assert "WHEN 'TOP' THEN 1" in sql and "joined_at ASC NULLS LAST" in sql


async def _insert_match(
    conn, *, guild_id, channel_id, game_id, season_id, user_id,
    status="FINISHED", membership="PARTICIPANT", team="A", winner="A",
    ended_at=NOW, role_enabled=False, assigned_role=None,
):
    match_id = await conn.fetchval(
        """INSERT INTO matches (
               guild_id, channel_id, game_id, creator_id, title, capacity, status,
               created_at, started_at, ended_at, season_id, role_rating_enabled
           ) VALUES ($1, $2, $3, $4, $5, 10, $6, $7, $7, $8, $9, $10)
           RETURNING id""",
        guild_id, channel_id, game_id, user_id, f"경기 {channel_id}", status,
        NOW, ended_at, season_id, role_enabled,
    )
    participant_id = await conn.fetchval(
        """INSERT INTO match_participants (
               match_id, user_id, team, membership, joined_at, assigned_role
           ) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
        match_id, user_id, team, membership, NOW, assigned_role,
    )
    if winner is not None:
        await conn.execute(
            """INSERT INTO match_results (match_id, winner_team, recorded_by)
               VALUES ($1, $2, $3)""",
            match_id, winner, user_id,
        )
    return int(match_id), int(participant_id)


@pytest.mark.asyncio
async def test_history_postgres_filters_orders_and_pages(service_and_scope, db_pool):
    service, guild_id, channel_id = service_and_scope
    game_key = f"test_{guild_id}"
    async with db_pool.acquire() as conn:
        game_id = await conn.fetchval(
            """INSERT INTO games (
                   "key", name, team_size, capacity, default_rating, k_factor,
                   rating_enabled, role_rating_enabled
               ) VALUES ($1, 'History', 5, 10, 1000, 32, TRUE, FALSE)
               RETURNING id""",
            game_key,
        )
        season_ids = [await conn.fetchval(
            """INSERT INTO seasons (guild_id, game_id, name, started_at, ended_at)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            guild_id, game_id, f"시즌 {index}", NOW, NOW if index == 1 else None,
        ) for index in (1, 2)]
        expected = []
        for index in range(6):
            ended_at = NOW.replace(day=4 - index // 2)
            match_id, _ = await _insert_match(
                conn, guild_id=guild_id, channel_id=channel_id + index,
                game_id=game_id, season_id=season_ids[index % 2], user_id=77,
                ended_at=ended_at,
            )
            expected.append((ended_at, match_id))
            await conn.execute(
                """INSERT INTO rating_history (
                       match_id, user_id, rating_before, rating_delta, rating_after
                   ) VALUES ($1, 77, 1000, 16, 1016)""",
                match_id,
            )
        await _insert_match(
            conn, guild_id=guild_id, channel_id=channel_id + 20, game_id=game_id,
            season_id=season_ids[0], user_id=77, membership="WAITLIST",
        )
        await _insert_match(
            conn, guild_id=guild_id, channel_id=channel_id + 21, game_id=game_id,
            season_id=season_ids[0], user_id=77, status="CANCELLED", winner=None,
        )
        await _insert_match(
            conn, guild_id=guild_id + 1, channel_id=channel_id + 22, game_id=game_id,
            season_id=season_ids[0], user_id=77,
        )

    first = await service.match_history(guild_id, 77, game_key=game_key)
    second = await service.match_history(guild_id, 77, game_key=game_key, page=2)
    assert first.total_count == 6 and first.total_pages == 2 and len(first.entries) == 5
    assert len(second.entries) == 1
    assert [entry.id for entry in (*first.entries, *second.entries)] == [
        match_id for _ended_at, match_id in sorted(expected, reverse=True)
    ]
    assert first.scope_name == "전체 시즌"
    assert first.entries[0].rating_after == 1016

    scoped = await service.match_history(
        guild_id, 77, game_key=game_key, season_id=season_ids[0]
    )
    assert scoped.total_count == 3 and scoped.scope_name == "시즌 1"


@pytest.mark.asyncio
async def test_detail_postgres_is_guild_scoped_and_role_sorted(service_and_scope, db_pool):
    service, guild_id, channel_id = service_and_scope
    game_key = f"test_{guild_id}"
    async with db_pool.acquire() as conn:
        game_id = await conn.fetchval(
            """INSERT INTO games (
                   "key", name, team_size, capacity, default_rating, k_factor,
                   rating_enabled, role_rating_enabled
               ) VALUES ($1, 'Role History', 5, 10, 1000, 32, TRUE, TRUE)
               RETURNING id""",
            game_key,
        )
        season_id = await conn.fetchval(
            """INSERT INTO seasons (guild_id, game_id, name, started_at)
               VALUES ($1, $2, '활성 시즌', $3) RETURNING id""",
            guild_id, game_id, NOW,
        )
        match_id, _ = await _insert_match(
            conn, guild_id=guild_id, channel_id=channel_id, game_id=game_id,
            season_id=season_id, user_id=1, role_enabled=True, assigned_role="SUPPORT",
        )
        for user_id, team, role in (
            (2, "A", "MID"), (3, "A", "TOP"), (4, "B", "ADC"), (5, "B", "JUNGLE")
        ):
            await conn.execute(
                """INSERT INTO match_participants (
                       match_id, user_id, team, membership, joined_at, assigned_role
                   ) VALUES ($1, $2, $3, 'PARTICIPANT', $4, $5)""",
                match_id, user_id, team, NOW, role,
            )
        for user_id, role in ((1, "SUPPORT"), (2, "MID"), (3, "TOP"), (4, "ADC"), (5, "JUNGLE")):
            await conn.execute(
                """INSERT INTO role_rating_history (
                       match_id, user_id, role, rating_before, rating_delta, rating_after
                   ) VALUES ($1, $2, $3, 1000, 0, 1000)""",
                match_id, user_id, role,
            )
        active_id, _ = await _insert_match(
            conn, guild_id=guild_id, channel_id=channel_id + 1, game_id=game_id,
            season_id=season_id, user_id=9, status="PLAYING", winner=None,
            ended_at=None, role_enabled=True, assigned_role="TOP",
        )

    assert await service.match_history_detail(guild_id + 1, match_id) is None
    detail = await service.match_history_detail(guild_id, match_id)
    assert detail is not None
    assert [(item.team, item.assigned_role) for item in detail.participants] == [
        ("A", "TOP"), ("A", "MID"), ("A", "SUPPORT"),
        ("B", "JUNGLE"), ("B", "ADC"),
    ]
    assert all(item.rating_before == 1000 for item in detail.participants)
    active = await service.match_history_detail(guild_id, active_id)
    assert active is not None and active.status == "PLAYING" and not active.participants
