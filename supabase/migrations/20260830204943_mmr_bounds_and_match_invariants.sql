-- MMR values are persisted in several current, snapshot, and history tables.
-- Repair any values written before this invariant existed, then enforce the
-- range for all future writes.  The statements are intentionally rerunnable so
-- a test database without Supabase migration history can apply them safely.
BEGIN;

-- Keep the game default positive (the historical games constraint requires
-- this) while making sure a default can never seed an out-of-range rating.
UPDATE public.games
SET default_rating = GREATEST(1, LEAST(10000, default_rating));

UPDATE public.match_participants
SET rating_snapshot = GREATEST(0, LEAST(10000, rating_snapshot))
WHERE rating_snapshot IS NOT NULL;

UPDATE public.match_participants
SET role_rating_snapshot = GREATEST(0, LEAST(10000, role_rating_snapshot))
WHERE role_rating_snapshot IS NOT NULL;

UPDATE public.player_ratings
SET rating = GREATEST(0, LEAST(10000, rating));

UPDATE public.player_role_ratings
SET rating = GREATEST(0, LEAST(10000, rating));

UPDATE public.rating_history
SET rating_before = GREATEST(0, LEAST(10000, rating_before)),
    rating_after = GREATEST(0, LEAST(10000, rating_after)),
    rating_delta = GREATEST(0, LEAST(10000, rating_after))
                 - GREATEST(0, LEAST(10000, rating_before));

UPDATE public.role_rating_history
SET rating_before = GREATEST(0, LEAST(10000, rating_before)),
    rating_after = GREATEST(0, LEAST(10000, rating_after)),
    rating_delta = GREATEST(0, LEAST(10000, rating_after))
                 - GREATEST(0, LEAST(10000, rating_before));

-- The original schema only checked that game defaults were positive. Replace
-- it with an equivalent lower bound plus the new upper bound.
ALTER TABLE public.games
    DROP CONSTRAINT IF EXISTS games_default_rating_check,
    ADD CONSTRAINT games_default_rating_check
        CHECK (default_rating BETWEEN 1 AND 10000);

ALTER TABLE public.match_participants
    DROP CONSTRAINT IF EXISTS match_participants_rating_snapshot_check,
    ADD CONSTRAINT match_participants_rating_snapshot_check
        CHECK (rating_snapshot IS NULL OR rating_snapshot BETWEEN 0 AND 10000),
    DROP CONSTRAINT IF EXISTS match_participants_role_rating_snapshot_check,
    ADD CONSTRAINT match_participants_role_rating_snapshot_check
        CHECK (
            role_rating_snapshot IS NULL
            OR role_rating_snapshot BETWEEN 0 AND 10000
        );

ALTER TABLE public.player_ratings
    DROP CONSTRAINT IF EXISTS player_ratings_rating_range_check,
    ADD CONSTRAINT player_ratings_rating_range_check
        CHECK (rating BETWEEN 0 AND 10000);

ALTER TABLE public.player_role_ratings
    DROP CONSTRAINT IF EXISTS player_role_ratings_rating_range_check,
    ADD CONSTRAINT player_role_ratings_rating_range_check
        CHECK (rating BETWEEN 0 AND 10000);

ALTER TABLE public.rating_history
    DROP CONSTRAINT IF EXISTS rating_history_rating_before_range_check,
    ADD CONSTRAINT rating_history_rating_before_range_check
        CHECK (rating_before BETWEEN 0 AND 10000),
    DROP CONSTRAINT IF EXISTS rating_history_rating_after_range_check,
    ADD CONSTRAINT rating_history_rating_after_range_check
        CHECK (rating_after BETWEEN 0 AND 10000);

ALTER TABLE public.role_rating_history
    DROP CONSTRAINT IF EXISTS role_rating_history_rating_before_range_check,
    ADD CONSTRAINT role_rating_history_rating_before_range_check
        CHECK (rating_before BETWEEN 0 AND 10000),
    DROP CONSTRAINT IF EXISTS role_rating_history_rating_after_range_check,
    ADD CONSTRAINT role_rating_history_rating_after_range_check
        CHECK (rating_after BETWEEN 0 AND 10000);

COMMIT;
