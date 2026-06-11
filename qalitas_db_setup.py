"""
qalitas_db_setup.py
===================
CrÃ©e les tables PostgreSQL pour les Agents 2, 3 et 4.
Compatible avec le schÃ©ma existant de l'Agent 1.

Usage:
    python qalitas_db_setup.py
    python qalitas_db_setup.py --drop-recreate   # recrée toutes les tables (DANGER: perd les données)
"""

import argparse
import os
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv()


def get_conn() -> psycopg.Connection:
    dsn = os.getenv("PG_DSN", "").strip()
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")
    return psycopg.connect(dsn)


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

DDL_AGENT2 = """
-- â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
-- AGENT 2 â€” ApplicabilitÃ© rÃ©glementaire
-- â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

-- Registre plateforme des tenants (navigation/switch SUPER_ADMIN)
CREATE TABLE IF NOT EXISTS tenant_directory (
    tenant_id            TEXT PRIMARY KEY,
    company_name         TEXT NOT NULL DEFAULT '',
    has_company_profile  BOOLEAN NOT NULL DEFAULT FALSE,
    documents_count      INTEGER NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION qalitas_refresh_tenant_directory_entry(p_tenant_id text)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_tenant text := lower(trim(coalesce(p_tenant_id, '')));
    v_company_name text := '';
    v_has_company_profile boolean := false;
    v_documents_count integer := 0;
    v_user_count integer := 0;
BEGIN
    IF v_tenant = '' THEN
        RETURN;
    END IF;

    SELECT coalesce(company_name, ''), true
    INTO v_company_name, v_has_company_profile
    FROM company_profiles
    WHERE lower(coalesce(tenant_id, '')) = v_tenant
    LIMIT 1;

    IF NOT FOUND THEN
        v_company_name := '';
        v_has_company_profile := false;
    END IF;

    SELECT COUNT(*)
    INTO v_documents_count
    FROM documents
    WHERE lower(coalesce(tenant_id, '')) = v_tenant;

    SELECT COUNT(*)
    INTO v_user_count
    FROM app_users
    WHERE lower(coalesce(tenant_id, '')) = v_tenant;

    IF v_has_company_profile OR v_documents_count > 0 OR v_user_count > 0 THEN
        INSERT INTO tenant_directory (
            tenant_id,
            company_name,
            has_company_profile,
            documents_count,
            created_at,
            updated_at,
            last_seen_at
        )
        VALUES (
            v_tenant,
            v_company_name,
            v_has_company_profile,
            v_documents_count,
            now(),
            now(),
            now()
        )
        ON CONFLICT (tenant_id) DO UPDATE
            SET company_name = EXCLUDED.company_name,
                has_company_profile = EXCLUDED.has_company_profile,
                documents_count = EXCLUDED.documents_count,
                updated_at = now(),
                last_seen_at = now();
    ELSE
        DELETE FROM tenant_directory WHERE tenant_id = v_tenant;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION qalitas_refresh_tenant_directory_from_profiles()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM qalitas_refresh_tenant_directory_entry(coalesce(NEW.tenant_id, OLD.tenant_id));
    IF TG_OP = 'UPDATE' AND lower(coalesce(NEW.tenant_id, '')) <> lower(coalesce(OLD.tenant_id, '')) THEN
        PERFORM qalitas_refresh_tenant_directory_entry(OLD.tenant_id);
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION qalitas_refresh_tenant_directory_from_documents()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM qalitas_refresh_tenant_directory_entry(coalesce(NEW.tenant_id, OLD.tenant_id));
    IF TG_OP = 'UPDATE' AND lower(coalesce(NEW.tenant_id, '')) <> lower(coalesce(OLD.tenant_id, '')) THEN
        PERFORM qalitas_refresh_tenant_directory_entry(OLD.tenant_id);
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION qalitas_refresh_tenant_directory_from_app_users()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM qalitas_refresh_tenant_directory_entry(coalesce(NEW.tenant_id, OLD.tenant_id));
    IF TG_OP = 'UPDATE' AND lower(coalesce(NEW.tenant_id, '')) <> lower(coalesce(OLD.tenant_id, '')) THEN
        PERFORM qalitas_refresh_tenant_directory_entry(OLD.tenant_id);
    END IF;
    RETURN NULL;
END;
$$;

-- Profil entreprise (un enregistrement par tenant / client)
CREATE TABLE IF NOT EXISTS company_profiles (
    profile_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           TEXT NOT NULL UNIQUE,          -- ex: "tenant_demo"
    company_name        TEXT NOT NULL,
    sector              TEXT,                          -- ex: "Industrie textile"
    sub_sector          TEXT,
    country             TEXT DEFAULT 'TN',
    certifications      TEXT[],                        -- ex: ARRAY['ISO9001','ISO14001','ISO45001']
    headcount_total     INTEGER,
    main_activities     TEXT,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

-- Utilisateurs applicatifs persistÃ©s (auth locale liÃ©e au tenant)
CREATE TABLE IF NOT EXISTS app_users (
    user_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           TEXT NOT NULL,
    username            TEXT NOT NULL,
    password_hash       TEXT NOT NULL,
    role                TEXT NOT NULL CHECK (role IN (
                            'SUPER_ADMIN',
                            'ADMIN_QHSE',
                            'ANALYSTE_CONFORMITE',
                            'AUDITEUR'
                        )),
    display_name        TEXT NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_app_users_tenant_username
    ON app_users (tenant_id, LOWER(username));

ALTER TABLE app_users
    DROP CONSTRAINT IF EXISTS app_users_role_check;
ALTER TABLE app_users
    ADD CONSTRAINT app_users_role_check CHECK (role IN (
        'SUPER_ADMIN',
        'ADMIN_QHSE',
        'ANALYSTE_CONFORMITE',
        'AUDITEUR'
    ));

-- Sites de l'entreprise
CREATE TABLE IF NOT EXISTS company_sites (
    site_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    site_code           TEXT,
    site_name           TEXT NOT NULL,
    city                TEXT,
    region              TEXT,
    site_type           TEXT,                          -- ex: "Site principal"
    employee_count      INTEGER,
    main_activities     TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- Processus et activitÃ©s
CREATE TABLE IF NOT EXISTS company_activities (
    activity_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    site_id             UUID REFERENCES company_sites(site_id),
    process_name        TEXT NOT NULL,
    activity_name       TEXT,
    activity_code       TEXT,
    description         TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- Ã‰quipements
CREATE TABLE IF NOT EXISTS company_equipment (
    equipment_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    site_id             UUID REFERENCES company_sites(site_id),
    internal_code       TEXT,
    designation         TEXT NOT NULL,
    nature              TEXT,
    equipment_type      TEXT,
    category            TEXT,
    location            TEXT,
    serial_number       TEXT,
    state               TEXT,
    brand               TEXT,
    model               TEXT,
    specific_data       TEXT,
    last_intervention   DATE,
    next_intervention   DATE,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- Produits (matiÃ¨res, produits finis, produits chimiques)
CREATE TABLE IF NOT EXISTS company_products (
    product_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    reference           TEXT,
    designation         TEXT NOT NULL,
    family              TEXT,
    category            TEXT,
    product_type        TEXT,
    nature              TEXT,
    is_active           BOOLEAN DEFAULT TRUE,
    unit                TEXT,
    site_name           TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- Aspects environnementaux
CREATE TABLE IF NOT EXISTS environmental_aspects (
    aspect_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    site_id             UUID REFERENCES company_sites(site_id),
    aspect_code         TEXT,
    designation         TEXT NOT NULL,
    domain              TEXT,
    sub_domain          TEXT,
    description         TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- Risques SST (registre complet)
CREATE TABLE IF NOT EXISTS sst_risks (
    risk_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    site_id             UUID REFERENCES company_sites(site_id),
    risk_code           TEXT,
    risk_type           TEXT,
    designation         TEXT NOT NULL,
    domain              TEXT,
    dangers             TEXT,
    activities          TEXT,
    dangerous_situations TEXT,
    damages             TEXT,
    description         TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- Risques SST significatifs (cotÃ©s et Ã©valuÃ©s)
CREATE TABLE IF NOT EXISTS sst_significant_risks (
    sig_risk_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    site_id             UUID REFERENCES company_sites(site_id),
    risk_id             UUID REFERENCES sst_risks(risk_id),
    year                INTEGER,
    activities          TEXT,
    domain              TEXT,
    risk_type           TEXT,
    dangers             TEXT,
    appreciation        TEXT,
    score               INTEGER,
    date_start          DATE,
    date_end            DATE,
    obligations         TEXT,
    exposure            TEXT,
    prevention_efficiency NUMERIC(5,2),
    rpn_efficiency      NUMERIC(10,2),
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- Objectifs stratÃ©giques et KPIs QHSE
CREATE TABLE IF NOT EXISTS strategic_objectives (
    objective_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    objective_text      TEXT NOT NULL,
    process_name        TEXT,
    indicator           TEXT,
    indicator_type      TEXT,                          -- StratÃ©gique / OpÃ©rationnel
    frequency           TEXT,
    calculation_method  TEXT,
    system_scope        TEXT,                          -- Q / E / S / QES
    unit                TEXT,
    strategic_axis      TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- â”€â”€â”€ DÃ©cisions d'applicabilitÃ© (sortie Agent 2) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

CREATE TABLE IF NOT EXISTS applicability_decisions (
    decision_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    requirement_id      UUID NOT NULL REFERENCES requirements(requirement_id) ON DELETE CASCADE,
    -- DÃ©cision
    status              TEXT NOT NULL CHECK (status IN (
                            'APPLICABLE',
                            'NON_APPLICABLE',
                            'APPLICABLE_SOUS_CONDITIONS',
                            'INCERTAIN'
                        )),
    -- Justification structurÃ©e
    justification       TEXT NOT NULL,
    article_ref         TEXT,
    condition_evaluated TEXT,
    company_data_used   TEXT,
    -- PÃ©rimÃ¨tre concernÃ©
    scope_site          TEXT,
    scope_process       TEXT,
    scope_activity      TEXT,
    -- MÃ©tadonnÃ©es
    confidence          REAL,
    is_voluntary        BOOLEAN DEFAULT FALSE,        -- applicabilitÃ© volontaire (alignement stratÃ©gique)
    voluntary_reason    TEXT,
    llm_model           TEXT,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE (profile_id, requirement_id)
);

CREATE INDEX IF NOT EXISTS idx_applicability_profile
    ON applicability_decisions (profile_id);
CREATE INDEX IF NOT EXISTS idx_applicability_status
    ON applicability_decisions (status);
CREATE INDEX IF NOT EXISTS idx_applicability_req
    ON applicability_decisions (requirement_id);

DROP TRIGGER IF EXISTS trg_tenant_directory_company_profiles ON company_profiles;
CREATE TRIGGER trg_tenant_directory_company_profiles
AFTER INSERT OR UPDATE OR DELETE ON company_profiles
FOR EACH ROW EXECUTE FUNCTION qalitas_refresh_tenant_directory_from_profiles();

DROP TRIGGER IF EXISTS trg_tenant_directory_documents ON documents;
CREATE TRIGGER trg_tenant_directory_documents
AFTER INSERT OR UPDATE OR DELETE ON documents
FOR EACH ROW EXECUTE FUNCTION qalitas_refresh_tenant_directory_from_documents();

DROP TRIGGER IF EXISTS trg_tenant_directory_app_users ON app_users;
CREATE TRIGGER trg_tenant_directory_app_users
AFTER INSERT OR UPDATE OR DELETE ON app_users
FOR EACH ROW EXECUTE FUNCTION qalitas_refresh_tenant_directory_from_app_users();

DO $$
DECLARE
    v_tenant text;
BEGIN
    FOR v_tenant IN
        SELECT DISTINCT lower(coalesce(tenant_id, '')) AS tenant_id
            FROM (
                SELECT tenant_id FROM company_profiles
                UNION ALL
                SELECT tenant_id FROM documents
                UNION ALL
                SELECT tenant_id FROM app_users
            ) src
        WHERE lower(coalesce(tenant_id, '')) <> ''
    LOOP
        PERFORM qalitas_refresh_tenant_directory_entry(v_tenant);
    END LOOP;
END;
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'qalitas_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_directory TO qalitas_app;
    END IF;
END;
$$;
"""

DDL_AGENT3 = """
-- â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
-- AGENT 3 â€” ConformitÃ© rÃ©glementaire opÃ©rationnelle
-- â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

-- Non-conformitÃ©s importÃ©es (source: DataSet)
CREATE TABLE IF NOT EXISTS nonconformities (
    nc_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    site_id             UUID REFERENCES company_sites(site_id),
    reference           TEXT NOT NULL,                -- ex: NCR/AI/005/2023
    year                INTEGER,
    nature              TEXT,                         -- RÃ©el / Potentiel
    process_name        TEXT,
    title               TEXT NOT NULL,
    source              TEXT,                         -- Audit Interne / Externe / ContrÃ´les QualitÃ©
    audit_type          TEXT,
    responsible_service TEXT,
    detected_at         DATE,
    state               TEXT,                         -- En cours / ClÃ´turÃ©e
    severity            TEXT,                         -- Mineure / Majeure / Critique
    nc_type             TEXT,                         -- Produit / Service / SÃ©curitÃ©
    frequency           TEXT,
    nc_category         TEXT,
    gravity             TEXT,
    priority            TEXT,
    closed_at           DATE,
    system_scope        TEXT,                         -- Q / E / S / QES
    progress_pct        REAL,
    closure_rate        REAL,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- Rapports d'audit (source: PDF)
CREATE TABLE IF NOT EXISTS audit_reports (
    report_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    reference           TEXT NOT NULL UNIQUE,         -- ex: AI/02/2023
    audit_type          TEXT,                         -- Audit Processus (SystÃ¨me)
    category            TEXT,                         -- ISO 45001 / SA 8000
    nature              TEXT,                         -- Audit Interne / Certification
    system_scope        TEXT,
    date_planned_start  DATE,
    date_planned_end    DATE,
    date_real_start     DATE,
    date_real_end       DATE,
    state               TEXT,
    objectives          TEXT,
    locations_visited   TEXT,
    auditor_names       TEXT,
    raw_text            TEXT,                         -- texte brut extrait du PDF
    source_file         TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- VÃ©rifications de conformitÃ© (sortie Agent 3 â€” par exigence applicable)
CREATE TABLE IF NOT EXISTS compliance_checks (
    check_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    decision_id         UUID NOT NULL REFERENCES applicability_decisions(decision_id) ON DELETE CASCADE,
    requirement_id      UUID NOT NULL REFERENCES requirements(requirement_id),
    -- RÃ©sultat
    compliance_status   TEXT NOT NULL CHECK (compliance_status IN (
                            'CONFORME',
                            'PARTIELLEMENT_CONFORME',
                            'NON_CONFORME',
                            'ABSENCE_DE_PREUVE',
                            'NON_EVALUE'
                        )),
    compliance_score    REAL,                         -- 0.0 Ã  1.0
    -- Analyse
    expected_proofs     TEXT,                         -- preuves attendues
    found_proofs        TEXT,                         -- preuves trouvÃ©es
    missing_proofs      TEXT,                         -- preuves manquantes
    analysis_detail     TEXT,
    -- MÃ©tadonnÃ©es
    evaluation_mode     TEXT DEFAULT 'PERIODIQUE',    -- CONTINU / PERIODIQUE / DECLENCHE
    llm_model           TEXT,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE (profile_id, requirement_id)
);

-- Ã‰carts identifiÃ©s (sortie Agent 3)
CREATE TABLE IF NOT EXISTS gaps (
    gap_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    check_id            UUID NOT NULL REFERENCES compliance_checks(check_id) ON DELETE CASCADE,
    requirement_id      UUID NOT NULL REFERENCES requirements(requirement_id),
    -- Classification
    gap_type            TEXT NOT NULL CHECK (gap_type IN (
                            'NC_REGLEMENTAIRE',
                            'ALERTE',
                            'OPPORTUNITE'
                        )),
    severity            TEXT NOT NULL CHECK (severity IN (
                            'MINEURE',
                            'MAJEURE',
                            'CRITIQUE'
                        )),
    -- Description
    description         TEXT NOT NULL,
    missing_proof       TEXT,
    nonconforming_proof TEXT,
    -- Impact potentiel
    legal_impact        TEXT,
    sst_impact          TEXT,
    env_impact          TEXT,
    financial_impact    TEXT,
    -- PÃ©rimÃ¨tre
    scope_site          TEXT,
    scope_process       TEXT,
    scope_activity      TEXT,
    -- Liens
    linked_nc_id        UUID REFERENCES nonconformities(nc_id),
    -- Traitement
    treatment_priority  TEXT CHECK (treatment_priority IN ('URGENTE', 'HAUTE', 'NORMALE', 'BASSE')),
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- Actions correctives liÃ©es aux Ã©carts
CREATE TABLE IF NOT EXISTS corrective_actions (
    action_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    gap_id              UUID REFERENCES gaps(gap_id) ON DELETE SET NULL,
    nc_id               UUID REFERENCES nonconformities(nc_id) ON DELETE SET NULL,
    -- Description
    action_title        TEXT NOT NULL,
    action_description  TEXT,
    action_type         TEXT CHECK (action_type IN ('CORRECTIVE', 'PREVENTIVE', 'AMELIORATION')),
    -- Pilotage
    responsible         TEXT,
    due_date            DATE,
    expected_proof      TEXT,
    -- Ã‰tat
    state               TEXT DEFAULT 'PLANIFIEE' CHECK (state IN (
                            'PLANIFIEE', 'EN_COURS', 'REALISEE', 'CLÃ”TUREE', 'ANNULEE'
                        )),
    completion_pct      REAL DEFAULT 0,
    closed_at           DATE,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_compliance_profile
    ON compliance_checks (profile_id);
CREATE INDEX IF NOT EXISTS idx_compliance_status
    ON compliance_checks (compliance_status);
CREATE INDEX IF NOT EXISTS idx_gaps_severity
    ON gaps (severity);
CREATE INDEX IF NOT EXISTS idx_gaps_type
    ON gaps (gap_type);
CREATE INDEX IF NOT EXISTS idx_nc_profile
    ON nonconformities (profile_id);
"""

DDL_SCOPE_ENHANCEMENTS = """
-- -------------------------------------------------------------------------
-- Schema v2: scopes organisation/site/processus/activite + preuves metier
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS company_processes (
    process_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id           UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    site_id              UUID REFERENCES company_sites(site_id) ON DELETE CASCADE,
    process_code         TEXT,
    process_name         TEXT NOT NULL,
    description          TEXT,
    created_at           TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_company_processes_profile
    ON company_processes (profile_id);
CREATE INDEX IF NOT EXISTS idx_company_processes_site
    ON company_processes (site_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_company_processes_profile_site_name
    ON company_processes (
        profile_id,
        COALESCE(site_id, '00000000-0000-0000-0000-000000000000'::uuid),
        lower(process_name)
    );

ALTER TABLE company_profiles
    ADD COLUMN IF NOT EXISTS headcount_total INTEGER;
ALTER TABLE company_profiles
    ADD COLUMN IF NOT EXISTS main_activities TEXT;

UPDATE company_profiles cp
SET headcount_total = site_totals.total_employees
FROM (
    SELECT profile_id, SUM(COALESCE(employee_count, 0))::int AS total_employees
    FROM company_sites
    GROUP BY profile_id
) site_totals
WHERE cp.profile_id = site_totals.profile_id
  AND cp.headcount_total IS NULL;

UPDATE company_profiles cp
SET main_activities = first_site.main_activities
FROM (
    SELECT DISTINCT ON (profile_id)
        profile_id,
        main_activities
    FROM company_sites
    WHERE COALESCE(main_activities, '') <> ''
    ORDER BY profile_id, created_at ASC
) first_site
WHERE cp.profile_id = first_site.profile_id
  AND COALESCE(cp.main_activities, '') = '';

ALTER TABLE company_activities
    ADD COLUMN IF NOT EXISTS process_id UUID REFERENCES company_processes(process_id);

INSERT INTO company_processes (profile_id, site_id, process_name)
SELECT DISTINCT
    ca.profile_id,
    ca.site_id,
    ca.process_name
FROM company_activities ca
WHERE ca.process_name IS NOT NULL
    AND btrim(ca.process_name) <> ''
    AND NOT EXISTS (
        SELECT 1
        FROM company_processes cp
        WHERE cp.profile_id = ca.profile_id
            AND COALESCE(cp.site_id, '00000000-0000-0000-0000-000000000000'::uuid)
                = COALESCE(ca.site_id, '00000000-0000-0000-0000-000000000000'::uuid)
            AND lower(cp.process_name) = lower(ca.process_name)
    );

UPDATE company_activities ca
SET process_id = cp.process_id
FROM company_processes cp
WHERE ca.process_id IS NULL
    AND cp.profile_id = ca.profile_id
    AND COALESCE(cp.site_id, '00000000-0000-0000-0000-000000000000'::uuid)
        = COALESCE(ca.site_id, '00000000-0000-0000-0000-000000000000'::uuid)
    AND lower(cp.process_name) = lower(ca.process_name);

ALTER TABLE applicability_decisions
    ADD COLUMN IF NOT EXISTS scope_level TEXT;
ALTER TABLE applicability_decisions
    ADD COLUMN IF NOT EXISTS scope_key TEXT;
ALTER TABLE applicability_decisions
    ADD COLUMN IF NOT EXISTS scope_label TEXT;
ALTER TABLE applicability_decisions
    ADD COLUMN IF NOT EXISTS site_id UUID REFERENCES company_sites(site_id);
ALTER TABLE applicability_decisions
    ADD COLUMN IF NOT EXISTS process_id UUID REFERENCES company_processes(process_id);
ALTER TABLE applicability_decisions
    ADD COLUMN IF NOT EXISTS activity_id UUID REFERENCES company_activities(activity_id);

UPDATE applicability_decisions
SET scope_level = CASE
    WHEN NULLIF(scope_activity, '') IS NOT NULL THEN 'ACTIVITY'
    WHEN NULLIF(scope_process, '') IS NOT NULL THEN 'PROCESS'
    WHEN NULLIF(scope_site, '') IS NOT NULL THEN 'SITE'
    ELSE 'ORGANIZATION'
END
WHERE scope_level IS NULL
    OR btrim(scope_level) = '';

UPDATE applicability_decisions
SET scope_key = CASE
    WHEN activity_id IS NOT NULL THEN 'ACTIVITY:' || activity_id::text
    WHEN process_id IS NOT NULL THEN 'PROCESS:' || process_id::text
    WHEN site_id IS NOT NULL THEN 'SITE:' || site_id::text
    WHEN NULLIF(scope_activity, '') IS NOT NULL THEN 'ACTIVITY:LEGACY:' || md5(lower(COALESCE(scope_site, '') || '|' || COALESCE(scope_process, '') || '|' || scope_activity))
    WHEN NULLIF(scope_process, '') IS NOT NULL THEN 'PROCESS:LEGACY:' || md5(lower(COALESCE(scope_site, '') || '|' || scope_process))
    WHEN NULLIF(scope_site, '') IS NOT NULL THEN 'SITE:LEGACY:' || md5(lower(scope_site))
    ELSE 'ORGANIZATION'
END
WHERE scope_key IS NULL
    OR btrim(scope_key) = '';

UPDATE applicability_decisions
SET scope_label = COALESCE(
    NULLIF(scope_label, ''),
    NULLIF(scope_activity, ''),
    NULLIF(scope_process, ''),
    NULLIF(scope_site, ''),
    'ORGANIZATION'
)
WHERE scope_label IS NULL
    OR btrim(scope_label) = '';

ALTER TABLE applicability_decisions
    ALTER COLUMN scope_level SET DEFAULT 'ORGANIZATION';
ALTER TABLE applicability_decisions
    ALTER COLUMN scope_key SET DEFAULT 'ORGANIZATION';
ALTER TABLE applicability_decisions
    ALTER COLUMN scope_level SET NOT NULL;
ALTER TABLE applicability_decisions
    ALTER COLUMN scope_key SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'applicability_decisions_scope_level_check'
    ) THEN
        ALTER TABLE applicability_decisions
            ADD CONSTRAINT applicability_decisions_scope_level_check
            CHECK (scope_level IN ('ORGANIZATION', 'SITE', 'PROCESS', 'ACTIVITY'));
    END IF;
END $$;

ALTER TABLE compliance_checks
    ADD COLUMN IF NOT EXISTS scope_level TEXT;
ALTER TABLE compliance_checks
    ADD COLUMN IF NOT EXISTS scope_key TEXT;
ALTER TABLE compliance_checks
    ADD COLUMN IF NOT EXISTS scope_label TEXT;
ALTER TABLE compliance_checks
    ADD COLUMN IF NOT EXISTS site_id UUID REFERENCES company_sites(site_id);
ALTER TABLE compliance_checks
    ADD COLUMN IF NOT EXISTS process_id UUID REFERENCES company_processes(process_id);
ALTER TABLE compliance_checks
    ADD COLUMN IF NOT EXISTS activity_id UUID REFERENCES company_activities(activity_id);
ALTER TABLE compliance_checks
    ADD COLUMN IF NOT EXISTS last_evaluated_at TIMESTAMPTZ;
ALTER TABLE compliance_checks
    ADD COLUMN IF NOT EXISTS evaluation_version INTEGER;
ALTER TABLE compliance_checks
    ADD COLUMN IF NOT EXISTS input_hash TEXT;
ALTER TABLE compliance_checks
    ADD COLUMN IF NOT EXISTS needs_recheck BOOLEAN;

UPDATE compliance_checks cc
SET
    scope_level = COALESCE(NULLIF(cc.scope_level, ''), ad.scope_level, 'ORGANIZATION'),
    scope_key = COALESCE(NULLIF(cc.scope_key, ''), ad.scope_key, 'ORGANIZATION'),
    scope_label = COALESCE(NULLIF(cc.scope_label, ''), ad.scope_label, 'ORGANIZATION'),
    site_id = COALESCE(cc.site_id, ad.site_id),
    process_id = COALESCE(cc.process_id, ad.process_id),
    activity_id = COALESCE(cc.activity_id, ad.activity_id),
    last_evaluated_at = COALESCE(cc.last_evaluated_at, cc.updated_at, cc.created_at, now()),
    evaluation_version = COALESCE(cc.evaluation_version, 1),
    needs_recheck = COALESCE(cc.needs_recheck, FALSE)
FROM applicability_decisions ad
WHERE ad.decision_id = cc.decision_id;

UPDATE compliance_checks
SET scope_level = COALESCE(NULLIF(scope_level, ''), 'ORGANIZATION'),
    scope_key = COALESCE(NULLIF(scope_key, ''), 'ORGANIZATION'),
    scope_label = COALESCE(NULLIF(scope_label, ''), 'ORGANIZATION'),
    last_evaluated_at = COALESCE(last_evaluated_at, updated_at, created_at, now()),
    evaluation_version = COALESCE(evaluation_version, 1),
    needs_recheck = COALESCE(needs_recheck, FALSE);

ALTER TABLE compliance_checks
    ALTER COLUMN scope_level SET DEFAULT 'ORGANIZATION';
ALTER TABLE compliance_checks
    ALTER COLUMN scope_key SET DEFAULT 'ORGANIZATION';
ALTER TABLE compliance_checks
    ALTER COLUMN evaluation_version SET DEFAULT 1;
ALTER TABLE compliance_checks
    ALTER COLUMN needs_recheck SET DEFAULT FALSE;
ALTER TABLE compliance_checks
    ALTER COLUMN scope_level SET NOT NULL;
ALTER TABLE compliance_checks
    ALTER COLUMN scope_key SET NOT NULL;
ALTER TABLE compliance_checks
    ALTER COLUMN evaluation_version SET NOT NULL;
ALTER TABLE compliance_checks
    ALTER COLUMN needs_recheck SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'compliance_checks_scope_level_check'
    ) THEN
        ALTER TABLE compliance_checks
            ADD CONSTRAINT compliance_checks_scope_level_check
            CHECK (scope_level IN ('ORGANIZATION', 'SITE', 'PROCESS', 'ACTIVITY'));
    END IF;
END $$;

ALTER TABLE gaps
    ADD COLUMN IF NOT EXISTS scope_level TEXT;
ALTER TABLE gaps
    ADD COLUMN IF NOT EXISTS scope_key TEXT;
ALTER TABLE gaps
    ADD COLUMN IF NOT EXISTS scope_label TEXT;
ALTER TABLE gaps
    ADD COLUMN IF NOT EXISTS site_id UUID REFERENCES company_sites(site_id);
ALTER TABLE gaps
    ADD COLUMN IF NOT EXISTS process_id UUID REFERENCES company_processes(process_id);
ALTER TABLE gaps
    ADD COLUMN IF NOT EXISTS activity_id UUID REFERENCES company_activities(activity_id);

UPDATE gaps g
SET
    scope_level = COALESCE(NULLIF(g.scope_level, ''), cc.scope_level, 'ORGANIZATION'),
    scope_key = COALESCE(NULLIF(g.scope_key, ''), cc.scope_key, 'ORGANIZATION'),
    scope_label = COALESCE(NULLIF(g.scope_label, ''), cc.scope_label, NULLIF(g.scope_activity, ''), NULLIF(g.scope_process, ''), NULLIF(g.scope_site, ''), 'ORGANIZATION'),
    site_id = COALESCE(g.site_id, cc.site_id),
    process_id = COALESCE(g.process_id, cc.process_id),
    activity_id = COALESCE(g.activity_id, cc.activity_id)
FROM compliance_checks cc
WHERE cc.check_id = g.check_id;

UPDATE gaps
SET scope_level = COALESCE(NULLIF(scope_level, ''), 'ORGANIZATION'),
    scope_key = COALESCE(NULLIF(scope_key, ''), 'ORGANIZATION'),
    scope_label = COALESCE(NULLIF(scope_label, ''), NULLIF(scope_activity, ''), NULLIF(scope_process, ''), NULLIF(scope_site, ''), 'ORGANIZATION');

ALTER TABLE gaps
    ALTER COLUMN scope_level SET DEFAULT 'ORGANIZATION';
ALTER TABLE gaps
    ALTER COLUMN scope_key SET DEFAULT 'ORGANIZATION';
ALTER TABLE gaps
    ALTER COLUMN scope_level SET NOT NULL;
ALTER TABLE gaps
    ALTER COLUMN scope_key SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'gaps_scope_level_check'
    ) THEN
        ALTER TABLE gaps
            ADD CONSTRAINT gaps_scope_level_check
            CHECK (scope_level IN ('ORGANIZATION', 'SITE', 'PROCESS', 'ACTIVITY'));
    END IF;
END $$;

ALTER TABLE corrective_actions
    ADD COLUMN IF NOT EXISTS scope_level TEXT;
ALTER TABLE corrective_actions
    ADD COLUMN IF NOT EXISTS scope_key TEXT;
ALTER TABLE corrective_actions
    ADD COLUMN IF NOT EXISTS scope_label TEXT;
ALTER TABLE corrective_actions
    ADD COLUMN IF NOT EXISTS site_id UUID REFERENCES company_sites(site_id);
ALTER TABLE corrective_actions
    ADD COLUMN IF NOT EXISTS process_id UUID REFERENCES company_processes(process_id);
ALTER TABLE corrective_actions
    ADD COLUMN IF NOT EXISTS activity_id UUID REFERENCES company_activities(activity_id);

UPDATE corrective_actions ca
SET
    scope_level = COALESCE(NULLIF(ca.scope_level, ''), g.scope_level, 'ORGANIZATION'),
    scope_key = COALESCE(NULLIF(ca.scope_key, ''), g.scope_key, 'ORGANIZATION'),
    scope_label = COALESCE(NULLIF(ca.scope_label, ''), g.scope_label, 'ORGANIZATION'),
    site_id = COALESCE(ca.site_id, g.site_id),
    process_id = COALESCE(ca.process_id, g.process_id),
    activity_id = COALESCE(ca.activity_id, g.activity_id)
FROM gaps g
WHERE ca.gap_id = g.gap_id;

UPDATE corrective_actions
SET scope_level = COALESCE(NULLIF(scope_level, ''), 'ORGANIZATION'),
    scope_key = COALESCE(NULLIF(scope_key, ''), 'ORGANIZATION'),
    scope_label = COALESCE(NULLIF(scope_label, ''), 'ORGANIZATION');

ALTER TABLE corrective_actions
    ALTER COLUMN scope_level SET DEFAULT 'ORGANIZATION';
ALTER TABLE corrective_actions
    ALTER COLUMN scope_key SET DEFAULT 'ORGANIZATION';
ALTER TABLE corrective_actions
    ALTER COLUMN scope_level SET NOT NULL;
ALTER TABLE corrective_actions
    ALTER COLUMN scope_key SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'corrective_actions_scope_level_check'
    ) THEN
        ALTER TABLE corrective_actions
            ADD CONSTRAINT corrective_actions_scope_level_check
            CHECK (scope_level IN ('ORGANIZATION', 'SITE', 'PROCESS', 'ACTIVITY'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS compliance_evidence (
    evidence_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id           UUID NOT NULL REFERENCES company_profiles(profile_id) ON DELETE CASCADE,
    requirement_id       UUID REFERENCES requirements(requirement_id) ON DELETE SET NULL,
    source_report_id     UUID REFERENCES audit_reports(report_id) ON DELETE SET NULL,
    site_id              UUID REFERENCES company_sites(site_id) ON DELETE SET NULL,
    process_id           UUID REFERENCES company_processes(process_id) ON DELETE SET NULL,
    activity_id          UUID REFERENCES company_activities(activity_id) ON DELETE SET NULL,
    scope_level          TEXT NOT NULL DEFAULT 'ORGANIZATION',
    scope_key            TEXT NOT NULL DEFAULT 'ORGANIZATION',
    scope_label          TEXT,
    title                TEXT NOT NULL,
    file_name            TEXT,
    mime_type            TEXT,
    storage_path         TEXT,
    raw_text             TEXT,
    evidence_type        TEXT,
    source_type          TEXT,
    issued_at            TIMESTAMPTZ,
    uploaded_at          TIMESTAMPTZ DEFAULT now(),
    created_by           TEXT,
    input_hash           TEXT,
    created_at           TIMESTAMPTZ DEFAULT now(),
    updated_at           TIMESTAMPTZ DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'compliance_evidence_scope_level_check'
    ) THEN
        ALTER TABLE compliance_evidence
            ADD CONSTRAINT compliance_evidence_scope_level_check
            CHECK (scope_level IN ('ORGANIZATION', 'SITE', 'PROCESS', 'ACTIVITY'));
    END IF;
END $$;

INSERT INTO compliance_evidence (
    profile_id,
    source_report_id,
    scope_level,
    scope_key,
    scope_label,
    title,
    file_name,
    storage_path,
    raw_text,
    evidence_type,
    source_type,
    issued_at,
    uploaded_at,
    input_hash
)
SELECT
    ar.profile_id,
    ar.report_id,
    'ORGANIZATION',
    'ORGANIZATION',
    'ORGANIZATION',
    COALESCE(NULLIF(ar.reference, ''), NULLIF(ar.audit_type, ''), 'Audit report'),
    NULLIF(ar.source_file, ''),
    NULLIF(ar.source_file, ''),
    ar.raw_text,
    COALESCE(NULLIF(ar.audit_type, ''), 'AUDIT_REPORT'),
    'AUDIT_REPORT',
    COALESCE(
        ar.date_real_end::timestamptz,
        ar.date_real_start::timestamptz,
        ar.date_planned_end::timestamptz,
        ar.date_planned_start::timestamptz
    ),
    COALESCE(ar.created_at, now()),
    md5(
        COALESCE(ar.reference, '') || '|' ||
        COALESCE(ar.source_file, '') || '|' ||
        COALESCE(ar.raw_text, '')
    )
FROM audit_reports ar
WHERE NOT EXISTS (
    SELECT 1
    FROM compliance_evidence ce
    WHERE ce.source_report_id = ar.report_id
);

CREATE INDEX IF NOT EXISTS idx_applicability_scope_ref
    ON applicability_decisions (profile_id, scope_level, scope_key);
CREATE INDEX IF NOT EXISTS idx_compliance_scope_ref
    ON compliance_checks (profile_id, scope_level, scope_key);
CREATE INDEX IF NOT EXISTS idx_gaps_scope_ref
    ON gaps (profile_id, scope_level, scope_key);
CREATE INDEX IF NOT EXISTS idx_actions_scope_ref
    ON corrective_actions (profile_id, scope_level, scope_key);
CREATE INDEX IF NOT EXISTS idx_evidence_profile_scope
    ON compliance_evidence (profile_id, scope_level, scope_key);
CREATE INDEX IF NOT EXISTS idx_evidence_requirement
    ON compliance_evidence (requirement_id);
CREATE INDEX IF NOT EXISTS idx_evidence_source_report
    ON compliance_evidence (source_report_id);

ALTER TABLE applicability_decisions
    DROP CONSTRAINT IF EXISTS applicability_decisions_profile_id_requirement_id_key;
ALTER TABLE compliance_checks
    DROP CONSTRAINT IF EXISTS compliance_checks_profile_id_requirement_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_applicability_profile_requirement_scope
    ON applicability_decisions (profile_id, requirement_id, scope_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_compliance_profile_requirement_scope
    ON compliance_checks (profile_id, requirement_id, scope_key);
"""

DDL_PHASE1_QUALITY = """
-- -------------------------------------------------------------------------
-- Phase 1 â€” QualitÃ© des exigences : ajout normative_strength
-- Capture la force normative de chaque exigence extraite par A1
-- Valeurs : IMPERATIF / CONDITIONNEL / FACULTATIF
-- -------------------------------------------------------------------------

ALTER TABLE requirements
    ADD COLUMN IF NOT EXISTS normative_strength TEXT DEFAULT 'IMPERATIF';

ALTER TABLE requirements
    ADD COLUMN IF NOT EXISTS human_validation_flag TEXT;
"""

DDL_AGENT4 = """
-- â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
-- AGENT 4 â€” Chat RAG (nÃ©cessite l'extension pgvector)
-- â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

-- Sessions de chat utilisateur
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id          UUID REFERENCES company_profiles(profile_id),
    user_role           TEXT,                         -- terrain / expert / direction
    created_at          TIMESTAMPTZ DEFAULT now(),
    last_activity_at    TIMESTAMPTZ DEFAULT now()
);

-- Messages (question + rÃ©ponse + sources)
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id          UUID NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    role                TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content             TEXT NOT NULL,
    -- Pour les messages assistant: sources citÃ©es
    source_requirement_ids  UUID[],
    source_articles         TEXT[],
    source_snippets         TEXT[],
    -- MÃ©tadonnÃ©es qualitÃ©
    has_uncertainty     BOOLEAN DEFAULT FALSE,
    uncertainty_note    TEXT,
    llm_model           TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_session
    ON chat_messages (session_id);
"""

DDL_PGVECTOR = """
-- â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
-- AGENT 4 â€” Embeddings (float4[] natif, pas de pgvector requis)
-- SimilaritÃ© cosine calculÃ©e cÃ´tÃ© Python avec numpy.
-- â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS requirement_embeddings (
    embedding_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requirement_id      UUID NOT NULL REFERENCES requirements(requirement_id) ON DELETE CASCADE,
    chunk_text          TEXT NOT NULL,
    embedding           vector(1536),                 -- text-embedding-3-small
    model               TEXT DEFAULT 'text-embedding-3-small',
    created_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE (requirement_id)
);

-- Migration douce: ancien schÃ©ma float4[] -> vector(1536)
DO $$
DECLARE
    emb_udt text;
BEGIN
    SELECT c.udt_name
        INTO emb_udt
        FROM information_schema.columns c
        WHERE c.table_name = 'requirement_embeddings'
        AND c.column_name = 'embedding'
        LIMIT 1;

    IF emb_udt = '_float4' THEN
        ALTER TABLE requirement_embeddings
            ALTER COLUMN embedding TYPE vector(1536)
            USING embedding::vector(1536);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_embeddings_req
    ON requirement_embeddings (requirement_id);

CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw_cosine
    ON requirement_embeddings USING hnsw (embedding vector_cosine_ops);
"""


# ---------------------------------------------------------------------------
# Fonctions utilitaires
# ---------------------------------------------------------------------------

def _exec(conn: psycopg.Connection, sql: str, label: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print(f"  [OK] {label}")
    except Exception as e:
        conn.rollback()
        print(f"  [ERREUR] {label}: {e}", file=sys.stderr)
        raise


def _pgvector_available(conn: psycopg.Connection) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            return cur.fetchone() is not None
    except Exception:
        return False


def _try_enable_pgvector(conn: psycopg.Connection) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
        print("  [OK] Extension pgvector activÃ©e")
        return True
    except Exception as e:
        conn.rollback()
        print(f"  [ATTENTION] pgvector non disponible: {e}")
        print("             L'Agent 4 (RAG) nÃ©cessitera pgvector. IgnorÃ© pour l'instant.")
        return False


def _drop_all(conn: psycopg.Connection) -> None:
    tables = [
        "requirement_embeddings",
        "tenant_directory",
        "app_users",
        "chat_messages",
        "chat_sessions",
        "compliance_evidence",
        "corrective_actions",
        "gaps",
        "compliance_checks",
        "audit_reports",
        "nonconformities",
        "applicability_decisions",
        "strategic_objectives",
        "sst_significant_risks",
        "sst_risks",
        "environmental_aspects",
        "company_products",
        "company_equipment",
        "company_activities",
        "company_processes",
        "company_sites",
        "company_profiles",
    ]
    with conn.cursor() as cur:
        for t in tables:
            cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    conn.commit()
    print("  [OK] Toutes les tables A2/A3/A4 supprimÃ©es")


# ---------------------------------------------------------------------------
# Point d'entrÃ©e
# ---------------------------------------------------------------------------

def setup(drop_recreate: bool = False) -> None:
    print("\n=== QALITAS â€” Setup DB Agents 2 / 3 / 4 ===\n")
    conn = get_conn()

    if drop_recreate:
        print("-- DROP & RECREATE --")
        _drop_all(conn)

    print("-- Agent 2 : tables applicabilitÃ© --")
    _exec(conn, DDL_AGENT2, "tables Agent 2")

    print("\n-- Agent 3 : tables conformitÃ© --")
    _exec(conn, DDL_AGENT3, "tables Agent 3")

    print("\n-- Migration schema v2 : scopes + preuves --")
    _exec(conn, DDL_SCOPE_ENHANCEMENTS, "schema v2 scopes/preuves")

    print("\n-- Phase 1 : qualitÃ© exigences (normative_strength) --")
    _exec(conn, DDL_PHASE1_QUALITY, "colonne normative_strength sur requirements")

    print("\n-- Agent 4 : tables chat --")
    _exec(conn, DDL_AGENT4, "tables Agent 4 (chat)")

    print("\n-- Agent 4 : extension pgvector --")
    if not _try_enable_pgvector(conn):
        conn.close()
        raise RuntimeError("pgvector est requis pour le schema Agent 4 natif")

    print("\n-- Agent 4 : embeddings (vector(1536)) --")
    _exec(conn, DDL_PGVECTOR, "table requirement_embeddings (vector)")

    conn.close()
    print("\n=== Setup terminÃ© ===\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Setup DB Agents 2/3/4")
    parser.add_argument(
        "--drop-recreate",
        action="store_true",
        help="Supprime et recrÃ©e toutes les tables A2/A3/A4 (PERD LES DONNÃ‰ES)",
    )
    args = parser.parse_args()
    setup(drop_recreate=args.drop_recreate)
