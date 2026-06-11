from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

import openpyxl
import psycopg
import xlrd

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

SUPPORTED_DATASET_TYPES: dict[str, str] = {
    "company_profile": "Profil entreprise",
    "sites": "Sites",
    "processes": "Processus",
    "activities": "Activites",
    "products": "Produits",
    "chemicals": "Substances chimiques",
    "equipment": "Equipements",
    "environmental_aspects": "Aspects environnementaux",
    "sst_risks": "Risques SST",
    "sst_significant_risks": "Risques SST significatifs",
    "strategic_objectives": "Objectifs strategiques",
    "nonconformities": "Non-conformites",
    "audit_reports_metadata": "Metadonnees audits",
    "compliance_evidence_manifest": "Manifest de preuves",
}

DATASET_TYPE_ALIASES: dict[str, set[str]] = {
    "company_profile": {"company_profile", "profil_entreprise", "company", "profile"},
    "sites": {"sites", "site", "company_sites"},
    "processes": {"processes", "processus", "company_processes"},
    "activities": {"activities", "activites", "company_activities"},
    "products": {"products", "produits", "company_products"},
    "chemicals": {"chemicals", "substances_chimiques", "substances", "chemicals_list"},
    "equipment": {"equipment", "equipements", "company_equipment"},
    "environmental_aspects": {"environmental_aspects", "aspects_environnementaux", "aspects"},
    "sst_risks": {"sst_risks", "risques_sst"},
    "sst_significant_risks": {"sst_significant_risks", "risques_sst_significatifs"},
    "strategic_objectives": {"strategic_objectives", "objectifs_strategiques", "objectifs"},
    "nonconformities": {"nonconformities", "non_conformites", "ncs"},
    "audit_reports_metadata": {"audit_reports_metadata", "audit_reports", "audits"},
    "compliance_evidence_manifest": {"compliance_evidence_manifest", "compliance_evidence", "preuves"},
}

HEADER_ALIASES: dict[str, dict[str, set[str]]] = {
    "company_profile": {
        "tenant_id": {"tenant_id"},
        "company_name": {"company_name", "nom_entreprise", "company"},
        "sector": {"sector", "secteur"},
        "sub_sector": {"sub_sector", "sous_secteur", "sous_secteur_activite"},
        "country": {"country", "pays"},
        "certifications": {"certifications", "certification"},
        "headcount": {"headcount", "effectif", "headcount_total"},
        "main_activities": {"main_activities", "activites_principales"},
    },
    "sites": {
        "site_code": {"site_code", "code_site", "code"},
        "name": {"name", "site_name", "nom_site", "nom_du_site"},
        "city": {"city", "ville", "localisation", "localisation_ville"},
        "region": {"region", "region_site"},
        "type": {"type", "site_type", "type_de_site"},
        "employee_count": {"employee_count", "effectif", "effectif_estimatif"},
        "main_activities": {"main_activities", "activites_principales"},
    },
    "processes": {
        "site_code": {"site_code", "code_site"},
        "site_name": {"site_name", "nom_site"},
        "process_code": {"process_code", "code_processus", "code"},
        "name": {"name", "process_name", "nom_processus", "processus"},
        "description": {"description", "commentaires", "commentaire"},
    },
    "activities": {
        "site_code": {"site_code", "code_site"},
        "site_name": {"site_name", "nom_site"},
        "process_code": {"process_code", "code_processus"},
        "process_name": {"process_name", "nom_processus", "processus_parent"},
        "code": {"code", "activity_code", "code_activite"},
        "name": {"name", "activity_name", "activite", "nom_activite"},
        "description": {"description", "commentaires"},
    },
    "products": {
        "reference": {"reference", "reference_interne"},
        "designation": {"designation", "produit", "nom_produit"},
        "family": {"family", "famille"},
        "category": {"category", "categorie", "categorie_client"},
        "product_type": {"product_type", "type", "type_produit"},
        "nature": {"nature"},
        "unit": {"unit", "unite", "unites_de_mesures"},
        "site_name": {"site_name", "site", "nom_site"},
        "is_active": {"is_active", "actif_ve", "actif", "active"},
    },
    "chemicals": {
        "designation": {"designation", "substance", "chemical", "name", "nom"},
    },
    "equipment": {
        "site_code": {"site_code", "code_site"},
        "site_name": {"site_name", "site", "nom_site"},
        "internal_code": {"internal_code", "code_interne", "code"},
        "designation": {"designation", "equipement"},
        "nature": {"nature"},
        "equipment_type": {"equipment_type", "type"},
        "category": {"category", "categorie"},
        "location": {"location", "emplacement"},
        "serial_number": {"serial_number", "numero_serie"},
        "state": {"state", "etat"},
        "brand": {"brand", "marque"},
        "model": {"model", "modele"},
        "specific_data": {"specific_data", "donnees_specifiques"},
        "last_intervention": {"last_intervention", "derniere_intervention"},
        "next_intervention": {"next_intervention", "prochaine_intervention"},
    },
    "environmental_aspects": {
        "site_code": {"site_code", "code_site"},
        "site_name": {"site_name", "site", "nom_site"},
        "aspect_code": {"aspect_code", "code"},
        "designation": {"designation"},
        "domain": {"domain", "domaine_aspect"},
        "sub_domain": {"sub_domain", "sous_domaine_aspect"},
        "description": {"description"},
    },
    "sst_risks": {
        "site_code": {"site_code", "code_site"},
        "site_name": {"site_name", "site", "nom_site"},
        "risk_code": {"risk_code", "code"},
        "risk_type": {"risk_type", "types_des_risques_sst"},
        "designation": {"designation"},
        "domain": {"domain", "domaines_des_risques_sst"},
        "dangers": {"dangers"},
        "activities": {"activities", "activites"},
        "dangerous_situations": {"dangerous_situations", "situations_evenements_dangereux"},
        "damages": {"damages", "dommages"},
        "description": {"description"},
    },
    "sst_significant_risks": {
        "site_code": {"site_code", "code_site"},
        "site_name": {"site_name", "site", "nom_site"},
        "risk_code": {"risk_code", "risques_sst"},
        "year": {"year", "annee"},
        "activities": {"activities", "activites"},
        "domain": {"domain", "domaines", "domaines_des_risques_sst"},
        "risk_type": {"risk_type", "types", "types_des_risques_sst"},
        "dangers": {"dangers"},
        "appreciation": {"appreciation", "appreciation_du_risque"},
        "score": {"score"},
        "date_start": {"date_start", "date_debut"},
        "date_end": {"date_end", "date_fin"},
        "obligations": {"obligations", "obligations_recommandation"},
        "exposure": {"exposure", "expositions"},
        "prevention_efficiency": {"prevention_efficiency", "efficacite_des_moyens_de_prevention"},
        "rpn_efficiency": {"rpn_efficiency", "rpn_efficacite"},
    },
    "strategic_objectives": {
        "objective_text": {"objective_text", "objectif"},
        "process_name": {"process_name", "processus"},
        "indicator": {"indicator", "indicateurs"},
        "indicator_type": {"indicator_type", "type"},
        "frequency": {"frequency", "frequence"},
        "calculation_method": {"calculation_method", "methode_de_calcul"},
        "system_scope": {"system_scope", "systeme"},
        "unit": {"unit", "unites_de_mesures"},
        "strategic_axis": {"strategic_axis", "type_d_orientations_strategiques"},
    },
    "nonconformities": {
        "site_code": {"site_code", "code_site"},
        "site_name": {"site_name", "site", "nom_site"},
        "reference": {"reference"},
        "year": {"year", "annee"},
        "nature": {"nature"},
        "process_name": {"process_name", "processus"},
        "title": {"title", "intitule"},
        "source": {"source"},
        "audit_type": {"audit_type", "type_d_audit"},
        "responsible_service": {"responsible_service", "service_responsable"},
        "detected_at": {"detected_at", "detectee_le"},
        "state": {"state", "etat"},
        "severity": {"severity", "intensite"},
        "nc_type": {"nc_type", "type"},
        "frequency": {"frequency", "frequence"},
        "nc_category": {"nc_category", "categorie_nc"},
        "gravity": {"gravity", "gravite"},
        "priority": {"priority", "priorites", "priorite"},
        "closed_at": {"closed_at", "date_de_cloture"},
        "system_scope": {"system_scope", "systeme"},
        "progress_pct": {"progress_pct", "avancement", "taux_de_realisation"},
        "closure_rate": {"closure_rate", "taux_de_cloture"},
    },
    "audit_reports_metadata": {
        "reference": {"reference"},
        "audit_type": {"audit_type", "type_d_audit"},
        "category": {"category", "categorie"},
        "nature": {"nature"},
        "system_scope": {"system_scope", "systeme"},
        "date_planned_start": {"date_planned_start"},
        "date_planned_end": {"date_planned_end"},
        "date_real_start": {"date_real_start"},
        "date_real_end": {"date_real_end"},
        "state": {"state", "etat"},
        "objectives": {"objectives", "objectifs"},
        "locations_visited": {"locations_visited", "locations_visitees"},
        "auditor_names": {"auditor_names", "auditeurs"},
        "source_file": {"source_file", "fichier_source", "storage_path"},
        "raw_text": {"raw_text", "texte_brut"},
        "scope_level": {"scope_level"},
        "scope_label": {"scope_label"},
        "site_code": {"site_code", "code_site"},
        "site_name": {"site_name", "nom_site"},
        "process_code": {"process_code", "code_processus"},
        "process_name": {"process_name", "nom_processus"},
        "activity_code": {"activity_code", "code_activite"},
        "activity_name": {"activity_name", "nom_activite"},
    },
    "compliance_evidence_manifest": {
        "title": {"title", "titre"},
        "file_name": {"file_name", "nom_fichier"},
        "storage_path": {"storage_path", "chemin_stockage", "source_file"},
        "raw_text": {"raw_text", "texte_brut"},
        "evidence_type": {"evidence_type", "type_de_preuve"},
        "source_type": {"source_type", "type_source"},
        "requirement_reference": {"requirement_reference", "requirement_id", "reference_exigence"},
        "scope_level": {"scope_level"},
        "scope_label": {"scope_label"},
        "site_code": {"site_code", "code_site"},
        "site_name": {"site_name", "nom_site"},
        "process_code": {"process_code", "code_processus"},
        "process_name": {"process_name", "nom_processus"},
        "activity_code": {"activity_code", "code_activite"},
        "activity_name": {"activity_name", "nom_activite"},
        "issued_at": {"issued_at", "date_emission"},
        "created_by": {"created_by", "cree_par"},
        "linked_audit_reference": {"linked_audit_reference", "reference_audit_lie"},
    },
}


def normalize_dataset_type(value: str) -> str:
    normalized = _normalize_key(value)
    for canonical, aliases in DATASET_TYPE_ALIASES.items():
        if normalized == canonical or normalized in aliases:
            return canonical
    raise ValueError(f"Type d'import non supporte: {value}")


def import_company_dataset(
    conn: psycopg.Connection,
    *,
    tenant_id: str,
    dataset_type: str,
    file_name: str,
    payload: bytes,
    actor: str = "system",
) -> dict[str, Any]:
    dataset_key = normalize_dataset_type(dataset_type)
    rows = _read_tabular_rows(file_name, payload)
    report = {
        "dataset_type": dataset_key,
        "dataset_label": SUPPORTED_DATASET_TYPES[dataset_key],
        "file_name": file_name,
        "total_rows": len(rows),
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "warnings": [],
    }
    if not rows:
        report["warnings"].append("Aucune ligne de donnees exploitable dans le fichier.")
        return report

    canonical_rows = [_canonicalize_row(dataset_key, row) for row in rows]
    if dataset_key == "company_profile":
        with conn.cursor() as cur:
            _import_company_profile(cur, tenant_id, canonical_rows, report)
        return report

    with conn.cursor() as cur:
        profile_id = _require_profile_id(cur, tenant_id)
        match dataset_key:
            case "sites":
                _import_sites(cur, profile_id, canonical_rows, report)
            case "processes":
                _import_processes(cur, profile_id, canonical_rows, report)
            case "activities":
                _import_activities(cur, profile_id, canonical_rows, report)
            case "products":
                _import_products(cur, profile_id, canonical_rows, report)
            case "chemicals":
                _import_chemicals(cur, profile_id, canonical_rows, report)
            case "equipment":
                _import_equipment(cur, profile_id, canonical_rows, report)
            case "environmental_aspects":
                _import_environmental_aspects(cur, profile_id, canonical_rows, report)
            case "sst_risks":
                _import_sst_risks(cur, profile_id, canonical_rows, report)
            case "sst_significant_risks":
                _import_sst_significant_risks(cur, profile_id, canonical_rows, report)
            case "strategic_objectives":
                _import_strategic_objectives(cur, profile_id, canonical_rows, report)
            case "nonconformities":
                _import_nonconformities(cur, profile_id, canonical_rows, report)
            case "audit_reports_metadata":
                _import_audit_reports_metadata(cur, profile_id, tenant_id, canonical_rows, report, actor=actor)
            case "compliance_evidence_manifest":
                _import_compliance_evidence_manifest(cur, profile_id, tenant_id, canonical_rows, report, actor=actor)
            case _:
                raise ValueError(f"Type d'import non implemente: {dataset_key}")
    return report


def _normalize_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = (
        unicodedata.normalize("NFKD", text)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    text = text.replace("%", " pct ")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _to_int(value: Any) -> int | None:
    raw = _clean(value)
    if not raw:
        return None
    digits = re.sub(r"[^\d-]", "", raw)
    if digits in {"", "-"}:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    raw = _clean(value)
    if not raw:
        return None
    raw = raw.replace("%", "").replace(",", ".")
    raw = re.sub(r"[^0-9.\-]", "", raw)
    if raw in {"", "-", ".", "-."}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _clean(value)
    if not raw:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(raw.split(".")[0], fmt).date()
        except ValueError:
            continue
    return None


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    raw = _clean(value)
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _to_bool(value: Any, *, default: bool = True) -> bool:
    raw = _clean(value).lower()
    if not raw:
        return default
    if raw in {"1", "true", "oui", "yes", "y", "actif", "active"}:
        return True
    if raw in {"0", "false", "non", "no", "n", "inactif", "inactive"}:
        return False
    return default


def _split_list(value: Any) -> list[str]:
    raw = _clean(value)
    if not raw:
        return []
    parts = re.split(r"[|,;\n]+", raw)
    return [part.strip() for part in parts if part and part.strip()]


def _read_tabular_rows(file_name: str, payload: bytes) -> list[dict[str, Any]]:
    suffix = Path(file_name).suffix.lower()
    if suffix == ".csv":
        return _read_csv_rows(payload)
    if suffix == ".xlsx":
        return _read_xlsx_rows(payload)
    if suffix == ".xls":
        return _read_xls_rows(payload)
    raise ValueError("Format non supporte. Utiliser CSV, XLSX ou XLS.")


def _read_csv_rows(payload: bytes) -> list[dict[str, Any]]:
    decoded = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            decoded = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        raise ValueError("Impossible de decoder le fichier CSV.")

    sample = decoded[:2048] or "a,b\n1,2"
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(decoded), dialect=dialect)
    rows: list[dict[str, Any]] = []
    for row in reader:
        if not row:
            continue
        if not any(_clean(value) for value in row.values()):
            continue
        rows.append(row)
    return rows


def _read_xlsx_rows(payload: bytes) -> list[dict[str, Any]]:
    workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    rows = list(sheet.iter_rows(values_only=True))
    workbook.close()
    return _sheet_rows_to_dicts(rows)


def _read_xls_rows(payload: bytes) -> list[dict[str, Any]]:
    workbook = xlrd.open_workbook(file_contents=payload)
    sheet = workbook.sheet_by_index(0)
    rows = []
    for i in range(sheet.nrows):
        rows.append([sheet.cell_value(i, j) for j in range(sheet.ncols)])
    return _sheet_rows_to_dicts(rows)


def _sheet_rows_to_dicts(rows: list[list[Any] | tuple[Any, ...]]) -> list[dict[str, Any]]:
    header_row_index = None
    for idx, row in enumerate(rows[:10]):
        if row and any(_clean(cell) for cell in row):
            header_row_index = idx
            break
    if header_row_index is None:
        return []
    header_row = rows[header_row_index]
    headers = [str(cell).strip() if _clean(cell) else f"col_{i}" for i, cell in enumerate(header_row)]
    data_rows: list[dict[str, Any]] = []
    for row in rows[header_row_index + 1 :]:
        if not row or not any(_clean(cell) for cell in row):
            continue
        item: dict[str, Any] = {}
        for index, header in enumerate(headers):
            item[header] = row[index] if index < len(row) else None
        data_rows.append(item)
    return data_rows


def _canonicalize_row(dataset_type: str, row: dict[str, Any]) -> dict[str, Any]:
    normalized_row = {_normalize_key(key): value for key, value in row.items()}
    aliases = HEADER_ALIASES[dataset_type]
    canonical: dict[str, Any] = {}
    for field_name, field_aliases in aliases.items():
        matched_value = None
        for alias in {field_name, *field_aliases}:
            if alias in normalized_row:
                matched_value = normalized_row[alias]
                break
        canonical[field_name] = matched_value
    return canonical


def _require_profile_id(cur: psycopg.Cursor[Any], tenant_id: str) -> str:
    cur.execute(
        """
        SELECT profile_id::text
        FROM company_profiles
        WHERE LOWER(COALESCE(tenant_id, '')) = LOWER(%s)
        LIMIT 1
        """,
        (tenant_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Tenant inconnu ou sans profil entreprise: {tenant_id}")
    return str(row[0])


def _lookup_site_id(
    cur: psycopg.Cursor[Any],
    profile_id: str,
    *,
    site_id: str | None = None,
    site_code: str | None = None,
    site_name: str | None = None,
) -> str | None:
    direct_site_id = _clean(site_id)
    if direct_site_id and UUID_RE.match(direct_site_id):
        cur.execute(
            """
            SELECT site_id::text
            FROM company_sites
            WHERE profile_id = %s::uuid AND site_id = %s::uuid
            LIMIT 1
            """,
            (profile_id, direct_site_id),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])

    normalized_site_code = _clean(site_code)
    if normalized_site_code:
        cur.execute(
            """
            SELECT site_id::text
            FROM company_sites
            WHERE profile_id = %s::uuid
              AND LOWER(COALESCE(site_code, '')) = LOWER(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, normalized_site_code),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])

    normalized_site_name = _clean(site_name)
    if normalized_site_name:
        cur.execute(
            """
            SELECT site_id::text
            FROM company_sites
            WHERE profile_id = %s::uuid
              AND LOWER(COALESCE(site_name, '')) = LOWER(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, normalized_site_name),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])
    return None


def _lookup_process_id(
    cur: psycopg.Cursor[Any],
    profile_id: str,
    *,
    process_id: str | None = None,
    site_id: str | None = None,
    process_code: str | None = None,
    process_name: str | None = None,
) -> str | None:
    direct_process_id = _clean(process_id)
    if direct_process_id and UUID_RE.match(direct_process_id):
        cur.execute(
            """
            SELECT process_id::text
            FROM company_processes
            WHERE profile_id = %s::uuid AND process_id = %s::uuid
            LIMIT 1
            """,
            (profile_id, direct_process_id),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])

    normalized_process_code = _clean(process_code)
    if normalized_process_code:
        cur.execute(
            """
            SELECT process_id::text
            FROM company_processes
            WHERE profile_id = %s::uuid
              AND COALESCE(site_id::text, '') = COALESCE(%s, '')
              AND LOWER(COALESCE(process_code, '')) = LOWER(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, site_id or "", normalized_process_code),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])

    normalized_process_name = _clean(process_name)
    if normalized_process_name:
        cur.execute(
            """
            SELECT process_id::text
            FROM company_processes
            WHERE profile_id = %s::uuid
              AND COALESCE(site_id::text, '') = COALESCE(%s, '')
              AND LOWER(COALESCE(process_name, '')) = LOWER(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, site_id or "", normalized_process_name),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])
    return None


def _lookup_activity_id(
    cur: psycopg.Cursor[Any],
    profile_id: str,
    *,
    activity_id: str | None = None,
    site_id: str | None = None,
    process_id: str | None = None,
    activity_code: str | None = None,
    activity_name: str | None = None,
) -> str | None:
    direct_activity_id = _clean(activity_id)
    if direct_activity_id and UUID_RE.match(direct_activity_id):
        cur.execute(
            """
            SELECT activity_id::text
            FROM company_activities
            WHERE profile_id = %s::uuid AND activity_id = %s::uuid
            LIMIT 1
            """,
            (profile_id, direct_activity_id),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])

    normalized_activity_code = _clean(activity_code)
    if normalized_activity_code:
        cur.execute(
            """
            SELECT activity_id::text
            FROM company_activities
            WHERE profile_id = %s::uuid
              AND COALESCE(site_id::text, '') = COALESCE(%s, '')
              AND COALESCE(process_id::text, '') = COALESCE(%s, '')
              AND LOWER(COALESCE(activity_code, '')) = LOWER(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, site_id or "", process_id or "", normalized_activity_code),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])

    normalized_activity_name = _clean(activity_name)
    if normalized_activity_name:
        cur.execute(
            """
            SELECT activity_id::text
            FROM company_activities
            WHERE profile_id = %s::uuid
              AND COALESCE(site_id::text, '') = COALESCE(%s, '')
              AND COALESCE(process_id::text, '') = COALESCE(%s, '')
              AND LOWER(COALESCE(activity_name, '')) = LOWER(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, site_id or "", process_id or "", normalized_activity_name),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])
    return None


def _resolve_site_reference(
    cur: psycopg.Cursor[Any],
    profile_id: str,
    row: dict[str, Any],
    report: dict[str, Any],
    row_number: int,
) -> str | None:
    site_id = _lookup_site_id(
        cur,
        profile_id,
        site_code=_clean(row.get("site_code")),
        site_name=_clean(row.get("site_name")) or _clean(row.get("name")),
    )
    has_site_ref = bool(_clean(row.get("site_code")) or _clean(row.get("site_name")))
    if has_site_ref and not site_id:
        report["warnings"].append(f"Ligne {row_number}: site introuvable, ligne ignoree.")
    return site_id


def _resolve_process_reference(
    cur: psycopg.Cursor[Any],
    profile_id: str,
    row: dict[str, Any],
    report: dict[str, Any],
    row_number: int,
    *,
    site_id: str | None,
    create_if_missing: bool = False,
) -> str | None:
    process_name = _clean(row.get("process_name")) or _clean(row.get("name"))
    process_code = _clean(row.get("process_code"))
    process_id = _lookup_process_id(
        cur,
        profile_id,
        site_id=site_id,
        process_code=process_code,
        process_name=process_name,
    )
    has_process_ref = bool(process_code or process_name)
    if process_id or not has_process_ref:
        return process_id
    if not create_if_missing or not process_name:
        report["warnings"].append(f"Ligne {row_number}: processus introuvable, ligne ignoree.")
        return None
    cur.execute(
        """
        INSERT INTO company_processes
            (profile_id, site_id, process_code, process_name)
        VALUES (%s::uuid, %s::uuid, %s, %s)
        RETURNING process_id::text
        """,
        (profile_id, site_id, process_code or None, process_name),
    )
    report["inserted"] += 1
    report["warnings"].append(f"Ligne {row_number}: processus cree automatiquement ({process_name}).")
    return str(cur.fetchone()[0])


def _resolve_activity_reference(
    cur: psycopg.Cursor[Any],
    profile_id: str,
    row: dict[str, Any],
    site_id: str | None,
    process_id: str | None,
) -> str | None:
    return _lookup_activity_id(
        cur,
        profile_id,
        site_id=site_id,
        process_id=process_id,
        activity_code=_clean(row.get("activity_code")) or _clean(row.get("code")),
        activity_name=_clean(row.get("activity_name")) or _clean(row.get("name")),
    )


def _build_scope(
    *,
    site_id: str | None,
    process_id: str | None,
    activity_id: str | None,
    scope_level: str | None,
    scope_label: str | None,
) -> tuple[str, str, str]:
    normalized_level = _clean(scope_level).upper()
    label = _clean(scope_label)
    if activity_id:
        return ("ACTIVITY", f"ACTIVITY:{activity_id}", label or "ACTIVITY")
    if process_id:
        return ("PROCESS", f"PROCESS:{process_id}", label or "PROCESS")
    if site_id:
        return ("SITE", f"SITE:{site_id}", label or "SITE")
    if normalized_level in {"ORGANIZATION", "SITE", "PROCESS", "ACTIVITY"}:
        return ("ORGANIZATION", "ORGANIZATION", label or normalized_level)
    return ("ORGANIZATION", "ORGANIZATION", label or "ORGANIZATION")


def _resolve_requirement_id(cur: psycopg.Cursor[Any], reference: str | None) -> str | None:
    raw = _clean(reference)
    if not raw:
        return None
    if UUID_RE.match(raw):
        cur.execute(
            "SELECT requirement_id::text FROM requirements WHERE requirement_id = %s::uuid LIMIT 1",
            (raw,),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])
    cur.execute(
        """
        SELECT requirement_id::text
        FROM requirements
        WHERE LOWER(COALESCE(requirement_no, '')) = LOWER(%s)
           OR LOWER(COALESCE(citation_ref, '')) = LOWER(%s)
        ORDER BY extracted_at DESC NULLS LAST, created_at DESC NULLS LAST
        LIMIT 1
        """,
        (raw, raw),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


def _import_company_profile(
    cur: psycopg.Cursor[Any],
    tenant_id: str,
    rows: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    row = rows[0]
    if len(rows) > 1:
        report["warnings"].append("Plusieurs lignes detectees: seule la premiere ligne de company_profile est prise en compte.")
    file_tenant = _clean(row.get("tenant_id"))
    if file_tenant and file_tenant.lower() != tenant_id.lower():
        report["warnings"].append("Le tenant_id du fichier ne correspond pas au tenant cible. Le tenant cible est conserve.")
    company_name = _clean(row.get("company_name"))
    if not company_name:
        raise ValueError("company_name est obligatoire pour importer company_profile.")

    cur.execute(
        "SELECT profile_id::text FROM company_profiles WHERE LOWER(COALESCE(tenant_id, '')) = LOWER(%s) LIMIT 1",
        (tenant_id,),
    )
    existing = cur.fetchone()
    cur.execute(
        """
        INSERT INTO company_profiles
            (tenant_id, company_name, sector, sub_sector, country, certifications, headcount_total, main_activities)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id) DO UPDATE SET
            company_name = EXCLUDED.company_name,
            sector = EXCLUDED.sector,
            sub_sector = EXCLUDED.sub_sector,
            country = EXCLUDED.country,
            certifications = EXCLUDED.certifications,
            headcount_total = EXCLUDED.headcount_total,
            main_activities = EXCLUDED.main_activities,
            updated_at = now()
        """,
        (
            tenant_id,
            company_name,
            _clean(row.get("sector")) or None,
            _clean(row.get("sub_sector")) or None,
            (_clean(row.get("country")) or "TN").upper(),
            _split_list(row.get("certifications")),
            _to_int(row.get("headcount")),
            _clean(row.get("main_activities")) or None,
        ),
    )
    if existing:
        report["updated"] += 1
    else:
        report["inserted"] += 1


def _import_sites(cur: psycopg.Cursor[Any], profile_id: str, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    for index, row in enumerate(rows, start=1):
        site_name = _clean(row.get("name"))
        if not site_name:
            report["skipped"] += 1
            report["warnings"].append(f"Ligne {index}: nom de site manquant.")
            continue
        site_code = _clean(row.get("site_code"))
        target_site_id = _lookup_site_id(cur, profile_id, site_code=site_code, site_name=site_name)
        if target_site_id:
            cur.execute(
                """
                UPDATE company_sites
                SET site_code = %s,
                    site_name = %s,
                    city = %s,
                    region = %s,
                    site_type = %s,
                    employee_count = %s,
                    main_activities = %s
                WHERE profile_id = %s::uuid AND site_id = %s::uuid
                """,
                (
                    site_code or None,
                    site_name,
                    _clean(row.get("city")) or None,
                    _clean(row.get("region")) or None,
                    _clean(row.get("type")) or None,
                    _to_int(row.get("employee_count")),
                    _clean(row.get("main_activities")) or None,
                    profile_id,
                    target_site_id,
                ),
            )
            report["updated"] += 1
        else:
            cur.execute(
                """
                INSERT INTO company_sites
                    (profile_id, site_code, site_name, city, region, site_type, employee_count, main_activities)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    profile_id,
                    site_code or None,
                    site_name,
                    _clean(row.get("city")) or None,
                    _clean(row.get("region")) or None,
                    _clean(row.get("type")) or None,
                    _to_int(row.get("employee_count")),
                    _clean(row.get("main_activities")) or None,
                ),
            )
            report["inserted"] += 1


def _import_processes(cur: psycopg.Cursor[Any], profile_id: str, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    for index, row in enumerate(rows, start=1):
        process_name = _clean(row.get("name"))
        if not process_name:
            report["skipped"] += 1
            report["warnings"].append(f"Ligne {index}: nom de processus manquant.")
            continue
        site_id = _resolve_site_reference(cur, profile_id, row, report, index)
        if (_clean(row.get("site_code")) or _clean(row.get("site_name"))) and not site_id:
            report["skipped"] += 1
            continue
        process_code = _clean(row.get("process_code"))
        target_process_id = _lookup_process_id(
            cur,
            profile_id,
            site_id=site_id,
            process_code=process_code,
            process_name=process_name,
        )
        if target_process_id:
            cur.execute(
                """
                UPDATE company_processes
                SET process_code = %s,
                    process_name = %s,
                    description = %s,
                    site_id = %s::uuid
                WHERE profile_id = %s::uuid AND process_id = %s::uuid
                """,
                (
                    process_code or None,
                    process_name,
                    _clean(row.get("description")) or None,
                    site_id,
                    profile_id,
                    target_process_id,
                ),
            )
            report["updated"] += 1
        else:
            cur.execute(
                """
                INSERT INTO company_processes
                    (profile_id, site_id, process_code, process_name, description)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s)
                """,
                (
                    profile_id,
                    site_id,
                    process_code or None,
                    process_name,
                    _clean(row.get("description")) or None,
                ),
            )
            report["inserted"] += 1


def _import_activities(cur: psycopg.Cursor[Any], profile_id: str, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    for index, row in enumerate(rows, start=1):
        activity_name = _clean(row.get("name"))
        if not activity_name:
            report["skipped"] += 1
            report["warnings"].append(f"Ligne {index}: nom d'activite manquant.")
            continue
        site_id = _resolve_site_reference(cur, profile_id, row, report, index)
        if (_clean(row.get("site_code")) or _clean(row.get("site_name"))) and not site_id:
            report["skipped"] += 1
            continue
        process_id = _resolve_process_reference(
            cur,
            profile_id,
            row,
            report,
            index,
            site_id=site_id,
            create_if_missing=True,
        )
        if (_clean(row.get("process_code")) or _clean(row.get("process_name"))) and not process_id:
            report["skipped"] += 1
            continue
        activity_code = _clean(row.get("code"))
        target_activity_id = _lookup_activity_id(
            cur,
            profile_id,
            site_id=site_id,
            process_id=process_id,
            activity_code=activity_code,
            activity_name=activity_name,
        )
        process_name = _clean(row.get("process_name"))
        if target_activity_id:
            cur.execute(
                """
                UPDATE company_activities
                SET site_id = %s::uuid,
                    process_id = %s::uuid,
                    process_name = %s,
                    activity_name = %s,
                    activity_code = %s,
                    description = %s
                WHERE profile_id = %s::uuid AND activity_id = %s::uuid
                """,
                (
                    site_id,
                    process_id,
                    process_name or None,
                    activity_name,
                    activity_code or None,
                    _clean(row.get("description")) or None,
                    profile_id,
                    target_activity_id,
                ),
            )
            report["updated"] += 1
        else:
            cur.execute(
                """
                INSERT INTO company_activities
                    (profile_id, site_id, process_id, process_name, activity_name, activity_code, description)
                VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s)
                """,
                (
                    profile_id,
                    site_id,
                    process_id,
                    process_name or None,
                    activity_name,
                    activity_code or None,
                    _clean(row.get("description")) or None,
                ),
            )
            report["inserted"] += 1


def _import_products(cur: psycopg.Cursor[Any], profile_id: str, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    for index, row in enumerate(rows, start=1):
        designation = _clean(row.get("designation"))
        if not designation:
            report["skipped"] += 1
            report["warnings"].append(f"Ligne {index}: designation produit manquante.")
            continue
        reference = _clean(row.get("reference"))
        cur.execute(
            """
            SELECT product_id::text
            FROM company_products
            WHERE profile_id = %s::uuid
              AND (
                    (COALESCE(%s, '') <> '' AND LOWER(COALESCE(reference, '')) = LOWER(%s))
                 OR LOWER(COALESCE(designation, '')) = LOWER(%s)
              )
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, reference or "", reference or "", designation),
        )
        found = cur.fetchone()
        if found:
            cur.execute(
                """
                UPDATE company_products
                SET reference = %s,
                    designation = %s,
                    family = %s,
                    category = %s,
                    product_type = %s,
                    nature = %s,
                    unit = %s,
                    site_name = %s,
                    is_active = %s
                WHERE product_id = %s::uuid
                """,
                (
                    reference or None,
                    designation,
                    _clean(row.get("family")) or None,
                    _clean(row.get("category")) or None,
                    _clean(row.get("product_type")) or None,
                    _clean(row.get("nature")) or None,
                    _clean(row.get("unit")) or None,
                    _clean(row.get("site_name")) or None,
                    _to_bool(row.get("is_active"), default=True),
                    str(found[0]),
                ),
            )
            report["updated"] += 1
        else:
            cur.execute(
                """
                INSERT INTO company_products
                    (profile_id, reference, designation, family, category, product_type, nature, is_active, unit, site_name)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    profile_id,
                    reference or None,
                    designation,
                    _clean(row.get("family")) or None,
                    _clean(row.get("category")) or None,
                    _clean(row.get("product_type")) or None,
                    _clean(row.get("nature")) or None,
                    _to_bool(row.get("is_active"), default=True),
                    _clean(row.get("unit")) or None,
                    _clean(row.get("site_name")) or None,
                ),
            )
            report["inserted"] += 1


def _import_chemicals(cur: psycopg.Cursor[Any], profile_id: str, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    for index, row in enumerate(rows, start=1):
        designation = _clean(row.get("designation"))
        if not designation:
            report["skipped"] += 1
            report["warnings"].append(f"Ligne {index}: designation chimique manquante.")
            continue
        cur.execute(
            """
            SELECT product_id::text
            FROM company_products
            WHERE profile_id = %s::uuid
              AND (
                    COALESCE(reference, '') LIKE 'chemical:%%'
                 OR UPPER(COALESCE(category, '')) = 'CHEMICAL'
                 OR UPPER(COALESCE(product_type, '')) = 'CHEMICAL'
              )
              AND LOWER(COALESCE(designation, '')) = LOWER(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, designation),
        )
        found = cur.fetchone()
        chemical_ref = f"chemical:{hashlib.sha1(designation.lower().encode('utf-8')).hexdigest()[:12]}"
        if found:
            report["updated"] += 1
            continue
        cur.execute(
            """
            INSERT INTO company_products
                (profile_id, reference, designation, family, category, product_type, nature, is_active)
            VALUES (%s::uuid, %s, %s, 'CHEMICAL', 'CHEMICAL', 'CHEMICAL', 'CHEMICAL', TRUE)
            """,
            (profile_id, chemical_ref, designation),
        )
        report["inserted"] += 1


def _import_equipment(cur: psycopg.Cursor[Any], profile_id: str, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    for index, row in enumerate(rows, start=1):
        designation = _clean(row.get("designation"))
        if not designation:
            report["skipped"] += 1
            report["warnings"].append(f"Ligne {index}: designation equipement manquante.")
            continue
        site_id = _resolve_site_reference(cur, profile_id, row, report, index)
        if (_clean(row.get("site_code")) or _clean(row.get("site_name"))) and not site_id:
            report["skipped"] += 1
            continue
        internal_code = _clean(row.get("internal_code"))
        serial_number = _clean(row.get("serial_number"))
        cur.execute(
            """
            SELECT equipment_id::text
            FROM company_equipment
            WHERE profile_id = %s::uuid
              AND (
                    (COALESCE(%s, '') <> '' AND LOWER(COALESCE(internal_code, '')) = LOWER(%s))
                 OR (
                        LOWER(COALESCE(designation, '')) = LOWER(%s)
                    AND COALESCE(LOWER(serial_number), '') = COALESCE(LOWER(%s), '')
                 )
              )
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, internal_code or "", internal_code or "", designation, serial_number or ""),
        )
        found = cur.fetchone()
        query_params = (
            profile_id,
            site_id,
            internal_code or None,
            designation,
            _clean(row.get("nature")) or None,
            _clean(row.get("equipment_type")) or None,
            _clean(row.get("category")) or None,
            _clean(row.get("location")) or None,
            serial_number or None,
            _clean(row.get("state")) or None,
            _clean(row.get("brand")) or None,
            _clean(row.get("model")) or None,
            _clean(row.get("specific_data")) or None,
            _to_date(row.get("last_intervention")),
            _to_date(row.get("next_intervention")),
        )
        if found:
            cur.execute(
                """
                UPDATE company_equipment
                SET site_id = %s::uuid,
                    internal_code = %s,
                    designation = %s,
                    nature = %s,
                    equipment_type = %s,
                    category = %s,
                    location = %s,
                    serial_number = %s,
                    state = %s,
                    brand = %s,
                    model = %s,
                    specific_data = %s,
                    last_intervention = %s,
                    next_intervention = %s
                WHERE profile_id = %s::uuid AND equipment_id = %s::uuid
                """,
                query_params[1:] + (profile_id, str(found[0])),
            )
            report["updated"] += 1
        else:
            cur.execute(
                """
                INSERT INTO company_equipment
                    (profile_id, site_id, internal_code, designation, nature, equipment_type, category, location,
                     serial_number, state, brand, model, specific_data, last_intervention, next_intervention)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                query_params,
            )
            report["inserted"] += 1


def _import_environmental_aspects(cur: psycopg.Cursor[Any], profile_id: str, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    for index, row in enumerate(rows, start=1):
        designation = _clean(row.get("designation"))
        if not designation:
            report["skipped"] += 1
            report["warnings"].append(f"Ligne {index}: designation aspect manquante.")
            continue
        site_id = _resolve_site_reference(cur, profile_id, row, report, index)
        if (_clean(row.get("site_code")) or _clean(row.get("site_name"))) and not site_id:
            report["skipped"] += 1
            continue
        aspect_code = _clean(row.get("aspect_code"))
        domain = _clean(row.get("domain"))
        cur.execute(
            """
            SELECT aspect_id::text
            FROM environmental_aspects
            WHERE profile_id = %s::uuid
              AND (
                    (COALESCE(%s, '') <> '' AND LOWER(COALESCE(aspect_code, '')) = LOWER(%s))
                 OR (
                        LOWER(COALESCE(designation, '')) = LOWER(%s)
                    AND LOWER(COALESCE(domain, '')) = LOWER(%s)
                 )
              )
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, aspect_code or "", aspect_code or "", designation, domain),
        )
        found = cur.fetchone()
        if found:
            cur.execute(
                """
                UPDATE environmental_aspects
                SET site_id = %s::uuid,
                    aspect_code = %s,
                    designation = %s,
                    domain = %s,
                    sub_domain = %s,
                    description = %s
                WHERE profile_id = %s::uuid AND aspect_id = %s::uuid
                """,
                (
                    site_id,
                    aspect_code or None,
                    designation,
                    domain or None,
                    _clean(row.get("sub_domain")) or None,
                    _clean(row.get("description")) or None,
                    profile_id,
                    str(found[0]),
                ),
            )
            report["updated"] += 1
        else:
            cur.execute(
                """
                INSERT INTO environmental_aspects
                    (profile_id, site_id, aspect_code, designation, domain, sub_domain, description)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s)
                """,
                (
                    profile_id,
                    site_id,
                    aspect_code or None,
                    designation,
                    domain or None,
                    _clean(row.get("sub_domain")) or None,
                    _clean(row.get("description")) or None,
                ),
            )
            report["inserted"] += 1


def _import_sst_risks(cur: psycopg.Cursor[Any], profile_id: str, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    for index, row in enumerate(rows, start=1):
        designation = _clean(row.get("designation"))
        if not designation:
            report["skipped"] += 1
            report["warnings"].append(f"Ligne {index}: designation risque SST manquante.")
            continue
        site_id = _resolve_site_reference(cur, profile_id, row, report, index)
        if (_clean(row.get("site_code")) or _clean(row.get("site_name"))) and not site_id:
            report["skipped"] += 1
            continue
        risk_code = _clean(row.get("risk_code"))
        domain = _clean(row.get("domain"))
        cur.execute(
            """
            SELECT risk_id::text
            FROM sst_risks
            WHERE profile_id = %s::uuid
              AND (
                    (COALESCE(%s, '') <> '' AND LOWER(COALESCE(risk_code, '')) = LOWER(%s))
                 OR (
                        LOWER(COALESCE(designation, '')) = LOWER(%s)
                    AND LOWER(COALESCE(domain, '')) = LOWER(%s)
                 )
              )
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, risk_code or "", risk_code or "", designation, domain),
        )
        found = cur.fetchone()
        if found:
            cur.execute(
                """
                UPDATE sst_risks
                SET site_id = %s::uuid,
                    risk_code = %s,
                    risk_type = %s,
                    designation = %s,
                    domain = %s,
                    dangers = %s,
                    activities = %s,
                    dangerous_situations = %s,
                    damages = %s,
                    description = %s
                WHERE profile_id = %s::uuid AND risk_id = %s::uuid
                """,
                (
                    site_id,
                    risk_code or None,
                    _clean(row.get("risk_type")) or None,
                    designation,
                    domain or None,
                    _clean(row.get("dangers")) or None,
                    _clean(row.get("activities")) or None,
                    _clean(row.get("dangerous_situations")) or None,
                    _clean(row.get("damages")) or None,
                    _clean(row.get("description")) or None,
                    profile_id,
                    str(found[0]),
                ),
            )
            report["updated"] += 1
        else:
            cur.execute(
                """
                INSERT INTO sst_risks
                    (profile_id, site_id, risk_code, risk_type, designation, domain, dangers,
                     activities, dangerous_situations, damages, description)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    profile_id,
                    site_id,
                    risk_code or None,
                    _clean(row.get("risk_type")) or None,
                    designation,
                    domain or None,
                    _clean(row.get("dangers")) or None,
                    _clean(row.get("activities")) or None,
                    _clean(row.get("dangerous_situations")) or None,
                    _clean(row.get("damages")) or None,
                    _clean(row.get("description")) or None,
                ),
            )
            report["inserted"] += 1


def _import_sst_significant_risks(cur: psycopg.Cursor[Any], profile_id: str, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    for index, row in enumerate(rows, start=1):
        site_id = _resolve_site_reference(cur, profile_id, row, report, index)
        if (_clean(row.get("site_code")) or _clean(row.get("site_name"))) and not site_id:
            report["skipped"] += 1
            continue
        risk_ref = _clean(row.get("risk_code"))
        risk_id = None
        if risk_ref:
            cur.execute(
                """
                SELECT risk_id::text
                FROM sst_risks
                WHERE profile_id = %s::uuid
                  AND LOWER(COALESCE(risk_code, '')) = LOWER(%s)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (profile_id, risk_ref.split(":")[0].strip()),
            )
            found_risk = cur.fetchone()
            risk_id = str(found_risk[0]) if found_risk else None
        activities = _clean(row.get("activities"))
        year = _to_int(row.get("year"))
        cur.execute(
            """
            SELECT sig_risk_id::text
            FROM sst_significant_risks
            WHERE profile_id = %s::uuid
              AND COALESCE(site_id::text, '') = COALESCE(%s, '')
              AND COALESCE(risk_id::text, '') = COALESCE(%s, '')
              AND COALESCE(year, 0) = COALESCE(%s, 0)
              AND LOWER(COALESCE(activities, '')) = LOWER(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, site_id or "", risk_id or "", year, activities),
        )
        found = cur.fetchone()
        params = (
            profile_id,
            site_id,
            risk_id,
            year,
            activities or None,
            _clean(row.get("domain")) or None,
            _clean(row.get("risk_type")) or None,
            _clean(row.get("dangers")) or None,
            _clean(row.get("appreciation")) or None,
            _to_int(row.get("score")),
            _to_date(row.get("date_start")),
            _to_date(row.get("date_end")),
            _clean(row.get("obligations")) or None,
            _clean(row.get("exposure")) or None,
            _to_float(row.get("prevention_efficiency")),
            _to_float(row.get("rpn_efficiency")),
        )
        if found:
            cur.execute(
                """
                UPDATE sst_significant_risks
                SET site_id = %s::uuid,
                    risk_id = %s::uuid,
                    year = %s,
                    activities = %s,
                    domain = %s,
                    risk_type = %s,
                    dangers = %s,
                    appreciation = %s,
                    score = %s,
                    date_start = %s,
                    date_end = %s,
                    obligations = %s,
                    exposure = %s,
                    prevention_efficiency = %s,
                    rpn_efficiency = %s
                WHERE profile_id = %s::uuid AND sig_risk_id = %s::uuid
                """,
                params[1:] + (profile_id, str(found[0])),
            )
            report["updated"] += 1
        else:
            cur.execute(
                """
                INSERT INTO sst_significant_risks
                    (profile_id, site_id, risk_id, year, activities, domain, risk_type, dangers, appreciation,
                     score, date_start, date_end, obligations, exposure, prevention_efficiency, rpn_efficiency)
                VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                params,
            )
            report["inserted"] += 1


def _import_strategic_objectives(cur: psycopg.Cursor[Any], profile_id: str, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    for index, row in enumerate(rows, start=1):
        objective_text = _clean(row.get("objective_text"))
        if not objective_text:
            report["skipped"] += 1
            report["warnings"].append(f"Ligne {index}: objectif manquant.")
            continue
        process_name = _clean(row.get("process_name"))
        indicator = _clean(row.get("indicator"))
        cur.execute(
            """
            SELECT objective_id::text
            FROM strategic_objectives
            WHERE profile_id = %s::uuid
              AND LOWER(COALESCE(objective_text, '')) = LOWER(%s)
              AND LOWER(COALESCE(process_name, '')) = LOWER(%s)
              AND LOWER(COALESCE(indicator, '')) = LOWER(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, objective_text, process_name, indicator),
        )
        found = cur.fetchone()
        if found:
            cur.execute(
                """
                UPDATE strategic_objectives
                SET objective_text = %s,
                    process_name = %s,
                    indicator = %s,
                    indicator_type = %s,
                    frequency = %s,
                    calculation_method = %s,
                    system_scope = %s,
                    unit = %s,
                    strategic_axis = %s
                WHERE profile_id = %s::uuid AND objective_id = %s::uuid
                """,
                (
                    objective_text,
                    process_name or None,
                    indicator or None,
                    _clean(row.get("indicator_type")) or None,
                    _clean(row.get("frequency")) or None,
                    _clean(row.get("calculation_method")) or None,
                    _clean(row.get("system_scope")) or None,
                    _clean(row.get("unit")) or None,
                    _clean(row.get("strategic_axis")) or None,
                    profile_id,
                    str(found[0]),
                ),
            )
            report["updated"] += 1
        else:
            cur.execute(
                """
                INSERT INTO strategic_objectives
                    (profile_id, objective_text, process_name, indicator, indicator_type, frequency,
                     calculation_method, system_scope, unit, strategic_axis)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    profile_id,
                    objective_text,
                    process_name or None,
                    indicator or None,
                    _clean(row.get("indicator_type")) or None,
                    _clean(row.get("frequency")) or None,
                    _clean(row.get("calculation_method")) or None,
                    _clean(row.get("system_scope")) or None,
                    _clean(row.get("unit")) or None,
                    _clean(row.get("strategic_axis")) or None,
                ),
            )
            report["inserted"] += 1


def _import_nonconformities(cur: psycopg.Cursor[Any], profile_id: str, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    for index, row in enumerate(rows, start=1):
        reference = _clean(row.get("reference"))
        title = _clean(row.get("title"))
        if not reference or not title:
            report["skipped"] += 1
            report["warnings"].append(f"Ligne {index}: reference ou intitule NC manquant.")
            continue
        site_id = _resolve_site_reference(cur, profile_id, row, report, index)
        if (_clean(row.get("site_code")) or _clean(row.get("site_name"))) and not site_id:
            report["skipped"] += 1
            continue
        cur.execute(
            """
            SELECT nc_id::text
            FROM nonconformities
            WHERE profile_id = %s::uuid
              AND LOWER(COALESCE(reference, '')) = LOWER(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (profile_id, reference),
        )
        found = cur.fetchone()
        params = (
            profile_id,
            site_id,
            reference,
            _to_int(row.get("year")),
            _clean(row.get("nature")) or None,
            _clean(row.get("process_name")) or None,
            title,
            _clean(row.get("source")) or None,
            _clean(row.get("audit_type")) or None,
            _clean(row.get("responsible_service")) or None,
            _to_date(row.get("detected_at")),
            _clean(row.get("state")) or None,
            _clean(row.get("severity")) or None,
            _clean(row.get("nc_type")) or None,
            _clean(row.get("frequency")) or None,
            _clean(row.get("nc_category")) or None,
            _clean(row.get("gravity")) or None,
            _clean(row.get("priority")) or None,
            _to_date(row.get("closed_at")),
            _clean(row.get("system_scope")) or None,
            _to_float(row.get("progress_pct")),
            _to_float(row.get("closure_rate")),
        )
        if found:
            cur.execute(
                """
                UPDATE nonconformities
                SET site_id = %s::uuid,
                    reference = %s,
                    year = %s,
                    nature = %s,
                    process_name = %s,
                    title = %s,
                    source = %s,
                    audit_type = %s,
                    responsible_service = %s,
                    detected_at = %s,
                    state = %s,
                    severity = %s,
                    nc_type = %s,
                    frequency = %s,
                    nc_category = %s,
                    gravity = %s,
                    priority = %s,
                    closed_at = %s,
                    system_scope = %s,
                    progress_pct = %s,
                    closure_rate = %s
                WHERE profile_id = %s::uuid AND nc_id = %s::uuid
                """,
                params[1:] + (profile_id, str(found[0])),
            )
            report["updated"] += 1
        else:
            cur.execute(
                """
                INSERT INTO nonconformities
                    (profile_id, site_id, reference, year, nature, process_name, title, source, audit_type,
                     responsible_service, detected_at, state, severity, nc_type, frequency, nc_category, gravity,
                     priority, closed_at, system_scope, progress_pct, closure_rate)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                params,
            )
            report["inserted"] += 1


def _import_audit_reports_metadata(
    cur: psycopg.Cursor[Any],
    profile_id: str,
    tenant_id: str,
    rows: list[dict[str, Any]],
    report: dict[str, Any],
    *,
    actor: str,
) -> None:
    for index, row in enumerate(rows, start=1):
        reference = _clean(row.get("reference"))
        if not reference:
            report["skipped"] += 1
            report["warnings"].append(f"Ligne {index}: reference audit manquante.")
            continue
        site_id = _resolve_site_reference(cur, profile_id, row, report, index)
        process_id = _resolve_process_reference(
            cur,
            profile_id,
            row,
            report,
            index,
            site_id=site_id,
            create_if_missing=False,
        )
        activity_id = _resolve_activity_reference(cur, profile_id, row, site_id, process_id)
        scope_level, scope_key, scope_label = _build_scope(
            site_id=site_id,
            process_id=process_id,
            activity_id=activity_id,
            scope_level=_clean(row.get("scope_level")),
            scope_label=_clean(row.get("scope_label")) or reference,
        )

        cur.execute(
            """
            INSERT INTO audit_reports
                (profile_id, reference, audit_type, category, nature, system_scope,
                 date_planned_start, date_planned_end, date_real_start, date_real_end,
                 state, objectives, locations_visited, auditor_names, raw_text, source_file)
            VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (reference) DO UPDATE SET
                audit_type = EXCLUDED.audit_type,
                category = EXCLUDED.category,
                nature = EXCLUDED.nature,
                system_scope = EXCLUDED.system_scope,
                date_planned_start = EXCLUDED.date_planned_start,
                date_planned_end = EXCLUDED.date_planned_end,
                date_real_start = EXCLUDED.date_real_start,
                date_real_end = EXCLUDED.date_real_end,
                state = EXCLUDED.state,
                objectives = EXCLUDED.objectives,
                locations_visited = EXCLUDED.locations_visited,
                auditor_names = EXCLUDED.auditor_names,
                raw_text = EXCLUDED.raw_text,
                source_file = EXCLUDED.source_file
            WHERE audit_reports.profile_id = EXCLUDED.profile_id
            RETURNING report_id::text
            """,
            (
                profile_id,
                reference,
                _clean(row.get("audit_type")) or None,
                _clean(row.get("category")) or None,
                _clean(row.get("nature")) or None,
                _clean(row.get("system_scope")) or None,
                _to_date(row.get("date_planned_start")),
                _to_date(row.get("date_planned_end")),
                _to_date(row.get("date_real_start")),
                _to_date(row.get("date_real_end")),
                _clean(row.get("state")) or None,
                _clean(row.get("objectives")) or None,
                _clean(row.get("locations_visited")) or None,
                _clean(row.get("auditor_names")) or None,
                _clean(row.get("raw_text")) or None,
                _clean(row.get("source_file")) or None,
            ),
        )
        found = cur.fetchone()
        was_inserted = bool(found)
        report_ref = reference
        if not found:
            report_ref = f"{reference}_{hashlib.md5(tenant_id.encode('utf-8')).hexdigest()[:8]}"
            report["warnings"].append(
                f"Ligne {index}: reference audit deja utilisee sur un autre tenant, suffixe applique ({report_ref})."
            )
            cur.execute(
                """
                INSERT INTO audit_reports
                    (profile_id, reference, audit_type, category, nature, system_scope,
                     date_planned_start, date_planned_end, date_real_start, date_real_end,
                     state, objectives, locations_visited, auditor_names, raw_text, source_file)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (reference) DO UPDATE SET
                    audit_type = EXCLUDED.audit_type,
                    category = EXCLUDED.category,
                    nature = EXCLUDED.nature,
                    system_scope = EXCLUDED.system_scope,
                    date_planned_start = EXCLUDED.date_planned_start,
                    date_planned_end = EXCLUDED.date_planned_end,
                    date_real_start = EXCLUDED.date_real_start,
                    date_real_end = EXCLUDED.date_real_end,
                    state = EXCLUDED.state,
                    objectives = EXCLUDED.objectives,
                    locations_visited = EXCLUDED.locations_visited,
                    auditor_names = EXCLUDED.auditor_names,
                    raw_text = EXCLUDED.raw_text,
                    source_file = EXCLUDED.source_file
                WHERE audit_reports.profile_id = EXCLUDED.profile_id
                RETURNING report_id::text
                """,
                (
                    profile_id,
                    report_ref,
                    _clean(row.get("audit_type")) or None,
                    _clean(row.get("category")) or None,
                    _clean(row.get("nature")) or None,
                    _clean(row.get("system_scope")) or None,
                    _to_date(row.get("date_planned_start")),
                    _to_date(row.get("date_planned_end")),
                    _to_date(row.get("date_real_start")),
                    _to_date(row.get("date_real_end")),
                    _clean(row.get("state")) or None,
                    _clean(row.get("objectives")) or None,
                    _clean(row.get("locations_visited")) or None,
                    _clean(row.get("auditor_names")) or None,
                    _clean(row.get("raw_text")) or None,
                    _clean(row.get("source_file")) or None,
                ),
            )
            found = cur.fetchone()
            was_inserted = True
        report_id = str(found[0])
        if was_inserted:
            report["inserted"] += 1
        else:
            report["updated"] += 1

        cur.execute(
            """
            SELECT evidence_id::text
            FROM compliance_evidence
            WHERE profile_id = %s::uuid
              AND source_report_id = %s::uuid
            LIMIT 1
            """,
            (profile_id, report_id),
        )
        evidence_row = cur.fetchone()
        evidence_params = (
            profile_id,
            report_id,
            scope_level,
            scope_key,
            scope_label,
            report_ref,
            Path(_clean(row.get("source_file")) or "").name or None,
            _clean(row.get("source_file")) or None,
            _clean(row.get("raw_text")) or None,
            _clean(row.get("audit_type")) or "AUDIT_REPORT",
            "AUDIT_REPORT",
            _to_datetime(row.get("date_real_end"))
            or _to_datetime(row.get("date_real_start"))
            or _to_datetime(row.get("date_planned_end"))
            or _to_datetime(row.get("date_planned_start")),
            actor,
            hashlib.md5(
                f"{report_ref}|{_clean(row.get('source_file'))}|{_clean(row.get('raw_text'))}".encode("utf-8")
            ).hexdigest(),
            site_id,
            process_id,
            activity_id,
        )
        if evidence_row:
            cur.execute(
                """
                UPDATE compliance_evidence
                SET scope_level = %s,
                    scope_key = %s,
                    scope_label = %s,
                    title = %s,
                    file_name = %s,
                    storage_path = %s,
                    raw_text = %s,
                    evidence_type = %s,
                    source_type = %s,
                    issued_at = %s,
                    created_by = %s,
                    input_hash = %s,
                    site_id = %s::uuid,
                    process_id = %s::uuid,
                    activity_id = %s::uuid,
                    updated_at = now()
                WHERE evidence_id = %s::uuid
                """,
                evidence_params[2:] + (str(evidence_row[0]),),
            )
        else:
            cur.execute(
                """
                INSERT INTO compliance_evidence
                    (profile_id, source_report_id, scope_level, scope_key, scope_label, title, file_name,
                     storage_path, raw_text, evidence_type, source_type, issued_at, created_by, input_hash,
                     site_id, process_id, activity_id)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::uuid, %s::uuid, %s::uuid)
                """,
                evidence_params,
            )


def _import_compliance_evidence_manifest(
    cur: psycopg.Cursor[Any],
    profile_id: str,
    tenant_id: str,
    rows: list[dict[str, Any]],
    report: dict[str, Any],
    *,
    actor: str,
) -> None:
    for index, row in enumerate(rows, start=1):
        title = _clean(row.get("title"))
        if not title:
            report["skipped"] += 1
            report["warnings"].append(f"Ligne {index}: titre de preuve manquant.")
            continue
        site_id = _resolve_site_reference(cur, profile_id, row, report, index)
        if (_clean(row.get("site_code")) or _clean(row.get("site_name"))) and not site_id:
            report["skipped"] += 1
            continue
        process_id = _resolve_process_reference(
            cur,
            profile_id,
            row,
            report,
            index,
            site_id=site_id,
            create_if_missing=False,
        )
        if (_clean(row.get("process_code")) or _clean(row.get("process_name"))) and not process_id:
            report["skipped"] += 1
            continue
        activity_id = _resolve_activity_reference(cur, profile_id, row, site_id, process_id)
        if (_clean(row.get("activity_code")) or _clean(row.get("activity_name"))) and not activity_id:
            report["warnings"].append(f"Ligne {index}: activite introuvable, scope degrade.")
        scope_level, scope_key, scope_label = _build_scope(
            site_id=site_id,
            process_id=process_id,
            activity_id=activity_id,
            scope_level=_clean(row.get("scope_level")),
            scope_label=_clean(row.get("scope_label")) or title,
        )
        requirement_reference = _clean(row.get("requirement_reference"))
        requirement_id = _resolve_requirement_id(cur, requirement_reference)
        if requirement_reference and not requirement_id:
            report["warnings"].append(f"Ligne {index}: exigence introuvable ({requirement_reference}), preuve non liee a une exigence.")

        linked_audit_reference = _clean(row.get("linked_audit_reference"))
        source_report_id = None
        if linked_audit_reference:
            cur.execute(
                """
                SELECT report_id::text
                FROM audit_reports
                WHERE profile_id = %s::uuid
                  AND LOWER(COALESCE(reference, '')) = LOWER(%s)
                LIMIT 1
                """,
                (profile_id, linked_audit_reference),
            )
            row_report = cur.fetchone()
            if row_report:
                source_report_id = str(row_report[0])
            else:
                report["warnings"].append(
                    f"Ligne {index}: audit lie introuvable ({linked_audit_reference}), preuve conservee sans rattachement audit."
                )

        storage_path = _clean(row.get("storage_path"))
        file_name = _clean(row.get("file_name")) or (Path(storage_path).name if storage_path else None)
        evidence_type = _clean(row.get("evidence_type")) or "A3_EVIDENCE"
        source_type = _clean(row.get("source_type")) or "IMPORT_MANIFEST"
        fingerprint = hashlib.md5(
            f"{title}|{file_name or ''}|{storage_path}|{scope_key}|{tenant_id}".encode("utf-8")
        ).hexdigest()
        cur.execute(
            """
            SELECT evidence_id::text
            FROM compliance_evidence
            WHERE profile_id = %s::uuid
              AND scope_key = %s
              AND (
                    (COALESCE(%s, '') <> '' AND LOWER(COALESCE(storage_path, '')) = LOWER(%s))
                 OR (COALESCE(%s, '') <> '' AND LOWER(COALESCE(file_name, '')) = LOWER(%s))
                 OR LOWER(COALESCE(title, '')) = LOWER(%s)
              )
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (
                profile_id,
                scope_key,
                storage_path or "",
                storage_path or "",
                file_name or "",
                file_name or "",
                title,
            ),
        )
        found = cur.fetchone()
        params = (
            profile_id,
            requirement_id,
            source_report_id,
            site_id,
            process_id,
            activity_id,
            scope_level,
            scope_key,
            scope_label,
            title,
            file_name,
            None,
            storage_path or None,
            _clean(row.get("raw_text")) or None,
            evidence_type,
            source_type,
            _to_datetime(row.get("issued_at")),
            _clean(row.get("created_by")) or actor,
            fingerprint,
        )
        if found:
            cur.execute(
                """
                UPDATE compliance_evidence
                SET requirement_id = %s::uuid,
                    source_report_id = %s::uuid,
                    site_id = %s::uuid,
                    process_id = %s::uuid,
                    activity_id = %s::uuid,
                    scope_level = %s,
                    scope_key = %s,
                    scope_label = %s,
                    title = %s,
                    file_name = %s,
                    mime_type = %s,
                    storage_path = %s,
                    raw_text = %s,
                    evidence_type = %s,
                    source_type = %s,
                    issued_at = %s,
                    created_by = %s,
                    input_hash = %s,
                    updated_at = now()
                WHERE evidence_id = %s::uuid
                """,
                params[1:] + (str(found[0]),),
            )
            report["updated"] += 1
        else:
            cur.execute(
                """
                INSERT INTO compliance_evidence
                    (profile_id, requirement_id, source_report_id, site_id, process_id, activity_id,
                     scope_level, scope_key, scope_label, title, file_name, mime_type, storage_path, raw_text,
                     evidence_type, source_type, issued_at, created_by, input_hash)
                VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                params,
            )
            report["inserted"] += 1
