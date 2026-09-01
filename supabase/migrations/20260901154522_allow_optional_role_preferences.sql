BEGIN;

-- Keep every valid legacy row (including all-null non-role rows), but do not
-- silently repair rows that were admitted through PostgreSQL's three-valued
-- CHECK semantics. Surface those rows before replacing the constraint.
DO $$
DECLARE
    invalid_participant_ids BIGINT[];
BEGIN
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
            AND mp.preferred_role_1 IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
            AND (
                mp.preferred_role_2 IS NULL
                OR (
                    mp.preferred_role_2 IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
                    AND mp.preferred_role_2 <> mp.preferred_role_1
                )
            )
            AND (
                mp.preferred_role_3 IS NULL
                OR (
                    mp.preferred_role_2 IS NOT NULL
                    AND mp.preferred_role_3 IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
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

ALTER TABLE public.match_participants
    DROP CONSTRAINT IF EXISTS match_participants_preferences_check;

ALTER TABLE public.match_participants
    ADD CONSTRAINT match_participants_preferences_check
    CHECK ((
        (
            preferred_role_1 IS NULL
            AND preferred_role_2 IS NULL
            AND preferred_role_3 IS NULL
        )
        OR (
            preferred_role_1 IS NOT NULL
            AND preferred_role_1 IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
            AND (
                preferred_role_2 IS NULL
                OR (
                    preferred_role_2 IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
                    AND preferred_role_2 <> preferred_role_1
                )
            )
            AND (
                preferred_role_3 IS NULL
                OR (
                    preferred_role_2 IS NOT NULL
                    AND preferred_role_3 IN ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
                    AND preferred_role_3 <> preferred_role_1
                    AND preferred_role_3 <> preferred_role_2
                )
            )
        )
    ) IS TRUE);

COMMIT;
