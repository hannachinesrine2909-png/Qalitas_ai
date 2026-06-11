-- QALITAS - Tenant RLS hardening (PostgreSQL)
-- ------------------------------------------------------------
-- This script adds Row Level Security policies for tenant isolation.
-- IMPORTANT:
-- 1) Execute with a privileged role (table owner/superuser).
-- 2) Your application connection MUST set the tenant context first:
--      SELECT qalitas_set_tenant('<tenant_id>');
--    (or SET app.tenant_id = '<tenant_id>';)
-- 3) Use a dedicated application role without SUPERUSER/BYPASSRLS in production.
-- 4) Test in staging before production.

-- ------------------------------------------------------------
-- Tenant context helper
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION qalitas_set_tenant(p_tenant text)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v text := lower(trim(coalesce(p_tenant, '')));
BEGIN
    IF v = '' THEN
        RAISE EXCEPTION 'tenant_id vide';
    END IF;
    IF v !~ '^[a-z0-9][a-z0-9_-]{1,63}$' THEN
        RAISE EXCEPTION 'tenant_id invalide: %', p_tenant;
    END IF;
    PERFORM set_config('app.tenant_id', v, false);
END;
$$;

CREATE OR REPLACE FUNCTION qalitas_current_tenant()
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT lower(coalesce(current_setting('app.tenant_id', true), ''))
$$;

-- ------------------------------------------------------------
-- Agent 1 tables
-- ------------------------------------------------------------
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE requirements ENABLE ROW LEVEL SECURITY;
ALTER TABLE requirement_validations ENABLE ROW LEVEL SECURITY;
ALTER TABLE requirement_embeddings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS p_documents_tenant ON documents;
CREATE POLICY p_documents_tenant ON documents
    USING (lower(coalesce(tenant_id, '')) = qalitas_current_tenant())
    WITH CHECK (lower(coalesce(tenant_id, '')) = qalitas_current_tenant());

DROP POLICY IF EXISTS p_requirements_tenant ON requirements;
CREATE POLICY p_requirements_tenant ON requirements
    USING (
        EXISTS (
            SELECT 1
            FROM documents d
            WHERE d.doc_id = requirements.doc_id
              AND lower(coalesce(d.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1
            FROM documents d
            WHERE d.doc_id = requirements.doc_id
              AND lower(coalesce(d.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_requirement_validations_tenant ON requirement_validations;
CREATE POLICY p_requirement_validations_tenant ON requirement_validations
    USING (
        EXISTS (
            SELECT 1
            FROM requirements r
            JOIN documents d ON d.doc_id = r.doc_id
            WHERE r.requirement_id = requirement_validations.requirement_id
              AND lower(coalesce(d.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1
            FROM requirements r
            JOIN documents d ON d.doc_id = r.doc_id
            WHERE r.requirement_id = requirement_validations.requirement_id
              AND lower(coalesce(d.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_requirement_embeddings_tenant ON requirement_embeddings;
CREATE POLICY p_requirement_embeddings_tenant ON requirement_embeddings
    USING (
        EXISTS (
            SELECT 1
            FROM requirements r
            JOIN documents d ON d.doc_id = r.doc_id
            WHERE r.requirement_id = requirement_embeddings.requirement_id
              AND lower(coalesce(d.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1
            FROM requirements r
            JOIN documents d ON d.doc_id = r.doc_id
            WHERE r.requirement_id = requirement_embeddings.requirement_id
              AND lower(coalesce(d.tenant_id, '')) = qalitas_current_tenant()
        )
    );

-- ------------------------------------------------------------
-- Agent 2/3/4 tables (profile-based)
-- ------------------------------------------------------------
ALTER TABLE company_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE company_sites ENABLE ROW LEVEL SECURITY;
ALTER TABLE company_processes ENABLE ROW LEVEL SECURITY;
ALTER TABLE company_activities ENABLE ROW LEVEL SECURITY;
ALTER TABLE company_equipment ENABLE ROW LEVEL SECURITY;
ALTER TABLE company_products ENABLE ROW LEVEL SECURITY;
ALTER TABLE environmental_aspects ENABLE ROW LEVEL SECURITY;
ALTER TABLE sst_risks ENABLE ROW LEVEL SECURITY;
ALTER TABLE sst_significant_risks ENABLE ROW LEVEL SECURITY;
ALTER TABLE strategic_objectives ENABLE ROW LEVEL SECURITY;
ALTER TABLE applicability_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE nonconformities ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE compliance_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE gaps ENABLE ROW LEVEL SECURITY;
ALTER TABLE corrective_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE compliance_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS p_company_profiles_tenant ON company_profiles;
CREATE POLICY p_company_profiles_tenant ON company_profiles
    USING (lower(coalesce(tenant_id, '')) = qalitas_current_tenant())
    WITH CHECK (lower(coalesce(tenant_id, '')) = qalitas_current_tenant());

DROP POLICY IF EXISTS p_app_users_tenant ON app_users;
CREATE POLICY p_app_users_tenant ON app_users
    USING (lower(coalesce(tenant_id, '')) = qalitas_current_tenant())
    WITH CHECK (lower(coalesce(tenant_id, '')) = qalitas_current_tenant());

DROP POLICY IF EXISTS p_company_sites_tenant ON company_sites;
CREATE POLICY p_company_sites_tenant ON company_sites
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = company_sites.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = company_sites.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_company_processes_tenant ON company_processes;
CREATE POLICY p_company_processes_tenant ON company_processes
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = company_processes.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = company_processes.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_company_activities_tenant ON company_activities;
CREATE POLICY p_company_activities_tenant ON company_activities
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = company_activities.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = company_activities.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_company_equipment_tenant ON company_equipment;
CREATE POLICY p_company_equipment_tenant ON company_equipment
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = company_equipment.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = company_equipment.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_company_products_tenant ON company_products;
CREATE POLICY p_company_products_tenant ON company_products
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = company_products.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = company_products.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_environmental_aspects_tenant ON environmental_aspects;
CREATE POLICY p_environmental_aspects_tenant ON environmental_aspects
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = environmental_aspects.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = environmental_aspects.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_sst_risks_tenant ON sst_risks;
CREATE POLICY p_sst_risks_tenant ON sst_risks
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = sst_risks.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = sst_risks.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_sst_significant_risks_tenant ON sst_significant_risks;
CREATE POLICY p_sst_significant_risks_tenant ON sst_significant_risks
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = sst_significant_risks.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = sst_significant_risks.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_strategic_objectives_tenant ON strategic_objectives;
CREATE POLICY p_strategic_objectives_tenant ON strategic_objectives
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = strategic_objectives.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = strategic_objectives.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_applicability_decisions_tenant ON applicability_decisions;
CREATE POLICY p_applicability_decisions_tenant ON applicability_decisions
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = applicability_decisions.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = applicability_decisions.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_nonconformities_tenant ON nonconformities;
CREATE POLICY p_nonconformities_tenant ON nonconformities
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = nonconformities.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = nonconformities.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_audit_reports_tenant ON audit_reports;
CREATE POLICY p_audit_reports_tenant ON audit_reports
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = audit_reports.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = audit_reports.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_compliance_checks_tenant ON compliance_checks;
CREATE POLICY p_compliance_checks_tenant ON compliance_checks
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = compliance_checks.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = compliance_checks.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_gaps_tenant ON gaps;
CREATE POLICY p_gaps_tenant ON gaps
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = gaps.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = gaps.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_corrective_actions_tenant ON corrective_actions;
CREATE POLICY p_corrective_actions_tenant ON corrective_actions
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = corrective_actions.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = corrective_actions.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_compliance_evidence_tenant ON compliance_evidence;
CREATE POLICY p_compliance_evidence_tenant ON compliance_evidence
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = compliance_evidence.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = compliance_evidence.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_chat_sessions_tenant ON chat_sessions;
CREATE POLICY p_chat_sessions_tenant ON chat_sessions
    USING (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = chat_sessions.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM company_profiles p
            WHERE p.profile_id = chat_sessions.profile_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

DROP POLICY IF EXISTS p_chat_messages_tenant ON chat_messages;
CREATE POLICY p_chat_messages_tenant ON chat_messages
    USING (
        EXISTS (
            SELECT 1
            FROM chat_sessions s
            JOIN company_profiles p ON p.profile_id = s.profile_id
            WHERE s.session_id = chat_messages.session_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1
            FROM chat_sessions s
            JOIN company_profiles p ON p.profile_id = s.profile_id
            WHERE s.session_id = chat_messages.session_id
              AND lower(coalesce(p.tenant_id, '')) = qalitas_current_tenant()
        )
    );

-- ------------------------------------------------------------
-- Optional strict mode (recommended for production)
-- ------------------------------------------------------------
-- Uncomment to force table owners/superusers to obey RLS as well.
-- ALTER TABLE documents FORCE ROW LEVEL SECURITY;
-- ALTER TABLE requirements FORCE ROW LEVEL SECURITY;
-- ALTER TABLE requirement_validations FORCE ROW LEVEL SECURITY;
-- ALTER TABLE requirement_embeddings FORCE ROW LEVEL SECURITY;
-- ALTER TABLE company_profiles FORCE ROW LEVEL SECURITY;
-- ALTER TABLE app_users FORCE ROW LEVEL SECURITY;
-- ALTER TABLE company_sites FORCE ROW LEVEL SECURITY;
-- ALTER TABLE company_processes FORCE ROW LEVEL SECURITY;
-- ALTER TABLE company_activities FORCE ROW LEVEL SECURITY;
-- ALTER TABLE company_equipment FORCE ROW LEVEL SECURITY;
-- ALTER TABLE company_products FORCE ROW LEVEL SECURITY;
-- ALTER TABLE environmental_aspects FORCE ROW LEVEL SECURITY;
-- ALTER TABLE sst_risks FORCE ROW LEVEL SECURITY;
-- ALTER TABLE sst_significant_risks FORCE ROW LEVEL SECURITY;
-- ALTER TABLE strategic_objectives FORCE ROW LEVEL SECURITY;
-- ALTER TABLE applicability_decisions FORCE ROW LEVEL SECURITY;
-- ALTER TABLE nonconformities FORCE ROW LEVEL SECURITY;
-- ALTER TABLE audit_reports FORCE ROW LEVEL SECURITY;
-- ALTER TABLE compliance_checks FORCE ROW LEVEL SECURITY;
-- ALTER TABLE gaps FORCE ROW LEVEL SECURITY;
-- ALTER TABLE corrective_actions FORCE ROW LEVEL SECURITY;
-- ALTER TABLE compliance_evidence FORCE ROW LEVEL SECURITY;
-- ALTER TABLE chat_sessions FORCE ROW LEVEL SECURITY;
-- ALTER TABLE chat_messages FORCE ROW LEVEL SECURITY;
