BEGIN;

ALTER TABLE public.matches
    ADD COLUMN voice_category_id BIGINT,
    ADD COLUMN team_a_voice_channel_id BIGINT,
    ADD COLUMN team_b_voice_channel_id BIGINT,
    ADD COLUMN voice_cleanup_at TIMESTAMPTZ,
    ADD CONSTRAINT matches_voice_channel_ids_check CHECK (
        team_a_voice_channel_id IS NULL
        OR team_b_voice_channel_id IS NULL
        OR team_a_voice_channel_id <> team_b_voice_channel_id
    );

CREATE INDEX matches_voice_cleanup_due_idx
    ON public.matches (voice_cleanup_at)
    WHERE voice_cleanup_at IS NOT NULL;

COMMIT;
