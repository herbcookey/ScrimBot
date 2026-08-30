-- Durable Discord delivery state and Draft inactivity deadlines.
-- This migration is intentionally self-contained and rerunnable for the
-- integration test fixture, which does not maintain Supabase migration
-- history.
BEGIN;

ALTER TABLE public.matches
    ADD COLUMN IF NOT EXISTS draft_deadline_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS recruitment_reminder_sent_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS recruitment_reminder_retry_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS recruitment_reminder_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS recruitment_reminder_token UUID;

UPDATE public.matches
SET recruitment_reminder_attempts = 0
WHERE recruitment_reminder_attempts IS NULL;

ALTER TABLE public.matches
    ALTER COLUMN recruitment_reminder_attempts SET DEFAULT 0,
    ALTER COLUMN recruitment_reminder_attempts SET NOT NULL;

ALTER TABLE public.matches
    DROP CONSTRAINT IF EXISTS matches_recruitment_reminder_attempts_check,
    ADD CONSTRAINT matches_recruitment_reminder_attempts_check
        CHECK (recruitment_reminder_attempts >= 0);

-- Rows written by the pre-delivery schema have no way to distinguish a
-- claimed reminder from a sent one. Treat those historical claims as sent so
-- a migration does not unexpectedly duplicate an old notification. New rows
-- use recruitment_reminder_sent_at as the durable acknowledgement.
UPDATE public.matches
SET recruitment_reminder_sent_at = recruitment_reminded_at
WHERE recruitment_reminder_sent_at IS NULL
  AND recruitment_reminded_at IS NOT NULL;

-- Give any currently abandoned Draft a bounded recovery window instead of
-- leaving it permanently active. New Draft rows receive their exact deadline
-- in the application transaction.
UPDATE public.matches
SET draft_deadline_at = COALESCE(created_at, now()) + interval '120 seconds'
WHERE status = 'DRAFTING'
  AND ended_at IS NULL
  AND draft_deadline_at IS NULL;

CREATE INDEX IF NOT EXISTS matches_draft_due_idx
    ON public.matches (draft_deadline_at)
    WHERE ended_at IS NULL AND status = 'DRAFTING';

CREATE INDEX IF NOT EXISTS matches_reminder_retry_idx
    ON public.matches (recruitment_reminder_retry_at)
    WHERE ended_at IS NULL AND status = 'RECRUITING';

COMMIT;
