"""PostgreSQL 통합 테스트 픽스처."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from uuid import uuid4

import pytest
import pytest_asyncio

SRC = Path(__file__).parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
TEST_BOT_OWNER_ID = 9_876_543_210
if TEST_DATABASE_URL and os.getenv("DATABASE_URL") == TEST_DATABASE_URL:
    pytest.fail("TEST_DATABASE_URL은 DATABASE_URL과 달라야 합니다", pytrace=False)

asyncpg = pytest.importorskip("asyncpg")


async def _require_test_database(conn) -> None:
    is_test_database = await conn.fetchval(
        "SELECT current_setting('inhouse_bot.test_database', true) = 'true'"
    )
    if not is_test_database:
        pytest.fail(
            "테스트 DB sentinel이 없습니다. 전용 DB에 "
            "ALTER DATABASE <db> SET inhouse_bot.test_database = 'true'를 "
            "설정하고 다시 연결하세요.",
            pytrace=False,
        )


async def _migration_applied(conn, migration_name: str) -> bool:
    """이미 적용된 migration은 다시 실행하지 않는다.

    Supabase CLI의 migration 이력을 테스트 DB에 요구하지 않고, 각
    migration이 남기는 최소 스키마 표식을 확인한다. 기존 3A 스키마와
    빈 DB를 같은 픽스처로 다루기 위한 처리다.
    """

    if migration_name.startswith("20260826012146"):
        return bool(await conn.fetchval(
            """
            SELECT to_regclass('public.seasons') IS NOT NULL
               AND to_regclass('public.player_ratings') IS NOT NULL
               AND to_regclass('public.rating_history') IS NOT NULL
            """
        )) and bool(await conn.fetchval(
            """
            SELECT count(*) = 1
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'matches'
              AND column_name = 'season_id'
            """
        )) and bool(await conn.fetchval(
            """
            SELECT count(*) = 1
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'match_participants'
              AND column_name = 'rating_snapshot'
            """
        ))
    if migration_name.startswith("20260826025650"):
        return bool(await conn.fetchval(
            """
            SELECT to_regclass('public.player_role_ratings') IS NOT NULL
               AND to_regclass('public.role_rating_history') IS NOT NULL
            """
        )) and bool(await conn.fetchval(
            """
            SELECT count(*) = 1
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'match_participants'
              AND column_name = 'assigned_role'
            """
        )) and bool(await conn.fetchval(
            """
            SELECT count(*) = 3
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND ((table_name = 'games' AND column_name = 'role_rating_enabled')
                OR (table_name = 'matches' AND column_name IN ('assignment_mode', 'role_rating_enabled')))
            """
        ))
    if migration_name.startswith("20260826034441"):
        return bool(await conn.fetchval(
            """SELECT count(*) = 4
               FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'matches'
                 AND column_name IN (
                    'voice_category_id', 'team_a_voice_channel_id',
                    'team_b_voice_channel_id', 'voice_cleanup_at'
                 )"""
        ))
    if migration_name.startswith("20260826060624"):
        return bool(await conn.fetchval(
            """SELECT count(*) = 2
               FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'matches'
                 AND column_name IN (
                 'team_a_voice_closed_at', 'team_b_voice_closed_at'
                 )"""
        ))
    if migration_name.startswith("20260826063825"):
        return bool(await conn.fetchval(
            "SELECT to_regclass('public.bot_admins') IS NOT NULL"
        ))
    if migration_name.startswith("20260831013642"):
        return bool(await conn.fetchval(
            """
            SELECT EXISTS (
                       SELECT 1 FROM pg_constraint
                       WHERE conrelid = 'public.seasons'::regclass
                         AND conname = 'seasons_name_length_check'
                   )
               AND EXISTS (
                       SELECT 1 FROM pg_constraint
                       WHERE conrelid = 'public.match_participants'::regclass
                         AND conname = 'match_participants_preferences_check'
                         AND pg_get_constraintdef(oid) LIKE '%IS NOT NULL%'
                   )
               AND to_regclass(
                       'public.match_participants_user_id_match_id_idx'
                   ) IS NOT NULL
            """
        ))
    return False


@pytest_asyncio.fixture(scope="session")
async def db_pool():
    """테스트 전용 DB에 미적용 마이그레이션을 순서대로 적용한다."""

    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL이 없어 PostgreSQL 통합 테스트를 건너뜁니다")

    migration_dir = Path(__file__).parents[1] / "supabase" / "migrations"
    migration_paths = sorted(migration_dir.glob("*.sql"))
    assert migration_paths, "PostgreSQL 마이그레이션 파일이 없습니다"
    pool = await asyncpg.create_pool(TEST_DATABASE_URL, min_size=1, max_size=10)
    try:
        async with pool.acquire() as conn:
            await _require_test_database(conn)
            for migration_path in migration_paths:
                if await _migration_applied(conn, migration_path.name):
                    continue
                await conn.execute(migration_path.read_text(encoding="utf-8"))
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def service_and_scope(db_pool, request):
    from inhouse_bot.services.matches import MatchService

    # 다른 테스트 데이터를 건드리지 않게 매번 다른 ID를 쓴다.
    token = uuid4().int
    guild_id = token % 9_000_000_000_000_000_000 + 1
    channel_id = (token >> 64) % 9_000_000_000_000_000_000 + 1
    service = MatchService(db_pool, bot_owner_id=TEST_BOT_OWNER_ID)
    legacy_mode = request.module.__name__.endswith("test_matches")
    if legacy_mode:
        async with db_pool.acquire() as conn:
            await _require_test_database(conn)
            await conn.execute(
                "UPDATE games SET role_rating_enabled = FALSE WHERE \"key\" = 'lol'"
            )
    try:
        yield service, guild_id, channel_id
    finally:
        async with db_pool.acquire() as conn:
            await _require_test_database(conn)
            # 전적 테스트에서 서버 분리를 보려고 guild_id + 1도 쓴다.
            # 다음 테스트에 남지 않게 둘 다 지운다.
            await conn.execute(
                "DELETE FROM bot_admins WHERE guild_id = ANY($1::bigint[])",
                [guild_id, guild_id + 1],
            )
            await conn.execute(
                "DELETE FROM player_role_ratings WHERE guild_id = ANY($1::bigint[])",
                [guild_id, guild_id + 1],
            )
            await conn.execute(
                "DELETE FROM player_ratings WHERE guild_id = ANY($1::bigint[])",
                [guild_id, guild_id + 1],
            )
            await conn.execute(
                "DELETE FROM matches WHERE guild_id = ANY($1::bigint[])",
                [guild_id, guild_id + 1],
            )
            await conn.execute(
                "DELETE FROM seasons WHERE guild_id = ANY($1::bigint[])",
                [guild_id, guild_id + 1],
            )
            await conn.execute(
                'DELETE FROM games WHERE "key" = ANY($1::text[])',
                ["test-3v3", "test-2v2", f"test_{guild_id}"],
            )
            if legacy_mode:
                await conn.execute(
                    "UPDATE games SET role_rating_enabled = TRUE WHERE \"key\" = 'lol'"
                )
