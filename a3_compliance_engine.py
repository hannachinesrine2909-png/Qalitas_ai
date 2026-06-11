"""
a3_compliance_engine.py
=======================
Agent 3 - Moteur de conformite reglementaire operationnelle QALITAS.

Pour chaque exigence applicable issue de l'Agent 2, l'agent :
  1. identifie les preuves attendues (type de preuve, periodicite) ;
  2. recherche les preuves existantes en base (NC, audits, enregistrements) ;
  3. evalue l'etat de conformite (CONFORME, PARTIELLEMENT_CONFORME,
     NON_CONFORME, ABSENCE_DE_PREUVE) ;
  4. qualifie les ecarts : type, gravite, impact ;
  5. genere des recommandations d'actions correctives ;
  6. sauvegarde les resultats dans compliance_checks, gaps et corrective_actions.

Usage CLI multi-tenant :
    python a3_compliance_engine.py --tenant <tenant_id>
    python a3_compliance_engine.py --tenant <tenant_id> --limit 10
    python a3_compliance_engine.py --tenant <tenant_id> --summary
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, timedelta
from typing import Any

import psycopg
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

from llm_client import get_llm_client
from a2_scope_resolution import effective_applicability_rows
from tenant_db import connect_db

load_dotenv()


def _configure_stdio_utf8() -> None:
    """Best effort: avoid Windows cp1252 print crashes on unusual chars."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _safe_console_text(value: Any, max_len: int | None = None) -> str:
    text = str(value or "")
    if max_len and max_len > 0:
        text = text[:max_len]
    # Drop hard control chars that can break some consoles/loggers.
    text = "".join(ch if (ch == "\n" or ch == "\t" or ord(ch) >= 32) else " " for ch in text)
    return text


def _normalize_text(value: Any) -> str:
    raw = str(value or "")
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip().lower()


_configure_stdio_utf8()

# Constants
# Ces constantes definissent la politique A3:
# - statuts de conformite autorises;
# - types et gravites des ecarts;
# - priorites de traitement;
# - age maximal accepte pour les preuves selon leur famille.

VALID_COMPLIANCE_STATUSES = {
    "CONFORME", "PARTIELLEMENT_CONFORME",
    "NON_CONFORME", "ABSENCE_DE_PREUVE", "NON_EVALUE",
}
VALID_GAP_TYPES = {"NC_REGLEMENTAIRE", "ALERTE", "OPPORTUNITE"}
VALID_SEVERITIES = {"MINEURE", "MAJEURE", "CRITIQUE"}
VALID_PRIORITIES = {"URGENTE", "HAUTE", "NORMALE", "BASSE"}
PRIORITY_RANK = {"BASSE": 0, "NORMALE": 1, "HAUTE": 2, "URGENTE": 3}
SEVERITY_DEFAULT_PRIORITY = {
    "CRITIQUE": "URGENTE",
    "MAJEURE": "HAUTE",
    "MINEURE": "NORMALE",
}
EVIDENCE_MAX_AGE_DAYS = {
    "AUDIT_REPORT": 365,
    "COMPLIANCE_DOC": 730,
    "NC_CLOSURE": 180,
    "GENERIC": 730,
}

SYSTEM_PROMPT_COMPLIANCE = """Tu es un auditeur reglementaire QHSE senior, oriente terrain et preuve.

Ta mission: evaluer si une exigence reglementaire applicable est effectivement respectee,
en te basant sur les preuves operationnelles disponibles (audits, non-conformites, enregistrements).

REGLES ABSOLUES:
1. Chaque decision doit etre basee sur des preuves concretes, pas sur des hypotheses.
2. Si aucune preuve n'est disponible: "ABSENCE_DE_PREUVE" (jamais "CONFORME" par defaut).
2b. Une preuve expiree ou obsolete ne suffit jamais pour conclure a une conformite pleine.
3. Les ecarts doivent etre qualifies avec precision (gravite, impact juridique/SST/env).
4. Les recommandations doivent etre operationnelles, actionnables et realistes.

NIVEAUX DE CONFORMITE:
- CONFORME: preuves trouvees, valides, completes et dans les delais.
- PARTIELLEMENT_CONFORME: preuves partielles ou incompletes.
- NON_CONFORME: preuves manquantes, obsoletes ou contradictoires.
- ABSENCE_DE_PREUVE: aucune donnee disponible pour evaluer.

FORMAT JSON STRICT:
{
  "compliance_status": "CONFORME|PARTIELLEMENT_CONFORME|NON_CONFORME|ABSENCE_DE_PREUVE",
  "compliance_score": 0.0 a 1.0,
  "expected_proofs": "Description des preuves qui devraient exister",
  "found_proofs": "Preuves trouvees dans les donnees disponibles (ou 'Aucune')",
  "missing_proofs": "Ce qui manque (ou null si conforme)",
  "analysis_detail": "Analyse detaillee de la situation",
  "gaps": [
    {
      "gap_type": "NC_REGLEMENTAIRE|ALERTE|OPPORTUNITE",
      "severity": "MINEURE|MAJEURE|CRITIQUE",
      "description": "Description precise de l'ecart",
      "missing_proof": "Preuve manquante specifique",
      "legal_impact": "Impact juridique potentiel (ou null)",
      "sst_impact": "Impact SST potentiel (ou null)",
      "env_impact": "Impact environnemental potentiel (ou null)",
      "treatment_priority": "URGENTE|HAUTE|NORMALE|BASSE",
      "scope_site": "Site concerne",
      "scope_process": "Processus concerne",
      "scope_activity": "Activite concernee"
    }
  ],
  "corrective_actions": [
    {
      "action_title": "Titre court de l'action",
      "action_description": "Description detaillee",
      "action_type": "CORRECTIVE|PREVENTIVE|AMELIORATION",
      "responsible": "Fonction/service responsable",
      "due_date_days": 30,
      "expected_proof": "Preuve attendue apres realisation"
    }
  ]
}
"""



# Pydantic schemas

class GapLLM(BaseModel):
    """Ecart produit par le LLM puis normalise avant sauvegarde."""

    gap_type: str = "NC_REGLEMENTAIRE"
    severity: str = "MINEURE"
    description: str
    missing_proof: str | None = None
    legal_impact: str | None = None
    sst_impact: str | None = None
    env_impact: str | None = None
    treatment_priority: str = "NORMALE"
    scope_site: str | None = None
    scope_process: str | None = None
    scope_activity: str | None = None

    @field_validator("gap_type", mode="before")
    @classmethod
    def validate_gap_type(cls, v: Any) -> str:
        val = str(v or "").strip().upper()
        return val if val in VALID_GAP_TYPES else "NC_REGLEMENTAIRE"

    @field_validator("severity", mode="before")
    @classmethod
    def validate_severity(cls, v: Any) -> str:
        val = str(v or "").strip().upper()
        return val if val in VALID_SEVERITIES else "MINEURE"

    @field_validator("treatment_priority", mode="before")
    @classmethod
    def validate_priority(cls, v: Any) -> str:
        val = str(v or "").strip().upper()
        return val if val in VALID_PRIORITIES else "NORMALE"


class ActionLLM(BaseModel):
    """Action corrective/preventive proposee pour traiter un ecart."""

    action_title: str
    action_description: str | None = None
    action_type: str = "CORRECTIVE"
    responsible: str | None = None
    due_date_days: int = 30
    expected_proof: str | None = None

    @field_validator("action_type", mode="before")
    @classmethod
    def validate_action_type(cls, v: Any) -> str:
        val = str(v or "").strip().upper()
        return val if val in {"CORRECTIVE", "PREVENTIVE", "AMELIORATION"} else "CORRECTIVE"


class ComplianceCheckLLM(BaseModel):
    """Resultat complet d'un controle de conformite pour une exigence applicable."""

    compliance_status: str = "NON_EVALUE"
    compliance_score: float = 0.0
    expected_proofs: str | None = None
    found_proofs: str | None = None
    missing_proofs: str | None = None
    analysis_detail: str | None = None
    gaps: list[GapLLM] = []
    corrective_actions: list[ActionLLM] = []

    @field_validator("compliance_status", mode="before")
    @classmethod
    def validate_status(cls, v: Any) -> str:
        val = str(v or "").strip().upper()
        return val if val in VALID_COMPLIANCE_STATUSES else "NON_EVALUE"

    @field_validator("compliance_score", mode="before")
    @classmethod
    def clamp_score(cls, v: Any) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.0


def _parse_dateish(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _classify_evidence_age_policy(doc: dict) -> int:
    """Retourne l'age maximal acceptable pour une preuve donnee."""
    source = " ".join(
        str(doc.get(key) or "")
        for key in ("evidence_type", "source_type", "title", "reference", "file_name")
    )
    normalized = _normalize_text(source)
    if any(token in normalized for token in ("nc_closure", "cloture", "closure")):
        return EVIDENCE_MAX_AGE_DAYS["NC_CLOSURE"]
    if "audit" in normalized:
        return EVIDENCE_MAX_AGE_DAYS["AUDIT_REPORT"]
    if any(token in normalized for token in ("registre", "document", "compliance", "preuve", "evidence", "report")):
        return EVIDENCE_MAX_AGE_DAYS["COMPLIANCE_DOC"]
    return EVIDENCE_MAX_AGE_DAYS["GENERIC"]


def _annotate_evidence_document_freshness(doc: dict, *, today: date | None = None) -> dict[str, Any]:
    """Ajoute les metadonnees de fraicheur a une preuve documentaire."""
    current = today or date.today()
    enriched = dict(doc)
    evidence_date = _parse_dateish(enriched.get("issued_at")) or _parse_dateish(enriched.get("uploaded_at"))
    max_age_days = _classify_evidence_age_policy(enriched)
    age_days = (current - evidence_date).days if evidence_date else None
    is_expired = bool(evidence_date and age_days is not None and age_days > max_age_days)
    if evidence_date and is_expired:
        freshness = "EXPIREE"
    elif evidence_date:
        freshness = "VALIDE"
    else:
        freshness = "SANS_DATE"
    enriched.update(
        {
            "issued_at": evidence_date.isoformat() if evidence_date else None,
            "evidence_age_days": age_days,
            "evidence_max_age_days": max_age_days,
            "is_expired": is_expired,
            "freshness_label": freshness,
        }
    )
    return enriched


def _summarize_evidence_freshness(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Resume les preuves valides, expirees et sans date pour le prompt et les regles."""
    total = len(documents)
    expired = sum(1 for doc in documents if bool(doc.get("is_expired")))
    fresh = sum(1 for doc in documents if doc.get("freshness_label") == "VALIDE")
    undated = sum(1 for doc in documents if doc.get("freshness_label") == "SANS_DATE")
    age_values = [int(doc["evidence_age_days"]) for doc in documents if doc.get("evidence_age_days") is not None]
    expired_age_values = [
        int(doc["evidence_age_days"])
        for doc in documents
        if bool(doc.get("is_expired")) and doc.get("evidence_age_days") is not None
    ]
    return {
        "total": total,
        "fresh_count": fresh,
        "expired_count": expired,
        "undated_count": undated,
        "only_expired": total > 0 and expired > 0 and fresh == 0,
        "freshest_age_days": min(age_values) if age_values else None,
        "oldest_age_days": max(age_values) if age_values else None,
        "oldest_expired_age_days": max(expired_age_values) if expired_age_values else None,
    }


def _derive_normative_strength(req: dict[str, Any]) -> str:
    req_type = str(req.get("req_type") or "").strip().upper()
    text = _normalize_text(
        " ".join(str(req.get(key) or "") for key in ("requirement_text", "citation_ref", "article_ref"))
    )
    if req_type == "INTERDICTION":
        return "IMPERATIF"
    if req_type in {"EXCEPTION", "CONDITION"}:
        return "CONDITIONNEL"
    if any(token in text for token in ("recommande", "devrait", "peut beneficier", "facultatif")):
        return "FACULTATIF"
    if any(token in text for token in ("si ", "lorsque", "sous reserve", "sauf ", "a condition")):
        return "CONDITIONNEL"
    if any(token in text for token in ("doit", "doivent", "tenu de", "tenus de", "obligatoire", "interdit", "ne peut", "est passible")):
        return "IMPERATIF"
    return "IMPERATIF" if req_type in {"OBLIGATION", "RESPONSABILITE", "REGISTRE", "DECLARATION", "CONTROLE"} else "CONDITIONNEL"


def _has_legal_penalty_signal(req: dict[str, Any]) -> bool:
    text = _normalize_text(
        " ".join(str(req.get(key) or "") for key in ("requirement_text", "citation_snippet", "citation_ref", "article_ref"))
    )
    return any(token in text for token in ("amende", "peine", "sanction", "penalite", "passible", "emprisonnement", "puni"))


def _has_sst_risk_signal(req: dict[str, Any], evidence_ctx: dict[str, Any]) -> bool:
    domain = _normalize_text(" ".join(str(req.get(key) or "") for key in ("qse_domain", "qse_sub_domain", "domain")))
    if any(token in domain for token in ("sst", "sante", "securite", "travail")):
        return True
    return bool(evidence_ctx.get("sst_obligations"))


def _compute_gap_severity(
    req_type: str,
    normative_strength: str,
    has_sst_risk: bool,
    has_legal_penalty: bool,
    evidence_age_days: int | None,
) -> str:
    """Calcule une gravite minimale a partir du risque juridique/SST et de la preuve."""
    score = 0
    req_type_norm = str(req_type or "").strip().upper()
    strength = str(normative_strength or "").strip().upper()
    if strength == "IMPERATIF":
        score += 3
    elif strength == "CONDITIONNEL":
        score += 1
    if req_type_norm == "INTERDICTION":
        score += 3
    elif req_type_norm in {"OBLIGATION", "RESPONSABILITE"}:
        score += 2
    if has_sst_risk:
        score += 2
    if has_legal_penalty:
        score += 2
    if evidence_age_days is not None and evidence_age_days > 365:
        score += 1
    if score >= 7:
        return "CRITIQUE"
    if score >= 4:
        return "MAJEURE"
    return "MINEURE"


def _priority_for_severity(severity: str) -> str:
    return SEVERITY_DEFAULT_PRIORITY.get(str(severity or "").strip().upper(), "NORMALE")


def _merge_priority(existing: str | None, suggested: str) -> str:
    current = str(existing or "").strip().upper()
    if current not in VALID_PRIORITIES:
        return suggested
    return current if PRIORITY_RANK.get(current, 0) >= PRIORITY_RANK.get(suggested, 0) else suggested


def _apply_gap_policy_overrides(req: dict[str, Any], evidence_ctx: dict[str, Any], check: ComplianceCheckLLM) -> None:
    """Recalibre les ecarts LLM avec les regles internes de criticite."""
    normative_strength = _derive_normative_strength(req)
    has_sst_risk = _has_sst_risk_signal(req, evidence_ctx)
    has_legal_penalty = _has_legal_penalty_signal(req)
    freshness = evidence_ctx.get("evidence_freshness") or {}
    age_days = freshness.get("oldest_expired_age_days") or freshness.get("oldest_age_days")
    for gap in check.gaps:
        severity = _compute_gap_severity(
            req_type=str(req.get("req_type") or ""),
            normative_strength=normative_strength,
            has_sst_risk=has_sst_risk,
            has_legal_penalty=has_legal_penalty,
            evidence_age_days=int(age_days) if age_days is not None else None,
        )
        gap.severity = severity
        gap.treatment_priority = _merge_priority(gap.treatment_priority, _priority_for_severity(severity))
        if freshness.get("only_expired") and not str(gap.missing_proof or "").strip():
            gap.missing_proof = "Mettre a jour ou renouveler la preuve expiree pour reconstituer une preuve recente."


def _apply_evidence_age_policy(req: dict[str, Any], evidence_ctx: dict[str, Any], check: ComplianceCheckLLM) -> None:
    """Evite de conclure conforme quand les seules preuves sont expirees."""
    freshness = evidence_ctx.get("evidence_freshness") or {}
    expired_count = int(freshness.get("expired_count") or 0)
    fresh_count = int(freshness.get("fresh_count") or 0)
    if expired_count <= 0:
        return
    note = (
        f"[A3_POLICY] {expired_count} preuve(s) expiree(s) detectee(s)"
        if fresh_count <= 0
        else f"[A3_POLICY] {expired_count} preuve(s) expiree(s) coexistent avec des preuves plus recentes"
    )
    if fresh_count <= 0 and check.compliance_status == "CONFORME":
        check.compliance_status = "PARTIELLEMENT_CONFORME"
        check.compliance_score = min(float(check.compliance_score or 0.0), 0.59)
    if fresh_count <= 0:
        expected = str(check.missing_proofs or "").strip()
        refresh_message = "Renouveler les preuves expirees par des documents ou enregistrements recents."
        check.missing_proofs = f"{expected}\n{refresh_message}".strip() if expected else refresh_message
    detail = str(check.analysis_detail or "").strip()
    check.analysis_detail = f"{note}\n{detail}".strip() if detail else note


def _apply_compliance_policies(req: dict[str, Any], evidence_ctx: dict[str, Any], check: ComplianceCheckLLM) -> ComplianceCheckLLM:
    """Applique les garde-fous metier apres la reponse LLM."""
    _apply_evidence_age_policy(req, evidence_ctx, check)
    _apply_gap_policy_overrides(req, evidence_ctx, check)
    return check


# Operational evidence loading

def _load_operational_evidence(conn: psycopg.Connection, profile_id: str) -> dict:
    """Charge toutes les preuves operationnelles disponibles pour ce tenant.

    A3 ne relit pas les fichiers source directement. Il consomme les tables
    normalisees par les imports entreprise et les uploads de preuves.
    """
    with conn.cursor() as cur:
        # Non-conformites: elles signalent des ecarts deja constates ou en
        # traitement, mais ne prouvent pas une conformite par defaut.
        cur.execute(
            """
            SELECT  nc_id::text, site_id::text, reference, title, process_name, source, audit_type,
                    detected_at, state, severity, nc_type, system_scope, year
            FROM nonconformities
            WHERE profile_id = %s
            ORDER BY detected_at DESC NULLS LAST, created_at DESC
            """,
            (profile_id,),
        )
        nc_fields = [
            "nc_id", "site_id", "reference", "title", "process_name", "source", "audit_type",
            "detected_at", "state", "severity", "nc_type", "system_scope", "year",
        ]
        nonconformities = [dict(zip(nc_fields, row)) for row in cur.fetchall()]

        # Preuves documentaires centralisees: PDF importes, manifests,
        # rapports d'audit materialises dans compliance_evidence.
        cur.execute(
            """
            SELECT
                ce.evidence_id::text,
                ce.source_report_id::text,
                ce.requirement_id::text,
                COALESCE(ce.scope_level, 'ORGANIZATION'),
                COALESCE(ce.scope_key, 'ORGANIZATION'),
                COALESCE(ce.scope_label, 'ORGANIZATION'),
                ce.site_id::text,
                ce.process_id::text,
                ce.activity_id::text,
                COALESCE(ce.title, ar.reference, ''),
                COALESCE(ce.file_name, ''),
                COALESCE(ce.evidence_type, ''),
                COALESCE(ce.source_type, ''),
                COALESCE(ce.mime_type, ''),
                COALESCE(ce.storage_path, ''),
                COALESCE(ce.raw_text, ''),
                COALESCE(ce.input_hash, ''),
                COALESCE(ce.issued_at, ce.uploaded_at, ce.created_at)::text,
                COALESCE(ce.uploaded_at, ce.created_at)::text,
                COALESCE(ar.reference, ''),
                COALESCE(ar.category, ''),
                COALESCE(ar.nature, ''),
                COALESCE(ar.system_scope, ''),
                COALESCE(ar.state, '')
            FROM compliance_evidence ce
            LEFT JOIN audit_reports ar ON ar.report_id = ce.source_report_id
            WHERE ce.profile_id = %s
            ORDER BY COALESCE(ce.uploaded_at, ce.created_at) DESC
            """,
            (profile_id,),
        )
        evidence_fields = [
            "evidence_id", "source_report_id", "requirement_id", "scope_level", "scope_key", "scope_label",
            "site_id", "process_id", "activity_id", "title", "file_name", "evidence_type", "source_type",
            "mime_type", "storage_path", "raw_text", "input_hash", "issued_at", "uploaded_at", "reference",
            "category", "nature", "system_scope", "state",
        ]
        evidence_documents = [dict(zip(evidence_fields, row)) for row in cur.fetchall()]

        # Rapports d'audit historiques. Ils servent au contexte et aux
        # syntheses, les preuves textuelles etant surtout dans compliance_evidence.
        cur.execute(
            """
            SELECT reference, audit_type, category, nature, system_scope,
                    date_real_start, date_real_end, state, objectives, locations_visited
            FROM audit_reports
            WHERE profile_id = %s
            ORDER BY date_real_start DESC NULLS LAST, created_at DESC
            """,
            (profile_id,),
        )
        audit_fields = [
            "reference", "audit_type", "category", "nature", "system_scope",
            "date_real_start", "date_real_end", "state", "objectives", "locations_visited",
        ]
        audit_reports = [dict(zip(audit_fields, row)) for row in cur.fetchall()]

        # Obligations issues de l'analyse des risques SST: elles orientent le
        # LLM vers les preuves attendues terrain.
        cur.execute(
            """
            SELECT ssr.sig_risk_id::text, ssr.site_id::text, sr.designation, ssr.year, ssr.activities, ssr.obligations,
                    ssr.score, ssr.prevention_efficiency
            FROM sst_significant_risks ssr
            JOIN sst_risks sr ON sr.risk_id = ssr.risk_id
            WHERE ssr.profile_id = %s
                AND ssr.obligations IS NOT NULL
            ORDER BY ssr.score DESC NULLS LAST, ssr.created_at DESC
            LIMIT 50
            """,
            (profile_id,),
        )
        sst_obligations = [
            {
                "sig_risk_id": row[0],
                "site_id": row[1],
                "risk": row[2],
                "year": row[3],
                "activity": row[4],
                "obligation": row[5],
                "score": row[6],
                "prevention_eff": row[7],
            }
            for row in cur.fetchall()
        ]

        # Aspects environnementaux: utiles pour qualifier les impacts et les
        # preuves attendues sur les exigences environnement.
        cur.execute(
            """
            SELECT aspect_id::text, site_id::text, designation, domain
            FROM environmental_aspects
            WHERE profile_id = %s
            ORDER BY domain, designation
            """,
            (profile_id,),
        )
        environmental_aspects = [
            {
                "aspect_id": row[0],
                "site_id": row[1],
                "designation": row[2],
                "domain": row[3],
            }
            for row in cur.fetchall()
        ]

    return {
        "nonconformities": nonconformities,
        "evidence_documents": evidence_documents,
        "audit_reports": audit_reports,
        "sst_obligations": sst_obligations,
        "environmental_aspects": environmental_aspects,
    }


def _scope_level_rank(scope_level: str) -> int:
    value = str(scope_level or "ORGANIZATION").upper()
    if value == "ORGANIZATION":
        return 0
    if value == "SITE":
        return 1
    if value == "PROCESS":
        return 2
    if value == "ACTIVITY":
        return 3
    return 9


def _row_matches_scope(req: dict, row: dict, *, use_scope_key: bool = True) -> bool:
    """Verifie si une preuve/NC appartient au meme perimetre que l'exigence."""
    req_scope_key = str(req.get("scope_key") or "ORGANIZATION")
    req_site_id = str(req.get("site_id") or "").strip()
    req_process_id = str(req.get("process_id") or "").strip()
    req_activity_id = str(req.get("activity_id") or "").strip()

    row_scope_key = str(row.get("scope_key") or "").strip()
    row_site_id = str(row.get("site_id") or "").strip()
    row_process_id = str(row.get("process_id") or "").strip()
    row_activity_id = str(row.get("activity_id") or "").strip()

    if req_scope_key == "ORGANIZATION":
        return True
    if use_scope_key and row_scope_key == "ORGANIZATION":
        return True
    if use_scope_key and row_scope_key and row_scope_key == req_scope_key:
        return True
    if req_activity_id and row_activity_id and row_activity_id == req_activity_id:
        return True
    if req_process_id and row_process_id and row_process_id == req_process_id:
        return True
    if req_site_id and row_site_id and row_site_id == req_site_id:
        return True
    if not row_scope_key and not row_site_id and not row_process_id and not row_activity_id:
        return True
    return False


def _extract_search_terms(req: dict) -> list[str]:
    """Extrait quelques mots utiles pour retrouver les bons passages de preuve."""
    source = " ".join(
        str(req.get(key) or "")
        for key in ("requirement_text", "citation_ref", "article_ref", "scope_label", "scope_process", "scope_activity")
    )
    stopwords = {
        "avec", "dans", "pour", "cette", "cette", "doit", "doivent", "etre", "sont", "plus", "moins",
        "ainsi", "comme", "tous", "toutes", "entreprise", "exigence", "reglementaire", "article",
        "site", "processus", "activite", "preuve", "preuves", "selon", "dont", "leurs", "leur",
    }
    words = re.findall(r"[a-z0-9]{4,}", _normalize_text(source))
    terms: list[str] = []
    seen: set[str] = set()
    for word in words:
        if word in stopwords or word.isdigit():
            continue
        if word in seen:
            continue
        seen.add(word)
        terms.append(word)
        if len(terms) >= 12:
            break
    return terms


def _best_text_excerpt(raw_text: str, search_terms: list[str], max_chars: int = 420) -> str:
    """Selectionne l'extrait de preuve le plus proche du texte de l'exigence."""
    text = str(raw_text or "").strip()
    if not text:
        return ""

    normalized_chunks = []
    for chunk in re.split(r"\n\s*\n|\r\n\s*\r\n", text):
        cleaned = re.sub(r"\s+", " ", chunk).strip()
        if cleaned:
            normalized_chunks.append(cleaned)
    if not normalized_chunks:
        normalized_chunks = [re.sub(r"\s+", " ", text).strip()]

    best_chunk = normalized_chunks[0]
    best_score = -1
    for chunk in normalized_chunks[:20]:
        norm_chunk = _normalize_text(chunk)
        score = sum(1 for term in search_terms if term in norm_chunk)
        score += min(len(chunk) // 300, 2)
        if score > best_score:
            best_score = score
            best_chunk = chunk
    return best_chunk[:max_chars]


def _build_requirement_evidence_context(req: dict, evidence: dict) -> dict[str, Any]:
    """Construit le contexte de preuve envoye au LLM pour une exigence.

    Cette fonction est le coeur du matching A3: elle filtre les preuves par
    scope, classe les documents par pertinence, resume les donnees terrain et
    calcule un input_hash pour savoir si une reevaluation est necessaire.
    """
    search_terms = _extract_search_terms(req)

    # 1) Filtrer chaque famille de preuve sur le meme scope que l'exigence.
    documents = [
        _annotate_evidence_document_freshness(row)
        for row in evidence.get("evidence_documents", [])
        if _row_matches_scope(req, row, use_scope_key=True)
    ]
    nonconformities = [
        row for row in evidence.get("nonconformities", [])
        if _row_matches_scope(req, row, use_scope_key=False)
    ]
    obligations = [
        row for row in evidence.get("sst_obligations", [])
        if _row_matches_scope(req, row, use_scope_key=False)
    ]
    aspects = [
        row for row in evidence.get("environmental_aspects", [])
        if _row_matches_scope(req, row, use_scope_key=False)
    ]

    # 2) Scorer les preuves texte: lien direct a l'exigence, meme scope,
    # scope global, puis presence des mots importants dans le texte brut.
    for doc in documents:
        score = 0
        if str(doc.get("requirement_id") or "") == str(req.get("requirement_id") or ""):
            score += 10
        if str(doc.get("scope_key") or "ORGANIZATION") == str(req.get("scope_key") or "ORGANIZATION"):
            score += 6
        elif str(doc.get("scope_key") or "ORGANIZATION") == "ORGANIZATION":
            score += 2
        raw_text = str(doc.get("raw_text") or "")
        normalized_text = _normalize_text(raw_text)
        score += sum(1 for term in search_terms if term in normalized_text)
        doc["_score"] = score
        doc["_excerpt"] = _best_text_excerpt(raw_text, search_terms)

    # 3) Garder les preuves les plus pertinentes en tete du prompt.
    documents = sorted(
        documents,
        key=lambda row: (
            int(row.get("_score") or 0),
            -_scope_level_rank(str(row.get("scope_level") or "ORGANIZATION")),
            str(row.get("uploaded_at") or ""),
        ),
        reverse=True,
    )

    lines: list[str] = []
    # 4) Construire un bloc texte compact pour le prompt LLM.
    lines.append(
        "PERIMETRE A3: "
        f"{req.get('scope_level') or 'ORGANIZATION'} | "
        f"{req.get('scope_label') or req.get('scope_site') or 'ORGANIZATION'}"
    )

    freshness = _summarize_evidence_freshness(documents)
    top_docs = documents[:5]
    lines.append(f"\nPREUVES DOCUMENTAIRES TEXTE ({len(documents)}):")
    if top_docs:
        for doc in top_docs:
            freshness_bits = []
            if doc.get("evidence_age_days") is not None:
                freshness_bits.append(f"age={int(doc.get('evidence_age_days') or 0)}j")
            if doc.get("freshness_label") == "EXPIREE":
                freshness_bits.append("expiree")
            elif doc.get("freshness_label") == "VALIDE":
                freshness_bits.append("valide")
            elif doc.get("freshness_label") == "SANS_DATE":
                freshness_bits.append("sans_date")
            lines.append(
                f"  - [{doc.get('uploaded_at') or '?'}] "
                f"{doc.get('title') or doc.get('reference') or doc.get('file_name') or 'preuve'} | "
                f"type={doc.get('evidence_type') or '-'} | "
                f"scope={doc.get('scope_label') or doc.get('scope_key') or 'ORGANIZATION'}"
                f"{' | ' + ', '.join(freshness_bits) if freshness_bits else ''}"
            )
            excerpt = str(doc.get("_excerpt") or "").strip()
            if excerpt:
                lines.append(f"    Extrait: {excerpt}")
    else:
        lines.append("  - Aucune preuve documentaire scopee disponible")
    if freshness["expired_count"] > 0:
        lines.append(
            f"  - Fraicheur: {freshness['fresh_count']} valide(s), "
            f"{freshness['expired_count']} expiree(s), {freshness['undated_count']} sans date"
        )

    lines.append(f"\nNON-CONFORMITES LIEES ({len(nonconformities)}):")
    if nonconformities:
        for nc in nonconformities[:8]:
            date_str = str(nc.get("detected_at") or "")[:10] or "?"
            lines.append(
                f"  - [{date_str}] {nc.get('reference') or '-'} | "
                f"{str(nc.get('title') or '')[:80]} | "
                f"Etat={nc.get('state') or '-'} | Severite={nc.get('severity') or '-'}"
            )
    else:
        lines.append("  - Aucune non-conformite reliee a ce scope")

    if obligations:
        lines.append(f"\nOBLIGATIONS SST / RISQUES ({len(obligations)}):")
        for item in obligations[:6]:
            lines.append(
                f"  - Score={item.get('score') or '?'} | "
                f"{str(item.get('risk') or '')[:60]} | "
                f"Obligation: {str(item.get('obligation') or '')[:120]}"
            )

    if aspects:
        lines.append(f"\nASPECTS ENVIRONNEMENTAUX ({len(aspects)}):")
        for item in aspects[:6]:
            lines.append(
                f"  - {item.get('domain') or '-'} | {str(item.get('designation') or '')[:80]}"
            )

    hash_payload = {
        "decision_id": str(req.get("decision_id") or ""),
        "requirement_id": str(req.get("requirement_id") or ""),
        "scope_key": str(req.get("scope_key") or "ORGANIZATION"),
        "app_status": str(req.get("app_status") or ""),
        "requirement_text": str(req.get("requirement_text") or ""),
        "citation_ref": str(req.get("citation_ref") or ""),
        "justification": str(req.get("justification") or ""),
        "documents": [
            {
                "evidence_id": row.get("evidence_id"),
                "scope_key": row.get("scope_key"),
                "requirement_id": row.get("requirement_id"),
                "input_hash": row.get("input_hash"),
                "uploaded_at": row.get("uploaded_at"),
                "issued_at": row.get("issued_at"),
                "is_expired": bool(row.get("is_expired")),
                "freshness_label": row.get("freshness_label"),
            }
            for row in documents
        ],
        "nonconformities": [
            {
                "nc_id": row.get("nc_id"),
                "site_id": row.get("site_id"),
                "reference": row.get("reference"),
                "state": row.get("state"),
                "severity": row.get("severity"),
            }
            for row in nonconformities
        ],
        "obligations": [
            {
                "sig_risk_id": row.get("sig_risk_id"),
                "site_id": row.get("site_id"),
                "obligation": row.get("obligation"),
                "score": row.get("score"),
            }
            for row in obligations
        ],
        "aspects": [
            {
                "aspect_id": row.get("aspect_id"),
                "site_id": row.get("site_id"),
                "designation": row.get("designation"),
                "domain": row.get("domain"),
            }
            for row in aspects
        ],
    }
    input_hash = hashlib.sha256(
        json.dumps(hash_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    # input_hash est stocke dans compliance_checks pour eviter les reruns si
    # ni l'exigence, ni le scope, ni les preuves rattachees n'ont change.
    return {
        "documents": documents,
        "nonconformities": nonconformities,
        "sst_obligations": obligations,
        "environmental_aspects": aspects,
        "evidence_freshness": freshness,
        "evidence_block": "\n".join(lines),
        "input_hash": input_hash,
    }


# Database helpers

def _load_applicable_requirements(
    conn: psycopg.Connection,
    profile_id: str,
    doc_id: str | None = None,
    limit: int | None = None,
    site_ids: list[str] | None = None,
    process_ids: list[str] | None = None,
    activity_ids: list[str] | None = None,
) -> list[dict]:
    """Charge les exigences applicables depuis A2, scope par scope.

    A3 ne controle que les decisions A2 APPLICABLE ou
    APPLICABLE_SOUS_CONDITIONS. Les exigences non applicables sont ignorees.
    """
    doc_clause = ""
    params: list[Any] = [profile_id]
    if doc_id:
        doc_clause = " AND r.doc_id = %s::uuid"
        params.append(str(doc_id))

    scope_filters: list[str] = []
    for values, column in (
        (site_ids or [], "ad.site_id"),
        (process_ids or [], "ad.process_id"),
        (activity_ids or [], "ad.activity_id"),
    ):
        cleaned = [str(value).strip() for value in values if str(value).strip()]
        if cleaned:
            placeholders = ",".join(["%s::uuid"] * len(cleaned))
            scope_filters.append(f"{column} IN ({placeholders})")
            params.extend(cleaned)
    scope_clause = f" AND {' AND '.join(scope_filters)}" if scope_filters else ""

    params.append(limit or 9999)

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT ad.decision_id::text, ad.requirement_id::text, ad.status AS app_status,
                   ad.justification,
                   COALESCE(ad.scope_level, 'ORGANIZATION') AS scope_level,
                   COALESCE(ad.scope_key, 'ORGANIZATION') AS scope_key,
                   COALESCE(ad.scope_label, 'ORGANIZATION') AS scope_label,
                   ad.site_id::text,
                   ad.process_id::text,
                   ad.activity_id::text,
                   ad.scope_site,
                   ad.scope_process,
                   ad.scope_activity,
                   r.requirement_text, r.req_type, r.qse_domain,
                   r.citation_ref, d.title AS doc_title
            FROM applicability_decisions ad
            JOIN requirements r ON r.requirement_id = ad.requirement_id
            JOIN documents d ON d.doc_id = r.doc_id
            WHERE ad.profile_id = %s
              AND ad.status IN ('APPLICABLE', 'APPLICABLE_SOUS_CONDITIONS')
              {doc_clause}
              {scope_clause}
            ORDER BY r.qse_domain, ad.status
            LIMIT %s
        """, params)

        rows = cur.fetchall()
        cols = [
            "decision_id", "requirement_id", "app_status", "justification",
            "scope_level", "scope_key", "scope_label", "site_id", "process_id", "activity_id",
            "scope_site", "scope_process", "scope_activity",
            "requirement_text", "req_type", "qse_domain", "citation_ref", "doc_title",
        ]
        return effective_applicability_rows([dict(zip(cols, r)) for r in rows])


def _load_existing_checks(
    conn: psycopg.Connection,
    profile_id: str,
    requirement_ids: list[str] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Charge les controles A3 existants pour le mode delta."""
    params: list[Any] = [profile_id]
    filter_sql = ""
    if requirement_ids:
        placeholders = ",".join(["%s::uuid"] * len(requirement_ids))
        filter_sql = f" AND requirement_id IN ({placeholders})"
        params.extend(requirement_ids)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT check_id::text,
                   requirement_id::text,
                   COALESCE(scope_key, 'ORGANIZATION'),
                   compliance_status,
                   COALESCE(input_hash, ''),
                   COALESCE(needs_recheck, FALSE),
                   COALESCE(evaluation_version, 0),
                   decision_id::text
            FROM compliance_checks
            WHERE profile_id = %s
              {filter_sql}
            """,
            tuple(params),
        )
        rows = cur.fetchall()

    output: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        req_id = str(row[1] or "")
        scope_key = str(row[2] or "ORGANIZATION")
        output[(req_id, scope_key)] = {
            "check_id": str(row[0] or ""),
            "requirement_id": req_id,
            "scope_key": scope_key,
            "compliance_status": str(row[3] or ""),
            "input_hash": str(row[4] or ""),
            "needs_recheck": bool(row[5]),
            "evaluation_version": int(row[6] or 0),
            "decision_id": str(row[7] or ""),
        }
    return output


def _save_compliance_check(
    conn: psycopg.Connection,
    profile_id: str,
    req: dict,
    check: ComplianceCheckLLM,
    llm_model: str | None,
    input_hash: str,
) -> str:
    """Sauvegarde le controle A3, ses ecarts et ses actions.

    La sauvegarde est idempotente par (profile_id, requirement_id, scope_key).
    En cas de rerun, les anciens gaps/actions du check sont remplaces par le
    nouveau diagnostic.
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO compliance_checks
                (profile_id, decision_id, requirement_id,
                scope_level, scope_key, scope_label, site_id, process_id, activity_id,
                compliance_status,
                compliance_score, expected_proofs, found_proofs, missing_proofs,
                analysis_detail, evaluation_mode, llm_model,
                last_evaluated_at, input_hash, needs_recheck, evaluation_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,FALSE,1)
            ON CONFLICT (profile_id, requirement_id, scope_key) DO UPDATE SET
                decision_id         = EXCLUDED.decision_id,
                scope_level         = EXCLUDED.scope_level,
                scope_label         = EXCLUDED.scope_label,
                site_id             = EXCLUDED.site_id,
                process_id          = EXCLUDED.process_id,
                activity_id         = EXCLUDED.activity_id,
                compliance_status = EXCLUDED.compliance_status,
                compliance_score  = EXCLUDED.compliance_score,
                expected_proofs   = EXCLUDED.expected_proofs,
                found_proofs      = EXCLUDED.found_proofs,
                missing_proofs    = EXCLUDED.missing_proofs,
                analysis_detail   = EXCLUDED.analysis_detail,
                evaluation_mode   = EXCLUDED.evaluation_mode,
                llm_model         = EXCLUDED.llm_model,
                last_evaluated_at = now(),
                input_hash        = EXCLUDED.input_hash,
                needs_recheck     = FALSE,
                evaluation_version = COALESCE(compliance_checks.evaluation_version, 0) + 1,
                updated_at        = now()
            RETURNING check_id
        """, (
            profile_id,
            str(req["decision_id"]),
            str(req["requirement_id"]),
            str(req.get("scope_level") or "ORGANIZATION"),
            str(req.get("scope_key") or "ORGANIZATION"),
            str(req.get("scope_label") or "ORGANIZATION"),
            req.get("site_id"),
            req.get("process_id"),
            req.get("activity_id"),
            check.compliance_status,
            check.compliance_score,
            check.expected_proofs,
            check.found_proofs,
            check.missing_proofs,
            check.analysis_detail,
            "DECLENCHE",
            llm_model,
            input_hash,
        ))
        check_id = str(cur.fetchone()[0])

        # Nettoyage avant rerun : supprimer les actions puis les écarts liés
        cur.execute("""
            DELETE FROM corrective_actions
            WHERE gap_id IN (SELECT gap_id FROM gaps WHERE check_id = %s)
        """, (check_id,))
        cur.execute("DELETE FROM gaps WHERE check_id = %s", (check_id,))

        # Sauvegarder les écarts et collecter leurs IDs
        gap_ids: list[str] = []
        for gap in check.gaps:
            cur.execute("""
                INSERT INTO gaps
                    (profile_id, check_id, requirement_id, gap_type, severity,
                     description, missing_proof, legal_impact, sst_impact, env_impact,
                     scope_level, scope_key, scope_label, site_id, process_id, activity_id,
                     scope_site, scope_process, scope_activity, treatment_priority)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s)
                RETURNING gap_id
            """, (
                profile_id, check_id, str(req["requirement_id"]),
                gap.gap_type, gap.severity, gap.description,
                gap.missing_proof, gap.legal_impact, gap.sst_impact, gap.env_impact,
                str(req.get("scope_level") or "ORGANIZATION"),
                str(req.get("scope_key") or "ORGANIZATION"),
                str(req.get("scope_label") or "ORGANIZATION"),
                req.get("site_id"),
                req.get("process_id"),
                req.get("activity_id"),
                gap.scope_site or req.get("scope_site"),
                gap.scope_process or req.get("scope_process"),
                gap.scope_activity or req.get("scope_activity"),
                gap.treatment_priority,
            ))
            gap_ids.append(str(cur.fetchone()[0]))

        # Sauvegarder les actions correctives liées à leur écart par index
        for i, action in enumerate(check.corrective_actions):
            due = date.today() + timedelta(days=action.due_date_days)
            linked_gap_id = gap_ids[i] if i < len(gap_ids) else (gap_ids[0] if gap_ids else None)
            cur.execute("""
                INSERT INTO corrective_actions
                    (profile_id, gap_id, action_title, action_description,
                     action_type, responsible, due_date, expected_proof,
                     scope_level, scope_key, scope_label, site_id, process_id, activity_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::uuid,%s::uuid,%s::uuid)
            """, (
                profile_id, linked_gap_id,
                action.action_title, action.action_description,
                action.action_type, action.responsible,
                due, action.expected_proof,
                str(req.get("scope_level") or "ORGANIZATION"),
                str(req.get("scope_key") or "ORGANIZATION"),
                str(req.get("scope_label") or "ORGANIZATION"),
                req.get("site_id"),
                req.get("process_id"),
                req.get("activity_id"),
            ))

    conn.commit()
    return check_id


# Compliance evaluation

def _build_compliance_prompt(req: dict, evidence_block: str) -> str:
    """Assemble le prompt LLM a partir de l'exigence applicable et des preuves."""
    return f"""=== EXIGENCE APPLICABLE A EVALUER ===

Document: {req['doc_title']}
Reference: {req['citation_ref'] or 'N/A'}
Domaine QHSE: {req['qse_domain'] or 'Non precise'}
Type: {req['req_type']}
Perimetre identifie: Site={req['scope_site'] or '?'} | Processus={req['scope_process'] or '?'}
Applicabilite: {req['app_status']}

Texte de l'exigence:
{req['requirement_text']}

Justification de l'applicabilite:
{req['justification']}

=== DONNEES OPERATIONNELLES DISPONIBLES ===

{evidence_block}

=== INSTRUCTION ===

Evalue la conformite de l'entreprise a cette exigence reglementaire.

Raisonne en 4 etapes:
1. Quelles preuves sont attendues pour cette exigence? (documents, registres, controles, formations...)
2. Les donnees disponibles contiennent-elles ces preuves? (cherche dans les NC, audits, obligations)
3. Y a-t-il des ecarts? Comment les qualifier (type, gravite, impact)?
4. Quelles actions correctives sont necessaires?

IMPORTANT: si les donnees operationnelles ne mentionnent pas cette exigence,
c'est une ABSENCE_DE_PREUVE, pas une conformite automatique.

Reponds en JSON strict selon le format defini.
"""


def evaluate_compliance(
    req: dict,
    evidence_block: str,
    llm: Any,
) -> ComplianceCheckLLM:
    """Evalue la conformite d'une exigence applicable via le LLM."""
    user_prompt = _build_compliance_prompt(req, evidence_block)
    raw = llm.call_json(SYSTEM_PROMPT_COMPLIANCE, user_prompt, max_tokens=3000)

    # Normaliser les listes si elles sont None
    if "gaps" not in raw or raw["gaps"] is None:
        raw["gaps"] = []
    if "corrective_actions" not in raw or raw["corrective_actions"] is None:
        raw["corrective_actions"] = []

    return ComplianceCheckLLM(**raw)


# Main pipeline

def _select_compliance_pairs(
    requirements: list[dict],
    evidence: dict,
    existing_map: dict[tuple[str, str], dict[str, Any]],
    mode: str,
    force_recompute: bool,
) -> list[tuple[dict, dict[str, Any], dict[str, Any] | None]]:
    """Selectionne les controles a calculer pour ce run.

    En mode delta, A3 saute les controles deja a jour. Il relance un controle
    si A2 a change, si une preuve a change (input_hash), ou si needs_recheck
    a ete pose par un upload/modification.
    """
    selected: list[tuple[dict, dict[str, Any], dict[str, Any] | None]] = []
    is_delta = str(mode or "delta").strip().lower() != "full"

    for req in requirements:
        req_id = str(req.get("requirement_id") or "")
        scope_key = str(req.get("scope_key") or "ORGANIZATION")
        if not req_id:
            continue
        evidence_ctx = _build_requirement_evidence_context(req, evidence)
        existing = existing_map.get((req_id, scope_key))
        if not existing:
            selected.append((req, evidence_ctx, None))
            continue
        if not is_delta or force_recompute:
            selected.append((req, evidence_ctx, existing))
            continue
        if bool(existing.get("needs_recheck")):
            selected.append((req, evidence_ctx, existing))
            continue
        if str(existing.get("decision_id") or "") != str(req.get("decision_id") or ""):
            selected.append((req, evidence_ctx, existing))
            continue
        if str(existing.get("input_hash") or "") != str(evidence_ctx.get("input_hash") or ""):
            selected.append((req, evidence_ctx, existing))
            continue
    return selected


def _sync_non_applicable_checks(conn: psycopg.Connection, profile_id: str) -> int:
    """Desactive les controles A3 quand la decision A2 n'est plus applicable."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE compliance_checks cc
            SET compliance_status = 'NON_EVALUE',
                analysis_detail = '[A3_SYNC] Controle desactive: exigence non applicable ou decision A2 introuvable.',
                needs_recheck = FALSE,
                evaluation_mode = 'DECLENCHE',
                last_evaluated_at = now(),
                updated_at = now()
            WHERE cc.profile_id = %s
              AND EXISTS (
                    SELECT 1
                    FROM applicability_decisions ad
                    WHERE ad.profile_id = cc.profile_id
                      AND ad.requirement_id = cc.requirement_id
                      AND COALESCE(ad.scope_key, 'ORGANIZATION') = COALESCE(cc.scope_key, 'ORGANIZATION')
                      AND ad.status NOT IN ('APPLICABLE', 'APPLICABLE_SOUS_CONDITIONS')
              )
            """,
            (profile_id,),
        )
        updated = int(cur.rowcount or 0)
    conn.commit()
    return updated


def _get_conn(tenant_id: str | None = None) -> psycopg.Connection:
    dsn = os.getenv("PG_DSN", "").strip()
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")
    return connect_db(dsn, tenant_id=tenant_id)


def run_compliance(
    tenant_id: str,
    doc_id: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    delay_between: float = 2.0,
    mode: str = "delta",
    force: bool = False,
    force_recompute: bool = False,
    site_ids: list[str] | None = None,
    process_ids: list[str] | None = None,
    activity_ids: list[str] | None = None,
    stop_requested: Any | None = None,
) -> dict:
    """Lance le pipeline de conformite pour un tenant.

    Etapes principales:
      1. retrouver le profil entreprise;
      2. synchroniser les controles qui ne sont plus applicables;
      3. charger les preuves operationnelles;
      4. charger les exigences applicables issues de A2;
      5. selectionner les controles a recalculer;
      6. evaluer via LLM, appliquer les politiques internes, sauvegarder.
    """
    tenant_safe = _safe_console_text(tenant_id, max_len=80)
    print(f"\n=== Agent 3 - Conformite reglementaire ({tenant_safe}) ===\n")

    conn = _get_conn(tenant_id)
    llm = get_llm_client()
    run_mode = str(mode or "delta").strip().lower()
    if run_mode not in {"full", "delta"}:
        raise ValueError("mode doit etre 'full' ou 'delta'")
    force_flag = bool(force or force_recompute)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT profile_id::text, company_name FROM company_profiles WHERE tenant_id=%s",
            (tenant_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Tenant inconnu: {tenant_safe}")
        profile_id = str(row[0])
        company_name = str(row[1] or "")

    print(f"-- Entreprise: {_safe_console_text(company_name, max_len=180)} --")

    # A3 suit A2: si une exigence n'est plus applicable, son controle ne doit
    # plus rester comme une non-conformite active.
    synced_non_applicable = _sync_non_applicable_checks(conn, profile_id)
    if synced_non_applicable:
        print(f"  Sync A2/A3     : {synced_non_applicable} controle(s) passes en NON_EVALUE")

    print("\n-- Chargement des preuves operationnelles --")
    evidence = _load_operational_evidence(conn, profile_id)
    print(f"  Non-conformites : {len(evidence['nonconformities'])}")
    print(f"  Rapports audit  : {len(evidence['audit_reports'])}")
    print(f"  Preuves texte   : {len(evidence['evidence_documents'])}")
    print(f"  Obligations SST : {len(evidence['sst_obligations'])}")

    # A3 part uniquement du registre d'applicabilite produit par A2.
    print("\n-- Chargement des exigences applicables --")
    requirements = _load_applicable_requirements(
        conn,
        profile_id,
        doc_id=doc_id,
        limit=limit,
        site_ids=site_ids,
        process_ids=process_ids,
        activity_ids=activity_ids,
    )
    existing_map = _load_existing_checks(
        conn,
        profile_id,
        requirement_ids=[str(row["requirement_id"]) for row in requirements],
    )
    # Le mode delta limite les appels LLM aux controles nouveaux ou modifies.
    pairs = _select_compliance_pairs(
        requirements=requirements,
        evidence=evidence,
        existing_map=existing_map,
        mode=run_mode,
        force_recompute=force_flag,
    )
    print(f"  {len(requirements)} exigence(s) applicables candidates")
    print(f"  {len(pairs)} couple(s) exigence x scope a evaluer")

    if not requirements or not pairs:
        print("\n  [INFO] Aucune exigence a evaluer pour les filtres et le mode demandes")
        conn.close()
        return {
            "total": 0,
            "counts": {status: 0 for status in VALID_COMPLIANCE_STATUSES},
            "gaps": 0,
            "actions": 0,
            "mode": run_mode,
            "force_recompute": force_flag,
            "stopped": False,
        }

    counts = {s: 0 for s in VALID_COMPLIANCE_STATUSES}
    counts["ERROR"] = 0
    error_samples: list[str] = []
    total_gaps = 0
    total_actions = 0

    print(f"\n-- Evaluation ({len(pairs)} couples exigence x scope) --\n")
    print(f"  Strategie      : mode={run_mode} | force={force_flag}")

    for i, (req, evidence_ctx, existing) in enumerate(pairs, 1):
        if callable(stop_requested) and bool(stop_requested()):
            print("  [STOP] Arret demande, mise en pause apres sauvegarde des controles traites.")
            conn.close()
            total = sum(v for k, v in counts.items() if k != "ERROR")
            return {
                "total": total,
                "counts": counts,
                "gaps": total_gaps,
                "actions": total_actions,
                "error_samples": error_samples,
                "mode": run_mode,
                "force_recompute": force_flag,
                "stopped": True,
            }

        req_text_short = _safe_console_text((req["requirement_text"] or ""), max_len=65)
        qse_domain = _safe_console_text(req.get("qse_domain") or "?", max_len=20)
        scope_short = _safe_console_text(
            f"{req.get('scope_level') or 'ORGANIZATION'}:{req.get('scope_label') or req.get('scope_key') or 'ORGANIZATION'}",
            max_len=44,
        )
        rerun_hint = ""
        if existing:
            rerun_hint = f" | ancien={existing.get('compliance_status') or '?'}"
        print(f"  [{i:3d}/{len(pairs)}] {qse_domain:20s} | {scope_short}{rerun_hint}")
        print(f"           {req_text_short}...")

        if dry_run:
            print("           -> [DRY RUN] ignore")
            continue

        try:
            # Le LLM propose le diagnostic, puis les politiques internes
            # corrigent les cas sensibles: preuves expirees, gravite, priorite.
            check = evaluate_compliance(req, str(evidence_ctx.get("evidence_block") or ""), llm)
            check = _apply_compliance_policies(req, evidence_ctx, check)
            _save_compliance_check(
                conn,
                profile_id,
                req,
                check,
                llm.last_model_used,
                str(evidence_ctx.get("input_hash") or ""),
            )

            counts[check.compliance_status] += 1
            total_gaps += len(check.gaps)
            total_actions += len(check.corrective_actions)

            icon_map = {
                "CONFORME": "OK",
                "PARTIELLEMENT_CONFORME": "~~",
                "NON_CONFORME": "NC",
                "ABSENCE_DE_PREUVE": "??",
                "NON_EVALUE": "..",
            }
            icon = icon_map.get(check.compliance_status, "  ")
            provider = str(llm.last_provider_used or "llm")
            print(
                f"           -> [{icon}] {check.compliance_status} "
                f"(score={check.compliance_score:.2f}, "
                f"ecarts={len(check.gaps)}, actions={len(check.corrective_actions)}) "
                f"| {provider} | {req.get('scope_key') or 'ORGANIZATION'}"
            )

        except Exception as e:
            counts["ERROR"] += 1
            if len(error_samples) < 5:
                error_samples.append(_safe_console_text(str(e), max_len=240))
            print(f"           -> [ERREUR] {_safe_console_text(str(e), max_len=120)}", file=sys.stderr)

        if callable(stop_requested) and bool(stop_requested()):
            print("  [STOP] Arret demande, pause immediate.")
            conn.close()
            total = sum(v for k, v in counts.items() if k != "ERROR")
            return {
                "total": total,
                "counts": counts,
                "gaps": total_gaps,
                "actions": total_actions,
                "error_samples": error_samples,
                "mode": run_mode,
                "force_recompute": force_flag,
                "stopped": True,
            }

        if delay_between > 0 and i < len(pairs):
            time.sleep(delay_between)

    conn.close()

    total = sum(v for k, v in counts.items() if k != "ERROR")
    print(f"\n{'='*60}")
    print(f"RESUME - {tenant_safe}")
    print(f"  Total evalue        : {total}")
    print(f"  Conforme            : {counts.get('CONFORME', 0)}")
    print(f"  Partiellement conf. : {counts.get('PARTIELLEMENT_CONFORME', 0)}")
    print(f"  Non conforme        : {counts.get('NON_CONFORME', 0)}")
    print(f"  Absence de preuve   : {counts.get('ABSENCE_DE_PREUVE', 0)}")
    print(f"  Ecarts generes      : {total_gaps}")
    print(f"  Actions generees    : {total_actions}")
    if counts["ERROR"]:
        print(f"  Erreurs             : {counts['ERROR']}")
    if total > 0:
        score = counts.get("CONFORME", 0) / total
        print(f"  Taux de conformite  : {score:.1%}")
    print(f"{'='*60}\n")

    return {
        "total": total,
        "counts": counts,
        "gaps": total_gaps,
        "actions": total_actions,
        "error_samples": error_samples,
        "mode": run_mode,
        "force_recompute": force_flag,
        "stopped": False,
    }

def _proof_item_count(text: Any) -> int:
    """Estimate how many proof elements are listed in a free-text field."""
    raw = str(text or "").strip()
    if not raw:
        return 0
    normalized = (
        raw.replace("\r", "\n")
        .replace("\u2022", "\n")
        .replace(";", "\n")
        .replace("|", "\n")
    )
    chunks = [c.strip(" \t-_:") for c in normalized.split("\n")]
    cleaned = []
    for chunk in chunks:
        if not chunk:
            continue
        low = chunk.lower()
        if low in {"aucune", "aucun", "none", "null", "n/a", "na", "-", "?"}:
            continue
        if low.startswith("aucune preuve"):
            continue
        if len(chunk) < 4:
            continue
        cleaned.append(re.sub(r"\s+", " ", chunk.strip()))

    seen: set[str] = set()
    count = 0
    for item in cleaned:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        count += 1
    return count


def get_compliance_summary(tenant_id: str) -> dict:
    """Retourne le resume de conformite pour l'API."""
    conn = _get_conn(tenant_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT profile_id FROM company_profiles WHERE tenant_id=%s", (tenant_id,)
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            return {"error": f"Tenant inconnu: {tenant_id}"}
        profile_id = str(row[0])

        cur.execute(
            """
            SELECT compliance_status, COUNT(*), AVG(compliance_score)
            FROM compliance_checks
            WHERE profile_id=%s
            GROUP BY compliance_status
            """,
            (profile_id,),
        )
        status_rows = cur.fetchall()

        cur.execute(
            """
            SELECT severity, gap_type, COUNT(*)
            FROM gaps
            WHERE profile_id=%s
            GROUP BY severity, gap_type
            ORDER BY severity, gap_type
            """,
            (profile_id,),
        )
        gap_rows = cur.fetchall()

        cur.execute(
            """
            SELECT state, COUNT(*)
            FROM corrective_actions
            WHERE profile_id=%s
            GROUP BY state
            """,
            (profile_id,),
        )
        action_rows = cur.fetchall()

        cur.execute(
            """
            SELECT cc.check_id::text,
                   cc.requirement_id::text,
                   cc.decision_id::text,
                   COALESCE(cc.scope_level, 'ORGANIZATION'),
                   COALESCE(cc.scope_key, 'ORGANIZATION'),
                   COALESCE(cc.scope_label, 'ORGANIZATION'),
                   cc.site_id::text,
                   cc.process_id::text,
                   cc.activity_id::text,
                   cc.compliance_status,
                   cc.compliance_score,
                   cc.expected_proofs,
                   cc.found_proofs,
                   cc.missing_proofs,
                   cc.analysis_detail,
                   cc.evaluation_mode,
                   cc.llm_model,
                   cc.updated_at,
                   cc.last_evaluated_at,
                   COALESCE(cc.evaluation_version, 0),
                   COALESCE(cc.input_hash, ''),
                   COALESCE(cc.needs_recheck, FALSE),
                   r.requirement_text,
                   r.req_type,
                   r.qse_domain,
                   r.qse_sub_domain,
                   r.citation_ref,
                   r.citation_snippet,
                   ad.article_ref,
                   ad.status,
                   ad.scope_site,
                   ad.scope_process,
                   ad.scope_activity,
                   d.title,
                   d.source
            FROM compliance_checks cc
            JOIN requirements r ON r.requirement_id = cc.requirement_id
            LEFT JOIN applicability_decisions ad ON ad.decision_id = cc.decision_id
            LEFT JOIN documents d ON d.doc_id = r.doc_id
            WHERE cc.profile_id=%s
            ORDER BY cc.compliance_score ASC NULLS FIRST, cc.updated_at DESC
            LIMIT 120
            """,
            (profile_id,),
        )
        detail_rows = cur.fetchall()

        requirement_ids = [str(r[1]) for r in detail_rows if r[1]]
        gaps_detail_rows: list[tuple] = []
        actions_detail_rows: list[tuple] = []
        if requirement_ids:
            req_ph = ",".join(["%s::uuid"] * len(requirement_ids))
            cur.execute(
                f"""
                SELECT g.gap_id::text,
                       g.requirement_id::text,
                       COALESCE(g.scope_key, 'ORGANIZATION'),
                       COALESCE(g.scope_level, 'ORGANIZATION'),
                       COALESCE(g.scope_label, 'ORGANIZATION'),
                       g.site_id::text,
                       g.process_id::text,
                       g.activity_id::text,
                       g.gap_type,
                       g.severity,
                       g.description,
                       g.missing_proof,
                       g.legal_impact,
                       g.sst_impact,
                       g.env_impact,
                       g.financial_impact,
                       g.scope_site,
                       g.scope_process,
                       g.scope_activity,
                       g.treatment_priority,
                       g.created_at::text
                FROM gaps g
                WHERE g.profile_id = %s
                  AND g.requirement_id IN ({req_ph})
                ORDER BY
                  CASE g.severity
                    WHEN 'CRITIQUE' THEN 0
                    WHEN 'MAJEURE' THEN 1
                    WHEN 'MINEURE' THEN 2
                    ELSE 3
                  END,
                  g.created_at DESC
                """,
                (profile_id, *requirement_ids),
            )
            gaps_detail_rows = cur.fetchall()

            cur.execute(
                f"""
                SELECT ca.action_id::text,
                       g.requirement_id::text,
                       COALESCE(ca.scope_key, COALESCE(g.scope_key, 'ORGANIZATION')),
                       COALESCE(ca.scope_level, COALESCE(g.scope_level, 'ORGANIZATION')),
                       COALESCE(ca.scope_label, COALESCE(g.scope_label, 'ORGANIZATION')),
                       ca.site_id::text,
                       ca.process_id::text,
                       ca.activity_id::text,
                       ca.gap_id::text,
                       ca.action_title,
                       ca.action_description,
                       ca.action_type,
                       ca.responsible,
                       ca.due_date::text,
                       ca.expected_proof,
                       ca.state,
                       ca.completion_pct,
                       g.scope_site,
                       g.scope_process,
                       g.scope_activity,
                       ca.created_at::text,
                       ca.updated_at::text
                FROM corrective_actions ca
                JOIN gaps g ON g.gap_id = ca.gap_id
                WHERE ca.profile_id = %s
                  AND g.requirement_id IN ({req_ph})
                ORDER BY
                  CASE ca.state
                    WHEN 'PLANIFIEE' THEN 0
                    WHEN 'EN_COURS' THEN 1
                    WHEN 'REALISEE' THEN 2
                    WHEN 'CLOTUREE' THEN 3
                    ELSE 4
                  END,
                  ca.due_date ASC NULLS LAST,
                  ca.created_at DESC
                """,
                (profile_id, *requirement_ids),
            )
            actions_detail_rows = cur.fetchall()

        cur.execute(
            """
            SELECT g.gap_id::text,
                   g.requirement_id::text,
                   COALESCE(g.scope_key, 'ORGANIZATION'),
                   COALESCE(g.scope_level, 'ORGANIZATION'),
                   COALESCE(g.scope_label, 'ORGANIZATION'),
                   g.gap_type,
                   g.severity,
                   g.description,
                   g.missing_proof,
                   g.treatment_priority,
                   g.scope_site,
                   g.scope_process,
                   g.scope_activity,
                   g.created_at::text,
                   r.qse_domain,
                   r.requirement_text
            FROM gaps g
            LEFT JOIN requirements r ON r.requirement_id = g.requirement_id
            WHERE g.profile_id = %s
            ORDER BY
              CASE g.severity
                WHEN 'CRITIQUE' THEN 0
                WHEN 'MAJEURE' THEN 1
                WHEN 'MINEURE' THEN 2
                ELSE 3
              END,
              g.created_at DESC
            LIMIT 80
            """,
            (profile_id,),
        )
        recent_gap_rows = cur.fetchall()

        cur.execute(
            """
            SELECT ca.action_id::text,
                   ca.gap_id::text,
                   COALESCE(ca.scope_key, COALESCE(g.scope_key, 'ORGANIZATION')),
                   COALESCE(ca.scope_level, COALESCE(g.scope_level, 'ORGANIZATION')),
                   COALESCE(ca.scope_label, COALESCE(g.scope_label, 'ORGANIZATION')),
                   ca.action_title,
                   ca.action_description,
                   ca.action_type,
                   ca.responsible,
                   ca.due_date::text,
                   ca.expected_proof,
                   ca.state,
                   ca.completion_pct,
                   g.scope_site,
                   g.scope_process,
                   g.scope_activity,
                   ca.created_at::text,
                   ca.updated_at::text,
                   g.requirement_id::text,
                   g.severity,
                   g.gap_type,
                   r.qse_domain,
                   r.requirement_text
            FROM corrective_actions ca
            LEFT JOIN gaps g ON g.gap_id = ca.gap_id
            LEFT JOIN requirements r ON r.requirement_id = g.requirement_id
            WHERE ca.profile_id = %s
            ORDER BY
              CASE ca.state
                WHEN 'PLANIFIEE' THEN 0
                WHEN 'EN_COURS' THEN 1
                WHEN 'REALISEE' THEN 2
                WHEN 'CLOTUREE' THEN 3
                ELSE 4
              END,
              ca.due_date ASC NULLS LAST,
              ca.created_at DESC
            LIMIT 120
            """,
            (profile_id,),
        )
        recent_action_rows = cur.fetchall()

        evidence = _load_operational_evidence(conn, profile_id)

    conn.close()

    total_checks = sum(r[1] for r in status_rows)
    conforme = next((r[1] for r in status_rows if r[0] == "CONFORME"), 0)
    compliance_rate = conforme / total_checks if total_checks > 0 else 0

    gaps_by_scope: dict[str, list[dict[str, Any]]] = {}
    for row in gaps_detail_rows:
        req_id = str(row[1] or "")
        scope_key = str(row[2] or "ORGANIZATION")
        compound_key = f"{req_id}::{scope_key}"
        if not req_id:
            continue
        gaps_by_scope.setdefault(compound_key, []).append(
            {
                "gap_id": row[0],
                "requirement_id": row[1],
                "scope_key": scope_key,
                "scope_level": row[3],
                "scope_label": row[4],
                "site_id": row[5],
                "process_id": row[6],
                "activity_id": row[7],
                "gap_type": row[8],
                "severity": row[9],
                "description": row[10],
                "missing_proof": row[11],
                "legal_impact": row[12],
                "sst_impact": row[13],
                "env_impact": row[14],
                "financial_impact": row[15],
                "scope_site": row[16],
                "scope_process": row[17],
                "scope_activity": row[18],
                "treatment_priority": row[19],
                "created_at": row[20],
            }
        )

    actions_by_scope: dict[str, list[dict[str, Any]]] = {}
    for row in actions_detail_rows:
        req_id = str(row[1] or "")
        scope_key = str(row[2] or "ORGANIZATION")
        compound_key = f"{req_id}::{scope_key}"
        if not req_id:
            continue
        actions_by_scope.setdefault(compound_key, []).append(
            {
                "action_id": row[0],
                "requirement_id": row[1],
                "scope_key": scope_key,
                "scope_level": row[3],
                "scope_label": row[4],
                "site_id": row[5],
                "process_id": row[6],
                "activity_id": row[7],
                "gap_id": row[8],
                "action_title": row[9],
                "action_description": row[10],
                "action_type": row[11],
                "responsible": row[12],
                "due_date": row[13],
                "expected_proof": row[14],
                "state": row[15],
                "completion_pct": float(row[16] or 0),
                "scope_site": row[17],
                "scope_process": row[18],
                "scope_activity": row[19],
                "created_at": row[20],
                "updated_at": row[21],
            }
        )

    worst_items: list[dict[str, Any]] = []
    for row in detail_rows:
        status = str(row[9] or "")
        expected = str(row[11] or "")
        found = str(row[12] or "")
        missing = str(row[13] or "")

        expected_count = _proof_item_count(expected)
        found_count = _proof_item_count(found)
        missing_count = _proof_item_count(missing)

        if expected_count <= 0:
            expected_count = max(found_count, missing_count)
        if expected_count <= 0 and status.upper() == "ABSENCE_DE_PREUVE":
            expected_count = 1
        expected_count = max(expected_count, found_count)

        req_id = str(row[1] or "")
        scope_key = str(row[4] or "ORGANIZATION")
        compound_key = f"{req_id}::{scope_key}"
        evidence_ctx = _build_requirement_evidence_context(
            {
                "requirement_id": req_id,
                "scope_level": row[3],
                "scope_key": scope_key,
                "scope_label": row[5],
                "site_id": row[6],
                "process_id": row[7],
                "activity_id": row[8],
                "requirement_text": row[22] or "",
                "req_type": row[23],
                "qse_domain": row[24],
                "qse_sub_domain": row[25],
                "citation_ref": row[26],
                "citation_snippet": row[27],
                "article_ref": row[28],
                "scope_site": row[30],
                "scope_process": row[31],
                "scope_activity": row[32],
                "app_status": row[29],
                "justification": "",
            },
            evidence,
        )
        worst_items.append(
            {
                "check_id": row[0],
                "requirement_id": req_id,
                "decision_id": row[2],
                "scope_level": row[3],
                "scope_key": scope_key,
                "scope_label": row[5],
                "site_id": row[6],
                "process_id": row[7],
                "activity_id": row[8],
                "status": status,
                "score": round(row[10] or 0, 3),
                "expected_proofs": expected,
                "found_proofs": found,
                "missing_proofs": missing,
                "analysis_detail": row[14],
                "evaluation_mode": row[15] or "PERIODIQUE",
                "llm_model": row[16],
                "updated_at": row[17].isoformat() if row[17] else None,
                "last_evaluated_at": row[18].isoformat() if row[18] else None,
                "evaluation_version": int(row[19] or 0),
                "input_hash": row[20],
                "needs_recheck": bool(row[21]),
                "requirement_text": row[22] or "",
                "requirement": (row[22] or "")[:160],
                "req_type": row[23],
                "domain": row[24],
                "qse_domain": row[24],
                "qse_sub_domain": row[25],
                "citation_ref": row[26],
                "citation_snippet": row[27],
                "article_ref": row[28],
                "applicability_status": row[29],
                "scope_site": row[30],
                "scope_process": row[31],
                "scope_activity": row[32],
                "doc_title": row[33],
                "doc_source": row[34],
                "evidence_freshness": evidence_ctx.get("evidence_freshness") or {},
                "proof_progress": {
                    "expected": int(expected_count),
                    "found": int(found_count),
                    "missing": int(max(0, expected_count - found_count) if missing_count <= 0 else missing_count),
                },
                "gaps": gaps_by_scope.get(compound_key, []),
                "actions": actions_by_scope.get(compound_key, []),
            }
        )

    nc_reg_rows = [r for r in gap_rows if str(r[1] or "").upper() == "NC_REGLEMENTAIRE"]
    nc_major = sum(int(r[2] or 0) for r in nc_reg_rows if str(r[0] or "").upper() == "MAJEURE")
    nc_minor = sum(int(r[2] or 0) for r in nc_reg_rows if str(r[0] or "").upper() == "MINEURE")
    nc_critical = sum(int(r[2] or 0) for r in nc_reg_rows if str(r[0] or "").upper() == "CRITIQUE")
    nc_total = nc_major + nc_minor + nc_critical
    proof_items_with_expired = sum(1 for item in worst_items if int((item.get("evidence_freshness") or {}).get("expired_count") or 0) > 0)
    proof_items_only_expired = sum(1 for item in worst_items if bool((item.get("evidence_freshness") or {}).get("only_expired")))

    return {
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "total_checks": total_checks,
        "compliance_rate": round(compliance_rate, 3),
        "status_breakdown": {r[0]: {"count": r[1], "avg_score": round(r[2] or 0, 3)} for r in status_rows},
        "gaps_breakdown": [{"severity": r[0], "gap_type": r[1], "count": r[2]} for r in gap_rows],
        "actions_breakdown": {r[0]: r[1] for r in action_rows},
        "nc_reglementaire": {
            "total": int(nc_total),
            "major": int(nc_major),
            "minor": int(nc_minor),
            "critical": int(nc_critical),
        },
        "proof_freshness": {
            "items_with_expired_proofs": int(proof_items_with_expired),
            "items_only_expired_proofs": int(proof_items_only_expired),
        },
        "recent_gaps": [
            {
                "gap_id": r[0],
                "requirement_id": r[1],
                "scope_key": r[2],
                "scope_level": r[3],
                "scope_label": r[4],
                "gap_type": r[5],
                "severity": r[6],
                "description": r[7],
                "missing_proof": r[8],
                "treatment_priority": r[9],
                "scope_site": r[10],
                "scope_process": r[11],
                "scope_activity": r[12],
                "created_at": r[13],
                "qse_domain": r[14],
                "requirement_text": r[15] or "",
            }
            for r in recent_gap_rows
        ],
        "recent_actions": [
            {
                "action_id": r[0],
                "gap_id": r[1],
                "scope_key": r[2],
                "scope_level": r[3],
                "scope_label": r[4],
                "action_title": r[5],
                "action_description": r[6],
                "action_type": r[7],
                "responsible": r[8],
                "due_date": r[9],
                "expected_proof": r[10],
                "state": r[11],
                "completion_pct": float(r[12] or 0),
                "scope_site": r[13],
                "scope_process": r[14],
                "scope_activity": r[15],
                "created_at": r[16],
                "updated_at": r[17],
                "requirement_id": r[18],
                "gap_severity": r[19],
                "gap_type": r[20],
                "qse_domain": r[21],
                "requirement_text": r[22] or "",
            }
            for r in recent_action_rows
        ],
        "worst_items": worst_items,
    }


# CLI

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent 3 - Conformite reglementaire")
    parser.add_argument("--tenant", required=True, help="Tenant ID a traiter")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--mode", default="delta", choices=["full", "delta"])
    parser.add_argument("--force", action="store_true", help="Recalcule les scopes deja controles")
    parser.add_argument("--site-id", action="append", default=[], help="Limiter a un site_id (multi)")
    parser.add_argument("--process-id", action="append", default=[], help="Limiter a un process_id (multi)")
    parser.add_argument("--activity-id", action="append", default=[], help="Limiter a un activity_id (multi)")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    if args.summary:
        summary = get_compliance_summary(args.tenant)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        run_compliance(
            tenant_id=args.tenant,
            limit=args.limit,
            dry_run=args.dry_run,
            delay_between=args.delay,
            mode=args.mode,
            force=args.force,
            site_ids=list(args.site_id or []),
            process_ids=list(args.process_id or []),
            activity_ids=list(args.activity_id or []),
        )
