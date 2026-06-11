"""
a2_applicability_engine.py
==========================
Agent 2 - Moteur d'applicabilite reglementaire QALITAS.

Pour chaque exigence reglementaire promue (Agent 1), l'agent :
    1. Lit le contexte reel de l'entreprise (depuis la DB)
    2. Transforme ce contexte en perimetres d'evaluation (organisation, site,
       processus, activite)
    3. Croise chaque exigence avec les perimetres pertinents
    4. Decide par regle deterministe quand c'est fiable, sinon via LLM :
       APPLICABLE / APPLICABLE_FUTUR / NON_APPLICABLE /
       APPLICABLE_SOUS_CONDITIONS / INCERTAIN
    5. Produit une justification tracable (article -> condition -> donnee entreprise)
    6. Sauvegarde la decision dans applicability_decisions

Usage:
    python a2_applicability_engine.py --tenant <tenant_id>
    python a2_applicability_engine.py --tenant <tenant_id> --limit 20
    python a2_applicability_engine.py --tenant <tenant_id> --req-id <uuid>
    python a2_applicability_engine.py --tenant <tenant_id> --rerun-status INCERTAIN
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
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
    text = "".join(ch if (ch == "\n" or ch == "\t" or ord(ch) >= 32) else " " for ch in text)
    return text


_configure_stdio_utf8()

# --- Constantes ---------------------------------------------------------------
# Les constantes ci-dessous pilotent la politique metier du moteur:
# - statuts autorises;
# - seuils de confiance minimaux;
# - signaux textuels pour router une exigence vers un perimetre local;
# - modeles de regles deterministes avant de payer un appel LLM.

VALID_STATUSES = {"APPLICABLE", "APPLICABLE_FUTUR", "NON_APPLICABLE", "APPLICABLE_SOUS_CONDITIONS", "INCERTAIN"}
FUTURE_ELIGIBLE_STATUSES = {"APPLICABLE", "APPLICABLE_SOUS_CONDITIONS"}

DEFAULT_QUALITY_GATE_MIN_CONF = 0.70
RULE_ENGINE_MODEL = "A2_RULE_ENGINE_V1"
DEFAULT_A2_DELAY_SECONDS = 0.15
DEFAULT_A2_COMMIT_BATCH_SIZE = 20
DEFAULT_A2_LLM_MAX_OUTPUT_TOKENS = 1024

MIN_CONFIDENCE_BY_TYPE: dict[str, float] = {
    "OBLIGATION": 0.75,
    "INTERDICTION": 0.80,
    "RESPONSABILITE": 0.75,
    "REGISTRE": 0.65,
    "DECLARATION": 0.65,
    "CONTROLE": 0.70,
    "CONDITION": 0.70,
    "EXCEPTION": 0.60,
    "AUTRE": DEFAULT_QUALITY_GATE_MIN_CONF,
}

FRENCH_MONTHS: dict[str, int] = {
    "janvier": 1,
    "fevrier": 2,
    "fevr": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
}

TEMPORAL_EFFECTIVE_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:a compter du|a partir du|applicable a compter du|applicable a partir du|"
        r"entre en vigueur le|entre en application le|prend effet le)\s+(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})"
    ),
    re.compile(
        r"(?:a compter du|a partir du|applicable a compter du|applicable a partir du|"
        r"entre en vigueur le|entre en application le|prend effet le)\s+(\d{1,2})(?:er)?\s+"
        r"(janvier|fevrier|fevr|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre)\s+(\d{4})"
    ),
)

TEMPORAL_DELAY_PATTERN = re.compile(
    r"(?:dans un delai de|au plus tard dans un delai de)\s+(\d{1,4})\s+(jour|jours|mois|an|ans)"
)

SECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "TEXTILE": ("textile", "habillement", "filature", "tissage", "confection"),
    "CHIMIQUE": ("chimique", "petrochim", "solvant", "engrais", "pesticide"),
    "AGROALIMENTAIRE": ("agroaliment", "alimentaire", "abattoir", "laitier", "conserverie"),
    "CONSTRUCTION": ("chantier", "construction", "btp", "travaux publics", "batiment"),
    "TRANSPORT": ("transport", "logistique", "routier", "ferroviaire", "maritime", "aerien"),
    "SANTE": ("hopital", "clinique", "medical", "sante"),
    "EDUCATION": ("ecole", "universite", "enseignement", "etablissement scolaire"),
}

SECTOR_SCOPE_CUES = (
    "secteur",
    "entreprises de",
    "entreprises du",
    "etablissements de",
    "etablissements du",
)

CHEMICAL_CUES = (
    "substance dangereuse",
    "substances dangereuses",
    "produits chimiques",
    "produit chimique",
    "fiche de donnees de securite",
    "fds",
    "solvant",
    "acide",
    "toxique",
    "inflammable",
    "corrosif",
)

ACTIVITY_THEME_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "TRAVAUX_EN_HAUTEUR": {
        "requirement": ("travaux en hauteur", "echafaud", "harnais", "ligne de vie", "nacelle"),
        "company": ("hauteur", "echafaud", "harnais", "nacelle"),
    },
    "LEVAGE_MANUTENTION": {
        "requirement": ("levage", "grue", "pont roulant", "chariot elevateur", "transpalette"),
        "company": ("grue", "pont roulant", "chariot", "transpalette", "levage"),
    },
    "PRESSION_CHAUDIERE": {
        "requirement": ("chaudiere", "equipement sous pression", "compresseur"),
        "company": ("chaudiere", "compresseur", "pression"),
    },
}

GLOBAL_ONLY_REQ_TYPES: tuple[str, ...] = ("DECLARATION", "RESPONSABILITE")
ROUTE_BY_TYPE_SCOPE_LEVELS: dict[str, tuple[str, ...]] = {
    "DECLARATION": ("ORGANIZATION",),
    "RESPONSABILITE": ("ORGANIZATION",),
    "REGISTRE": ("ORGANIZATION", "SITE", "PROCESS"),
    "CONTROLE": ("ORGANIZATION", "SITE", "PROCESS"),
}
ROUTE_BY_SUBDOMAIN_SCOPE_LEVELS: dict[str, tuple[str, ...]] = {
    "DECLARATION": ("ORGANIZATION",),
    "RESPONSABILITES": ("ORGANIZATION",),
    "REGISTRE": ("ORGANIZATION", "SITE", "PROCESS"),
    "CONTROLE": ("ORGANIZATION", "SITE", "PROCESS"),
}
ORGANIZATION_HEAVY_DOMAINS: tuple[str, ...] = ("ADMINISTRATIF", "GOUVERNANCE")
FIELD_OPERATIONAL_DOMAINS: tuple[str, ...] = ("SST", "ENVIRONNEMENT")

GLOBAL_ONLY_CUES: tuple[str, ...] = (
    "declar",
    "communiqu",
    "inform",
    "notif",
    "transmet",
    "autoris",
    "agrement",
    "designation",
    "nomination",
    "delegation",
    "responsable",
    "politique",
    "programme",
)

LOCAL_SCOPE_CUES: tuple[str, ...] = (
    "site",
    "atelier",
    "chantier",
    "zone",
    "local",
    "poste",
    "machine",
    "equipement",
    "installation",
    "processus",
    "activite",
    "stockage",
    "chaudiere",
    "pression",
    "levage",
    "grue",
    "chariot",
    "transpalette",
    "echafaud",
    "harnais",
    "nacelle",
    "bruit",
    "vibration",
    "dechet",
    "dechets",
    "emission",
    "rejet",
    "substance dangereuse",
    "substances dangereuses",
    "produits chimiques",
    "produit chimique",
)

SITE_SCOPE_CUES: tuple[str, ...] = (
    "site",
    "etablissement",
    "local",
    "zone",
    "entrepot",
    "depot",
)

PROCESS_SCOPE_CUES: tuple[str, ...] = (
    "processus",
    "procedure",
    "mode operatoire",
    "circuit",
    "ligne",
    "fabrication",
)

ACTIVITY_SCOPE_CUES: tuple[str, ...] = (
    "activite",
    "atelier",
    "chantier",
    "poste",
    "tache",
    "operation",
)

ENVIRONMENT_LOCAL_CUES: tuple[str, ...] = (
    "dechet",
    "dechets",
    "emission",
    "rejet",
    "pollution",
    "effluent",
    "deversement",
)

SST_LOCAL_CUES: tuple[str, ...] = (
    "bruit",
    "vibration",
    "chute",
    "incendie",
    "explosion",
    "electrique",
    "manutention",
    "levage",
    "harnais",
    "echafaud",
)

TOKEN_STOPWORDS: set[str] = {
    "avec",
    "dans",
    "pour",
    "sans",
    "entre",
    "toute",
    "toutes",
    "tous",
    "cette",
    "cet",
    "sont",
    "doivent",
    "doit",
    "etre",
    "plus",
    "moins",
    "leurs",
    "leur",
    "ainsi",
    "comme",
    "mise",
    "place",
    "sites",
    "activites",
    "processus",
    "entreprise",
    "employeur",
    "etablissement",
    "organisme",
    "organisation",
}

INSTITUTIONAL_ACTOR_PREFIXES: tuple[str, ...] = (
    "le ministre",
    "la ministre",
    "le ministere",
    "le secretaire d'etat",
    "la direction generale",
    "la direction regionale",
    "la direction",
    "le chef de l'inspection",
    "l'inspection du travail",
    "le president du conseil de prud'hommes",
    "le conseil de prud'hommes",
    "les tribunaux",
    "le tribunal",
    "les juridictions",
    "la juridiction",
    "les services competents",
    "les syndicats",
    "le syndicat",
)

PRIVATE_COMPANY_CUES: tuple[str, ...] = (
    "entreprise",
    "employeur",
    "etablissement",
    "exploitant",
    "societe",
    "organisme",
)

SYSTEM_PROMPT = """Tu es un expert reglementaire QHSE senior specialise en droit tunisien et en normes ISO (9001, 14001, 45001).

Ta mission: determiner si une exigence reglementaire est APPLICABLE a une entreprise donnee, en te basant sur le contexte reel de l'entreprise.

REGLES ABSOLUES:
1. Tu DOIS justifier chaque decision en citant:
    - L'article ou le texte reglementaire exact
    - La condition legale evaluee (seuil, activite, substance, effectif, equipement)
    - La donnee reelle de l'entreprise qui confirme ou infirme la condition
2. Tu ne dois JAMAIS inventer des donnees - si une information manque, utilise "INCERTAIN"
3. Tu dois separer clairement: OBLIGATOIRE vs RECOMMANDE vs EXCLURE
4. Tes decisions doivent etre defendables en audit externe

STATUTS POSSIBLES:
- APPLICABLE: l'exigence s'applique clairement a l'entreprise
- APPLICABLE_FUTUR: l'exigence s'appliquera, mais uniquement a partir d'une date future explicite
- NON_APPLICABLE: l'exigence ne s'applique pas (avec justification precise)
- APPLICABLE_SOUS_CONDITIONS: applicable uniquement si certaines conditions sont reunies
- INCERTAIN: donnees insuffisantes pour statuer (liste les informations manquantes)

FORMAT DE REPONSE (JSON strict):
{
    "status": "APPLICABLE" | "APPLICABLE_FUTUR" | "NON_APPLICABLE" | "APPLICABLE_SOUS_CONDITIONS" | "INCERTAIN",
    "confidence": 0.0 a 1.0,
    "justification": "Justification complete et argumentee...",
    "article_ref": "Reference precise de l'article ou du texte",
    "condition_evaluated": "Condition legale evaluee (ex: effectif > 50, utilisation de substances X...)",
    "company_data_used": "Donnees entreprise utilisees pour la decision",
    "scope_site": "Site(s) concerne(s) ou null",
    "scope_process": "Processus concerne(s) ou null",
    "scope_activity": "Activite(s) concernee(s) ou null",
    "is_voluntary": false,
    "voluntary_reason": null,
    "missing_data": "Donnees manquantes si INCERTAIN, sinon null"
}
"""


# --- Schema de reponse LLM ----------------------------------------------------

class ApplicabilityDecisionLLM(BaseModel):
    status: str
    confidence: float = 0.5
    justification: str
    article_ref: str | None = None
    condition_evaluated: str | None = None
    company_data_used: str | None = None
    scope_site: str | None = None
    scope_process: str | None = None
    scope_activity: str | None = None
    is_voluntary: bool = False
    voluntary_reason: str | None = None
    missing_data: str | None = None

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v: Any) -> str:
        val = str(v or "").strip().upper()
        if val not in VALID_STATUSES:
            return "INCERTAIN"
        return val

    @field_validator("confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, v: Any) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.5


# --- Contexte entreprise ------------------------------------------------------

@dataclass
class CompanyBaseContext:
    """Contexte brut charge depuis la DB avant decoupage en scopes."""

    profile_id: str
    tenant_id: str
    company_name: str
    sector: str
    country: str
    certifications: list[str]
    headcount_total: int | None
    main_activities: str | None
    sites: list[dict[str, Any]]
    processes: list[dict[str, Any]]
    activities: list[dict[str, Any]]
    equipment_rows: list[dict[str, Any]]
    product_rows: list[dict[str, Any]]
    environmental_rows: list[dict[str, Any]]
    sst_risk_rows: list[dict[str, Any]]
    objectives: list[str]


@dataclass
class CompanyContext:
    """Vue focalisee de l'entreprise pour un perimetre A2 donne."""

    profile_id: str
    tenant_id: str
    company_name: str
    sector: str
    country: str
    certifications: list[str]
    scope_level: str
    scope_key: str
    scope_label: str
    site_id: str | None = None
    process_id: str | None = None
    activity_id: str | None = None
    site_name: str = ""
    city: str = ""
    employee_count: int | None = None
    main_activities: str | None = None
    process_name: str | None = None
    activity_name: str | None = None
    equipment_list: list[str] = field(default_factory=list)
    product_types: list[str] = field(default_factory=list)
    chemical_aspects: list[str] = field(default_factory=list)
    environmental_aspects: list[str] = field(default_factory=list)
    sst_risk_domains: list[str] = field(default_factory=list)
    sst_risk_types: list[str] = field(default_factory=list)
    objectives: list[str] = field(default_factory=list)


def _load_company_base_context(conn: psycopg.Connection, tenant_id: str) -> CompanyBaseContext:
    """Charge le contexte complet de l'entreprise depuis la DB."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT  p.profile_id, p.tenant_id, p.company_name, p.sector,
                    p.country, p.certifications, p.headcount_total, p.main_activities
            FROM company_profiles p
            WHERE p.tenant_id = %s
        """, (tenant_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Tenant introuvable: {tenant_id}")

        profile_id = str(row[0])

        cur.execute("""
            SELECT  site_id::text, COALESCE(site_name, ''), COALESCE(city, ''),
                    employee_count, COALESCE(main_activities, '')
            FROM company_sites
            WHERE profile_id = %s
            ORDER BY created_at NULLS LAST, site_name
        """, (profile_id,))
        site_rows = [
            {
                "site_id": str(r[0]) if r[0] else None,
                "site_name": str(r[1] or ""),
                "city": str(r[2] or ""),
                "employee_count": int(r[3]) if r[3] is not None else None,
                "main_activities": str(r[4] or ""),
            }
            for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT  cp.process_id::text, cp.site_id::text,
                    COALESCE(cp.process_name, ''), COALESCE(s.site_name, ''), COALESCE(cp.description, '')
            FROM company_processes cp
            LEFT JOIN company_sites s ON s.site_id = cp.site_id
            WHERE cp.profile_id = %s
            ORDER BY cp.created_at NULLS LAST, cp.process_name
        """, (profile_id,))
        process_rows = [
            {
                "process_id": str(r[0]) if r[0] else None,
                "site_id": str(r[1]) if r[1] else None,
                "process_name": str(r[2] or ""),
                "site_name": str(r[3] or ""),
                "description": str(r[4] or ""),
            }
            for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT  ca.activity_id::text, ca.site_id::text, ca.process_id::text,
                    COALESCE(ca.process_name, cp.process_name, ''),
                    COALESCE(ca.activity_name, ''), COALESCE(s.site_name, ''),
                    COALESCE(ca.description, '')
            FROM company_activities ca
            LEFT JOIN company_processes cp ON cp.process_id = ca.process_id
            LEFT JOIN company_sites s ON s.site_id = ca.site_id
            WHERE ca.profile_id = %s
            ORDER BY ca.created_at NULLS LAST, COALESCE(ca.activity_name, '')
        """, (profile_id,))
        activity_rows = [
            {
                "activity_id": str(r[0]) if r[0] else None,
                "site_id": str(r[1]) if r[1] else None,
                "process_id": str(r[2]) if r[2] else None,
                "process_name": str(r[3] or ""),
                "activity_name": str(r[4] or ""),
                "site_name": str(r[5] or ""),
                "description": str(r[6] or ""),
            }
            for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT site_id::text, COALESCE(designation, ''), COALESCE(category, '')
            FROM company_equipment
            WHERE profile_id = %s AND designation IS NOT NULL
        """, (profile_id,))
        equipment_rows = [
            {
                "site_id": str(r[0]) if r[0] else None,
                "designation": str(r[1] or ""),
                "category": str(r[2] or ""),
            }
            for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT COALESCE(site_name, ''), COALESCE(nature, ''), COALESCE(family, ''),
                    COALESCE(category, ''), COALESCE(product_type, ''), COALESCE(designation, '')
            FROM company_products
            WHERE profile_id = %s
        """, (profile_id,))
        product_rows = [
            {
                "site_name": str(r[0] or ""),
                "nature": str(r[1] or ""),
                "family": str(r[2] or ""),
                "category": str(r[3] or ""),
                "product_type": str(r[4] or ""),
                "designation": str(r[5] or ""),
            }
            for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT site_id::text, COALESCE(designation, ''), COALESCE(domain, '')
            FROM environmental_aspects
            WHERE profile_id = %s
        """, (profile_id,))
        environmental_rows = [
            {
                "site_id": str(r[0]) if r[0] else None,
                "designation": str(r[1] or ""),
                "domain": str(r[2] or ""),
            }
            for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT site_id::text, COALESCE(domain, ''), COALESCE(risk_type, ''), COALESCE(designation, '')
            FROM sst_risks
            WHERE profile_id = %s
        """, (profile_id,))
        sst_risk_rows = [
            {
                "site_id": str(r[0]) if r[0] else None,
                "domain": str(r[1] or ""),
                "risk_type": str(r[2] or ""),
                "designation": str(r[3] or ""),
            }
            for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT DISTINCT objective_text FROM strategic_objectives
            WHERE profile_id = %s AND objective_text IS NOT NULL
            LIMIT 15
        """, (profile_id,))
        objectives = [r[0][:100] for r in cur.fetchall()]

    return CompanyBaseContext(
        profile_id=profile_id,
        tenant_id=str(row[1]),
        company_name=str(row[2]),
        sector=str(row[3] or ""),
        country=str(row[4] or "TN"),
        certifications=list(row[5] or []),
        headcount_total=int(row[6]) if row[6] is not None else None,
        main_activities=str(row[7] or ""),
        sites=site_rows,
        processes=process_rows,
        activities=activity_rows,
        equipment_rows=equipment_rows,
        product_rows=product_rows,
        environmental_rows=environmental_rows,
        sst_risk_rows=sst_risk_rows,
        objectives=objectives,
    )


def _dedupe_strings(values: list[str], limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        key = _normalize_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(text)
        if limit and len(output) >= limit:
            break
    return output


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _rows_for_site(rows: list[dict[str, Any]], site_id: str | None) -> list[dict[str, Any]]:
    if not site_id:
        return list(rows)
    return [row for row in rows if str(row.get("site_id") or "") == site_id]


def _match_site_name(value: str, site_name: str) -> bool:
    left = _normalize_text(value)
    right = _normalize_text(site_name)
    if not left or not right:
        return False
    return left == right


def _product_rows_for_site(rows: list[dict[str, Any]], site_name: str | None) -> list[dict[str, Any]]:
    if not site_name:
        return list(rows)
    return [row for row in rows if _match_site_name(str(row.get("site_name") or ""), site_name)]


def _equipment_labels(rows: list[dict[str, Any]]) -> list[str]:
    return _dedupe_strings([
        _first_non_empty(
            " - ".join(part for part in [row.get("designation"), row.get("category")] if str(part or "").strip()),
            row.get("designation"),
            row.get("category"),
        )
        for row in rows
    ])


def _product_labels(rows: list[dict[str, Any]]) -> list[str]:
    return _dedupe_strings([
        _first_non_empty(
            row.get("product_type"),
            row.get("designation"),
            row.get("family"),
            row.get("category"),
            row.get("nature"),
        )
        for row in rows
    ])


def _environmental_labels(rows: list[dict[str, Any]]) -> list[str]:
    return _dedupe_strings([
        _first_non_empty(
            " - ".join(part for part in [row.get("designation"), row.get("domain")] if str(part or "").strip()),
            row.get("designation"),
            row.get("domain"),
        )
        for row in rows
    ])


def _sst_domain_labels(rows: list[dict[str, Any]]) -> list[str]:
    return _dedupe_strings([str(row.get("domain") or "") for row in rows])


def _sst_type_labels(rows: list[dict[str, Any]]) -> list[str]:
    return _dedupe_strings([
        _first_non_empty(
            " - ".join(part for part in [row.get("risk_type"), row.get("designation")] if str(part or "").strip()),
            row.get("risk_type"),
            row.get("designation"),
        )
        for row in rows
    ])


def _chemical_labels(
    product_rows: list[dict[str, Any]],
    environmental_rows: list[dict[str, Any]],
) -> list[str]:
    chemical_terms = (
        "chim",
        "solvant",
        "acide",
        "base",
        "tox",
        "corros",
        "inflamm",
        "peinture",
        "deterg",
        "hydrocarb",
        "gaz",
    )
    hits: list[str] = []
    for row in product_rows:
        blob = " | ".join(
            str(row.get(key) or "")
            for key in ("designation", "product_type", "family", "category", "nature")
        )
        if any(term in _normalize_text(blob) for term in chemical_terms):
            hits.append(_first_non_empty(row.get("designation"), row.get("product_type"), row.get("family"), blob))
    for row in environmental_rows:
        blob = " | ".join(str(row.get(key) or "") for key in ("designation", "domain"))
        if any(term in _normalize_text(blob) for term in chemical_terms):
            hits.append(_first_non_empty(row.get("designation"), row.get("domain"), blob))
    return _dedupe_strings(hits, limit=20)


def _site_lookup(base_ctx: CompanyBaseContext) -> dict[str, dict[str, Any]]:
    return {
        str(row["site_id"]): row
        for row in base_ctx.sites
        if str(row.get("site_id") or "").strip()
    }


def _process_lookup(base_ctx: CompanyBaseContext) -> dict[str, dict[str, Any]]:
    return {
        str(row["process_id"]): row
        for row in base_ctx.processes
        if str(row.get("process_id") or "").strip()
    }


def _resolve_total_headcount(base_ctx: CompanyBaseContext) -> int | None:
    if base_ctx.headcount_total is not None:
        return int(base_ctx.headcount_total)
    site_counts = [int(row["employee_count"]) for row in base_ctx.sites if row.get("employee_count") is not None]
    return sum(site_counts) if site_counts else None

# Crée Les Périmètres -------------------------------------------------------------------------------------

def _build_scope_context(
    base_ctx: CompanyBaseContext,
    scope_level: str,
    site_row: dict[str, Any] | None = None,
    process_row: dict[str, Any] | None = None,
    activity_row: dict[str, Any] | None = None,
) -> CompanyContext:
    """Construit une vue metier coherente pour un scope precis.

    Le scope ORGANIZATION garde une vision globale, alors que SITE/PROCESS/
    ACTIVITY filtrent les donnees rattachees au site concerne. Cela donne au
    LLM et aux regles une entree concise, sans perdre les preuves utiles.
    """
    sites_by_id = _site_lookup(base_ctx)
    processes_by_id = _process_lookup(base_ctx)

    site_id = str(
        (site_row or {}).get("site_id")
        or (process_row or {}).get("site_id")
        or (activity_row or {}).get("site_id")
        or ""
    ).strip() or None
    process_id = str(
        (process_row or {}).get("process_id")
        or (activity_row or {}).get("process_id")
        or ""
    ).strip() or None
    activity_id = str((activity_row or {}).get("activity_id") or "").strip() or None

    if not site_row and site_id:
        site_row = sites_by_id.get(site_id)
    if not process_row and process_id:
        process_row = processes_by_id.get(process_id)

    site_name = _first_non_empty(
        (site_row or {}).get("site_name"),
        (process_row or {}).get("site_name"),
        (activity_row or {}).get("site_name"),
    )
    process_name = _first_non_empty(
        (process_row or {}).get("process_name"),
        (activity_row or {}).get("process_name"),
    )
    activity_name = _first_non_empty((activity_row or {}).get("activity_name"))

    if scope_level == "ORGANIZATION":
        scope_key = "ORGANIZATION"
        scope_label = base_ctx.company_name or "Organisation"
        city = _first_non_empty(base_ctx.country)
        employee_count = _resolve_total_headcount(base_ctx)
        activities_text = _first_non_empty(
            base_ctx.main_activities,
            ", ".join(_dedupe_strings([str(row.get("activity_name") or "") for row in base_ctx.activities], limit=12)),
            ", ".join(_dedupe_strings([str(row.get("process_name") or "") for row in base_ctx.processes], limit=8)),
        )
        equipment_rows = list(base_ctx.equipment_rows)
        product_rows = list(base_ctx.product_rows)
        environmental_rows = list(base_ctx.environmental_rows)
        sst_rows = list(base_ctx.sst_risk_rows)
    elif scope_level == "SITE":
        if not site_id:
            raise ValueError("Scope SITE invalide: site_id manquant")
        scope_key = f"SITE:{site_id}"
        scope_label = _first_non_empty(
            " - ".join(part for part in [site_name, (site_row or {}).get("city")] if str(part or "").strip()),
            site_name,
            site_id,
        )
        city = str((site_row or {}).get("city") or "")
        employee_count = (site_row or {}).get("employee_count")
        site_activities = [
            str(row.get("activity_name") or "")
            for row in base_ctx.activities
            if str(row.get("site_id") or "") == site_id
        ]
        site_processes = [
            str(row.get("process_name") or "")
            for row in base_ctx.processes
            if str(row.get("site_id") or "") == site_id
        ]
        activities_text = _first_non_empty(
            (site_row or {}).get("main_activities"),
            ", ".join(_dedupe_strings(site_activities, limit=12)),
            ", ".join(_dedupe_strings(site_processes, limit=8)),
            base_ctx.main_activities,
        )
        equipment_rows = _rows_for_site(base_ctx.equipment_rows, site_id)
        product_rows = _product_rows_for_site(base_ctx.product_rows, site_name)
        environmental_rows = _rows_for_site(base_ctx.environmental_rows, site_id)
        sst_rows = _rows_for_site(base_ctx.sst_risk_rows, site_id)
    elif scope_level == "PROCESS":
        if not process_id:
            raise ValueError("Scope PROCESS invalide: process_id manquant")
        scope_key = f"PROCESS:{process_id}"
        scope_label = _first_non_empty(
            " / ".join(part for part in [process_name, site_name] if str(part or "").strip()),
            process_name,
            process_id,
        )
        city = str((site_row or {}).get("city") or "")
        employee_count = (site_row or {}).get("employee_count")
        process_activities = [
            str(row.get("activity_name") or "")
            for row in base_ctx.activities
            if str(row.get("process_id") or "") == process_id
        ]
        activities_text = _first_non_empty(
            ", ".join(_dedupe_strings(process_activities, limit=12)),
            process_name,
            (site_row or {}).get("main_activities"),
            base_ctx.main_activities,
        )
        equipment_rows = _rows_for_site(base_ctx.equipment_rows, site_id)
        product_rows = _product_rows_for_site(base_ctx.product_rows, site_name)
        environmental_rows = _rows_for_site(base_ctx.environmental_rows, site_id)
        sst_rows = _rows_for_site(base_ctx.sst_risk_rows, site_id)
    elif scope_level == "ACTIVITY":
        if not activity_id:
            raise ValueError("Scope ACTIVITY invalide: activity_id manquant")
        scope_key = f"ACTIVITY:{activity_id}"
        scope_label = _first_non_empty(
            " / ".join(part for part in [activity_name, process_name, site_name] if str(part or "").strip()),
            activity_name,
            activity_id,
        )
        city = str((site_row or {}).get("city") or "")
        employee_count = (site_row or {}).get("employee_count")
        activities_text = _first_non_empty(
            activity_name,
            process_name,
            (site_row or {}).get("main_activities"),
            base_ctx.main_activities,
        )
        equipment_rows = _rows_for_site(base_ctx.equipment_rows, site_id)
        product_rows = _product_rows_for_site(base_ctx.product_rows, site_name)
        environmental_rows = _rows_for_site(base_ctx.environmental_rows, site_id)
        sst_rows = _rows_for_site(base_ctx.sst_risk_rows, site_id)
    else:
        raise ValueError(f"Scope inconnu: {scope_level}")

    return CompanyContext(
        profile_id=base_ctx.profile_id,
        tenant_id=base_ctx.tenant_id,
        company_name=base_ctx.company_name,
        sector=base_ctx.sector,
        country=base_ctx.country,
        certifications=list(base_ctx.certifications or []),
        scope_level=scope_level,
        scope_key=scope_key,
        scope_label=scope_label,
        site_id=site_id,
        process_id=process_id,
        activity_id=activity_id,
        site_name=site_name,
        city=city,
        employee_count=int(employee_count) if employee_count is not None else None,
        main_activities=activities_text or None,
        process_name=process_name or None,
        activity_name=activity_name or None,
        equipment_list=_equipment_labels(equipment_rows),
        product_types=_product_labels(product_rows),
        chemical_aspects=_chemical_labels(product_rows, environmental_rows),
        environmental_aspects=_environmental_labels(environmental_rows),
        sst_risk_domains=_sst_domain_labels(sst_rows),
        sst_risk_types=_sst_type_labels(sst_rows),
        objectives=_dedupe_strings(list(base_ctx.objectives or []), limit=15),
    )

# Prépare Chaque Scope-------------------------------------------------------

def _build_scope_contexts(
    base_ctx: CompanyBaseContext,
    site_ids: list[str] | None = None,
    process_ids: list[str] | None = None,
    activity_ids: list[str] | None = None,
) -> list[CompanyContext]:
    """Cree la liste des perimetres que A2 peut evaluer.

    Sans filtre explicite, le moteur evalue l'organisation entiere puis tous
    les sites, processus et activites connus. Avec des filtres API/CLI, il se
    limite aux identifiants demandes, ce qui permet de relancer A2 localement.
    """
    site_filter = {str(value).strip() for value in (site_ids or []) if str(value).strip()}
    process_filter = {str(value).strip() for value in (process_ids or []) if str(value).strip()}
    activity_filter = {str(value).strip() for value in (activity_ids or []) if str(value).strip()}
    filtered = bool(site_filter or process_filter or activity_filter)

    scopes: list[CompanyContext] = []
    seen: set[str] = set()
    
    def add_scope(candidate: CompanyContext) -> None:
        if candidate.scope_key in seen:
            return
        seen.add(candidate.scope_key)
        scopes.append(candidate)

    # Le scope organisation sert de garde-fou pour les exigences globales
    # (declarations, responsabilites, politiques, autorisations...).
    if not filtered:
        add_scope(_build_scope_context(base_ctx, "ORGANIZATION"))

    sites_by_id = _site_lookup(base_ctx)
    processes_by_id = _process_lookup(base_ctx)

    # Les scopes locaux sont construits dans l'ordre hierarchique. Le set
    # "seen" evite les doublons quand plusieurs lignes pointent le meme scope.
    for site in base_ctx.sites:
        site_id = str(site.get("site_id") or "").strip()
        if site_filter and site_id not in site_filter:
            continue
        if site_id:
            add_scope(_build_scope_context(base_ctx, "SITE", site_row=site))

    for process in base_ctx.processes:
        process_id = str(process.get("process_id") or "").strip()
        site_id = str(process.get("site_id") or "").strip()
        if process_filter and process_id not in process_filter:
            continue
        if site_filter and site_id and site_id not in site_filter:
            continue
        if process_id:
            add_scope(_build_scope_context(base_ctx, "PROCESS", site_row=sites_by_id.get(site_id), process_row=process))

    for activity in base_ctx.activities:
        activity_id = str(activity.get("activity_id") or "").strip()
        site_id = str(activity.get("site_id") or "").strip()
        process_id = str(activity.get("process_id") or "").strip()
        if activity_filter and activity_id not in activity_filter:
            continue
        if process_filter and process_id and process_id not in process_filter:
            continue
        if site_filter and site_id and site_id not in site_filter:
            continue
        if activity_id:
            add_scope(
                _build_scope_context(
                    base_ctx,
                    "ACTIVITY",
                    site_row=sites_by_id.get(site_id),
                    process_row=processes_by_id.get(process_id),
                    activity_row=activity,
                )
            )

    if not scopes and not filtered:
        add_scope(_build_scope_context(base_ctx, "ORGANIZATION"))

    level_rank = {"ORGANIZATION": 0, "SITE": 1, "PROCESS": 2, "ACTIVITY": 3}
    scopes.sort(key=lambda ctx: (level_rank.get(ctx.scope_level, 9), ctx.scope_label.lower(), ctx.scope_key))
    return scopes


def _format_context_block(ctx: CompanyContext) -> str:
    """Formate le contexte entreprise en texte structure pour le prompt."""
    lines = [
        f"PERIMETRE D'EVALUATION: {ctx.scope_level} | {ctx.scope_label}",
        f"ENTREPRISE: {ctx.company_name}",
        f"SECTEUR: {ctx.sector}",
        f"PAYS: {ctx.country}",
        f"CERTIFICATIONS: {', '.join(ctx.certifications) or 'Aucune'}",
        f"",
        f"SITE: {ctx.site_name or 'Organisation complete'} - {ctx.city or 'Non precise'}",
        f"PROCESSUS: {ctx.process_name or 'Non precise'}",
        f"ACTIVITE: {ctx.activity_name or 'Non precisee'}",
        f"EFFECTIF: {ctx.employee_count or 'inconnu'} employes",
        f"ACTIVITES PRINCIPALES: {ctx.main_activities or 'Non precise'}",
        f"",
        f"EQUIPEMENTS ({len(ctx.equipment_list)}):",
    ]
    for eq in ctx.equipment_list[:25]:
        lines.append(f"  - {eq}")
    if len(ctx.equipment_list) > 25:
        lines.append(f"  ... et {len(ctx.equipment_list) - 25} autres")

    lines += [
        f"",
        f"TYPES DE PRODUITS: {', '.join(ctx.product_types[:10]) or 'Non precise'}",
        f"",
        f"ASPECTS ENVIRONNEMENTAUX ({len(ctx.environmental_aspects)}):",
    ]
    for asp in ctx.environmental_aspects[:20]:
        lines.append(f"  - {asp}")

    lines += [
        f"",
        f"DOMAINES DE RISQUES SST: {', '.join(ctx.sst_risk_domains)}",
        f"TYPES DE RISQUES SST: {', '.join(ctx.sst_risk_types[:12])}",
        f"",
        f"OBJECTIFS QHSE:",
    ]
    for obj in ctx.objectives[:8]:
        lines.append(f"  - {obj}")

    return "\n".join(lines)


def _normalize_text(value: Any) -> str:
    raw = str(value or "")
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip().lower()


def _contains_any(text: str, candidates: tuple[str, ...]) -> bool:
    return any(term in text for term in candidates)


def _tokenize_words(value: Any, *, min_len: int = 5) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", _normalize_text(value))
        if len(token) >= min_len and token not in TOKEN_STOPWORDS
    }
    return tokens


def _scope_company_blob(ctx: CompanyContext) -> str:
    parts: list[str] = [
        ctx.scope_label,
        ctx.site_name,
        ctx.process_name,
        ctx.activity_name,
        ctx.main_activities,
        " ".join(list(ctx.equipment_list or [])[:20]),
        " ".join(list(ctx.product_types or [])[:12]),
        " ".join(list(ctx.chemical_aspects or [])[:10]),
        " ".join(list(ctx.environmental_aspects or [])[:10]),
        " ".join(list(ctx.sst_risk_domains or [])[:8]),
        " ".join(list(ctx.sst_risk_types or [])[:8]),
    ]
    return _normalize_text(" | ".join(str(part or "") for part in parts if str(part or "").strip()))


def _scope_label_blob(ctx: CompanyContext) -> str:
    parts = [ctx.scope_label, ctx.site_name, ctx.process_name, ctx.activity_name]
    return _normalize_text(" | ".join(str(part or "") for part in parts if str(part or "").strip()))


def _increment_counter(counter: dict[str, int], key: str, amount: int = 1) -> None:
    safe_key = str(key or "UNKNOWN").strip().upper() or "UNKNOWN"
    counter[safe_key] = counter.get(safe_key, 0) + int(amount)


def _safe_average(total: float, count: int) -> float:
    return round((float(total) / float(count)), 4) if count else 0.0


def _safe_percent(part: int, whole: int) -> float:
    return round((float(part) * 100.0 / float(whole)), 2) if whole else 0.0


def _finalize_engine_stats(engine_stats: dict[str, Any], total_evaluated: int) -> dict[str, Any]:
    stats = dict(engine_stats or {})
    llm_calls = int(stats.get("llm_calls") or 0)
    rule_applied = int(stats.get("rule_applied") or 0)
    pair_seconds_total = float(stats.get("pair_seconds_total") or 0.0)
    llm_seconds_total = float(stats.get("llm_seconds_total") or 0.0)
    rule_seconds_total = float(stats.get("rule_seconds_total") or 0.0)
    commit_seconds_total = float(stats.get("commit_seconds_total") or 0.0)
    commit_count = int(stats.get("commit_count") or 0)
    pairs_initial = int(stats.get("pairs_initial") or 0)
    pairs_retained = int(stats.get("pairs_retained") or 0)
    pairs_pruned = int(stats.get("pairs_pruned") or 0)
    runtime_seconds_total = float(stats.get("runtime_seconds_total") or 0.0)

    stats["avg_seconds_per_pair"] = _safe_average(pair_seconds_total, total_evaluated)
    stats["avg_seconds_per_llm_call"] = _safe_average(llm_seconds_total, llm_calls)
    stats["avg_seconds_per_rule"] = _safe_average(rule_seconds_total, rule_applied)
    stats["avg_commit_seconds"] = _safe_average(commit_seconds_total, commit_count)
    stats["prune_rate_pct"] = _safe_percent(pairs_pruned, pairs_initial)
    stats["retained_rate_pct"] = _safe_percent(pairs_retained, pairs_initial)
    stats["llm_pair_rate_pct"] = _safe_percent(llm_calls, total_evaluated)
    stats["rule_pair_rate_pct"] = _safe_percent(rule_applied, total_evaluated)
    stats["pairs_per_minute"] = round((float(total_evaluated) * 60.0 / runtime_seconds_total), 2) if runtime_seconds_total > 0 else 0.0
    return stats


def _scope_has_exact_phrase_match(req_text_norm: str, ctx: CompanyContext) -> bool:
    for phrase in (ctx.activity_name, ctx.process_name, ctx.site_name, ctx.scope_label):
        phrase_norm = _normalize_text(phrase)
        if phrase_norm and len(phrase_norm) >= 5 and phrase_norm in req_text_norm:
            return True
    return False


def _has_local_scope_cues(req_text_norm: str) -> bool:
    return _contains_any(req_text_norm, LOCAL_SCOPE_CUES)


def _is_global_only_requirement(req: dict, req_text_norm: str) -> bool:
    req_type = str(req.get("req_type") or "").strip().upper()
    if req_type in GLOBAL_ONLY_REQ_TYPES and not _has_local_scope_cues(req_text_norm):
        return True
    if req_type == "DECLARATION" and _contains_any(req_text_norm, GLOBAL_ONLY_CUES) and not _has_local_scope_cues(req_text_norm):
        return True
    return False


def _infer_scope_levels_from_cues(req_text_norm: str) -> set[str]:
    levels: set[str] = set()
    if _contains_any(req_text_norm, SITE_SCOPE_CUES):
        levels.add("SITE")
    if _contains_any(req_text_norm, PROCESS_SCOPE_CUES):
        levels.add("PROCESS")
    if _contains_any(req_text_norm, ACTIVITY_SCOPE_CUES):
        levels.add("ACTIVITY")
    return levels


def _has_operational_theme(req_text_norm: str) -> bool:
    if _contains_any(req_text_norm, CHEMICAL_CUES):
        return True
    if _contains_any(req_text_norm, ENVIRONMENT_LOCAL_CUES):
        return True
    if _contains_any(req_text_norm, SST_LOCAL_CUES):
        return True
    for conf in ACTIVITY_THEME_RULES.values():
        if _contains_any(req_text_norm, conf["requirement"]):
            return True
    return False


def _route_scope_levels_for_requirement(req: dict, req_text_norm: str) -> set[str] | None:
    req_type = str(req.get("req_type") or "").strip().upper()
    qse_domain = str(req.get("qse_domain") or "").strip().upper()
    qse_sub_domain = str(req.get("qse_sub_domain") or "").strip().upper()
    has_local = _has_local_scope_cues(req_text_norm)
    inferred_local_levels = _infer_scope_levels_from_cues(req_text_norm)
    operational_theme = _has_operational_theme(req_text_norm)

    if _is_global_only_requirement(req, req_text_norm):
        return {"ORGANIZATION"}

    routed = ROUTE_BY_TYPE_SCOPE_LEVELS.get(req_type)
    if not routed:
        routed = ROUTE_BY_SUBDOMAIN_SCOPE_LEVELS.get(qse_sub_domain)
    if routed:
        allowed = set(routed)
        if inferred_local_levels:
            allowed.update(inferred_local_levels)
        if operational_theme and req_type in {"REGISTRE", "CONTROLE"}:
            allowed.add("ACTIVITY")
        return allowed

    if qse_domain in ORGANIZATION_HEAVY_DOMAINS and req_type in {"OBLIGATION", "INTERDICTION", "CONDITION", "EXCEPTION"} and not has_local:
        return {"ORGANIZATION", "SITE"}

    if qse_domain in FIELD_OPERATIONAL_DOMAINS and (operational_theme or inferred_local_levels):
        allowed = set(inferred_local_levels or {"SITE", "PROCESS", "ACTIVITY"})
        allowed.add("SITE")
        return allowed

    return None


def _scope_candidate_score(req: dict, req_text_norm: str, ctx: CompanyContext) -> int:
    """Score heuristique pour garder les scopes locaux les plus plausibles."""
    if str(ctx.scope_level or "").upper() == "ORGANIZATION":
        return 0

    label_blob = _scope_label_blob(ctx)
    company_blob = _scope_company_blob(ctx)
    if not company_blob:
        return 0

    score = 0
    label_tokens = _tokenize_words(label_blob, min_len=5)
    req_tokens = _tokenize_words(req_text_norm, min_len=5)
    overlap = label_tokens.intersection(req_tokens)
    score += min(4, len(overlap))

    for phrase, points in (
        (ctx.activity_name, 6),
        (ctx.process_name, 5),
        (ctx.site_name, 4),
        (ctx.scope_label, 4),
    ):
        phrase_norm = _normalize_text(phrase)
        if phrase_norm and len(phrase_norm) >= 5 and phrase_norm in req_text_norm:
            score += points

    if _contains_any(req_text_norm, CHEMICAL_CUES) and (ctx.chemical_aspects or any("chim" in _normalize_text(p) for p in ctx.product_types)):
        score += 3

    if _contains_any(req_text_norm, ENVIRONMENT_LOCAL_CUES) and ctx.environmental_aspects:
        score += 2

    if _contains_any(req_text_norm, SST_LOCAL_CUES) and (ctx.sst_risk_domains or ctx.sst_risk_types):
        score += 2

    for conf in ACTIVITY_THEME_RULES.values():
        if _contains_any(req_text_norm, conf["requirement"]) and _contains_any(company_blob, conf["company"]):
            score += 3
            break

    if score == 0:
        company_tokens = _tokenize_words(company_blob, min_len=6)
        company_overlap = company_tokens.intersection(req_tokens)
        if company_overlap:
            score += min(2, len(company_overlap))

    return score


def _select_candidate_scopes_for_requirement(
    req: dict,
    scopes: list[CompanyContext],
    *,
    scope_filters_applied: bool = False,
) -> list[CompanyContext]:
    """Reduit le produit cartesien exigences x scopes.

    Sans cette etape, chaque exigence serait evaluee contre tous les sites,
    processus et activites. On commence donc par router par type/domaine, puis
    on score les scopes locaux avec les mots du texte reglementaire et les
    donnees entreprise (equipements, produits, risques, aspects).
    """
    if scope_filters_applied or len(scopes) <= 1:
        return list(scopes)

    req_text_norm = _normalize_text(
        " ".join(
            str(req.get(key) or "")
            for key in ("requirement_text", "citation_snippet", "citation_ref", "qse_domain", "qse_sub_domain")
        )
    )
    if not req_text_norm:
        return list(scopes)

    # Premier filtre: certaines familles d'exigences sont naturellement
    # globales, d'autres doivent rester proches du terrain.
    routed_scope_levels = _route_scope_levels_for_requirement(req, req_text_norm)
    candidate_pool = list(scopes)
    if routed_scope_levels:
        routed_scopes = [
            scope for scope in scopes
            if str(scope.scope_level or "").upper() in routed_scope_levels
        ]
        if routed_scopes and len(routed_scopes) < len(scopes):
            candidate_pool = routed_scopes

    org_scopes = [scope for scope in candidate_pool if str(scope.scope_level or "").upper() == "ORGANIZATION"]
    local_scopes = [scope for scope in candidate_pool if str(scope.scope_level or "").upper() != "ORGANIZATION"]
    if not local_scopes:
        return list(candidate_pool)

    # Si le texte ne parle pas de site, atelier, equipement, dechet, bruit,
    # etc., on ne force pas un pruning local agressif.
    if not _has_local_scope_cues(req_text_norm):
        return list(candidate_pool)

    # Deuxieme filtre: on garde les scopes dont le libelle ou les donnees
    # metier ressemblent vraiment a l'exigence.
    scored_local = [(scope, _scope_candidate_score(req, req_text_norm, scope)) for scope in local_scopes]
    exact_local = [
        (scope, score) for scope, score in scored_local
        if _scope_has_exact_phrase_match(req_text_norm, scope) and score >= 4
    ]
    if exact_local:
        max_exact_score = max(score for _, score in exact_local)
        retained_local = [scope for scope, score in exact_local if score >= max(4, max_exact_score - 1)]
        if 0 < len(retained_local) < len(local_scopes):
            retained_keys = {scope.scope_key for scope in retained_local}
            retained_org_keys = {scope.scope_key for scope in org_scopes} if str(req.get("req_type") or "").strip().upper() in {"CONDITION", "EXCEPTION"} else set()
            return [
                scope for scope in candidate_pool
                if scope.scope_key in retained_keys or scope.scope_key in retained_org_keys
            ]

    strong_local = [(scope, score) for scope, score in scored_local if score >= 3]
    if not strong_local:
        return list(candidate_pool)

    max_score = max(score for _, score in strong_local)
    retained_local = [scope for scope, score in strong_local if score >= max(3, max_score - 1)]
    if not retained_local:
        return list(candidate_pool)
    if len(retained_local) >= len(local_scopes):
        return list(candidate_pool)

    weak_local_count = sum(1 for _, score in scored_local if score <= 1)
    if weak_local_count > 0:
        meaningful_threshold = max(2, max_score - 2)
        meaningful_local = [scope for scope, score in scored_local if score >= meaningful_threshold]
        if 0 < len(meaningful_local) < len(local_scopes):
            retained_keys = {scope.scope_key for scope in meaningful_local}
            retained_org_keys = {scope.scope_key for scope in org_scopes} if str(req.get("req_type") or "").strip().upper() in {"CONDITION", "EXCEPTION"} else set()
            return [
                scope for scope in candidate_pool
                if scope.scope_key in retained_keys or scope.scope_key in retained_org_keys
            ]

    if len(retained_local) > max(4, len(local_scopes) // 2):
        return list(candidate_pool)

    retained_keys = {scope.scope_key for scope in retained_local}
    retained_org_keys = {scope.scope_key for scope in org_scopes} if str(req.get("req_type") or "").strip().upper() in {"CONDITION", "EXCEPTION"} else set()
    return [
        scope for scope in candidate_pool
        if scope.scope_key in retained_keys or scope.scope_key in retained_org_keys
    ]


def _quality_gate_min_confidence() -> float:
    raw = os.getenv("A2_QUALITY_GATE_MIN_CONF", str(DEFAULT_QUALITY_GATE_MIN_CONF)).strip()
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_QUALITY_GATE_MIN_CONF


def _min_confidence_for_req_type(req_type: str | None) -> float:
    req_type_norm = str(req_type or "AUTRE").strip().upper() or "AUTRE"
    default_value = float(MIN_CONFIDENCE_BY_TYPE.get(req_type_norm, DEFAULT_QUALITY_GATE_MIN_CONF))
    global_floor = _quality_gate_min_confidence()
    env_key = f"A2_QUALITY_GATE_MIN_CONF_{req_type_norm}"
    raw = os.getenv(env_key, "").strip()
    if raw:
        try:
            return max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            return max(default_value, global_floor)
    return max(default_value, global_floor)


def _delay_between_seconds() -> float:
    raw = os.getenv("A2_DELAY_BETWEEN_SECONDS", str(DEFAULT_A2_DELAY_SECONDS)).strip()
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_A2_DELAY_SECONDS


def _commit_batch_size() -> int:
    raw = os.getenv("A2_COMMIT_BATCH_SIZE", str(DEFAULT_A2_COMMIT_BATCH_SIZE)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_A2_COMMIT_BATCH_SIZE
    return value if value >= 1 else DEFAULT_A2_COMMIT_BATCH_SIZE


def _a2_llm_max_output_tokens() -> int:
    raw = os.getenv("A2_LLM_MAX_OUTPUT_TOKENS", str(DEFAULT_A2_LLM_MAX_OUTPUT_TOKENS)).strip()
    try:
        return max(256, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_A2_LLM_MAX_OUTPUT_TOKENS


def _company_data_digest(ctx: CompanyContext) -> str:
    sector = ctx.sector or "inconnu"
    effectif = str(ctx.employee_count) if ctx.employee_count is not None else "inconnu"
    activities = ctx.main_activities or "non precisees"
    return (
        f"Scope={ctx.scope_level}:{ctx.scope_label}; "
        f"Secteur={sector}; Effectif={effectif}; Activites={activities}"
    )


def _coerce_year(value: str) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    if year < 100:
        return 2000 + year if year < 70 else 1900 + year
    return year


def _safe_date(year: int | None, month: int | None, day: int | None) -> date | None:
    if not year or not month or not day:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _extract_temporal_metadata(req: dict[str, Any]) -> dict[str, Any]:
    """Detecte les dates d'effet futures et delais transitoires dans le texte."""
    raw_text = " ".join(
        [
            str(req.get("requirement_text") or ""),
            str(req.get("citation_snippet") or ""),
            str(req.get("citation_ref") or ""),
            str(req.get("doc_title") or ""),
        ]
    ).strip()
    text_norm = _normalize_text(raw_text)
    effective: date | None = None
    temporal_basis = ""

    for pattern in TEMPORAL_EFFECTIVE_DATE_PATTERNS:
        match = pattern.search(text_norm)
        if not match:
            continue
        if len(match.groups()) == 3 and match.group(2).isalpha():
            day = int(match.group(1))
            month = FRENCH_MONTHS.get(str(match.group(2) or "").strip().lower())
            year = _coerce_year(match.group(3))
        else:
            day = int(match.group(1))
            month = int(match.group(2))
            year = _coerce_year(match.group(3))
        effective = _safe_date(year, month, day)
        temporal_basis = match.group(0)
        if effective:
            break

    transition_period_days: int | None = None
    delay_match = TEMPORAL_DELAY_PATTERN.search(text_norm)
    if delay_match:
        try:
            amount = int(delay_match.group(1))
        except (TypeError, ValueError):
            amount = 0
        unit = str(delay_match.group(2) or "").strip().lower()
        multiplier = 1
        if unit.startswith("mois"):
            multiplier = 30
        elif unit.startswith("an"):
            multiplier = 365
        transition_period_days = amount * multiplier if amount > 0 else None
        if not temporal_basis:
            temporal_basis = delay_match.group(0)

    return {
        "effective_date": effective.isoformat() if effective else None,
        "transition_period_days": transition_period_days,
        "temporal_basis": temporal_basis or None,
    }


def _with_temporal_metadata(req: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(req or {})
    enriched.update(_extract_temporal_metadata(enriched))
    return enriched


def _apply_future_applicability_gate(
    decision: ApplicabilityDecisionLLM,
    req: dict[str, Any],
) -> tuple[ApplicabilityDecisionLLM, bool]:
    """Convertit une decision applicable en APPLICABLE_FUTUR si la date d'effet est future."""
    if str(decision.status or "").strip().upper() not in FUTURE_ELIGIBLE_STATUSES:
        return decision, False

    effective_date_raw = str(req.get("effective_date") or "").strip()
    if not effective_date_raw:
        return decision, False

    try:
        effective_dt = date.fromisoformat(effective_date_raw)
    except ValueError:
        return decision, False

    if effective_dt <= date.today():
        return decision, False

    note = f"[TEMPORAL_GATE] Exigence applicable a partir du {effective_dt.strftime('%d/%m/%Y')}."
    base_justification = str(decision.justification or "").strip()
    next_justification = f"{note}\n{base_justification}" if base_justification else note
    return (
        ApplicabilityDecisionLLM(
            status="APPLICABLE_FUTUR",
            confidence=float(decision.confidence or 0.0),
            justification=next_justification,
            article_ref=decision.article_ref,
            condition_evaluated=decision.condition_evaluated,
            company_data_used=decision.company_data_used,
            scope_site=decision.scope_site,
            scope_process=decision.scope_process,
            scope_activity=decision.scope_activity,
            is_voluntary=decision.is_voluntary,
            voluntary_reason=decision.voluntary_reason,
            missing_data=decision.missing_data,
        ),
        True,
    )


def _build_rule_decision(
    req: dict,
    ctx: CompanyContext,
    rule_name: str,
    status: str,
    confidence: float,
    condition_evaluated: str,
    company_data_used: str,
    justification: str,
) -> ApplicabilityDecisionLLM:
    article_ref = str(req.get("citation_ref") or req.get("doc_title") or "reference_non_precisee")
    return ApplicabilityDecisionLLM(
        status=status,
        confidence=confidence,
        justification=f"[RULE:{rule_name}] {justification}",
        article_ref=article_ref,
        condition_evaluated=condition_evaluated,
        company_data_used=company_data_used,
        scope_site=ctx.site_name or None,
        scope_process=ctx.process_name or None,
        scope_activity=ctx.activity_name or None,
        is_voluntary=False,
        voluntary_reason=None,
        missing_data=None,
    )


def _extract_employee_threshold(text_norm: str) -> tuple[str, int] | None:
    min_patterns = (
        r"(?:au moins|min(?:imum)?|plus de|sup(?:erieur)?\s*(?:a|a\W)|>=?)\s*(\d{1,4})\s*(?:salaries?|employes?|travailleurs?)",
        r"(\d{1,4})\s*(?:salaries?|employes?|travailleurs?)\s*(?:et plus|ou plus)",
    )
    max_patterns = (
        r"(?:moins de|inferieur(?:e)?\s*(?:a|a\W)|<=?|au plus|max(?:imum)?)\s*(\d{1,4})\s*(?:salaries?|employes?|travailleurs?)",
    )
    for pattern in min_patterns:
        match = re.search(pattern, text_norm)
        if match:
            return ("MIN", int(match.group(1)))
    for pattern in max_patterns:
        match = re.search(pattern, text_norm)
        if match:
            return ("MAX", int(match.group(1)))
    return None


def _detect_sectors(text_norm: str) -> set[str]:
    hits: set[str] = set()
    for sector, keys in SECTOR_KEYWORDS.items():
        if _contains_any(text_norm, keys):
            hits.add(sector)
    return hits


def _apply_deterministic_rules(req: dict, ctx: CompanyContext) -> ApplicabilityDecisionLLM | None:
    """Applique les cas simples et auditables avant l'appel LLM.

    Les regles ne couvrent que des signaux forts: seuil d'effectif, secteur,
    exposition chimique, themes equipement/activite et acteur institutionnel.
    Les cas ambigus retournent None et basculent vers le LLM.
    """
    req_text_norm = _normalize_text(req.get("requirement_text") or "")
    if not req_text_norm:
        return None

    # Rule 1: effectif explicite
    threshold = _extract_employee_threshold(req_text_norm)
    if threshold and ctx.employee_count is not None:
        op, value = threshold
        company_n = int(ctx.employee_count)
        if op == "MIN":
            if company_n >= value:
                return _build_rule_decision(
                    req=req,
                    ctx=ctx,
                    rule_name="EFFECTIF_MIN_MATCH",
                    status="APPLICABLE",
                    confidence=0.96,
                    condition_evaluated=f"Effectif entreprise >= {value}",
                    company_data_used=f"Effectif entreprise={company_n}",
                    justification=f"Le texte cible les entreprises avec un effectif minimal de {value}; l'entreprise est a {company_n}.",
                )
            return _build_rule_decision(
                req=req,
                ctx=ctx,
                rule_name="EFFECTIF_MIN_MISMATCH",
                status="NON_APPLICABLE",
                confidence=0.96,
                condition_evaluated=f"Effectif entreprise >= {value}",
                company_data_used=f"Effectif entreprise={company_n}",
                justification=f"Le seuil minimal est {value} et l'entreprise est en dessous ({company_n}).",
            )
        if op == "MAX":
            if company_n <= value:
                return _build_rule_decision(
                    req=req,
                    ctx=ctx,
                    rule_name="EFFECTIF_MAX_MATCH",
                    status="APPLICABLE",
                    confidence=0.95,
                    condition_evaluated=f"Effectif entreprise <= {value}",
                    company_data_used=f"Effectif entreprise={company_n}",
                    justification=f"Le texte cible un effectif maximal de {value}; l'entreprise est a {company_n}.",
                )
            return _build_rule_decision(
                req=req,
                ctx=ctx,
                rule_name="EFFECTIF_MAX_MISMATCH",
                status="NON_APPLICABLE",
                confidence=0.95,
                condition_evaluated=f"Effectif entreprise <= {value}",
                company_data_used=f"Effectif entreprise={company_n}",
                justification=f"Le texte vise <= {value} salaries et l'entreprise est a {company_n}.",
            )

    # Rule 2: secteur explicite (mono-secteur + indice de champ d'application)
    req_sectors = _detect_sectors(req_text_norm)
    ctx_sectors = _detect_sectors(_normalize_text(ctx.sector))
    has_scope_cue = _contains_any(req_text_norm, SECTOR_SCOPE_CUES)
    if has_scope_cue and len(req_sectors) == 1 and ctx_sectors:
        expected = next(iter(req_sectors))
        if expected in ctx_sectors:
            return _build_rule_decision(
                req=req,
                ctx=ctx,
                rule_name="SECTOR_MATCH",
                status="APPLICABLE",
                confidence=0.91,
                condition_evaluated=f"Champ sectoriel={expected}",
                company_data_used=f"Secteur entreprise={ctx.sector or 'inconnu'}",
                justification=f"L'exigence vise explicitement le secteur {expected}; le profil entreprise correspond.",
            )
        return _build_rule_decision(
            req=req,
            ctx=ctx,
            rule_name="SECTOR_MISMATCH",
            status="NON_APPLICABLE",
            confidence=0.90,
            condition_evaluated=f"Champ sectoriel={expected}",
            company_data_used=f"Secteur entreprise={ctx.sector or 'inconnu'}",
            justification=f"L'exigence vise le secteur {expected}, different du profil entreprise ({ctx.sector or 'inconnu'}).",
        )

    # Rule 3: exigences chimiques avec preuves d'exposition (positif uniquement)
    has_chemical_cue = _contains_any(req_text_norm, CHEMICAL_CUES)
    if has_chemical_cue and (ctx.chemical_aspects or any("chim" in _normalize_text(p) for p in ctx.product_types)):
        sample = ", ".join(ctx.chemical_aspects[:3]) or ", ".join(ctx.product_types[:3]) or "donnees chimiques disponibles"
        return _build_rule_decision(
            req=req,
            ctx=ctx,
            rule_name="CHEMICAL_EXPOSURE_MATCH",
            status="APPLICABLE",
            confidence=0.88,
            condition_evaluated="Presence de substances/produits chimiques dans les operations",
            company_data_used=sample,
            justification="L'exigence concerne les risques chimiques et l'entreprise declare des aspects/produits chimiques associes.",
        )

    # Rule 4: themes activite/equipement (positif uniquement)
    company_blob = " ".join([
        _normalize_text(ctx.main_activities),
        " ".join(_normalize_text(e) for e in ctx.equipment_list[:80]),
    ])
    for rule_name, conf in ACTIVITY_THEME_RULES.items():
        if _contains_any(req_text_norm, conf["requirement"]) and _contains_any(company_blob, conf["company"]):
            return _build_rule_decision(
                req=req,
                ctx=ctx,
                rule_name=rule_name,
                status="APPLICABLE",
                confidence=0.86,
                condition_evaluated=f"Correspondance activite/equipement pour {rule_name}",
                company_data_used=_company_data_digest(ctx),
                justification=f"Le theme {rule_name} est present dans l'exigence et retrouve dans les activites/equipements declaratifs.",
            )

    # Rule 5: acteur institutionnel explicite, different d'une entreprise privee.
    head_clause = req_text_norm.split(".")[0][:220]
    if head_clause.startswith(INSTITUTIONAL_ACTOR_PREFIXES) and not _contains_any(head_clause, PRIVATE_COMPANY_CUES):
        actor = next((prefix for prefix in INSTITUTIONAL_ACTOR_PREFIXES if head_clause.startswith(prefix)), "acteur institutionnel")
        return _build_rule_decision(
            req=req,
            ctx=ctx,
            rule_name="INSTITUTIONAL_ACTOR_MISMATCH",
            status="NON_APPLICABLE",
            confidence=0.95,
            condition_evaluated=f"Obligation portee par {actor}",
            company_data_used=f"Entreprise privee: {ctx.company_name or 'organisation'} | secteur={ctx.sector or 'inconnu'}",
            justification="Le sujet juridique vise une autorite publique, une juridiction ou un tiers institutionnel, "
            "et non l'entreprise evaluee.",
        )

    return None


def _purge_legacy_scope_decisions(
    conn: psycopg.Connection,
    profile_id: str,
    requirement_ids: list[str],
) -> int:
    req_ids = [str(value).strip() for value in requirement_ids if str(value).strip()]
    if not req_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM applicability_decisions
            WHERE profile_id = %s::uuid
                AND requirement_id = ANY(%s::uuid[])
                AND COALESCE(scope_key, '') LIKE '%%:LEGACY:%%'
            """,
            (profile_id, req_ids),
        )
        deleted = int(cur.rowcount or 0)
    conn.commit()
    return deleted


def _apply_quality_gate(
    decision: ApplicabilityDecisionLLM,
    req: dict,
    ctx: CompanyContext,
    min_confidence: float,
) -> tuple[ApplicabilityDecisionLLM, bool]:
    """Controle que la decision est suffisamment justifiee pour etre defendable."""
    normalized_article_ref = str(decision.article_ref or req.get("citation_ref") or req.get("doc_title") or "").strip()
    missing_fields: list[str] = []
    if not str(decision.justification or "").strip():
        missing_fields.append("justification")
    if not normalized_article_ref:
        missing_fields.append("article_ref")
    if not str(decision.condition_evaluated or "").strip():
        missing_fields.append("condition_evaluated")
    if not str(decision.company_data_used or "").strip():
        missing_fields.append("company_data_used")

    low_conf = decision.status != "INCERTAIN" and float(decision.confidence or 0) < float(min_confidence)
    if not missing_fields and not low_conf:
        if normalized_article_ref and normalized_article_ref != decision.article_ref:
            decision.article_ref = normalized_article_ref
        return decision, False

    reasons = []
    if missing_fields:
        reasons.append(f"champs manquants: {', '.join(missing_fields)}")
    if low_conf:
        reasons.append(f"confiance {decision.confidence:.2f} < seuil {min_confidence:.2f}")

    merged_justification = str(decision.justification or "").strip()
    qg_note = f"[QUALITY_GATE] Decision declassee en INCERTAIN ({'; '.join(reasons)})."
    full_justification = f"{qg_note}\n{merged_justification}" if merged_justification else qg_note
    missing_data = " ; ".join(reasons)

    gated = ApplicabilityDecisionLLM(
        status="INCERTAIN",
        confidence=min(float(decision.confidence or 0.55), 0.55),
        justification=full_justification,
        article_ref=normalized_article_ref or "reference_non_precisee",
        condition_evaluated=str(decision.condition_evaluated or "Condition legale non suffisamment explicitee."),
        company_data_used=str(decision.company_data_used or _company_data_digest(ctx)),
        scope_site=decision.scope_site,
        scope_process=decision.scope_process,
        scope_activity=decision.scope_activity,
        is_voluntary=False,
        voluntary_reason=decision.voluntary_reason,
        missing_data=missing_data,
    )
    return gated, True


# --- Fonctions DB -------------------------------------------------------------

def _load_requirements(
    conn: psycopg.Connection,
    tenant_id: str,
    doc_id: str | None = None,
    status_filter: str | None = None,
    req_id: str | None = None,
    limit: int | None = None,
    profile_id: str | None = None,
    req_statuses: tuple[str, ...] = ("PROMOTED",),
    min_quality: float = 0.70,
) -> list[dict]:
    """Charge les exigences A1 promues depuis la DB selon les filtres."""
    status_placeholders = ",".join(["%s"] * len(req_statuses))
    doc_filter_sql = ""
    doc_filter_params: list[Any] = []
    if doc_id:
        doc_filter_sql = " AND r.doc_id = %s::uuid"
        doc_filter_params.append(str(doc_id))

    with conn.cursor() as cur:
        if req_id:
            params = [req_id, *req_statuses, tenant_id, *doc_filter_params]
            cur.execute(
                f"""
                SELECT  r.requirement_id, r.requirement_text, r.req_type,
                        r.qse_domain, r.qse_sub_domain, r.citation_ref,
                        r.citation_snippet, r.confidence,
                        d.title, d.source, d.jurisdiction
                FROM requirements r
                JOIN documents d ON d.doc_id = r.doc_id
                WHERE r.requirement_id = %s
                    AND r.status IN ({status_placeholders})
                    AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                    {doc_filter_sql}
                """,
                tuple(params),
            )
        elif status_filter and profile_id:
            cur.execute(
                f"""
                SELECT r.requirement_id, r.requirement_text, r.req_type,
                        r.qse_domain, r.qse_sub_domain, r.citation_ref,
                        r.citation_snippet, r.confidence,
                        d.title, d.source, d.jurisdiction
                FROM requirements r
                JOIN documents d ON d.doc_id = r.doc_id
                WHERE r.status IN ({status_placeholders})
                    AND COALESCE(r.quality_score, r.confidence, 0) >= %s
                    AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                    {doc_filter_sql}
                    AND EXISTS (
                        SELECT 1
                        FROM applicability_decisions ad
                        WHERE ad.requirement_id = r.requirement_id
                            AND ad.profile_id = %s
                            AND ad.status = %s
                    )
                ORDER BY r.created_at
                LIMIT %s
                """,
                tuple([*req_statuses, min_quality, tenant_id, *doc_filter_params, profile_id, status_filter, limit or 9999]),
            )
        else:
            params = [*req_statuses, min_quality, tenant_id, *doc_filter_params, limit or 9999]
            cur.execute(
                f"""
                SELECT  r.requirement_id, r.requirement_text, r.req_type,
                        r.qse_domain, r.qse_sub_domain, r.citation_ref,
                        r.citation_snippet, r.confidence,
                        d.title, d.source, d.jurisdiction
                FROM requirements r
                JOIN documents d ON d.doc_id = r.doc_id
                WHERE r.status IN ({status_placeholders})
                    AND COALESCE(r.quality_score, r.confidence, 0) >= %s
                    AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                    {doc_filter_sql}
                ORDER BY r.qse_domain NULLS LAST, r.quality_score DESC NULLS LAST
                LIMIT %s
                """,
                tuple(params),
            )

        rows = cur.fetchall()
        cols = [
            "requirement_id", "requirement_text", "req_type",
            "qse_domain", "qse_sub_domain", "citation_ref",
            "citation_snippet", "confidence",
            "doc_title", "doc_source", "jurisdiction",
        ]
        return [_with_temporal_metadata(dict(zip(cols, r))) for r in rows]


def _load_existing_decisions(
    conn: psycopg.Connection,
    profile_id: str,
    requirement_ids: list[str] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    params: list[Any] = [profile_id]
    filter_sql = ""
    if requirement_ids:
        placeholders = ",".join(["%s"] * len(requirement_ids))
        filter_sql = f" AND requirement_id::text IN ({placeholders})"
        params.extend(requirement_ids)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT  decision_id::text,
                    requirement_id::text,
                    COALESCE(scope_key, 'ORGANIZATION'),
                    status,
                    confidence,
                    scope_level,
                    scope_label
            FROM applicability_decisions
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
            "decision_id": str(row[0] or ""),
            "requirement_id": req_id,
            "scope_key": scope_key,
            "status": str(row[3] or ""),
            "confidence": float(row[4]) if row[4] is not None else None,
            "scope_level": str(row[5] or ""),
            "scope_label": str(row[6] or ""),
        }
    return output


def _save_decision(
    conn: psycopg.Connection,
    req_id: str,
    ctx: CompanyContext,
    decision: ApplicabilityDecisionLLM,
    llm_model: str | None,
    *,
    commit: bool = True,
) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO applicability_decisions
                (profile_id, requirement_id,
                    scope_level, scope_key, scope_label,
                    site_id, process_id, activity_id,
                    status, justification,
                    article_ref, condition_evaluated, company_data_used,
                    scope_site, scope_process, scope_activity,
                    confidence, is_voluntary, voluntary_reason, llm_model)
            VALUES (%s,%s,%s,%s,%s,%s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (profile_id, requirement_id, scope_key) DO UPDATE SET
                scope_level        = EXCLUDED.scope_level,
                scope_label        = EXCLUDED.scope_label,
                site_id            = EXCLUDED.site_id,
                process_id         = EXCLUDED.process_id,
                activity_id        = EXCLUDED.activity_id,
                status             = EXCLUDED.status,
                justification      = EXCLUDED.justification,
                article_ref        = EXCLUDED.article_ref,
                condition_evaluated= EXCLUDED.condition_evaluated,
                company_data_used  = EXCLUDED.company_data_used,
                scope_site         = EXCLUDED.scope_site,
                scope_process      = EXCLUDED.scope_process,
                scope_activity     = EXCLUDED.scope_activity,
                confidence         = EXCLUDED.confidence,
                is_voluntary       = EXCLUDED.is_voluntary,
                voluntary_reason   = EXCLUDED.voluntary_reason,
                llm_model          = EXCLUDED.llm_model,
                updated_at         = now()
        """, (
            ctx.profile_id, req_id,
            ctx.scope_level,
            ctx.scope_key,
            ctx.scope_label,
            ctx.site_id,
            ctx.process_id,
            ctx.activity_id,
            decision.status,
            decision.justification,
            decision.article_ref,
            decision.condition_evaluated,
            decision.company_data_used,
            decision.scope_site,
            decision.scope_process,
            decision.scope_activity,
            decision.confidence,
            decision.is_voluntary,
            decision.voluntary_reason,
            llm_model,
        ))
        cur.execute(
            """
            UPDATE compliance_checks
            SET needs_recheck = TRUE,
                updated_at = now()
            WHERE profile_id = %s::uuid
                AND requirement_id = %s::uuid
                AND COALESCE(scope_key, 'ORGANIZATION') = %s
            """,
            (ctx.profile_id, req_id, ctx.scope_key),
        )
    if commit:
        conn.commit()


# --- Moteur de decision -------------------------------------------------------

def _build_user_prompt(req: dict, ctx: CompanyContext, context_block: str) -> str:
    temporal_lines: list[str] = []
    if req.get("effective_date"):
        temporal_lines.append(f"Date d'effet identifiee: {req['effective_date']}")
    if req.get("transition_period_days"):
        temporal_lines.append(f"Delai transitoire detecte: {req['transition_period_days']} jour(s)")
    if req.get("temporal_basis"):
        temporal_lines.append(f"Base temporelle: {req['temporal_basis']}")
    temporal_block = "\n".join(temporal_lines) if temporal_lines else "Aucune temporalite explicite detectee."

    return f"""=== EXIGENCE REGLEMENTAIRE A EVALUER ===

Reference document: {req['doc_title']} ({req['doc_source'] or ''})
Juridiction: {req['jurisdiction'] or 'Tunisie'}
Type d'exigence: {req['req_type']}
Domaine QHSE: {req['qse_domain'] or 'Non precise'} / {req['qse_sub_domain'] or ''}
Citation: {req['citation_ref'] or ''}

Texte de l'exigence:
{req['requirement_text']}

Extrait contextuel:
{req['citation_snippet'] or '(non disponible)'}

Temporalite detectee:
{temporal_block}

=== CONTEXTE REEL DE L'ENTREPRISE ===

{context_block}

=== INSTRUCTION ===

Evalue si cette exigence reglementaire est applicable a cette entreprise.
Raisonne etape par etape:
1. Quel est le champ d'application de cette exigence ? (qui, quoi, seuils)
2. Les conditions sont-elles reunies pour cette entreprise?
3. Des donnees manquent-elles pour statuer?
4. Quelle est ta decision finale avec justification?

Reponds en JSON strict selon le format defini.
"""


def evaluate_requirement(
    req: dict,
    ctx: CompanyContext,
    context_block: str,
    llm: Any,
) -> ApplicabilityDecisionLLM:
    """Evalue une exigence reglementaire par rapport au contexte entreprise."""
    user_prompt = _build_user_prompt(req, ctx, context_block)
    raw = llm.call_json(SYSTEM_PROMPT, user_prompt, max_tokens=_a2_llm_max_output_tokens())
    return ApplicabilityDecisionLLM(**raw)


def _select_evaluation_pairs(
    requirements: list[dict],
    scopes: list[CompanyContext],
    existing_map: dict[tuple[str, str], dict[str, Any]],
    mode: str,
    force_recompute: bool,
    rerun_status: str | None = None,
    scope_filters_applied: bool = False,
) -> tuple[list[tuple[dict, CompanyContext, dict[str, Any] | None]], dict[str, Any]]:
    """Prepare les couples exigence x scope a calculer pour ce run.

    En mode delta, les decisions deja presentes sont ignorees. Avec
    rerun_status, on ne reprend que les couples dont l'ancien statut correspond
    au statut demande (ex: relancer seulement les INCERTAIN).
    """
    selected: list[tuple[dict, CompanyContext, dict[str, Any] | None]] = []
    rerun_status_norm = str(rerun_status or "").strip().upper() or None
    is_delta = str(mode or "delta").strip().lower() != "full"
    stats = {
        "pairs_initial": 0,
        "pairs_retained": 0,
        "pairs_pruned": 0,
        "requirements_pruned": 0,
        "pairs_skipped_existing": 0,
        "pairs_skipped_rerun_status": 0,
        "pairs_initial_by_scope_level": {},
        "pairs_retained_by_scope_level": {},
        "pairs_selected_by_scope_level": {},
    }

    for req in requirements:
        req_id = str(req.get("requirement_id") or "")
        if not req_id:
            continue
        # On commence par reduire les scopes possibles pour cette exigence.
        candidate_scopes = _select_candidate_scopes_for_requirement(
            req,
            scopes,
            scope_filters_applied=scope_filters_applied,
        )
        stats["pairs_initial"] += len(scopes)
        for scope in scopes:
            _increment_counter(stats["pairs_initial_by_scope_level"], scope.scope_level)
        stats["pairs_retained"] += len(candidate_scopes)
        for scope in candidate_scopes:
            _increment_counter(stats["pairs_retained_by_scope_level"], scope.scope_level)
        if len(candidate_scopes) < len(scopes):
            stats["pairs_pruned"] += len(scopes) - len(candidate_scopes)
            stats["requirements_pruned"] += 1

        for scope in candidate_scopes:
            existing = existing_map.get((req_id, scope.scope_key))
            # Delta/rerun: ne recalculer que ce qui est utile au run courant.
            if rerun_status_norm:
                if not existing or str(existing.get("status") or "").upper() != rerun_status_norm:
                    stats["pairs_skipped_rerun_status"] += 1
                    continue
            elif is_delta and not force_recompute and existing is not None:
                stats["pairs_skipped_existing"] += 1
                continue
            selected.append((req, scope, existing))
            _increment_counter(stats["pairs_selected_by_scope_level"], scope.scope_level)

    return selected, stats


# --- Pipeline principal -------------------------------------------------------

def run_applicability(
    tenant_id: str,
    doc_id: str | None = None,
    limit: int | None = None,
    req_id: str | None = None,
    rerun_status: str | None = None,
    dry_run: bool = False,
    delay_between: float | None = None,
    min_quality: float = 0.70,
    mode: str = "delta",
    force: bool = False,
    force_recompute: bool = False,
    site_ids: list[str] | None = None,
    process_ids: list[str] | None = None,
    activity_ids: list[str] | None = None,
    stop_requested: Any | None = None,
) -> dict:
    """
    Lance le pipeline d'applicabilite pour un tenant.
    Retourne un resume des decisions.

    Etapes principales:
        1. charger le contexte entreprise;
        2. construire les scopes A2;
        3. charger les exigences A1 promues;
        4. selectionner les couples exigence x scope;
        5. calculer, controler et sauvegarder chaque decision.
    """
    tenant_safe = _safe_console_text(tenant_id, max_len=80)
    print(f"\n=== Agent 2 - Applicabilite reglementaire ({tenant_safe}) ===\n")
    runtime_started_at = time.perf_counter()

    conn = _get_conn(tenant_id)
    llm = get_llm_client()
    quality_gate_min_conf = _quality_gate_min_confidence()
    commit_batch_size = _commit_batch_size()
    run_mode = str(mode or "delta").strip().lower()
    if run_mode not in {"full", "delta"}:
        raise ValueError("mode doit etre 'full' ou 'delta'")
    force_flag = bool(force or force_recompute)
    effective_delay_between = _delay_between_seconds() if delay_between is None else max(0.0, float(delay_between or 0.0))
    site_filter = [str(value).strip() for value in (site_ids or []) if str(value).strip()]
    process_filter = [str(value).strip() for value in (process_ids or []) if str(value).strip()]
    activity_filter = [str(value).strip() for value in (activity_ids or []) if str(value).strip()]

    # Charger le contexte entreprise
    print("-- Chargement du contexte entreprise --")
    load_context_started_at = time.perf_counter()
    base_ctx = _load_company_base_context(conn, tenant_id)
    scopes = _build_scope_contexts(
        base_ctx,
        site_ids=site_filter,
        process_ids=process_filter,
        activity_ids=activity_filter,
    )
    load_context_seconds = time.perf_counter() - load_context_started_at
    context_blocks = {scope.scope_key: _format_context_block(scope) for scope in scopes}
    scope_counts: dict[str, int] = {}
    for scope in scopes:
        scope_counts[scope.scope_level] = scope_counts.get(scope.scope_level, 0) + 1
    print(f"  Entreprise   : {_safe_console_text(base_ctx.company_name, max_len=180)}")
    print(f"  Scopes A2    : {len(scopes)} ({', '.join(f'{k}={v}' for k, v in scope_counts.items())})")
    print(f"  Sites        : {len(base_ctx.sites)}")
    print(f"  Processus    : {len(base_ctx.processes)}")
    print(f"  Activites    : {len(base_ctx.activities)}")

    # Charger les exigences a evaluer
    print("\n-- Chargement des exigences --")
    load_requirements_started_at = time.perf_counter()
    requirements = _load_requirements(
        conn,
        tenant_id=tenant_id,
        doc_id=doc_id,
        status_filter=rerun_status,
        req_id=req_id,
        limit=limit,
        profile_id=base_ctx.profile_id,
        min_quality=min_quality,
    )
    existing_map = _load_existing_decisions(
        conn,
        profile_id=base_ctx.profile_id,
        requirement_ids=[str(item["requirement_id"]) for item in requirements],
    )
    pair_selection_started_at = time.perf_counter()
    pairs, pairing_stats = _select_evaluation_pairs(
        requirements=requirements,
        scopes=scopes,
        existing_map=existing_map,
        mode=run_mode,
        force_recompute=force_flag,
        rerun_status=rerun_status,
        scope_filters_applied=bool(site_filter or process_filter or activity_filter),
    )
    pair_selection_seconds = time.perf_counter() - pair_selection_started_at
    load_requirements_seconds = time.perf_counter() - load_requirements_started_at
    print(f"  {len(requirements)} exigence(s) candidates")
    print(f"  {len(pairs)} couple(s) exigence x scope a evaluer")
    print(
        "  Scopes retenus : "
        f"initial={pairing_stats['pairs_initial']} | "
        f"retenus={pairing_stats['pairs_retained']} | "
        f"prunes={pairing_stats['pairs_pruned']}"
    )
    print(
        "  Paires scope   : "
        f"selectionnees={', '.join(f'{k}={v}' for k, v in pairing_stats['pairs_selected_by_scope_level'].items()) or '-'}"
    )

    if not requirements or not pairs:
        print("\n  [INFO] Aucune exigence a evaluer pour les filtres et le mode demandes")
        conn.close()
        engine_stats = _finalize_engine_stats({
            "rule_applied": 0,
            "llm_calls": 0,
            "quality_gate_downgrades": 0,
            "requirements_loaded": len(requirements),
            "scopes_total": len(scopes),
            "legacy_scope_purged": 0,
            "context_load_seconds": round(load_context_seconds, 4),
            "requirements_load_seconds": round(load_requirements_seconds, 4),
            "pair_selection_seconds": round(pair_selection_seconds, 4),
            "evaluation_loop_seconds": 0.0,
            "runtime_seconds_total": round(time.perf_counter() - runtime_started_at, 4),
            "pair_seconds_total": 0.0,
            "llm_seconds_total": 0.0,
            "rule_seconds_total": 0.0,
            "commit_seconds_total": 0.0,
            "commit_count": 0,
            **pairing_stats,
        }, 0)
        return {
            "total": 0,
            "decisions": [],
            "counts": {status: 0 for status in VALID_STATUSES},
            "engine_stats": engine_stats,
            "scope_counts": scope_counts,
            "mode": run_mode,
            "force_recompute": force_flag,
            "stopped": False,
        }

    # Compteurs
    counts = {s: 0 for s in VALID_STATUSES}
    counts["ERROR"] = 0
    engine_stats = {
        "rule_applied": 0,
        "llm_calls": 0,
        "quality_gate_downgrades": 0,
        "legacy_scope_purged": 0,
        "requirements_loaded": len(requirements),
        "scopes_total": len(scopes),
        "context_load_seconds": round(load_context_seconds, 4),
        "requirements_load_seconds": round(load_requirements_seconds, 4),
        "pair_selection_seconds": round(pair_selection_seconds, 4),
        "evaluation_loop_seconds": 0.0,
        "runtime_seconds_total": 0.0,
        "pair_seconds_total": 0.0,
        "llm_seconds_total": 0.0,
        "rule_seconds_total": 0.0,
        "commit_seconds_total": 0.0,
        "commit_count": 0,
        **pairing_stats,
    }
    results = []
    expected_pairs_by_req: dict[str, int] = {}
    succeeded_pairs_by_req: dict[str, int] = {}
    for req, _ctx_item, _existing in pairs:
        req_key = str(req.get("requirement_id") or "").strip()
        if not req_key:
            continue
        expected_pairs_by_req[req_key] = expected_pairs_by_req.get(req_key, 0) + 1

    print(f"\n-- Evaluation ({len(pairs)} couples exigence x scope) --\n")
    print(f"  Mode Fast+Safe : regles + LLM ambigu + quality gate (base={quality_gate_min_conf:.2f}, seuils par type)")
    print(f"  Strategie      : mode={run_mode} | force={force_flag} | rerun_status={rerun_status or '-'}")
    print(f"  Optimisation   : delay_llm={effective_delay_between:.2f}s | commit_batch={commit_batch_size} | llm_max_tokens={_a2_llm_max_output_tokens()}")

    pending_commits = 0
    evaluation_started_at = time.perf_counter()

    for i, (req, ctx, existing) in enumerate(pairs, 1):
        if callable(stop_requested) and bool(stop_requested()):
            if pending_commits:
                commit_started_at = time.perf_counter()
                conn.commit()
                engine_stats["commit_seconds_total"] += time.perf_counter() - commit_started_at
                engine_stats["commit_count"] += 1
            print("  [STOP] Arrêt demandé, mise en pause après sauvegarde des éléments déjà traités.")
            conn.close()
            total = sum(v for k, v in counts.items() if k != "ERROR")
            engine_stats["evaluation_loop_seconds"] = round(time.perf_counter() - evaluation_started_at, 4)
            engine_stats["runtime_seconds_total"] = round(time.perf_counter() - runtime_started_at, 4)
            engine_stats = _finalize_engine_stats(engine_stats, total)
            return {
                "total": total,
                "counts": counts,
                "decisions": results,
                "engine_stats": engine_stats,
                "scope_counts": scope_counts,
                "mode": run_mode,
                "force_recompute": force_flag,
                "stopped": True,
            }

        req_text_short = _safe_console_text((req["requirement_text"] or ""), max_len=70)
        qse_domain = _safe_console_text(req.get("qse_domain") or "?", max_len=20)
        scope_short = _safe_console_text(f"{ctx.scope_level}:{ctx.scope_label}", max_len=44)
        current_status = str((existing or {}).get("status") or "")
        rerun_hint = f" | ancien={current_status}" if current_status else ""
        print(f"  [{i:3d}/{len(pairs)}] {qse_domain:20s} | {scope_short}{rerun_hint}")
        print(f"           {req_text_short}...")

        if dry_run:
            print(f"           -> [DRY RUN] ignore")
            continue

        pair_started_at = time.perf_counter()
        try:
            source = "LLM"
            llm_model = llm.last_model_used
            decision_started_at = time.perf_counter()
            # Decision en deux vitesses: regle fiable d'abord, LLM seulement
            # quand le texte exige une interpretation plus nuancee.
            decision = _apply_deterministic_rules(req, ctx)
            if decision is not None:
                source = "RULE"
                llm_model = RULE_ENGINE_MODEL
                engine_stats["rule_applied"] += 1
            else:
                engine_stats["llm_calls"] += 1
                decision = evaluate_requirement(req, ctx, context_blocks[ctx.scope_key], llm)
                llm_model = llm.last_model_used
            decision_seconds = time.perf_counter() - decision_started_at
            if source == "RULE":
                engine_stats["rule_seconds_total"] += decision_seconds
            else:
                engine_stats["llm_seconds_total"] += decision_seconds

            # Les exigences futures restent tracables, mais ne sont pas
            # confondues avec les obligations immediates.
            decision, marked_future = _apply_future_applicability_gate(decision, req)
            if marked_future:
                source = f"{source}_TEMP"

            # Le quality gate transforme les reponses faibles en INCERTAIN
            # plutot que de conserver une decision peu defendable.
            req_min_confidence = _min_confidence_for_req_type(req.get("req_type"))
            decision, downgraded = _apply_quality_gate(
                decision=decision,
                req=req,
                ctx=ctx,
                min_confidence=req_min_confidence,
            )
            if downgraded:
                engine_stats["quality_gate_downgrades"] += 1
                source = f"{source}_QG"

            _save_decision(
                conn,
                str(req["requirement_id"]),
                ctx,
                decision,
                llm_model,
                commit=False,
            )
            pending_commits += 1
            if pending_commits >= commit_batch_size:
                commit_started_at = time.perf_counter()
                conn.commit()
                engine_stats["commit_seconds_total"] += time.perf_counter() - commit_started_at
                engine_stats["commit_count"] += 1
                pending_commits = 0
            counts[decision.status] += 1
            results.append({
                "requirement_id": str(req["requirement_id"]),
                "scope_level": ctx.scope_level,
                "scope_key": ctx.scope_key,
                "scope_label": ctx.scope_label,
                "site_id": ctx.site_id,
                "process_id": ctx.process_id,
                "activity_id": ctx.activity_id,
                "status": decision.status,
                "confidence": decision.confidence,
                "source": source,
                "effective_date": req.get("effective_date"),
                "transition_period_days": req.get("transition_period_days"),
            })
            req_key = str(req.get("requirement_id") or "").strip()
            if req_key:
                succeeded_pairs_by_req[req_key] = succeeded_pairs_by_req.get(req_key, 0) + 1
            status_icon =  {"APPLICABLE": "OK", "APPLICABLE_FUTUR": ">", "NON_APPLICABLE": "X",
                            "APPLICABLE_SOUS_CONDITIONS": "~", "INCERTAIN": "?"}.get(decision.status, "?")
            provider = "rules"
            if source.startswith("LLM"):
                provider = str(llm.last_provider_used or "llm")
            print(
                f"           -> [{status_icon}] {decision.status} (conf: {decision.confidence:.2f})"
                f" | {provider} | {source} | {ctx.scope_key}"
            )

        except Exception as e:
            counts["ERROR"] += 1
            print(f"           -> [ERREUR] {_safe_console_text(str(e), max_len=100)}", file=sys.stderr)
        finally:
            engine_stats["pair_seconds_total"] += time.perf_counter() - pair_started_at

        if callable(stop_requested) and bool(stop_requested()):
            if pending_commits:
                commit_started_at = time.perf_counter()
                conn.commit()
                engine_stats["commit_seconds_total"] += time.perf_counter() - commit_started_at
                engine_stats["commit_count"] += 1
            print("  [STOP] Arrêt demandé, pause immédiate.")
            conn.close()
            total = sum(v for k, v in counts.items() if k != "ERROR")
            engine_stats["evaluation_loop_seconds"] = round(time.perf_counter() - evaluation_started_at, 4)
            engine_stats["runtime_seconds_total"] = round(time.perf_counter() - runtime_started_at, 4)
            engine_stats = _finalize_engine_stats(engine_stats, total)
            return {
                "total": total,
                "counts": counts,
                "decisions": results,
                "engine_stats": engine_stats,
                "scope_counts": scope_counts,
                "mode": run_mode,
                "force_recompute": force_flag,
                "stopped": True,
            }

        if source.startswith("LLM") and effective_delay_between > 0 and i < len(pairs):
            time.sleep(effective_delay_between)

    if pending_commits:
        commit_started_at = time.perf_counter()
        conn.commit()
        engine_stats["commit_seconds_total"] += time.perf_counter() - commit_started_at
        engine_stats["commit_count"] += 1

    should_prune_legacy = (
        run_mode == "full"
        and not rerun_status
        and not site_filter
        and not process_filter
        and not activity_filter
    )
    if should_prune_legacy:
        fully_recomputed_req_ids = [
            req_id
            for req_id, expected in expected_pairs_by_req.items()
            if expected > 0 and succeeded_pairs_by_req.get(req_id, 0) == expected
        ]
        if fully_recomputed_req_ids:
            engine_stats["legacy_scope_purged"] = _purge_legacy_scope_decisions(
                conn,
                profile_id=base_ctx.profile_id,
                requirement_ids=fully_recomputed_req_ids,
            )

    engine_stats["evaluation_loop_seconds"] = round(time.perf_counter() - evaluation_started_at, 4)
    engine_stats["runtime_seconds_total"] = round(time.perf_counter() - runtime_started_at, 4)
    total = sum(v for k, v in counts.items() if k != "ERROR")
    engine_stats = _finalize_engine_stats(engine_stats, total)

    conn.close()

    # Resume
    print(f"\n{'='*60}")
    print(f"RESUME - {tenant_safe}")
    print(f"  Total evalue       : {total}")
    print(f"  Applicable         : {counts['APPLICABLE']}")
    print(f"  Applicable futur   : {counts['APPLICABLE_FUTUR']}")
    print(f"  Non applicable     : {counts['NON_APPLICABLE']}")
    print(f"  Sous conditions    : {counts['APPLICABLE_SOUS_CONDITIONS']}")
    print(f"  Incertain          : {counts['INCERTAIN']}")
    if counts["ERROR"]:
        print(f"  Erreurs            : {counts['ERROR']}")
    print(f"  Regles appliquees  : {engine_stats['rule_applied']}")
    print(f"  Appels LLM         : {engine_stats['llm_calls']}")
    print(f"  Quality gate       : {engine_stats['quality_gate_downgrades']} declassement(s)")
    print(f"  Legacy purged      : {engine_stats['legacy_scope_purged']}")
    print(f"  Pairs initiaux     : {engine_stats['pairs_initial']}")
    print(f"  Pairs retenus      : {engine_stats['pairs_retained']}")
    print(f"  Pairs prunes       : {engine_stats['pairs_pruned']} ({engine_stats['requirements_pruned']} exigence(s))")
    print(f"  Pairs skips exist. : {engine_stats['pairs_skipped_existing']}")
    print(f"  Pairs skips rerun  : {engine_stats['pairs_skipped_rerun_status']}")
    print(f"  Temps total        : {engine_stats['runtime_seconds_total']:.2f}s")
    print(f"  Temps loop         : {engine_stats['evaluation_loop_seconds']:.2f}s")
    print(f"  Moy / paire        : {engine_stats['avg_seconds_per_pair']:.3f}s")
    print(f"  Moy / appel LLM    : {engine_stats['avg_seconds_per_llm_call']:.3f}s")
    print(f"  Debit              : {engine_stats['pairs_per_minute']:.2f} paires/min")
    print(f"  Scopes evalues     : {', '.join(f'{k}={v}' for k, v in scope_counts.items())}")
    print(f"{'='*60}\n")

    return {
        "total": total,
        "counts": counts,
        "decisions": results,
        "engine_stats": engine_stats,
        "scope_counts": scope_counts,
        "mode": run_mode,
        "force_recompute": force_flag,
        "stopped": False,
    }


# --- Utilitaires --------------------------------------------------------------

def _get_conn(tenant_id: str | None = None) -> psycopg.Connection:
    dsn = os.getenv("PG_DSN", "").strip()
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")
    return connect_db(dsn, tenant_id=tenant_id)


def get_applicability_summary(tenant_id: str) -> dict:
    """Retourne le resume des decisions d'applicabilite pour l'API."""
    conn = _get_conn(tenant_id)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.profile_id FROM company_profiles p WHERE p.tenant_id = %s
        """, (tenant_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return {"error": f"Tenant inconnu: {tenant_id}"}
        profile_id = str(row[0])

        cur.execute("""
            SELECT  ad.requirement_id::text,
                    ad.status,
                    COALESCE(ad.scope_level, 'ORGANIZATION'),
                    COALESCE(ad.scope_key, 'ORGANIZATION'),
                    COALESCE(ad.scope_label, 'ORGANIZATION'),
                    ad.site_id::text,
                    ad.process_id::text,
                    ad.activity_id::text,
                    r.qse_domain,
                    r.qse_sub_domain,
                    r.req_type,
                    ad.justification,
                    ad.article_ref,
                    ad.condition_evaluated,
                    ad.company_data_used,
                    ad.scope_site,
                    ad.scope_process,
                    ad.scope_activity,
                    ad.confidence,
                    ad.llm_model,
                    ad.created_at,
                    r.requirement_text,
                    r.citation_ref,
                    r.citation_snippet,
                    d.title,
                    d.source
            FROM applicability_decisions ad
            JOIN requirements r ON r.requirement_id = ad.requirement_id
            JOIN documents d ON d.doc_id = r.doc_id
            WHERE ad.profile_id = %s
            ORDER BY COALESCE(ad.scope_level, 'ORGANIZATION'),
                    COALESCE(ad.scope_label, 'ORGANIZATION'),
                    ad.status,
                    r.qse_domain,
                    ad.confidence DESC
        """, (profile_id,))
        rows = cur.fetchall()

    conn.close()
    decisions = effective_applicability_rows([
        _with_temporal_metadata({
            "requirement_id": r[0],
            "status": r[1],
            "scope_level": r[2],
            "scope_key": r[3],
            "scope_label": r[4],
            "site_id": r[5],
            "process_id": r[6],
            "activity_id": r[7],
            "qse_domain": r[8],
            "qse_sub_domain": r[9],
            "req_type": r[10],
            "justification": r[11],
            "article_ref": r[12],
            "condition_evaluated": r[13],
            "company_data_used": r[14],
            "scope_site": r[15],
            "scope_process": r[16],
            "scope_activity": r[17],
            "confidence": r[18],
            "decision_engine": r[19],
            "created_at": r[20].isoformat() if r[20] else None,
            "requirement_text": r[21] or "",
            "citation_ref": r[22] or "",
            "citation_snippet": r[23] or "",
            "doc_title": r[24] or "",
            "doc_source": r[25] or "",
        })
        for r in rows
    ])

    counts: dict[str, int] = {}
    scope_counts: dict[str, int] = {}
    for item in decisions:
        status = str(item.get("status") or "").strip().upper() or "UNKNOWN"
        scope_level = str(item.get("scope_level") or "ORGANIZATION").strip().upper() or "ORGANIZATION"
        counts[status] = counts.get(status, 0) + 1
        scope_counts[scope_level] = scope_counts.get(scope_level, 0) + 1

    return {
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "counts": counts,
        "scope_counts": scope_counts,
        "total": sum(counts.values()),
        "decisions": decisions,
    }


def review_applicability_decision(
    tenant_id: str,
    requirement_id: str,
    status: str,
    reviewer_username: str,
    reviewer_role: str,
    comment: str = "",
    scope_key: str | None = None,
) -> dict:
    """Applique une validation humaine sur une décision A2 existante."""
    normalized_status = str(status or "").strip().upper()
    if normalized_status not in VALID_STATUSES:
        raise ValueError(f"Statut A2 invalide: {status}")

    conn = _get_conn(tenant_id)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT profile_id::text FROM company_profiles WHERE tenant_id = %s LIMIT 1",
                (tenant_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Tenant inconnu: {tenant_id}")
            profile_id = str(row[0])

            cur.execute(
                """
                SELECT  decision_id::text,
                        COALESCE(scope_key, 'ORGANIZATION'),
                        status,
                            COALESCE(justification, '')
                FROM applicability_decisions
                WHERE profile_id = %s AND requirement_id = %s::uuid
                ORDER BY
                    CASE COALESCE(scope_level, 'ORGANIZATION')
                        WHEN 'ORGANIZATION' THEN 0
                        WHEN 'SITE' THEN 1
                        WHEN 'PROCESS' THEN 2
                        WHEN 'ACTIVITY' THEN 3
                        ELSE 9
                    END,
                    COALESCE(scope_label, '')
                """,
                (profile_id, requirement_id),
            )
            candidates = cur.fetchall()
            if not candidates:
                raise ValueError("Aucune décision A2 trouvée pour cette exigence")

            candidate_rows = effective_applicability_rows([
                {
                    "decision_id": row[0],
                    "requirement_id": requirement_id,
                    "scope_key": row[1],
                    "status": row[2],
                    "justification": row[3],
                }
                for row in candidates
            ])
            if candidate_rows:
                candidates = [
                    (
                        row.get("decision_id"),
                        row.get("scope_key"),
                        row.get("status"),
                        row.get("justification"),
                    )
                    for row in candidate_rows
                ]

            target_scope_key = str(scope_key or "").strip() or None
            selected = None
            if target_scope_key:
                for candidate in candidates:
                    if str(candidate[1] or "ORGANIZATION") == target_scope_key:
                        selected = candidate
                        break
                if selected is None:
                    raise ValueError(f"Aucune décision A2 trouvée pour le scope_key demandé: {target_scope_key}")
            elif len(candidates) == 1:
                selected = candidates[0]
            else:
                for candidate in candidates:
                    if str(candidate[1] or "ORGANIZATION") == "ORGANIZATION":
                        selected = candidate
                        break
                if selected is None:
                    raise ValueError("Plusieurs décisions A2 existent pour cette exigence; scope_key requis")

            previous_status = str(selected[2] or "").upper()
            base_justification = str(selected[3] or "")
            ts = datetime.now(UTC).isoformat(timespec="seconds")
            note = f"[VALIDATION_HUMAINE {ts}] {reviewer_username} ({reviewer_role}) : {previous_status} -> {normalized_status}"
            safe_comment = str(comment or "").strip()
            if safe_comment:
                note += f" | Commentaire: {safe_comment[:700]}"
            merged_justification = f"{base_justification}\n\n{note}" if base_justification else note

            cur.execute(
                """
                UPDATE applicability_decisions
                SET status = %s,
                    justification = %s,
                    updated_at = now()
                WHERE profile_id = %s
                    AND requirement_id = %s::uuid
                    AND COALESCE(scope_key, 'ORGANIZATION') = %s
                RETURNING decision_id::text, updated_at::text
                """,
                (
                    normalized_status,
                    merged_justification,
                    profile_id,
                    requirement_id,
                    str(selected[1] or "ORGANIZATION"),
                ),
            )
            updated = cur.fetchone()

        conn.commit()
    finally:
        conn.close()

    return {
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "requirement_id": requirement_id,
        "scope_key": str(selected[1] or "ORGANIZATION"),
        "previous_status": previous_status,
        "new_status": normalized_status,
        "updated_at": updated[1] if updated else None,
    }


# --- CLI ----------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent 2 - Applicabilite reglementaire")
    parser.add_argument("--tenant", required=True, help="Tenant ID")
    parser.add_argument("--limit", type=int, default=None, help="Nombre max d'exigences a evaluer")
    parser.add_argument("--req-id", default=None, help="Evaluer une seule exigence par UUID")
    parser.add_argument("--rerun-status", default=None,
                        choices=list(VALID_STATUSES),
                        help="Relancer les decisions avec ce statut")
    parser.add_argument("--dry-run", action="store_true", help="Simule sans ecrire en DB")
    parser.add_argument("--delay", type=float, default=1.0, help="Delai entre appels LLM (secondes)")
    parser.add_argument("--mode", default="delta", choices=["full", "delta"], help="Mode de calcul A2")
    parser.add_argument("--force", action="store_true", help="Recalcule les scopes deja existants")
    parser.add_argument("--site-id", action="append", default=[], help="Limiter l'evaluation a un site_id (multi)")
    parser.add_argument("--process-id", action="append", default=[], help="Limiter l'evaluation a un process_id (multi)")
    parser.add_argument("--activity-id", action="append", default=[], help="Limiter l'evaluation a un activity_id (multi)")
    parser.add_argument("--summary", action="store_true", help="Affiche le resume des decisions existantes")
    args = parser.parse_args()

    if args.summary:
        summary = get_applicability_summary(args.tenant)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        run_applicability(
            tenant_id=args.tenant,
            limit=args.limit,
            req_id=args.req_id,
            rerun_status=args.rerun_status,
            dry_run=args.dry_run,
            delay_between=args.delay,
            mode=args.mode,
            force=args.force,
            site_ids=list(args.site_id or []),
            process_ids=list(args.process_id or []),
            activity_ids=list(args.activity_id or []),
        )
