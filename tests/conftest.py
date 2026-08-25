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
if TEST_DATABASE_URL and os.getenv("DATABASE_URL") == TEST_DATABASE_URL:
    pytest.fail("TEST_DATABASE_URL은 DATABASE_URL과 달라야 합니다", pytrace=False)

asyncpg = pytest.importorskip("asyncpg")


@pytest_asyncio.fixture(scope="session")
async def db_pool():
    """테스트 전용 DB에 저장된 마이그레이션을 순서대로 적용한다."""

    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL이 없어 PostgreSQL 통합 테스트를 건너뜁니다")

    migration_dir = Path(__file__).parents[1] / "supabase" / "migrations"
    migration_paths = sorted(migration_dir.glob("*.sql"))
    assert migration_paths, "PostgreSQL 마이그레이션 파일이 없습니다"
    pool = await asyncpg.create_pool(TEST_DATABASE_URL, min_size=1, max_size=10)
    try:
        async with pool.acquire() as conn:
            for migration_path in migration_paths:
                await conn.execute(migration_path.read_text(encoding="utf-8"))
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def service_and_scope(db_pool):
    from inhouse_bot.services.matches import MatchService

    # 다른 테스트 데이터를 건드리지 않게 매번 다른 ID를 쓴다.
    token = uuid4().int
    guild_id = token % 9_000_000_000_000_000_000 + 1
    channel_id = (token >> 64) % 9_000_000_000_000_000_000 + 1
    service = MatchService(db_pool)
    try:
        yield service, guild_id, channel_id
    finally:
        async with db_pool.acquire() as conn:
            # 전적 테스트에서 서버 분리를 보려고 guild_id + 1도 쓴다.
            # 다음 테스트에 남지 않게 둘 다 지운다.
            await conn.execute(
                "DELETE FROM matches WHERE guild_id = ANY($1::bigint[])",
                [guild_id, guild_id + 1],
            )
