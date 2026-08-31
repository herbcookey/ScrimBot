import asyncpg
import pytest


@pytest.mark.asyncio
async def test_season_and_role_preference_constraints(db_pool):
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                CREATE TEMP TABLE season_constraint_test
                (LIKE public.seasons INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING IDENTITY)
                ON COMMIT DROP;

                CREATE TEMP TABLE preference_constraint_test
                (LIKE public.match_participants INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING IDENTITY)
                ON COMMIT DROP;
                """
            )

            await conn.execute(
                """INSERT INTO season_constraint_test (guild_id, game_id, name)
                   VALUES (1, 1, repeat('가', 100))"""
            )
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        """INSERT INTO season_constraint_test (guild_id, game_id, name)
                           VALUES (1, 1, repeat('가', 101))"""
                    )

            await conn.execute(
                """
                INSERT INTO preference_constraint_test (match_id, user_id)
                VALUES (1, 1);

                INSERT INTO preference_constraint_test
                    (match_id, user_id, preferred_role_1, preferred_role_2)
                VALUES (1, 2, 'TOP', 'JUNGLE');

                INSERT INTO preference_constraint_test
                    (match_id, user_id, preferred_role_1, preferred_role_2, preferred_role_3)
                VALUES (1, 3, 'TOP', 'JUNGLE', 'MID');
                """
            )

            for values in ((None, "TOP", None), ("TOP", "TOP", None)):
                with pytest.raises(asyncpg.CheckViolationError):
                    async with conn.transaction():
                        await conn.execute(
                            """
                            INSERT INTO preference_constraint_test
                                (match_id, user_id, preferred_role_1,
                                 preferred_role_2, preferred_role_3)
                            VALUES (1, 9, $1, $2, $3)
                            """,
                            *values,
                        )

        index_definition = await conn.fetchval(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'match_participants'
              AND indexname = 'match_participants_user_id_match_id_idx'
            """
        )
        assert index_definition and "(user_id, match_id)" in index_definition
