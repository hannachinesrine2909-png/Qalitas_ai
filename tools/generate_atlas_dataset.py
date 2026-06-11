from __future__ import annotations

import csv
import textwrap
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT / "DataSet" / "Atlas"
TENANT_ID = "atlas_revetement_demo"


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _draw_wrapped_text(
    pdf: canvas.Canvas,
    text: str,
    *,
    x: float,
    y: float,
    width_chars: int = 95,
    font_name: str = "Helvetica",
    font_size: int = 10,
    leading: int = 14,
) -> float:
    pdf.setFont(font_name, font_size)
    cursor_y = y
    for raw_line in text.splitlines():
        wrapped = textwrap.wrap(raw_line, width=width_chars) or [""]
        for line in wrapped:
            if cursor_y < 72:
                pdf.showPage()
                pdf.setFont(font_name, font_size)
                cursor_y = A4[1] - 72
            pdf.drawString(x, cursor_y, line)
            cursor_y -= leading
    return cursor_y


def write_pdf(path: Path, title: str, subtitle: str, sections: list[tuple[str, list[str]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.setTitle(title)
    y = A4[1] - 56
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(48, y, title)
    y -= 24
    pdf.setFont("Helvetica", 10)
    y = _draw_wrapped_text(pdf, subtitle, x=48, y=y, width_chars=100, font_size=10, leading=14)
    y -= 10
    for heading, paragraphs in sections:
        if y < 120:
            pdf.showPage()
            y = A4[1] - 56
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(48, y, heading)
        y -= 18
        for paragraph in paragraphs:
            y = _draw_wrapped_text(pdf, paragraph, x=48, y=y, width_chars=98, font_size=10, leading=14)
            y -= 8
        y -= 4
    pdf.save()


def build_company_profile() -> None:
    fieldnames = [
        "tenant_id",
        "company_name",
        "sector",
        "sub_sector",
        "country",
        "certifications",
        "headcount",
        "main_activities",
        "admin_username",
        "admin_password",
        "admin_display_name",
        "admin_role",
    ]
    rows = [
        {
            "tenant_id": TENANT_ID,
            "company_name": "Atlas Revetement Industriel",
            "sector": "Metallurgie",
            "sub_sector": "Traitement de surface et peinture industrielle",
            "country": "TN",
            "certifications": "ISO9001|ISO14001|ISO45001",
            "headcount": "185",
            "main_activities": "traitement_surface|peinture_epoxy|stockage_solvants|gestion_dechets",
            "admin_username": "qhse@atlas.local",
            "admin_password": "AtlasQhse@123",
            "admin_display_name": "Responsable QHSE Atlas",
            "admin_role": "ADMIN_QHSE",
        }
    ]
    write_csv(DATASET_DIR / "company_profile.csv", fieldnames, rows)


def build_sites() -> None:
    fieldnames = ["site_code", "name", "city", "region", "type", "employee_count", "main_activities"]
    rows = [
        {
            "site_code": "AT1",
            "name": "Usine Mghira",
            "city": "Ben Arous",
            "region": "Mghira",
            "type": "Site principal",
            "employee_count": "135",
            "main_activities": "traitement_surface|peinture_epoxy|maintenance",
        },
        {
            "site_code": "AT2",
            "name": "Depot Bouargoub",
            "city": "Bouargoub",
            "region": "Nabeul",
            "type": "Depot logistique",
            "employee_count": "28",
            "main_activities": "stockage_solvants|stockage_dechets|expedition",
        },
        {
            "site_code": "AT3",
            "name": "Siege Tunis",
            "city": "Tunis",
            "region": "Lac 1",
            "type": "Siege",
            "employee_count": "22",
            "main_activities": "direction|qse|achats|veille_reglementaire",
        },
    ]
    write_csv(DATASET_DIR / "sites.csv", fieldnames, rows)


def build_processes() -> None:
    fieldnames = ["site_code", "process_code", "name", "description"]
    rows = [
        {"site_code": "AT1", "process_code": "PROC-SURF", "name": "Traitement de surface", "description": "Degraissage, phosphatation et preparation des pieces avant peinture"},
        {"site_code": "AT1", "process_code": "PROC-PAINT", "name": "Peinture industrielle", "description": "Application epoxy, sechage et controle visuel final"},
        {"site_code": "AT1", "process_code": "PROC-MAINT", "name": "Maintenance", "description": "Maintenance preventive du compresseur, de la cabine et des utilites"},
        {"site_code": "AT2", "process_code": "PROC-LOG", "name": "Logistique solvants et dechets", "description": "Reception solvants, stockage, expedition et gestion des dechets dangereux"},
        {"site_code": "AT3", "process_code": "PROC-QHSE", "name": "Pilotage QHSE", "description": "Veille reglementaire, audits, plan d'action et suivi des formations"},
    ]
    write_csv(DATASET_DIR / "processes.csv", fieldnames, rows)


def build_activities() -> None:
    fieldnames = ["site_code", "process_code", "process_name", "code", "name", "description"]
    rows = [
        {"site_code": "AT1", "process_code": "PROC-SURF", "process_name": "Traitement de surface", "code": "ACT-DEG", "name": "Degraissage alcalin", "description": "Nettoyage des pieces avant phosphatation"},
        {"site_code": "AT1", "process_code": "PROC-SURF", "process_name": "Traitement de surface", "code": "ACT-PHOS", "name": "Phosphatation", "description": "Preparation anticorrosion avec bains chimiques"},
        {"site_code": "AT1", "process_code": "PROC-PAINT", "process_name": "Peinture industrielle", "code": "ACT-PAINT", "name": "Application peinture epoxy", "description": "Application manuelle et automatique de peinture epoxy"},
        {"site_code": "AT1", "process_code": "PROC-PAINT", "process_name": "Peinture industrielle", "code": "ACT-CABINE", "name": "Cabine de cuisson", "description": "Cuisson et polymerisation des pieces peintes"},
        {"site_code": "AT1", "process_code": "PROC-MAINT", "process_name": "Maintenance", "code": "ACT-COMP", "name": "Maintenance compresseur", "description": "Controle et entretien du compresseur principal"},
        {"site_code": "AT2", "process_code": "PROC-LOG", "process_name": "Logistique solvants et dechets", "code": "ACT-STOCKSOLV", "name": "Stockage solvants", "description": "Stockage en local ferme des solvants inflammables"},
        {"site_code": "AT2", "process_code": "PROC-LOG", "process_name": "Logistique solvants et dechets", "code": "ACT-WASTE", "name": "Gestion des dechets dangereux", "description": "Conditionnement et expedition des dechets souilles par solvants"},
        {"site_code": "AT3", "process_code": "PROC-QHSE", "process_name": "Pilotage QHSE", "code": "ACT-VEILLE", "name": "Veille reglementaire", "description": "Mise a jour du registre et evaluation des obligations"},
        {"site_code": "AT3", "process_code": "PROC-QHSE", "process_name": "Pilotage QHSE", "code": "ACT-FORM", "name": "Gestion des formations", "description": "Planification et preuves de formation des operateurs exposes"},
    ]
    write_csv(DATASET_DIR / "activities.csv", fieldnames, rows)


def build_products() -> None:
    fieldnames = ["reference", "designation", "family", "category", "product_type", "nature", "unit", "site_name", "is_active"]
    rows = [
        {"reference": "ARI-001", "designation": "Chassis metallique epoxy bleu", "family": "Produit fini", "category": "Peinture", "product_type": "Produit", "nature": "Fabrication", "unit": "piece", "site_name": "Usine Mghira", "is_active": "true"},
        {"reference": "ARI-002", "designation": "Armoire electrique protection anticorrosion", "family": "Produit fini", "category": "Traitement de surface", "product_type": "Produit", "nature": "Fabrication", "unit": "piece", "site_name": "Usine Mghira", "is_active": "true"},
        {"reference": "ARI-003", "designation": "Support tubulaire peinture haute tenue", "family": "Produit fini", "category": "Peinture", "product_type": "Produit", "nature": "Fabrication", "unit": "piece", "site_name": "Usine Mghira", "is_active": "true"},
    ]
    write_csv(DATASET_DIR / "products.csv", fieldnames, rows)


def build_chemicals() -> None:
    fieldnames = ["designation"]
    rows = [
        {"designation": "Xylene"},
        {"designation": "Acetone"},
        {"designation": "Solvant epoxy SR-12"},
        {"designation": "Acide phosphorique"},
        {"designation": "Soude caustique"},
        {"designation": "Degraissant alcalin D-40"},
    ]
    write_csv(DATASET_DIR / "chemicals.csv", fieldnames, rows)


def build_environmental_aspects() -> None:
    fieldnames = ["site_code", "aspect_code", "designation", "domain", "sub_domain", "description"]
    rows = [
        {"site_code": "AT1", "aspect_code": "ASP-001", "designation": "Emission de COV", "domain": "Air", "sub_domain": "Rejets atmospheriques", "description": "Emissions de solvants pendant l'application epoxy"},
        {"site_code": "AT1", "aspect_code": "ASP-002", "designation": "Effluents de phosphatation", "domain": "Eau", "sub_domain": "Effluents industriels", "description": "Rinages chimiques provenant des bains de phosphatation"},
        {"site_code": "AT2", "aspect_code": "ASP-003", "designation": "Dechets dangereux souilles", "domain": "Dechets", "sub_domain": "Stockage temporaire", "description": "Futs vides, chiffons souilles et boues de peinture"},
        {"site_code": "AT1", "aspect_code": "ASP-004", "designation": "Bruit compresseur", "domain": "Bruit", "sub_domain": "Utilites", "description": "Niveau sonore eleve dans le local compresseur"},
        {"site_code": "AT2", "aspect_code": "ASP-005", "designation": "Risque de fuite solvants", "domain": "Sol", "sub_domain": "Pollution accidentelle", "description": "Fuite de solvants lors du stockage ou du transvasement"},
    ]
    write_csv(DATASET_DIR / "environmental_aspects.csv", fieldnames, rows)


def build_sst_risks() -> None:
    fieldnames = [
        "site_code",
        "risk_code",
        "risk_type",
        "designation",
        "domain",
        "dangers",
        "activities",
        "dangerous_situations",
        "damages",
        "description",
    ]
    rows = [
        {"site_code": "AT1", "risk_code": "RSK-001", "risk_type": "Chimique", "designation": "Inhalation de solvants", "domain": "SST", "dangers": "vapeurs de xylene et acetone", "activities": "Application peinture epoxy", "dangerous_situations": "exposition en cabine ou lors du melange", "damages": "irritation|maux de tete|effets chroniques", "description": "Risque d'exposition par inhalation en zone peinture"},
        {"site_code": "AT1", "risk_code": "RSK-002", "risk_type": "Incendie", "designation": "Incendie en depot solvants", "domain": "SST", "dangers": "produits inflammables", "activities": "Stockage solvants", "dangerous_situations": "futs ouverts ou etiquettes illisibles", "damages": "brulures|dommages materiels", "description": "Risque d'incendie lors du stockage et des transferts"},
        {"site_code": "AT1", "risk_code": "RSK-003", "risk_type": "Mecanique", "designation": "Projection lors maintenance compresseur", "domain": "SST", "dangers": "pieces sous pression", "activities": "Maintenance compresseur", "dangerous_situations": "depose sans consignation", "damages": "projection|coupure", "description": "Risque lors des interventions de maintenance"},
        {"site_code": "AT2", "risk_code": "RSK-004", "risk_type": "Manutention", "designation": "Manutention de futs", "domain": "SST", "dangers": "charge lourde et encombrante", "activities": "Gestion des dechets dangereux", "dangerous_situations": "deplacement manuel de futs", "damages": "TMS|chute|ecrasement", "description": "Risque de manutention dans le depot"},
        {"site_code": "AT3", "risk_code": "RSK-005", "risk_type": "Organisationnel", "designation": "Non maitrise de la veille", "domain": "SST", "dangers": "obligation non suivie", "activities": "Veille reglementaire", "dangerous_situations": "absence de mise a jour du registre", "damages": "non-conformite|retard", "description": "Risque organisationnel sur les obligations QHSE"},
    ]
    write_csv(DATASET_DIR / "sst_risks.csv", fieldnames, rows)


def build_sst_significant_risks() -> None:
    fieldnames = [
        "site_code",
        "risk_code",
        "year",
        "activities",
        "domain",
        "risk_type",
        "dangers",
        "appreciation",
        "score",
        "date_start",
        "date_end",
        "obligations",
        "exposure",
        "prevention_efficiency",
        "rpn_efficiency",
    ]
    rows = [
        {"site_code": "AT1", "risk_code": "RSK-001", "year": "2026", "activities": "Application peinture epoxy", "domain": "SST", "risk_type": "Chimique", "dangers": "vapeurs de solvants", "appreciation": "4(G)/4(F)/3(N)", "score": "16", "date_start": "2026-01-01", "date_end": "2026-12-31", "obligations": "Formation annuelle|visite medicale|ventilation", "exposure": "Continue", "prevention_efficiency": "68", "rpn_efficiency": "3.2"},
        {"site_code": "AT1", "risk_code": "RSK-002", "year": "2026", "activities": "Stockage solvants", "domain": "SST", "risk_type": "Incendie", "dangers": "liquides inflammables", "appreciation": "5(G)/3(F)/3(N)", "score": "15", "date_start": "2026-01-01", "date_end": "2026-12-31", "obligations": "Extincteurs|inspection hebdomadaire|retention", "exposure": "Frequente", "prevention_efficiency": "72", "rpn_efficiency": "3.5"},
        {"site_code": "AT2", "risk_code": "RSK-004", "year": "2026", "activities": "Gestion des dechets dangereux", "domain": "SST", "risk_type": "Manutention", "dangers": "futs et conteneurs", "appreciation": "3(G)/3(F)/3(N)", "score": "9", "date_start": "2026-01-01", "date_end": "2026-12-31", "obligations": "stockage separe|bordereau|aide a la manutention", "exposure": "Reguliere", "prevention_efficiency": "75", "rpn_efficiency": "2.8"},
    ]
    write_csv(DATASET_DIR / "sst_significant_risks.csv", fieldnames, rows)


def build_strategic_objectives() -> None:
    fieldnames = [
        "objective_text",
        "process_name",
        "indicator",
        "indicator_type",
        "frequency",
        "calculation_method",
        "system_scope",
        "unit",
        "strategic_axis",
    ]
    rows = [
        {"objective_text": "Porter a 100 pour cent la validation des exigences critiques", "process_name": "Pilotage QHSE", "indicator": "Taux de validation des exigences critiques", "indicator_type": "Pilotage", "frequency": "Mensuelle", "calculation_method": "(exigences_critique_validees / exigences_critique_total) * 100", "system_scope": "QHSE", "unit": "%", "strategic_axis": "Conformite reglementaire"},
        {"objective_text": "Reduire les incidents solvants de 30 pour cent", "process_name": "Logistique solvants et dechets", "indicator": "Nombre d'incidents solvants", "indicator_type": "SST", "frequency": "Mensuelle", "calculation_method": "compte incidents solvants par mois", "system_scope": "SST", "unit": "incident", "strategic_axis": "Prevention"},
        {"objective_text": "Avoir 100 pour cent des bordereaux de dechets archives sous 48 heures", "process_name": "Logistique solvants et dechets", "indicator": "Taux d'archivage bordereaux", "indicator_type": "Operationnel", "frequency": "Hebdomadaire", "calculation_method": "(bordereaux archives sous 48h / bordereaux emis) * 100", "system_scope": "Environnement", "unit": "%", "strategic_axis": "Traçabilite"},
    ]
    write_csv(DATASET_DIR / "strategic_objectives.csv", fieldnames, rows)


def build_equipment() -> None:
    fieldnames = [
        "site_code",
        "internal_code",
        "designation",
        "nature",
        "equipment_type",
        "category",
        "location",
        "serial_number",
        "state",
        "brand",
        "model",
        "specific_data",
        "last_intervention",
        "next_intervention",
    ]
    rows = [
        {"site_code": "AT1", "internal_code": "EQ-CAB-01", "designation": "Cabine peinture ligne 1", "nature": "Equipement", "equipment_type": "Production", "category": "Cabine peinture", "location": "Atelier peinture", "serial_number": "CAB-AT1-001", "state": "Actif", "brand": "SprayTech", "model": "PX-900", "specific_data": "Ventilation mecanique et filtration", "last_intervention": "2026-03-18", "next_intervention": "2026-09-18"},
        {"site_code": "AT1", "internal_code": "EQ-COMP-01", "designation": "Compresseur principal", "nature": "Equipement", "equipment_type": "Utilite", "category": "Compresseur", "location": "Local compresseur", "serial_number": "COMP-AT1-010", "state": "Actif", "brand": "Atlas Copco", "model": "GA37", "specific_data": "Air comprime atelier peinture", "last_intervention": "2026-03-05", "next_intervention": "2027-03-05"},
        {"site_code": "AT2", "internal_code": "EQ-RET-01", "designation": "Bac de retention mobile", "nature": "Equipement", "equipment_type": "Securite", "category": "Retention", "location": "Depot solvants", "serial_number": "RET-AT2-002", "state": "Actif", "brand": "SafeStore", "model": "RET240", "specific_data": "Capacite 240 litres", "last_intervention": "2026-02-11", "next_intervention": "2027-02-11"},
        {"site_code": "AT2", "internal_code": "EQ-EXT-01", "designation": "Extincteurs depot solvants", "nature": "Equipement", "equipment_type": "Securite", "category": "Incendie", "location": "Depot solvants", "serial_number": "EXT-AT2-100", "state": "Actif", "brand": "SecurFire", "model": "ABC9", "specific_data": "Lot de 4 extincteurs a poudre", "last_intervention": "2026-02-08", "next_intervention": "2026-08-08"},
        {"site_code": "AT1", "internal_code": "EQ-VOC-01", "designation": "Detection COV cabine peinture", "nature": "Equipement", "equipment_type": "Securite", "category": "Detection", "location": "Atelier peinture", "serial_number": "VOC-AT1-001", "state": "Projet", "brand": "EnviroSense", "model": "VOC-2027", "specific_data": "Exigence applicable a partir de 2027", "last_intervention": "", "next_intervention": "2027-01-15"},
    ]
    write_csv(DATASET_DIR / "equipment.csv", fieldnames, rows)


def build_legal_manifest() -> None:
    fieldnames = [
        "file_name",
        "document_title",
        "jurisdiction",
        "document_type",
        "publisher",
        "publication_date",
        "effective_date",
        "language",
        "notes",
    ]
    rows = [
        {
            "file_name": "ATLAS_Recueil_reglementaire_traitement_surface_2026.pdf",
            "document_title": "Recueil reglementaire Atlas - traitement de surface et solvants",
            "jurisdiction": "TN",
            "document_type": "Recueil reglementaire de demonstration",
            "publisher": "Veille interne Atlas",
            "publication_date": "2026-01-10",
            "effective_date": "2026-01-10",
            "language": "fr",
            "notes": "PDF de demonstration a charger dans A1 pour extraction et validation",
        }
    ]
    write_csv(DATASET_DIR / "legal_documents_manifest.csv", fieldnames, rows)


def build_audit_reports_metadata() -> None:
    fieldnames = [
        "reference",
        "audit_type",
        "category",
        "nature",
        "system_scope",
        "date_planned_start",
        "date_planned_end",
        "date_real_start",
        "date_real_end",
        "state",
        "objectives",
        "locations_visited",
        "auditor_names",
        "source_file",
        "scope_level",
        "site_code",
        "process_code",
        "activity_code",
    ]
    rows = [
        {
            "reference": "ATLAS-AUD-001",
            "audit_type": "Audit interne conformite stockage solvants",
            "category": "Conformite",
            "nature": "Audit Interne",
            "system_scope": "QHSE",
            "date_planned_start": "2026-04-05",
            "date_planned_end": "2026-04-05",
            "date_real_start": "2026-04-05",
            "date_real_end": "2026-04-05",
            "state": "TERMINE",
            "objectives": "Verifier le registre depot solvants, les etiquettes et le stockage exterieur",
            "locations_visited": "Depot Bouargoub",
            "auditor_names": "Leila Ben Salem|Walid Gharbi",
            "source_file": "audits/ATLAS-AUD-001.pdf",
            "scope_level": "SITE",
            "site_code": "AT2",
            "process_code": "PROC-LOG",
            "activity_code": "ACT-STOCKSOLV",
        },
        {
            "reference": "ATLAS-AUD-002",
            "audit_type": "Inspection incendie et evacuation",
            "category": "Securite",
            "nature": "Inspection",
            "system_scope": "SST",
            "date_planned_start": "2026-04-12",
            "date_planned_end": "2026-04-12",
            "date_real_start": "2026-04-12",
            "date_real_end": "2026-04-12",
            "state": "TERMINE",
            "objectives": "Verifier extincteurs, affichage evacuation et traces de formation solvants",
            "locations_visited": "Usine Mghira",
            "auditor_names": "Nadia Kacem",
            "source_file": "audits/ATLAS-AUD-002.pdf",
            "scope_level": "PROCESS",
            "site_code": "AT1",
            "process_code": "PROC-PAINT",
            "activity_code": "ACT-PAINT",
        },
        {
            "reference": "ATLAS-AUD-003",
            "audit_type": "Revue suivi medical et incidents",
            "category": "Sante",
            "nature": "Revue Interne",
            "system_scope": "QHSE",
            "date_planned_start": "2026-04-18",
            "date_planned_end": "2026-04-18",
            "date_real_start": "2026-04-18",
            "date_real_end": "2026-04-18",
            "state": "TERMINE",
            "objectives": "Verifier visites medicales annuelles et tenue du registre incidents solvants",
            "locations_visited": "Usine Mghira|Siege Tunis",
            "auditor_names": "Amine Trabelsi|Sarra Jaziri",
            "source_file": "audits/ATLAS-AUD-003.pdf",
            "scope_level": "SYSTEM",
            "site_code": "AT1",
            "process_code": "PROC-QHSE",
            "activity_code": "ACT-VEILLE",
        },
    ]
    write_csv(DATASET_DIR / "audit_reports_metadata.csv", fieldnames, rows)


def build_nonconformities() -> None:
    fieldnames = [
        "site_code",
        "reference",
        "year",
        "nature",
        "process_name",
        "title",
        "source",
        "audit_type",
        "responsible_service",
        "detected_at",
        "state",
        "severity",
        "nc_type",
        "frequency",
        "nc_category",
        "gravity",
        "priority",
        "closed_at",
        "system_scope",
        "progress_pct",
        "closure_rate",
    ]
    rows = [
        {
            "site_code": "AT2",
            "reference": "ATLAS-NC-001",
            "year": "2026",
            "nature": "Reelle",
            "process_name": "Logistique solvants et dechets",
            "title": "Stockage exterieur de futs sans bac de retention",
            "source": "ATLAS-AUD-001",
            "audit_type": "Audit Interne",
            "responsible_service": "Logistique",
            "detected_at": "2026-04-05",
            "state": "EN_COURS",
            "severity": "Majeure",
            "nc_type": "Securite",
            "frequency": "Occasionnelle",
            "nc_category": "Reglementaire",
            "gravity": "Critique",
            "priority": "Haute",
            "closed_at": "",
            "system_scope": "SST",
            "progress_pct": "30",
            "closure_rate": "15",
        },
        {
            "site_code": "AT1",
            "reference": "ATLAS-NC-002",
            "year": "2026",
            "nature": "Reelle",
            "process_name": "Pilotage QHSE",
            "title": "Visites medicales annuelles incompletes pour les operateurs exposes",
            "source": "ATLAS-AUD-003",
            "audit_type": "Revue Interne",
            "responsible_service": "RH|QHSE",
            "detected_at": "2026-04-18",
            "state": "EN_COURS",
            "severity": "Majeure",
            "nc_type": "Sante",
            "frequency": "Recurrente",
            "nc_category": "Reglementaire",
            "gravity": "Majeure",
            "priority": "Haute",
            "closed_at": "",
            "system_scope": "SST",
            "progress_pct": "20",
            "closure_rate": "10",
        },
        {
            "site_code": "AT1",
            "reference": "ATLAS-NC-003",
            "year": "2026",
            "nature": "Reelle",
            "process_name": "Pilotage QHSE",
            "title": "Registre des incidents de fuite non renseigne sous 24 heures",
            "source": "ATLAS-AUD-003",
            "audit_type": "Revue Interne",
            "responsible_service": "QHSE",
            "detected_at": "2026-04-18",
            "state": "EN_COURS",
            "severity": "Mineure",
            "nc_type": "Documentaire",
            "frequency": "Occasionnelle",
            "nc_category": "Reglementaire",
            "gravity": "Majeure",
            "priority": "Moyenne",
            "closed_at": "",
            "system_scope": "QHSE",
            "progress_pct": "10",
            "closure_rate": "5",
        },
        {
            "site_code": "AT1",
            "reference": "ATLAS-NC-004",
            "year": "2026",
            "nature": "Reelle",
            "process_name": "Pilotage QHSE",
            "title": "Formation solvants non renouvelee en 2026",
            "source": "ATLAS-AUD-002",
            "audit_type": "Inspection",
            "responsible_service": "QHSE",
            "detected_at": "2026-04-12",
            "state": "EN_COURS",
            "severity": "Mineure",
            "nc_type": "Formation",
            "frequency": "Annuelle",
            "nc_category": "SST",
            "gravity": "Majeure",
            "priority": "Moyenne",
            "closed_at": "",
            "system_scope": "SST",
            "progress_pct": "15",
            "closure_rate": "5",
        },
    ]
    write_csv(DATASET_DIR / "nonconformities.csv", fieldnames, rows)


def build_evidence_manifest() -> None:
    fieldnames = [
        "title",
        "file_name",
        "storage_path",
        "evidence_type",
        "source_type",
        "requirement_reference",
        "scope_level",
        "site_code",
        "process_code",
        "activity_code",
        "issued_at",
        "created_by",
        "linked_audit_reference",
    ]
    rows = [
        {
            "title": "Registre hebdomadaire depot solvants",
            "file_name": "ATLAS-PR-001_Registre_inspection_depot_solvants.pdf",
            "storage_path": "preuves/ATLAS-PR-001_Registre_inspection_depot_solvants.pdf",
            "evidence_type": "inspection_document",
            "source_type": "UPLOAD_PDF",
            "requirement_reference": "Article 1",
            "scope_level": "SITE",
            "site_code": "AT2",
            "process_code": "PROC-LOG",
            "activity_code": "ACT-STOCKSOLV",
            "issued_at": "2026-04-12T00:00:00Z",
            "created_by": "qhse@atlas.local",
            "linked_audit_reference": "ATLAS-AUD-001",
        },
        {
            "title": "Bordereaux dechets dangereux 2026",
            "file_name": "ATLAS-PR-002_Bordereaux_dechets_dangereux_2026.pdf",
            "storage_path": "preuves/ATLAS-PR-002_Bordereaux_dechets_dangereux_2026.pdf",
            "evidence_type": "compliance_document",
            "source_type": "UPLOAD_PDF",
            "requirement_reference": "Article 2",
            "scope_level": "ACTIVITY",
            "site_code": "AT2",
            "process_code": "PROC-LOG",
            "activity_code": "ACT-WASTE",
            "issued_at": "2026-03-22T00:00:00Z",
            "created_by": "qhse@atlas.local",
            "linked_audit_reference": "ATLAS-AUD-001",
        },
        {
            "title": "Verification extincteurs atelier peinture et depot solvants",
            "file_name": "ATLAS-PR-003_Verification_extincteurs_2026.pdf",
            "storage_path": "preuves/ATLAS-PR-003_Verification_extincteurs_2026.pdf",
            "evidence_type": "inspection_report",
            "source_type": "UPLOAD_PDF",
            "requirement_reference": "Article 3",
            "scope_level": "PROCESS",
            "site_code": "AT1",
            "process_code": "PROC-PAINT",
            "activity_code": "ACT-PAINT",
            "issued_at": "2026-02-08T00:00:00Z",
            "created_by": "qhse@atlas.local",
            "linked_audit_reference": "ATLAS-AUD-002",
        },
        {
            "title": "Plan d'evacuation atelier peinture",
            "file_name": "ATLAS-PR-004_Plan_evacuation_atelier_peinture.pdf",
            "storage_path": "preuves/ATLAS-PR-004_Plan_evacuation_atelier_peinture.pdf",
            "evidence_type": "display_document",
            "source_type": "UPLOAD_PDF",
            "requirement_reference": "Article 8",
            "scope_level": "PROCESS",
            "site_code": "AT1",
            "process_code": "PROC-PAINT",
            "activity_code": "ACT-PAINT",
            "issued_at": "2026-01-15T00:00:00Z",
            "created_by": "qhse@atlas.local",
            "linked_audit_reference": "ATLAS-AUD-002",
        },
        {
            "title": "Rapport annuel controle compresseur principal",
            "file_name": "ATLAS-PR-005_Controle_compresseur_2026.pdf",
            "storage_path": "preuves/ATLAS-PR-005_Controle_compresseur_2026.pdf",
            "evidence_type": "maintenance_report",
            "source_type": "UPLOAD_PDF",
            "requirement_reference": "Article 9",
            "scope_level": "ACTIVITY",
            "site_code": "AT1",
            "process_code": "PROC-MAINT",
            "activity_code": "ACT-COMP",
            "issued_at": "2026-03-05T00:00:00Z",
            "created_by": "qhse@atlas.local",
            "linked_audit_reference": "",
        },
        {
            "title": "Feuille de formation solvants 2024",
            "file_name": "ATLAS-PR-006_Formation_solvants_2024.pdf",
            "storage_path": "preuves/ATLAS-PR-006_Formation_solvants_2024.pdf",
            "evidence_type": "training_document",
            "source_type": "UPLOAD_PDF",
            "requirement_reference": "Article 4",
            "scope_level": "SYSTEM",
            "site_code": "AT3",
            "process_code": "PROC-QHSE",
            "activity_code": "ACT-FORM",
            "issued_at": "2024-02-20T00:00:00Z",
            "created_by": "qhse@atlas.local",
            "linked_audit_reference": "ATLAS-AUD-002",
        },
        {
            "title": "Checklist etiquettes contenants solvants semaine 15",
            "file_name": "ATLAS-PR-007_Checklist_etiquettes_solvants.pdf",
            "storage_path": "preuves/ATLAS-PR-007_Checklist_etiquettes_solvants.pdf",
            "evidence_type": "inspection_document",
            "source_type": "UPLOAD_PDF",
            "requirement_reference": "Article 11",
            "scope_level": "SITE",
            "site_code": "AT2",
            "process_code": "PROC-LOG",
            "activity_code": "ACT-STOCKSOLV",
            "issued_at": "2026-04-14T00:00:00Z",
            "created_by": "qhse@atlas.local",
            "linked_audit_reference": "ATLAS-AUD-001",
        },
    ]
    write_csv(DATASET_DIR / "compliance_evidence_manifest.csv", fieldnames, rows)


def build_legal_pdf() -> None:
    sections = [
        (
            "Article 1 - Registre depot solvants",
            [
                "L'employeur doit tenir a jour un registre hebdomadaire d'inspection du depot solvants. Le registre doit mentionner la date, le nom de l'inspecteur, l'etat des bacs de retention, la lisibilite des etiquettes et les actions immediates engagees.",
            ],
        ),
        (
            "Article 2 - Bordereaux dechets dangereux",
            [
                "L'employeur doit conserver pendant au moins trois ans les bordereaux de suivi des dechets dangereux evacues hors site. Les bordereaux doivent etre classes par date et facilement presentables lors de tout audit ou controle.",
            ],
        ),
        (
            "Article 3 - Verification des extincteurs",
            [
                "L'employeur doit faire verifier tous les six mois les extincteurs utilises pour la protection de l'atelier peinture et du depot solvants. Le rapport de verification doit etre date, signe et archivé.",
            ],
        ),
        (
            "Article 4 - Formation solvants",
            [
                "Le responsable QHSE doit organiser au moins une formation annuelle a la manipulation des solvants inflammables pour tout operateur expose. La formation doit etre tracee par une feuille de presence et un support de contenu.",
            ],
        ),
        (
            "Article 5 - Visite medicale annuelle",
            [
                "Le medecin du travail doit realiser une visite medicale annuelle pour chaque travailleur expose aux solvants et aux brouillards de peinture. L'employeur doit conserver la preuve de planification et de realisation.",
            ],
        ),
        (
            "Article 6 - Stockage exterieur interdit sans retention",
            [
                "Il est interdit de stocker a l'exterieur des futs de solvants dangereux sans bac de retention et sans protection contre la pluie.",
            ],
        ),
        (
            "Article 7 - Registre incident fuite",
            [
                "Tout incident de fuite de solvant doit etre enregistre dans les vingt-quatre heures dans un registre d'incident. Le registre doit preciser la cause, le volume estime, les mesures prises et le responsable de traitement.",
            ],
        ),
        (
            "Article 8 - Affichage evacuation incendie",
            [
                "L'employeur doit afficher le plan d'evacuation et la consigne incendie a chaque entree de l'atelier peinture, du depot solvants et du local compresseur.",
            ],
        ),
        (
            "Article 9 - Controle compresseur",
            [
                "Le responsable maintenance doit faire controler annuellement le compresseur d'air principal et conserver le rapport d'intervention dans le dossier technique de l'equipement.",
            ],
        ),
        (
            "Article 10 - Detection continue des COV",
            [
                "A compter du 1 janvier 2027, l'employeur doit installer un dispositif de detection continue des composes organiques volatils dans la cabine peinture.",
            ],
        ),
        (
            "Article 11 - Etiquetage des contenants",
            [
                "L'employeur doit verifier chaque semaine la presence et la lisibilite des etiquettes de danger sur les contenants de solvants.",
            ],
        ),
        (
            "Article 12 - Tri des dechets souilles",
            [
                "Il est interdit de melanger les chiffons souilles par solvant avec les dechets banals. Les contenants de dechets dangereux doivent etre identifies separement.",
            ],
        ),
    ]
    write_pdf(
        DATASET_DIR / "legal" / "ATLAS_Recueil_reglementaire_traitement_surface_2026.pdf",
        "Recueil reglementaire Atlas 2026",
        "Document fictif de demonstration pour tester A1, A2, A3 et A4 sur Atlas Revetement Industriel. Les articles ci-dessous sont rediges pour faciliter une extraction claire des obligations applicables au site de traitement de surface et de peinture industrielle.",
        sections,
    )


def build_proof_pdfs() -> None:
    proofs = {
        "ATLAS-PR-001_Registre_inspection_depot_solvants.pdf": (
            "Registre inspection depot solvants",
            "Preuve de demonstration rattachee a l'article 1 du recueil Atlas.",
            [
                (
                    "Synthese",
                    [
                        "Registre hebdomadaire d'inspection du depot solvants pour les semaines 13 a 15 de l'annee 2026.",
                        "Inspecteur: Hatem Saidi. Bac de retention propre, etiquettes lisibles, ventilation fonctionnelle, aucune fuite visible.",
                    ],
                ),
                (
                    "Extraits",
                    [
                        "28/03/2026 - 4 futs xylene controles - etiquettes conformes - bac de retention sec - action: aucune.",
                        "04/04/2026 - 6 contenants acetone controles - extincteur present - action: remplacement d'une etiquette abimee.",
                        "11/04/2026 - controle hebdomadaire realise et signe par le superviseur logistique.",
                    ],
                ),
            ],
        ),
        "ATLAS-PR-002_Bordereaux_dechets_dangereux_2026.pdf": (
            "Bordereaux dechets dangereux 2026",
            "Preuve de demonstration rattachee a l'article 2 du recueil Atlas.",
            [
                (
                    "Historique",
                    [
                        "Bordereaux de suivi des dechets dangereux emis les 14/02/2026, 19/03/2026 et 09/04/2026.",
                        "Les bordereaux couvrent les boues de peinture, chiffons souilles et futs vides contamines. Les originaux sont classes par date au depot Bouargoub.",
                    ],
                ),
                (
                    "References",
                    [
                        "BSD-ATLAS-2026-014 - transporteur EcoHazard - 420 kg de boues de peinture.",
                        "BSD-ATLAS-2026-019 - transporteur EcoHazard - 155 kg de chiffons souilles.",
                    ],
                ),
            ],
        ),
        "ATLAS-PR-003_Verification_extincteurs_2026.pdf": (
            "Verification extincteurs 2026",
            "Preuve de demonstration rattachee a l'article 3 du recueil Atlas.",
            [
                (
                    "Rapport",
                    [
                        "Verification semestrielle realisee le 08/02/2026 sur les extincteurs de l'atelier peinture et du depot solvants.",
                        "4 extincteurs poudre ABC et 2 CO2 controles. Pression conforme, acces libre, signalisation en place. Rapport signe par PrestControl Tunisie.",
                    ],
                ),
            ],
        ),
        "ATLAS-PR-004_Plan_evacuation_atelier_peinture.pdf": (
            "Plan evacuation atelier peinture",
            "Preuve de demonstration rattachee a l'article 8 du recueil Atlas.",
            [
                (
                    "Affichage",
                    [
                        "Plan d'evacuation et consigne incendie affiches a l'entree atelier peinture, a l'entree depot solvants et devant le local compresseur.",
                        "Mise a jour affichee le 15/01/2026 par le service QHSE.",
                    ],
                ),
            ],
        ),
        "ATLAS-PR-005_Controle_compresseur_2026.pdf": (
            "Controle compresseur 2026",
            "Preuve de demonstration rattachee a l'article 9 du recueil Atlas.",
            [
                (
                    "Intervention",
                    [
                        "Controle annuel du compresseur principal realise le 05/03/2026. Nettoyage, test securite, verification pression et signature du technicien maintenance.",
                        "Aucune anomalie critique constatee. Prochaine echeance: 05/03/2027.",
                    ],
                ),
            ],
        ),
        "ATLAS-PR-006_Formation_solvants_2024.pdf": (
            "Formation solvants 2024",
            "Preuve de demonstration rattachee a l'article 4 du recueil Atlas. Ce document est volontairement ancien pour tester le vieillissement des preuves.",
            [
                (
                    "Session",
                    [
                        "Formation manipulation des solvants inflammables organisee le 20/02/2024 pour 14 operateurs de l'atelier peinture.",
                        "Themes: etiquetage, EPI, conduite a tenir en cas de fuite, stockage et tri des dechets souilles.",
                    ],
                ),
            ],
        ),
        "ATLAS-PR-007_Checklist_etiquettes_solvants.pdf": (
            "Checklist etiquettes solvants",
            "Preuve de demonstration rattachee a l'article 11 du recueil Atlas.",
            [
                (
                    "Controle",
                    [
                        "Checklist hebdomadaire du 14/04/2026 sur les contenants de solvants du depot Bouargoub.",
                        "7 contenants controles. Etiquettes lisibles, pictogrammes presents, mention du produit et date d'ouverture verifiees.",
                    ],
                ),
            ],
        ),
    }
    for file_name, (title, subtitle, sections) in proofs.items():
        write_pdf(DATASET_DIR / "preuves" / file_name, title, subtitle, sections)


def build_audit_pdfs() -> None:
    audits = {
        "ATLAS-AUD-001.pdf": (
            "Audit interne conformite stockage solvants",
            "Audit de demonstration effectue le 05/04/2026 sur le depot Bouargoub.",
            [
                (
                    "Constats positifs",
                    [
                        "Le registre hebdomadaire d'inspection du depot solvants est disponible et renseigne.",
                        "Les bordereaux de suivi des dechets dangereux de 2026 sont classes et facilement consultables.",
                        "Les etiquettes de danger des contenants controles sont lisibles.",
                    ],
                ),
                (
                    "Ecart releve",
                    [
                        "Deux futs de solvants vides ont ete observes a l'exterieur sans bac de retention et sans protection contre la pluie. Cet ecart ouvre la NC ATLAS-NC-001.",
                    ],
                ),
            ],
        ),
        "ATLAS-AUD-002.pdf": (
            "Inspection incendie et evacuation",
            "Inspection de demonstration effectuee le 12/04/2026 dans l'atelier peinture.",
            [
                (
                    "Constats positifs",
                    [
                        "Le plan d'evacuation et la consigne incendie sont affiches aux emplacements requis.",
                        "Le rapport de verification des extincteurs du 08/02/2026 est disponible et conforme.",
                    ],
                ),
                (
                    "Point de vigilance",
                    [
                        "La derniere preuve de formation annuelle solvants date du 20/02/2024. Une mise a jour 2026 est attendue. Cet ecart alimente la NC ATLAS-NC-004.",
                    ],
                ),
            ],
        ),
        "ATLAS-AUD-003.pdf": (
            "Revue suivi medical et incidents",
            "Revue de demonstration effectuee le 18/04/2026.",
            [
                (
                    "Constats",
                    [
                        "18 travailleurs exposes sur 25 disposent d'une visite medicale annuelle tracee. La couverture n'est pas complete pour 2026.",
                        "Aucun registre d'incident fuite complet n'a pu etre presente pour le premier trimestre 2026.",
                    ],
                ),
                (
                    "Impact",
                    [
                        "Les ecarts releves ouvrent les non-conformites ATLAS-NC-002 et ATLAS-NC-003.",
                    ],
                ),
            ],
        ),
    }
    for file_name, (title, subtitle, sections) in audits.items():
        write_pdf(DATASET_DIR / "audits" / file_name, title, subtitle, sections)


def build_nc_pdfs() -> None:
    nc_docs = {
        "ATLAS-NC-001.pdf": (
            "Non-conformite ATLAS-NC-001",
            "Stockage exterieur de futs sans bac de retention - ouverte le 05/04/2026.",
            [
                (
                    "Description",
                    [
                        "Lors de l'audit ATLAS-AUD-001, deux futs de solvants vides ont ete observes a l'exterieur, poses au sol, sans bac de retention et sans protection contre la pluie.",
                        "Exigence liee: interdiction de stocker a l'exterieur des futs de solvants dangereux sans retention.",
                    ],
                ),
            ],
        ),
        "ATLAS-NC-002.pdf": (
            "Non-conformite ATLAS-NC-002",
            "Visites medicales annuelles incompletes - ouverte le 18/04/2026.",
            [
                (
                    "Description",
                    [
                        "La revue ATLAS-AUD-003 montre que 7 travailleurs exposes aux solvants ne disposent pas d'une visite medicale annuelle tracee pour l'exercice 2026.",
                        "Exigence liee: visite medicale annuelle pour chaque travailleur expose.",
                    ],
                ),
            ],
        ),
        "ATLAS-NC-003.pdf": (
            "Non-conformite ATLAS-NC-003",
            "Registre incidents fuite absent - ouverte le 18/04/2026.",
            [
                (
                    "Description",
                    [
                        "Aucun registre d'incident fuite complet n'a pu etre presente pour le premier trimestre 2026.",
                        "Exigence liee: tout incident de fuite de solvant doit etre enregistre dans les vingt-quatre heures.",
                    ],
                ),
            ],
        ),
        "ATLAS-NC-004.pdf": (
            "Non-conformite ATLAS-NC-004",
            "Formation solvants non renouvelee en 2026 - ouverte le 12/04/2026.",
            [
                (
                    "Description",
                    [
                        "La seule preuve disponible de formation solvants date du 20/02/2024. Aucune session annuelle 2025 ou 2026 n'est archivee.",
                        "Exigence liee: une formation annuelle a la manipulation des solvants doit etre organisee pour tout operateur expose.",
                    ],
                ),
            ],
        ),
    }
    for file_name, (title, subtitle, sections) in nc_docs.items():
        write_pdf(DATASET_DIR / "nonconformites" / file_name, title, subtitle, sections)


def build_readme() -> None:
    lines = [
        "Pack fictif de demonstration pour Atlas Revetement Industriel",
        "",
        f"Tenant recommande : {TENANT_ID}",
        "",
        "Ce pack est volontairement plus riche que Nova :",
        "- contexte entreprise complet pour A2",
        "- corpus PDF juridique synthetique pour A1",
        "- preuves, audits et non-conformites PDF pour A3",
        "- un cas futur pour tester APPLICABLE_FUTUR",
        "",
        "Ordre conseille dans la plateforme :",
        "1. Onboarding nouvelle entreprise avec les infos de company_profile.csv",
        "2. Import CSV : sites, processes, activities, products, chemicals",
        "3. Import CSV : equipment, environmental_aspects, sst_risks, sst_significant_risks, strategic_objectives",
        "4. Import CSV : audit_reports_metadata.csv, nonconformities.csv, compliance_evidence_manifest.csv",
        "5. Upload A1 : legal/ATLAS_Recueil_reglementaire_traitement_surface_2026.pdf",
        "6. Valider les exigences dans la page Exigences",
        "7. Uploader les preuves A3 : preuves/*.pdf, audits/*.pdf, nonconformites/*.pdf",
        "8. Lancer Applicabilite puis Conformite",
        "",
        "Attendu en demonstration :",
        "- conformites visibles : registre depot solvants, bordereaux dechets, extincteurs, plan evacuation, controle compresseur, etiquetage",
        "- ecarts visibles : visites medicales annuelles, registre incidents fuite, stockage exterieur sans retention",
        "- cas de preuve expiree : formation solvants 2024",
        "- cas futur : detection continue des COV a partir du 01/01/2027",
    ]
    (DATASET_DIR / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    build_company_profile()
    build_sites()
    build_processes()
    build_activities()
    build_products()
    build_chemicals()
    build_environmental_aspects()
    build_sst_risks()
    build_sst_significant_risks()
    build_strategic_objectives()
    build_equipment()
    build_legal_manifest()
    build_audit_reports_metadata()
    build_nonconformities()
    build_evidence_manifest()
    build_legal_pdf()
    build_proof_pdfs()
    build_audit_pdfs()
    build_nc_pdfs()
    build_readme()
    print(f"Atlas dataset generated in: {DATASET_DIR}")


if __name__ == "__main__":
    main()
