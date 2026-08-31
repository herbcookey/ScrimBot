"""asyncpg 연결 풀 처리."""

from typing import Any


async def create_pool(database_url: str, **kwargs: Any) -> Any:
    """``database_url``로 asyncpg 연결 풀을 만든다."""

    import asyncpg

    kwargs.setdefault("min_size", 1)
    return await asyncpg.create_pool(dsn=database_url, **kwargs)


async def close_pool(pool: Any | None) -> None:
    """만들어진 연결 풀이 있으면 닫는다."""

    if pool is not None:
        await pool.close()
