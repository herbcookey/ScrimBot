BEGIN;

CREATE TABLE public.bot_admins (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    added_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);

ALTER TABLE public.bot_admins ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.bot_admins FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE public.bot_admins FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE public.bot_admins FROM authenticated;
    END IF;
END
$$;

COMMIT;
