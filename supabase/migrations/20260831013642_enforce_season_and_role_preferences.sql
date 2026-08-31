BEGIN;

DO $$
DECLARE
    overlong_season_ids BIGINT[];
    invalid_participant_ids BIGINT[];
BEGIN
    SELECT array_agg(id ORDER BY id)
    INTO overlong_season_ids
    FROM public.seasons
    WHERE char_length(name) > 100;

    IF overlong_season_ids IS NOT NULL THEN
        RAISE EXCEPTION
            'seasons.name exceeds 100 characters; offending season IDs: %',
            overlong_season_ids;
    END IF;

    SELECT array_agg(mp.id ORDER BY mp.id)
    INTO invalid_participant_ids
    FROM public.match_participants AS mp
    WHERE (
        (
            mp.preferred_role_1 IS NULL
            AND mp.preferred_role_2 IS NULL
            AND mp.preferred_role_3 IS NULL
        )
        OR (
            mp.preferred_role_1 IS NOT NULL
            AND mp.preferred_role_2 IS NOT NULL
            AND mp.preferred_role_1 IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
            AND mp.preferred_role_2 IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
            AND mp.preferred_role_1 <> mp.preferred_role_2
            AND (
                mp.preferred_role_3 IS NULL
                OR (
                    mp.preferred_role_3 IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
                    AND mp.preferred_role_3 <> mp.preferred_role_1
                    AND mp.preferred_role_3 <> mp.preferred_role_2
                )
            )
        )
    ) IS NOT TRUE;

    IF invalid_participant_ids IS NOT NULL THEN
        RAISE EXCEPTION
            'invalid role preferences; offending match_participant IDs: %',
            invalid_participant_ids;
    END IF;
END
$$;

ALTER TABLE public.seasons
    DROP CONSTRAINT IF EXISTS seasons_name_length_check;

ALTER TABLE public.seasons
    ADD CONSTRAINT seasons_name_length_check
    CHECK (char_length(name) <= 100);

ALTER TABLE public.match_participants
    DROP CONSTRAINT IF EXISTS match_participants_preferences_check;

ALTER TABLE public.match_participants
    ADD CONSTRAINT match_participants_preferences_check
    CHECK (
        (
            preferred_role_1 IS NULL
            AND preferred_role_2 IS NULL
            AND preferred_role_3 IS NULL
        )
        OR (
            preferred_role_1 IS NOT NULL
            AND preferred_role_2 IS NOT NULL
            AND preferred_role_1 IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
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
    );

CREATE INDEX IF NOT EXISTS match_participants_user_id_match_id_idx
    ON public.match_participants (user_id, match_id);

COMMIT;
