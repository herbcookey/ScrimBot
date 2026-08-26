BEGIN;

ALTER TABLE public.games
    ADD COLUMN role_rating_enabled BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE public.games SET role_rating_enabled = TRUE WHERE "key" = 'lol';

ALTER TABLE public.matches
    ADD COLUMN assignment_mode TEXT NOT NULL DEFAULT 'BALANCED',
    ADD COLUMN role_rating_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN captain_a_id BIGINT,
    ADD COLUMN captain_b_id BIGINT,
    ADD COLUMN draft_pick_index INTEGER NOT NULL DEFAULT 0;

ALTER TABLE public.matches
    ADD CONSTRAINT matches_assignment_mode_check
        CHECK (assignment_mode IN ('BALANCED', 'DRAFT')),
    ADD CONSTRAINT matches_draft_pick_index_check
        CHECK (draft_pick_index BETWEEN 0 AND 8),
    DROP CONSTRAINT matches_status_check,
    ADD CONSTRAINT matches_status_check CHECK (
        status IN (
            'RECRUITING', 'READY_CHECK', 'DRAFTING',
            'PLAYING', 'FINISHED', 'CANCELLED'
        )
    );

ALTER TABLE public.match_participants
    ADD COLUMN preferred_role_1 TEXT,
    ADD COLUMN preferred_role_2 TEXT,
    ADD COLUMN preferred_role_3 TEXT,
    ADD COLUMN assigned_role TEXT,
    ADD COLUMN role_rating_snapshot INTEGER,
    ADD COLUMN draft_order INTEGER;

ALTER TABLE public.match_participants
    ADD CONSTRAINT match_participants_preferences_check CHECK (
        (preferred_role_1 IS NULL AND preferred_role_2 IS NULL AND preferred_role_3 IS NULL)
        OR (
            preferred_role_1 IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
            AND preferred_role_2 IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
            AND preferred_role_1 <> preferred_role_2
            AND (
                preferred_role_3 IS NULL
                OR (
                    preferred_role_3 IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
                    AND preferred_role_3 <> preferred_role_1
                    AND preferred_role_3 <> preferred_role_2
                )
            )
        )
    ),
    ADD CONSTRAINT match_participants_assigned_role_check
        CHECK (assigned_role IS NULL OR assigned_role IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')),
    ADD CONSTRAINT match_participants_draft_order_check
        CHECK (draft_order IS NULL OR draft_order BETWEEN 1 AND 8),
    ADD CONSTRAINT match_participants_match_draft_order_key
        UNIQUE (match_id, draft_order);

CREATE TABLE public.player_role_ratings (
    guild_id BIGINT NOT NULL,
    game_id BIGINT NOT NULL REFERENCES public.games (id),
    season_id BIGINT NOT NULL REFERENCES public.seasons (id),
    user_id BIGINT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')),
    rating INTEGER NOT NULL,
    games_played INTEGER NOT NULL DEFAULT 0 CHECK (games_played >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, game_id, season_id, user_id, role)
);

CREATE INDEX player_role_ratings_ranking_idx
    ON public.player_role_ratings
    (guild_id, game_id, season_id, role, rating DESC, games_played DESC, user_id);

CREATE TABLE public.role_rating_history (
    match_id BIGINT NOT NULL REFERENCES public.matches (id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')),
    rating_before INTEGER NOT NULL,
    rating_delta INTEGER NOT NULL,
    rating_after INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (match_id, user_id),
    CONSTRAINT role_rating_history_balance_check
        CHECK (rating_after = rating_before + rating_delta)
);

CREATE INDEX role_rating_history_user_role_idx
    ON public.role_rating_history (user_id, role, created_at DESC);

ALTER TABLE public.player_role_ratings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.role_rating_history ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.player_role_ratings FROM PUBLIC;
REVOKE ALL ON TABLE public.role_rating_history FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE public.player_role_ratings FROM anon;
        REVOKE ALL ON TABLE public.role_rating_history FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE public.player_role_ratings FROM authenticated;
        REVOKE ALL ON TABLE public.role_rating_history FROM authenticated;
    END IF;
END
$$;

COMMIT;
