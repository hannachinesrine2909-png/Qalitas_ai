-- QALITAS - Application role for tenant-aware runtime
-- ------------------------------------------------------------
-- Purpose:
-- 1) use a dedicated non-superuser role for the application
-- 2) let RLS policies in docs/rls_tenant_hardening.sql be effective
--
-- Before running:
-- - replace CHANGE_ME_PASSWORD
-- - execute as a privileged role (owner/superuser)
-- - ensure docs/rls_tenant_hardening.sql was already applied

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'qalitas_app') THEN
        CREATE ROLE qalitas_app LOGIN PASSWORD 'CHANGE_ME_PASSWORD';
    ELSE
        ALTER ROLE qalitas_app WITH LOGIN PASSWORD 'CHANGE_ME_PASSWORD';
    END IF;
END
$$;

ALTER ROLE qalitas_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE qalitas_app SET row_security = on;

GRANT CONNECT ON DATABASE "Qalitas_ai" TO qalitas_app;
GRANT USAGE ON SCHEMA public TO qalitas_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO qalitas_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO qalitas_app;
GRANT USAGE ON TYPE public.vector TO qalitas_app;
GRANT EXECUTE ON FUNCTION public.qalitas_set_tenant(text) TO qalitas_app;
GRANT EXECUTE ON FUNCTION public.qalitas_current_tenant() TO qalitas_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO qalitas_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO qalitas_app;
