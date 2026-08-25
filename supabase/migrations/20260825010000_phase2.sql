BEGIN;

ALTER TABLE public.matches
    ADD COLUMN IF NOT EXISTS recruitment_deadline_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS recruitment_reminded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ready_deadline_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cancel_reason TEXT;

ALTER TABLE public.matches
    DROP CONSTRAINT IF EXISTS matches_status_check;

ALTER TABLE public.matches
    ADD CONSTRAINT matches_status_check CHECK (
        status IN ('RECRUITING', 'READY_CHECK', 'PLAYING', 'FINISHED', 'CANCELLED')
    );

ALTER TABLE public.match_participants
    ADD COLUMN IF NOT EXISTS membership TEXT NOT NULL DEFAULT 'PARTICIPANT',
    ADD COLUMN IF NOT EXISTS ready_at TIMESTAMPTZ;

UPDATE public.match_participants
SET membership = 'PARTICIPANT'
WHERE membership IS NULL;

ALTER TABLE public.match_participants
    DROP CONSTRAINT IF EXISTS match_participants_membership_check;

ALTER TABLE public.match_participants
    ADD CONSTRAINT match_participants_membership_check
    CHECK (membership IN ('PARTICIPANT', 'WAITLIST'));

CREATE INDEX IF NOT EXISTS matches_recruitment_due_idx
    ON public.matches (recruitment_deadline_at)
    WHERE ended_at IS NULL AND status = 'RECRUITING';

CREATE INDEX IF NOT EXISTS matches_ready_due_idx
    ON public.matches (ready_deadline_at)
    WHERE ended_at IS NULL AND status = 'READY_CHECK';

CREATE INDEX IF NOT EXISTS match_participants_fifo_idx
    ON public.match_participants (match_id, membership, joined_at, id);

COMMIT;
