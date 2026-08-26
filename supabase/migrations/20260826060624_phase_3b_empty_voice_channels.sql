BEGIN;

ALTER TABLE public.matches
    ADD COLUMN team_a_voice_closed_at TIMESTAMPTZ,
    ADD COLUMN team_b_voice_closed_at TIMESTAMPTZ;

COMMIT;
