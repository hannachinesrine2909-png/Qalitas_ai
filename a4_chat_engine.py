"""
a4_chat_engine.py
=================
Agent 4 â€” Chat Expert RAG (Assistance rÃ©glementaire temps rÃ©el) QALITAS.

Architecture RAG custom:
  1. Indexation: requirements DB â†’ embeddings â†’ requirement_embeddings (pgvector)
  2. Runtime:
     a. Embed la question utilisateur
     b. pgvector similarity search â†’ top-k articles rÃ©glementaires
     c. Filtre: seulement les exigences applicables au tenant (Agent 2)
     d. Prompt LLM avec: question + articles + contexte entreprise
     e. RÃ©ponse sourcÃ©e avec citations
     f. Sauvegarde en DB (traÃ§abilitÃ© complÃ¨te)

Commandes:
    python a4_chat_engine.py --index --tenant <tenant_id>        # indexe les embeddings
    python a4_chat_engine.py --chat --tenant <tenant_id>         # mode chat interactif
    python a4_chat_engine.py --ingest-audits --tenant <tenant_id> # importe les PDF d'audit

Notes pgvector:
    Si pgvector n'est pas installÃ©, l'indexation Ã©choue gracieusement.
    Le mode chat fonctionne sans pgvector (recherche textuelle de secours).
"""

import argparse
import json
import os
import re
import sys
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from llm_client import get_llm_client
from a2_scope_resolution import effective_applicability_rows
from tenant_db import connect_db

load_dotenv()

DATASET_DIR = Path("DataSet")


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _normalize_intent_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-zA-Z0-9\s']", " ", text)
    return " ".join(text.lower().split())
# Retrieval tuning:
# - SPECIFIC_TOP_K limite les questions ciblees pour eviter le bruit;
# - GENERAL_TOP_K donne plus de contexte aux questions transverses;
# - CITATION_OVERLAP_MIN sert a rejeter les citations LLM qui ne recoupent pas
#   les sources reellement recuperees.
TOP_K = 8  # Nombre d'articles recuperes par recherche
SPECIFIC_TOP_K = 4
GENERAL_TOP_K = 12
CITATION_OVERLAP_MIN = 0.60


# â”€â”€â”€ SchÃ©mas Pydantic â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class ChatResponseLLM(BaseModel):
    """Schema de sortie unique pour les reponses A4."""

    answer: str
    obligation_type: str = "MIXTE"   # OBLIGATOIRE / RECOMMANDE / MIXTE
    has_uncertainty: bool = False
    uncertainty_note: str | None = None
    source_citations: list[dict] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)


def _truncate_text(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _truncate_sentence_text(value: Any, limit: int = 1400) -> str:
    text = re.sub(r"\n{3,}", "\n\n", str(value or "").strip())
    if len(text) <= limit:
        return text

    cut = text[:limit].rstrip()
    for marker in (".\n", ". ", "\n- ", "\n1. ", "\n2. "):
        idx = cut.rfind(marker)
        if idx >= int(limit * 0.55):
            return cut[: idx + 1].rstrip() + "..."
    return cut.rstrip() + "..."


def _normalize_chat_response_shape(response: ChatResponseLLM) -> ChatResponseLLM:
    response.answer = _truncate_sentence_text(response.answer, 1400)
    response.recommended_actions = [
        _truncate_text(action, 120)
        for action in response.recommended_actions
        if str(action or "").strip()
    ][:4]
    response.source_citations = response.source_citations[:4]
    return response


def _scope_rank(scope_level: str | None) -> int:
    return {
        "ORGANIZATION": 0,
        "SITE": 1,
        "PROCESS": 2,
        "ACTIVITY": 3,
    }.get(str(scope_level or "").upper(), 9)


def _format_scope_text(row: dict[str, Any]) -> str:
    scope_label = str(row.get("scope_label") or "").strip()
    if scope_label and scope_label.upper() != "ORGANIZATION":
        return scope_label

    parts: list[str] = []
    site = str(row.get("scope_site") or row.get("site_name") or "").strip()
    process = str(row.get("scope_process") or row.get("process_name") or "").strip()
    activity = str(row.get("scope_activity") or row.get("activity_name") or "").strip()
    if site:
        parts.append(f"Site: {site}")
    if process:
        parts.append(f"Processus: {process}")
    if activity:
        parts.append(f"Activite: {activity}")
    if not parts:
        return "Organisation"
    return " | ".join(parts)


def _counts_to_text(counts: dict[str, int]) -> str:
    if not counts:
        return "aucune donnee"
    ordered = sorted(counts.items(), key=lambda item: (-int(item[1] or 0), str(item[0])))
    return ", ".join(f"{label}={value}" for label, value in ordered)


def _snapshot_int(snapshot: dict[str, Any], key: str) -> int:
    try:
        return int(snapshot.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _snapshot_float(snapshot: dict[str, Any], key: str) -> float | None:
    raw = snapshot.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _format_percent(value: float | None, *, digits: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{round(value * 100, digits):.{digits}f}%"


def _is_quantity_question(text: str) -> bool:
    return any(token in text for token in ("combien", "nombre", "nb", "total", "taux", "pourcentage", "ratio"))


def _is_compliance_rate_question(question: str) -> bool:
    text = _normalize_text(question)
    if not text:
        return False
    asks_rate = any(token in text for token in ("taux", "pourcentage", "ratio"))
    asks_compliance = "conformit" in text
    return asks_rate and asks_compliance


def _match_deterministic_kpi_kind(question: str) -> str | None:
    text = _normalize_text(question)
    if not text:
        return None

    if _is_compliance_rate_question(question):
        return "compliance_rate"

    if (
        _is_quantity_question(text)
        and ("non conform" in text or "non-conform" in text)
    ):
        return "non_conformity_count"

    if (
        _is_quantity_question(text)
        and "preuve" in text
        and ("manqu" in text or "absence de preuve" in text)
    ):
        return "absence_proof_count"

    if (
        _is_quantity_question(text)
        and ("applicable" in text or "a2" in text)
        and ("non evalue" in text or "pas evalue" in text or "reste" in text or "restent" in text)
    ):
        return "unevaluated_applicable_count"

    return None


def _is_operational_context_question(question: str) -> bool:
    """Detecte les questions generales qui doivent charger le contexte A3 global."""
    text = _normalize_intent_text(question)
    if not text or _classify_query_specificity(question) == "SPECIFIC":
        return False

    asks_proofs = any(token in text for token in ("preuve", "preuves", "justificatif", "justificatifs"))
    asks_missing = any(token in text for token in ("manqu", "absence", "absent", "insuffisant", "completer"))
    asks_compliance = any(token in text for token in ("conforme", "conformite", "non conform", "non-conform"))
    asks_gaps = any(token in text for token in ("ecart", "ecarts", "gap", "gaps"))
    asks_actions = "action" in text or "corriger" in text or "traiter" in text

    return (asks_proofs and (asks_missing or asks_compliance)) or asks_gaps or (asks_actions and asks_compliance)


def _build_deterministic_kpi_response(
    question: str,
    operational_context: dict[str, Any],
) -> ChatResponseLLM | None:
    kpi_kind = _match_deterministic_kpi_kind(question)
    if not kpi_kind:
        return None

    snapshot = operational_context.get("snapshot") or {}
    total_checks = _snapshot_int(snapshot, "total_checks")
    conformes = _snapshot_int(snapshot, "conforme_total")
    non_conformes = _snapshot_int(snapshot, "non_conforme_total")
    partiels = _snapshot_int(snapshot, "partiel_total")
    absences = _snapshot_int(snapshot, "absence_preuve_total")
    applicable_total = _snapshot_int(snapshot, "applicable_total")
    unevaluated = _snapshot_int(snapshot, "unevaluated_applicable_total")
    compliance_rate = _snapshot_float(snapshot, "compliance_rate")
    coverage_rate = _snapshot_float(snapshot, "compliance_coverage_rate")

    if kpi_kind == "compliance_rate" and total_checks <= 0:
        answer = (
            "Le taux de conformitÃ© n'est pas disponible Ã  ce stade, car aucune Ã©valuation A3 "
            "n'a encore Ã©tÃ© enregistrÃ©e pour ce tenant. "
            f"Le pÃ©rimÃ¨tre A2 compte actuellement {applicable_total} exigence(s) applicable(s)."
        )
        actions = [
            "Lancer l'Ã©valuation de conformitÃ© A3 sur les exigences applicables.",
        ]
        if applicable_total > 0:
            actions.append("VÃ©rifier que les preuves et audits nÃ©cessaires sont bien importÃ©s avant le calcul A3.")
        return ChatResponseLLM(
            answer=answer,
            obligation_type="MIXTE",
            has_uncertainty=True,
            uncertainty_note="Aucun contrÃ´le A3 disponible pour calculer le taux de conformitÃ©.",
            source_citations=[
                {
                    "source_kind": "METRIC",
                    "article_ref": "SynthÃ¨se A2/A3",
                    "doc_title": "Indicateurs du tenant",
                    "excerpt": (
                        f"Exigences applicables A2: {applicable_total}. "
                        "Aucune vÃ©rification A3 enregistrÃ©e."
                    ),
                    "obligation": "Indicateur de pilotage",
                }
            ],
            recommended_actions=actions,
        )

    if kpi_kind == "compliance_rate":
        answer_lines = [
            (
                f"Le taux de conformitÃ© officiel de cette entreprise est de {_format_percent(compliance_rate)} "
                f"selon l'Agent 3, soit {conformes} exigence(s) conforme(s) sur {total_checks} vÃ©rification(s) A3."
            )
        ]

        detail_bits: list[str] = []
        if partiels > 0:
            detail_bits.append(f"{partiels} partiellement conforme(s)")
        if non_conformes > 0:
            detail_bits.append(f"{non_conformes} non conforme(s)")
        if absences > 0:
            detail_bits.append(f"{absences} en absence de preuve")
        if detail_bits:
            answer_lines.append("Le dÃ©tail des statuts A3 est le suivant : " + ", ".join(detail_bits) + ".")

        if applicable_total > 0:
            answer_lines.append(
                (
                    f"Ã€ ne pas confondre avec le pÃ©rimÃ¨tre applicable A2, qui compte {applicable_total} exigence(s). "
                    f"La couverture actuelle des Ã©valuations de conformitÃ© est de {_format_percent(coverage_rate)} "
                    f"({total_checks}/{applicable_total}), ce qui laisse {unevaluated} exigence(s) applicable(s) "
                    "encore non Ã©valuÃ©e(s) en conformitÃ©."
                )
            )

        actions: list[str] = []
        if unevaluated > 0:
            actions.append("Poursuivre les Ã©valuations A3 sur les exigences applicables non encore couvertes.")
        if non_conformes > 0 or absences > 0:
            actions.append("Traiter en prioritÃ© les non-conformitÃ©s ouvertes et les absences de preuve identifiÃ©es par A3.")

        return ChatResponseLLM(
            answer=" ".join(answer_lines),
            obligation_type="MIXTE",
            has_uncertainty=False,
            uncertainty_note=None,
            source_citations=[
                {
                    "source_kind": "METRIC",
                    "article_ref": "SynthÃ¨se A3",
                    "doc_title": "Indicateurs conformitÃ© du tenant",
                    "excerpt": (
                        f"Taux de conformitÃ© officiel: {_format_percent(compliance_rate)} "
                        f"({conformes} conforme(s) / {total_checks} vÃ©rification(s) A3). "
                        f"PÃ©rimÃ¨tre applicable A2: {applicable_total}. "
                        f"Couverture A3: {_format_percent(coverage_rate)}. "
                        f"Exigences applicables non Ã©valuÃ©es: {unevaluated}."
                    ),
                    "obligation": "Indicateur de pilotage",
                }
            ],
            recommended_actions=actions,
        )

    if kpi_kind == "non_conformity_count":
        if total_checks <= 0:
            return ChatResponseLLM(
                answer=(
                    "Le nombre de non-conformitÃ©s n'est pas disponible Ã  ce stade, car aucune vÃ©rification A3 "
                    "n'a encore Ã©tÃ© enregistrÃ©e pour ce tenant."
                ),
                obligation_type="MIXTE",
                has_uncertainty=True,
                uncertainty_note="Aucun contrÃ´le A3 disponible.",
                source_citations=[
                    {
                        "source_kind": "METRIC",
                        "article_ref": "SynthÃ¨se A3",
                        "doc_title": "Indicateurs conformitÃ© du tenant",
                        "excerpt": "Aucune vÃ©rification A3 enregistrÃ©e pour ce tenant.",
                        "obligation": "Indicateur de pilotage",
                    }
                ],
                recommended_actions=["Lancer ou complÃ©ter les vÃ©rifications A3 avant de conclure sur les non-conformitÃ©s."],
            )

        return ChatResponseLLM(
            answer=(
                f"Le tenant compte actuellement {non_conformes} vÃ©rification(s) A3 classÃ©e(s) non conforme(s). "
                f"Sur le mÃªme pÃ©rimÃ¨tre, {partiels} sont partiellement conformes, {absences} sont en absence de preuve "
                f"et {conformes} sont conformes, pour un total de {total_checks} vÃ©rification(s) A3."
            ),
            obligation_type="MIXTE",
            has_uncertainty=False,
            uncertainty_note=None,
            source_citations=[
                {
                    "source_kind": "METRIC",
                    "article_ref": "SynthÃ¨se A3",
                    "doc_title": "Indicateurs conformitÃ© du tenant",
                    "excerpt": (
                        f"Non conformes: {non_conformes}. Partiellement conformes: {partiels}. "
                        f"Absence de preuve: {absences}. Conformes: {conformes}. "
                        f"Total vÃ©rifications A3: {total_checks}."
                    ),
                    "obligation": "Indicateur de pilotage",
                }
            ],
            recommended_actions=[
                "Traiter en prioritÃ© les vÃ©rifications classÃ©es non conformes et suivre les actions correctives associÃ©es."
            ] if non_conformes > 0 else [],
        )

    if kpi_kind == "absence_proof_count":
        if total_checks <= 0:
            return ChatResponseLLM(
                answer=(
                    "Le nombre de cas en absence de preuve n'est pas disponible Ã  ce stade, car aucune vÃ©rification A3 "
                    "n'a encore Ã©tÃ© enregistrÃ©e pour ce tenant."
                ),
                obligation_type="MIXTE",
                has_uncertainty=True,
                uncertainty_note="Aucun contrÃ´le A3 disponible.",
                source_citations=[
                    {
                        "source_kind": "METRIC",
                        "article_ref": "SynthÃ¨se A3",
                        "doc_title": "Indicateurs conformitÃ© du tenant",
                        "excerpt": "Aucune vÃ©rification A3 enregistrÃ©e pour ce tenant.",
                        "obligation": "Indicateur de pilotage",
                    }
                ],
                recommended_actions=["Importer les preuves et exÃ©cuter les vÃ©rifications A3 pour disposer d'un indicateur exploitable."],
            )

        return ChatResponseLLM(
            answer=(
                f"Le tenant compte actuellement {absences} vÃ©rification(s) A3 en absence de preuve. "
                "Cet indicateur correspond au nombre d'exigences Ã©valuÃ©es sans preuve jugÃ©e suffisante, "
                "et non nÃ©cessairement au nombre de documents distincts manquants."
            ),
            obligation_type="MIXTE",
            has_uncertainty=False,
            uncertainty_note=None,
            source_citations=[
                {
                    "source_kind": "METRIC",
                    "article_ref": "SynthÃ¨se A3",
                    "doc_title": "Indicateurs conformitÃ© du tenant",
                    "excerpt": (
                        f"Absence de preuve: {absences}. Total vÃ©rifications A3: {total_checks}. "
                        f"Conformes: {conformes}. Non conformes: {non_conformes}."
                    ),
                    "obligation": "Indicateur de pilotage",
                }
            ],
            recommended_actions=[
                "ComplÃ©ter en prioritÃ© les preuves attendues pour les vÃ©rifications A3 en absence de preuve."
            ] if absences > 0 else [],
        )

    if kpi_kind == "unevaluated_applicable_count":
        if applicable_total <= 0:
            return ChatResponseLLM(
                answer=(
                    "Aucune exigence applicable A2 n'est actuellement comptabilisÃ©e pour ce tenant, "
                    "ce qui ne permet pas d'identifier un reste Ã  Ã©valuer en conformitÃ©."
                ),
                obligation_type="MIXTE",
                has_uncertainty=True,
                uncertainty_note="Aucun pÃ©rimÃ¨tre applicable A2 disponible.",
                source_citations=[
                    {
                        "source_kind": "METRIC",
                        "article_ref": "SynthÃ¨se A2",
                        "doc_title": "Indicateurs applicabilitÃ© du tenant",
                        "excerpt": "Exigences applicables A2: 0.",
                        "obligation": "Indicateur de pilotage",
                    }
                ],
                recommended_actions=["VÃ©rifier l'analyse d'applicabilitÃ© A2 avant de lancer l'Ã©valuation de conformitÃ©."],
            )

        return ChatResponseLLM(
            answer=(
                f"Le pÃ©rimÃ¨tre A2 compte {applicable_total} exigence(s) applicable(s). "
                f"Parmi elles, {total_checks} ont dÃ©jÃ  Ã©tÃ© Ã©valuÃ©es par l'Agent 3 et {unevaluated} "
                f"restent encore non Ã©valuÃ©e(s) en conformitÃ©, soit une couverture actuelle de {_format_percent(coverage_rate)}."
            ),
            obligation_type="MIXTE",
            has_uncertainty=False,
            uncertainty_note=None,
            source_citations=[
                {
                    "source_kind": "METRIC",
                    "article_ref": "SynthÃ¨se A2/A3",
                    "doc_title": "Indicateurs applicabilitÃ© et conformitÃ© du tenant",
                    "excerpt": (
                        f"Exigences applicables A2: {applicable_total}. VÃ©rifications A3: {total_checks}. "
                        f"Exigences applicables non Ã©valuÃ©es: {unevaluated}. Couverture: {_format_percent(coverage_rate)}."
                    ),
                    "obligation": "Indicateur de pilotage",
                }
            ],
            recommended_actions=[
                "Poursuivre les vÃ©rifications A3 sur les exigences applicables encore non couvertes."
            ] if unevaluated > 0 else [],
        )

    return None


def _is_missing_proofs_request(question: str) -> bool:
    text = _normalize_intent_text(question)
    asks_proof = any(token in text for token in ("preuve", "preuves", "justificatif", "justificatifs", "document", "documents"))
    asks_missing = any(token in text for token in ("manqu", "absence", "absent", "insuffisant", "completer"))
    asks_conformity = any(token in text for token in ("conforme", "conformite", "non conform", "non-conform"))
    return asks_proof and asks_missing and asks_conformity


def _missing_proof_row_from_requirement(item: dict[str, Any]) -> tuple[str, str, str, str, dict[str, Any]] | None:
    status = str(item.get("compliance_status") or "NON_EVALUE").upper()
    missing = str(item.get("missing_proofs") or "").strip()
    expected = str(item.get("expected_proofs") or "").strip()
    if not missing and status not in {"ABSENCE_DE_PREUVE", "NON_CONFORME", "PARTIELLEMENT_CONFORME", "PARTIEL"}:
        return None

    point = item.get("citation_ref") or _format_scope_text(item) or "Controle A3"
    proof = missing or expected or "Justificatif attendu non detaille dans le controle A3."
    action = "Importer ou rattacher le justificatif, puis relancer le controle A3."
    if status == "ABSENCE_DE_PREUVE":
        status_label = "Absence de preuve"
    elif status == "NON_CONFORME":
        status_label = "Non conforme"
    elif status in {"PARTIELLEMENT_CONFORME", "PARTIEL"}:
        status_label = "Partiel"
    else:
        status_label = status.replace("_", " ").title()
        _truncate_text(point, 52),
        status_label,
        _truncate_text(proof, 95),
        action,
        item,


def _missing_proof_row_from_gap(item: dict[str, Any]) -> tuple[str, str, str, str, dict[str, Any]] | None:
    missing = str(item.get("missing_proof") or "").strip()
    description = str(item.get("description") or "").strip()
    if not missing and not description:
        return None

    severity = str(item.get("severity") or "A_CONTROLER").replace("_", " ").title()
    point = f"Gap {severity}"
    proof = missing or description
    action = "Traiter l'ecart et joindre la preuve attendue."
    return (
        _truncate_text(point, 52),
        severity,
        _truncate_text(proof, 95),
        action,
        item,
    )


def _build_missing_proofs_response(
    question: str,
    operational_context: dict[str, Any],
    response_format: str = "synthesis",
) -> ChatResponseLLM | None:
    if not _is_missing_proofs_request(question):
        return None

    rows: list[tuple[str, str, str, str, dict[str, Any], str]] = []
    for item in operational_context.get("requirements") or []:
        row = _missing_proof_row_from_requirement(item)
        if row:
            rows.append((*row, "A3_REQUIREMENT"))
    for item in operational_context.get("gaps") or []:
        row = _missing_proof_row_from_gap(item)
        if row:
            rows.append((*row, "GAP"))

    snapshot = operational_context.get("snapshot") or {}
    absence_count = _snapshot_int(snapshot, "absence_preuve_total")
    non_conforme_count = _snapshot_int(snapshot, "non_conforme_total")

    if not rows:
        answer = (
            "**Justificatifs manquants**\n"
            "Aucun justificatif manquant detaille n'est disponible dans le contexte A3 charge.\n\n"
            "**A controler**\n"
            f"- Absences de preuve A3 comptabilisees: {absence_count}.\n"
            f"- Non-conformites A3 comptabilisees: {non_conforme_count}.\n"
            "- Verifier que les preuves et les controles A3 sont bien importes."
        )
        return ChatResponseLLM(
            answer=answer,
            obligation_type="MIXTE",
            has_uncertainty=True,
            uncertainty_note="Compteurs disponibles, mais aucun detail de justificatif manquant charge.",
            source_citations=[
                {
                    "source_kind": "METRIC",
                    "article_ref": "Synthese A3",
                    "doc_title": "Indicateurs conformite du tenant",
                    "excerpt": f"Absences de preuve: {absence_count}. Non-conformites: {non_conforme_count}.",
                    "obligation": "Indicateur de pilotage",
                }
            ],
            recommended_actions=["Verifier l'import des preuves et relancer l'analyse de conformite A3."],
        )

    rows = rows[:5]
    if response_format == "table":
        answer_lines = ["**Justificatifs manquants**", "- Point | Statut | Justificatif / action"]
        answer_lines.extend(
            f"- {point} | {status} | {proof} -> {action}"
            for point, status, proof, action, _item, _kind in rows
        )
        answer = "\n".join(answer_lines)
    else:
        answer_lines = [
            "**Justificatifs manquants**",
            f"{len(rows)} point(s) prioritaire(s) sont a completer pour viser un statut conforme.",
            "",
            "**Priorites**",
        ]
        answer_lines.extend(
            f"- {point}: {proof} ({status})."
            for point, status, proof, _action, _item, _kind in rows
        )
        answer_lines.extend(
            [
                "",
                "**Actions**",
                "1. Importer ou rattacher les justificatifs manquants.",
                "2. Associer chaque preuve au bon site, processus ou exigence.",
                "3. Relancer A3 pour confirmer le passage en conforme.",
            ]
        )
        answer = "\n".join(answer_lines)

    citations = []
    for point, _status, proof, _action, item, kind in rows[:4]:
        citations.append(
            {
                "source_kind": kind,
                "requirement_id": item.get("requirement_id"),
                "article_ref": item.get("citation_ref") or point,
                "doc_title": _format_scope_text(item),
                "excerpt": proof,
                "obligation": item.get("compliance_status") or item.get("gap_type") or "Controle A3",
            }
        )

    return ChatResponseLLM(
        answer=answer,
        obligation_type="MIXTE",
        has_uncertainty=False,
        source_citations=citations,
        recommended_actions=[
            "Importer ou rattacher les justificatifs manquants aux exigences concernees.",
            "Relancer l'analyse A3 apres mise a jour des preuves.",
        ],
    )


def _glossary_response(term: str, answer: str, actions: list[str] | None = None) -> ChatResponseLLM:
    return ChatResponseLLM(
        answer=answer,
        obligation_type="MIXTE",
        has_uncertainty=False,
        uncertainty_note=None,
        source_citations=[
            {
                "source_kind": "GLOSSARY",
                "article_ref": "Glossaire QALITAS",
                "doc_title": f"Definition: {term}",
                "excerpt": _truncate_text(answer, 260),
                "obligation": "Definition metier interne",
            }
        ],
        recommended_actions=actions or [],
    )


def _has_term(text: str, aliases: tuple[str, ...]) -> bool:
    words = set(text.split())
    for alias in aliases:
        if " " in alias:
            if alias in text:
                return True
        elif alias in words:
            return True
    return False


def _is_definition_question(text: str) -> bool:
    if not text:
        return False
    if len(text.split()) <= 3:
        return True
    return any(
        marker in text
        for marker in (
            "c'est quoi",
            "c est quoi",
            "qu'est ce",
            "qu est ce",
            "definition",
            "definis",
            "explique moi",
            "explique",
            "veut dire",
            "signifie",
            "difference entre",
        )
    )


def _build_deterministic_assistant_response(question: str) -> ChatResponseLLM | None:
    text = _normalize_intent_text(question)
    if not text:
        return None

    words = set(text.split())
    short = len(words) <= 4

    if short and words.intersection({"bonjour", "bonsoir", "salut", "hello", "coucou"}):
        return ChatResponseLLM(
            answer=(
                "Bonjour, comment puis-je vous aider ?\n\n"
                "Je peux par exemple:\n"
                "- expliquer une notion QHSE;\n"
                "- retrouver une obligation applicable;\n"
                "- resumer les ecarts et preuves manquantes;\n"
                "- proposer les prochaines actions de conformite."
            ),
            obligation_type="MIXTE",
            recommended_actions=[
                "Poser une question sur une exigence, une preuve, un ecart ou une definition metier."
            ],
        )

    if short and (
        words.intersection({"merci", "ok", "d'accord", "daccord", "parfait"})
        or text in {"bien sur", "bien sure"}
    ):
        return ChatResponseLLM(
            answer=(
                "Bien sur. Donnez-moi une exigence, un domaine QHSE ou un ecart a analyser, "
                "et je vous repondrai de facon courte avec les actions utiles."
            ),
            obligation_type="MIXTE",
        )

    if any(marker in text for marker in ("comment tu peux m'aider", "comment peux tu m'aider", "que peux tu faire", "aide moi", "besoin d'aide")):
        return ChatResponseLLM(
            answer=(
                "**Ce que je peux faire**\n"
                "- Expliquer une notion: applicabilite, conformite, exigence, preuve, loi.\n"
                "- Identifier les obligations applicables a votre contexte.\n"
                "- Prioriser les ecarts critiques et les preuves manquantes.\n"
                "- Transformer une analyse A3 en actions correctives.\n\n"
                "**Exemple**\n"
                "Demandez: 'Quelle difference entre applicabilite et conformite ?' ou "
                "'Quels justificatifs manquent pour passer en conforme ?'"
            ),
            obligation_type="MIXTE",
            recommended_actions=[
                "Choisir un domaine, un site, une exigence ou une notion a clarifier.",
            ],
        )

    if not _is_definition_question(text):
        return None

    if _has_term(text, ("applicabilite",)) and _has_term(text, ("conformite",)):
        return _glossary_response(
            "Applicabilite vs conformite",
            (
                "**Difference principale**\n"
                "- L'applicabilite dit si une exigence concerne l'entreprise, un site, un processus ou une activite.\n"
                "- La conformite dit si cette exigence applicable est respectee, prouvee et verifiable.\n\n"
                "**Dans QALITAS**\n"
                "A2 decide l'applicabilite. A3 evalue ensuite la conformite avec les preuves disponibles."
            ),
            ["Verifier d'abord le resultat A2, puis controler les preuves et le statut A3."],
        )

    glossary: list[tuple[tuple[str, ...], str, str, list[str]]] = [
        (
            ("applicabilite", "applicable"),
            "Applicabilite",
            (
                "**Applicabilite**\n"
                "Une exigence est applicable quand elle concerne reellement le tenant, son activite, ses sites, "
                "ses produits, ses risques ou ses processus.\n\n"
                "**Dans QALITAS**\n"
                "L'Agent 2 compare l'exigence extraite par A1 avec le contexte entreprise pour decider: "
                "applicable, non applicable ou a clarifier."
            ),
            ["Controler le contexte entreprise utilise par A2: sites, activites, produits, risques et processus."],
        ),
        (
            ("conformite", "conforme"),
            "Conformite",
            (
                "**Conformite**\n"
                "La conformite signifie qu'une exigence applicable est respectee dans les faits et justifiee par "
                "des preuves fiables.\n\n"
                "**Dans QALITAS**\n"
                "L'Agent 3 analyse les exigences applicables, les preuves, les audits et les enregistrements pour "
                "classer le statut: conforme, partiel, non conforme ou absence de preuve."
            ),
            ["Identifier les preuves attendues, puis traiter les absences de preuve et les non-conformites."],
        ),
        (
            ("exigence", "exigences"),
            "Exigence reglementaire",
            (
                "**Exigence reglementaire**\n"
                "C'est une obligation, interdiction, condition ou mesure extraite d'un texte officiel.\n\n"
                "**Dans QALITAS**\n"
                "L'Agent 1 extrait ces exigences depuis les documents juridiques et conserve le texte, le domaine, "
                "le type, la source et la reference de citation."
            ),
            ["Verifier la source et la formulation de l'exigence avant de l'utiliser dans A2/A3."],
        ),
        (
            ("loi", "lois", "reglementation", "reglementaire", "texte"),
            "Loi et reglementation",
            (
                "**Loi / reglementation**\n"
                "Une loi ou un texte reglementaire fixe les obligations officielles applicables a une activite, "
                "un risque ou une organisation.\n\n"
                "**Dans QALITAS**\n"
                "Ces textes constituent le corpus source: ils sont importes, segmentes, extraits puis relies aux "
                "decisions d'applicabilite et de conformite."
            ),
            ["Toujours rattacher une conclusion a une reference juridique ou a une preuve operationnelle."],
        ),
        (
            ("preuve", "preuves", "justificatif", "justificatifs"),
            "Preuve de conformite",
            (
                "**Preuve de conformite**\n"
                "C'est un document ou enregistrement qui demontre qu'une exigence est respectee: audit, registre, "
                "certificat, formation, controle, rapport ou procedure.\n\n"
                "**Dans QALITAS**\n"
                "Les preuves alimentent l'Agent 3 pour confirmer, nuancer ou refuser un statut conforme."
            ),
            ["Importer les preuves manquantes et les rattacher au site, processus ou exigence concerne."],
        ),
        (
            ("ecart", "ecarts", "gap", "gaps", "non conformite", "non conformites"),
            "Ecart de conformite",
            (
                "**Ecart / non-conformite**\n"
                "Un ecart apparait quand l'etat reel ne satisfait pas une exigence applicable, ou quand la preuve "
                "est absente ou insuffisante.\n\n"
                "**Dans QALITAS**\n"
                "L'Agent 3 qualifie l'ecart, sa gravite, son impact et les actions correctives a engager."
            ),
            ["Prioriser les ecarts critiques, puis associer une action, une preuve attendue et une echeance."],
        ),
    ]

    for aliases, term, answer, actions in glossary:
        if _has_term(text, aliases):
            return _glossary_response(term, answer, actions)

    return None


def _load_company_site_rows(conn: psycopg.Connection, profile_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT site_name, city, region, site_type, employee_count, main_activities
            FROM company_sites
            WHERE profile_id = %s
            ORDER BY created_at NULLS LAST, site_name
            """,
            (profile_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "site_name": row[0],
            "city": row[1],
            "region": row[2],
            "site_type": row[3],
            "employee_count": row[4],
            "main_activities": row[5],
        }
        for row in rows
    ]


def _best_site_match(question_text: str, sites: list[dict[str, Any]]) -> dict[str, Any] | None:
    question_tokens = set(question_text.split())
    best: tuple[int, dict[str, Any]] | None = None
    for site in sites:
        haystack = _normalize_intent_text(
            " ".join(
                str(site.get(key) or "")
                for key in ("site_name", "city", "region", "site_type", "main_activities")
            )
        )
        tokens = set(haystack.split())
        score = len(question_tokens.intersection(tokens))
        if score > 0 and (best is None or score > best[0]):
            best = (score, site)
    return best[1] if best else None


def _build_company_context_response(
    conn: psycopg.Connection,
    profile_id: str,
    question: str,
) -> ChatResponseLLM | None:
    text = _normalize_intent_text(question)
    if not text:
        return None

    asks_count = any(token in text for token in ("combien", "nombre", "nb", "total", "effectif"))
    asks_employee = any(token in text for token in ("employe", "employes", "salarie", "salaries", "personnel", "effectif"))
    asks_sites = any(token in text for token in ("site", "sites", "implantation", "implantations"))
    asks_which = any(token in text for token in ("quel", "quels", "quelle", "quelles", "lesquels", "lesquelles", "liste"))

    if not asks_count and not asks_which:
        return None
    if not asks_employee and not asks_sites:
        return None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_name, headcount_total
            FROM company_profiles
            WHERE profile_id = %s
            """,
            (profile_id,),
        )
        profile_row = cur.fetchone()

    sites = _load_company_site_rows(conn, profile_id)
    if not profile_row:
        return None

    company_name = str(profile_row[0] or "Entreprise active")
    profile_headcount = profile_row[1]

    if asks_employee:
        matched_site = _best_site_match(text, sites)
        if matched_site is not None:
            site_name = matched_site.get("site_name") or matched_site.get("city") or "Site"
            count = matched_site.get("employee_count")
            if count is None:
                answer = (
                    f"**Effectif**\n"
                    f"L'effectif du site {site_name} n'est pas renseigne dans le contexte entreprise."
                )
            else:
                answer = (
                    f"**Effectif**\n"
                    f"Le site {site_name} emploie {int(count)} personne(s).\n\n"
                    f"**Source**\n"
                    f"Donnee issue du contexte entreprise du tenant actif."
                )
            excerpt = (
                f"Site: {site_name}; ville: {matched_site.get('city') or 'N/A'}; "
                f"effectif: {count if count is not None else 'N/A'}; "
                f"activites: {matched_site.get('main_activities') or 'N/A'}."
            )
            return ChatResponseLLM(
                answer=answer,
                obligation_type="MIXTE",
                source_citations=[
                    {
                        "source_kind": "COMPANY_CONTEXT",
                        "article_ref": "Contexte entreprise",
                        "doc_title": f"Site {site_name}",
                        "excerpt": excerpt,
                        "obligation": "Donnee declarative entreprise",
                    }
                ],
            )

        total = profile_headcount
        if total is None:
            total = sum(int(site.get("employee_count") or 0) for site in sites)
        answer = (
            f"**Effectif total**\n"
            f"{company_name} compte {int(total or 0)} personne(s) dans le contexte entreprise."
        )
        if sites:
            answer += "\n\n**Detail sites**\n" + "\n".join(
                f"- {site.get('site_name') or site.get('city') or 'Site'}: {site.get('employee_count') or 'N/A'} personne(s)"
                for site in sites[:6]
            )
        return ChatResponseLLM(
            answer=answer,
            obligation_type="MIXTE",
            source_citations=[
                {
                    "source_kind": "COMPANY_CONTEXT",
                    "article_ref": "Contexte entreprise",
                    "doc_title": "Profil entreprise et sites",
                    "excerpt": _truncate_text(answer, 260),
                    "obligation": "Donnee declarative entreprise",
                }
            ],
        )

    if asks_sites:
        if not sites:
            answer = "**Sites**\nAucun site n'est renseigne dans le contexte entreprise du tenant actif."
        else:
            answer = (
                f"**Sites**\n"
                f"Le tenant actif compte {len(sites)} site(s) renseigne(s).\n\n"
                "**Liste**\n"
                + "\n".join(
                    f"- {site.get('site_name') or 'Site'}"
                    f"{f' ({site.get('city')})' if site.get('city') else ''}"
                    f": {site.get('employee_count') or 'N/A'} personne(s)"
                    for site in sites[:8]
                )
            )
        return ChatResponseLLM(
            answer=answer,
            obligation_type="MIXTE",
            source_citations=[
                {
                    "source_kind": "COMPANY_CONTEXT",
                    "article_ref": "Contexte entreprise",
                    "doc_title": "Sites du tenant actif",
                    "excerpt": _truncate_text(answer, 260),
                    "obligation": "Donnee declarative entreprise",
                }
            ],
        )

    return None


def _classify_query_specificity(question: str) -> str:
    text = _normalize_text(question)
    if not text:
        return "GENERAL"
    specific_patterns = (
        r"\bart\.?\s*\d+",
        r"\barticle\s+\d+",
        r"\bdecret\b.*\b\d{2,4}[-/]\d+",
        r"\bloi\b.*\b\d{2,4}[-/]\d+",
        r"\barrete\b.*\b\d{2,4}",
        r"\bjort\b",
    )
    if any(re.search(pattern, text) for pattern in specific_patterns):
        return "SPECIFIC"
    if any(ch.isdigit() for ch in text) and any(token in text for token in ("article", "decret", "loi", "arrete", "jort")):
        return "SPECIFIC"
    if len(text.split()) <= 6 and any(token in text for token in ("quelle", "quel", "que dit", "obligation", "article")):
        return "SPECIFIC"
    return "GENERAL"


def _top_k_for_question(question: str) -> int:
    return SPECIFIC_TOP_K if _classify_query_specificity(question) == "SPECIFIC" else GENERAL_TOP_K


def _token_set(text: Any) -> set[str]:
    normalized = _normalize_text(text)
    return {
        token
        for token in normalized.replace("/", " ").replace("-", " ").split()
        if len(token) >= 3
    }


def _token_overlap_ratio(a: Any, b: Any) -> float:
    tokens_a = _token_set(a)
    tokens_b = _token_set(b)
    if not tokens_a or not tokens_b:
        return 0.0
    common = tokens_a & tokens_b
    return len(common) / max(1, min(len(tokens_a), len(tokens_b)))


def _build_citation_source_index(
    retrieved_docs: list[dict[str, Any]],
    operational_context: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    by_req_id: dict[str, dict[str, Any]] = {}
    all_sources: list[dict[str, Any]] = []

    def register(source: dict[str, Any]) -> None:
        if not isinstance(source, dict):
            return
        cloned = dict(source)
        req_id = str(cloned.get("requirement_id") or "").strip()
        if req_id and req_id not in by_req_id:
            by_req_id[req_id] = cloned
        all_sources.append(cloned)

    for item in retrieved_docs or []:
        register(item)
    for item in operational_context.get("requirements") or []:
        register(item)
    for item in operational_context.get("gaps") or []:
        register(item)
    for item in operational_context.get("actions") or []:
        register(item)
    for item in operational_context.get("proofs") or []:
        register(item)
    return by_req_id, all_sources


def _resolve_citation_source(
    citation: dict[str, Any],
    by_req_id: dict[str, dict[str, Any]],
    all_sources: list[dict[str, Any]],
) -> dict[str, Any] | None:
    req_id = str(citation.get("requirement_id") or "").strip()
    if req_id and req_id in by_req_id:
        return by_req_id[req_id]

    article_ref = _normalize_text(citation.get("article_ref"))
    doc_title = _normalize_text(citation.get("doc_title"))
    for source in all_sources:
        if article_ref and article_ref == _normalize_text(source.get("citation_ref")):
            return source
        if doc_title and doc_title == _normalize_text(source.get("doc_title")):
            return source
    return None


def _verify_source_citations(
    citations: list[dict[str, Any]],
    retrieved_docs: list[dict[str, Any]],
    operational_context: dict[str, Any],
) -> list[dict[str, Any]]:
    by_req_id, all_sources = _build_citation_source_index(retrieved_docs, operational_context)
    verified: list[dict[str, Any]] = []
    for raw in citations or []:
        citation = dict(raw or {})
        if str(citation.get("source_kind") or "").strip().upper() in {
            "METRIC",
            "GLOSSARY",
            "SYSTEM",
            "COMPANY_CONTEXT",
            "A3_REQUIREMENT",
            "GAP",
            "ACTION",
            "EVIDENCE",
        }:
            citation["verified"] = True
            citation["verification_score"] = 1.0
            verified.append(citation)
            continue
        source = _resolve_citation_source(citation, by_req_id, all_sources)
        if not source:
            citation["verified"] = False
            citation["warning"] = "Source non retrouvee dans le contexte du tenant."
            verified.append(citation)
            continue
        citation["requirement_id"] = citation.get("requirement_id") or source.get("requirement_id")
        actual_text = source.get("requirement_text") or source.get("chunk_text") or ""
        overlap = _token_overlap_ratio(citation.get("excerpt"), actual_text)
        citation["verification_score"] = round(overlap, 3)
        citation["verified"] = overlap >= CITATION_OVERLAP_MIN
        if not citation["verified"]:
            citation["warning"] = "Extrait a controler: recouvrement faible avec la source."
        citation["article_ref"] = citation.get("article_ref") or source.get("citation_ref") or source.get("doc_title") or "N/A"
        citation["doc_title"] = citation.get("doc_title") or source.get("doc_title") or ""
        verified.append(citation)
    return verified


# â”€â”€â”€ Utilitaires DB â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _get_conn(tenant_id: str | None = None) -> psycopg.Connection:
    """
    RÃ©solution DSN:
    - PG_DSN_A4 (optionnel): DB dÃ©diÃ©e Agent 4 / pgvector
    - PG_DSN (fallback): DB principale plateforme
    """
    dsn_main = os.getenv("PG_DSN", "").strip()
    dsn_a4 = os.getenv("PG_DSN_A4", "").strip()
    if not dsn_main and not dsn_a4:
        raise RuntimeError("PG_DSN (ou PG_DSN_A4) manquant dans .env")

    candidates: list[tuple[str, str]] = []
    if dsn_a4:
        candidates.append(("PG_DSN_A4", dsn_a4))
    if dsn_main and dsn_main != dsn_a4:
        candidates.append(("PG_DSN", dsn_main))

    errors: list[str] = []
    for name, dsn in candidates:
        try:
            return connect_db(dsn, tenant_id=tenant_id, connect_timeout=2)
        except Exception as e:
            first_line = str(e).splitlines()[0] if str(e) else repr(e)
            errors.append(f"{name}: {first_line}")

    raise RuntimeError("Connexion DB impossible pour Agent 4. " + " | ".join(errors))


def _to_vector_literal(values: Any) -> str:
    """
    Normalise divers formats d'embedding vers un literal pgvector [x1,x2,...].
    Accepte list/tuple/ndarray et string deja au format [] ou {}.
    """
    if values is None:
        raise ValueError("embedding vide")
    if isinstance(values, str):
        s = values.strip()
        if s.startswith("[") and s.endswith("]"):
            return s
        if s.startswith("{") and s.endswith("}"):
            return "[" + s[1:-1] + "]"
        return "[" + s + "]"
    try:
        return "[" + ",".join(str(float(x)) for x in values) + "]"
    except Exception as e:
        raise ValueError(f"format embedding non supporte: {type(values)}") from e


def _ensure_pgvector_schema(conn: psycopg.Connection) -> None:
    """
    Garantit un schema embeddings natif pgvector.
    - active l'extension vector
    - cree la table si absente
    - migre l'ancien type float4[] vers vector(1536) si necessaire
    - cree les index utiles (BTree + HNSW cosine)
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        has_vector_ext = bool(cur.fetchone())
        cur.execute("""
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_name = 'requirement_embeddings'
            )
        """)
        table_exists = bool(cur.fetchone()[0])
        cur.execute("""
            SELECT udt_name
            FROM information_schema.columns
            WHERE table_name = 'requirement_embeddings'
              AND column_name = 'embedding'
            LIMIT 1
        """)
        row = cur.fetchone()
        emb_udt = str(row[0]) if row and row[0] else ""

    if has_vector_ext and table_exists and emb_udt == "vector":
        return

    with conn.cursor() as cur:
        if not has_vector_ext:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS requirement_embeddings (
                embedding_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                requirement_id UUID NOT NULL REFERENCES requirements(requirement_id) ON DELETE CASCADE,
                chunk_text     TEXT NOT NULL,
                embedding      vector(1536),
                model          TEXT DEFAULT 'text-embedding-3-small',
                created_at     TIMESTAMPTZ DEFAULT now(),
                UNIQUE (requirement_id)
            )
        """)
        cur.execute("""
            SELECT udt_name
            FROM information_schema.columns
            WHERE table_name = 'requirement_embeddings'
              AND column_name = 'embedding'
            LIMIT 1
        """)
        row = cur.fetchone()
        emb_udt = str(row[0]) if row and row[0] else ""
        if emb_udt == "_float4":
            cur.execute("""
                ALTER TABLE requirement_embeddings
                    ALTER COLUMN embedding TYPE vector(1536)
                    USING embedding::vector(1536)
            """)
        elif emb_udt and emb_udt != "vector":
            raise RuntimeError(
                f"Type embedding non supporte pour requirement_embeddings.embedding: {emb_udt}"
            )
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_embeddings_req
            ON requirement_embeddings (requirement_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw_cosine
            ON requirement_embeddings USING hnsw (embedding vector_cosine_ops)
        """)
    conn.commit()


def _pgvector_available(conn: psycopg.Connection) -> bool:
    """VÃ©rifie extension + type vector + prÃ©sence d'embeddings."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            if not cur.fetchone():
                return False
            cur.execute("""
                SELECT udt_name
                FROM information_schema.columns
                WHERE table_name = 'requirement_embeddings'
                  AND column_name = 'embedding'
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row or str(row[0]) != "vector":
                return False
            cur.execute("SELECT COUNT(*) FROM requirement_embeddings WHERE embedding IS NOT NULL")
            return int(cur.fetchone()[0]) > 0
    except Exception:
        return False


# â”€â”€â”€ Ingestion des rapports d'audit PDF â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def ingest_audit_reports(tenant_id: str) -> int:
    """Importe les rapports d'audit PDF depuis DataSet/ vers la DB."""
    try:
        import pdfplumber
    except ImportError:
        print("  [WARN] pdfplumber non installÃ©. Installez: pip install pdfplumber")
        return 0

    conn = _get_conn(tenant_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT profile_id FROM company_profiles WHERE tenant_id=%s", (tenant_id,)
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            raise ValueError(f"Tenant inconnu: {tenant_id}")
        profile_id = str(row[0])

    pdf_files = sorted(DATASET_DIR.glob("ReportAuditFiche*.pdf"))
    if not pdf_files:
        print("  [INFO] Aucun fichier ReportAuditFiche*.pdf trouvÃ© dans DataSet/")
        conn.close()
        return 0

    imported = 0
    for pdf_path in pdf_files:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                full_text = "\n".join(
                    page.extract_text() or "" for page in pdf.pages
                )

            # Extraction basique des mÃ©tadonnÃ©es depuis le texte
            ref = _extract_field(full_text, "RÃ©fÃ©rence", ":")
            state = _extract_field(full_text, "Etat", ":")
            audit_type = _extract_field(full_text, "Type Audit", ":")
            category = _extract_field(full_text, "CatÃ©gorie", ":")
            nature = _extract_field(full_text, "Nature", ":")
            system_scope = _extract_field(full_text, "SystÃ¨me", ":")

            if not ref:
                ref = pdf_path.stem

            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO audit_reports
                        (profile_id, reference, audit_type, category, nature,
                         system_scope, state, raw_text, source_file)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (reference) DO UPDATE SET
                        raw_text = EXCLUDED.raw_text,
                        state    = EXCLUDED.state
                """, (
                    profile_id, ref, audit_type, category, nature,
                    system_scope, state, full_text[:50000], pdf_path.name,
                ))
            conn.commit()
            imported += 1
            print(f"  [OK] {pdf_path.name} â†’ ref={ref}")

        except Exception as e:
            print(f"  [WARN] {pdf_path.name}: {e}")

    conn.close()
    print(f"\n  {imported} rapport(s) d'audit importÃ©(s)")
    return imported


def _extract_field(text: str, label: str, sep: str = ":") -> str | None:
    """Extrait une valeur simple depuis le texte brut d'un PDF."""
    import re
    pattern = rf"{re.escape(label)}\s*{re.escape(sep)}\s*([^\n]+)"
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        return m.group(1).strip()[:200]
    return None


# â”€â”€â”€ Indexation (Embeddings) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _get_openai_embedding(text: str, client: Any) -> list[float] | None:
    """Calcule l'embedding d'un texte via OpenAI text-embedding-3-small."""
    try:
        resp = client.embeddings.create(
            model="text-embedding-3-small",
            input=text[:8000],  # limite de sÃ©curitÃ©
        )
        return resp.data[0].embedding
    except Exception as e:
        print(f"  [WARN] Embedding Ã©chouÃ©: {e}")
        return None


def index_requirements(tenant_id: str, force: bool = False) -> int:
    """
    Calcule et stocke les embeddings de toutes les exigences applicables.
    Stockage natif pgvector (vector(1536)).
    Necessite OpenAI API pour le calcul des embeddings.

    Cette etape est le "I" de RAG cote indexation: elle transforme les textes
    reglementaires applicables en vecteurs recherchables par similarite.
    """
    print(f"\n=== Agent 4 â€” Indexation des embeddings ({tenant_id}) ===\n")

    conn = _get_conn(tenant_id)

    try:
        _ensure_pgvector_schema(conn)
    except Exception as e:
        print(f"  [ERREUR] Schema pgvector indisponible: {e}")
        conn.close()
        return 0

    # Client OpenAI pour les embeddings
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not openai_key:
        print("  [ERREUR] OPENAI_API_KEY manquante (nÃ©cessaire pour les embeddings)")
        conn.close()
        return 0

    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=openai_key)
    except ImportError:
        print("  [ERREUR] openai non installÃ©")
        conn.close()
        return 0

    # Charger le profil
    with conn.cursor() as cur:
        cur.execute(
            "SELECT profile_id FROM company_profiles WHERE tenant_id=%s", (tenant_id,)
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            raise ValueError(f"Tenant inconnu: {tenant_id}")
        profile_id = str(row[0])

    # Charger uniquement les exigences A2 applicables: le chat ne doit pas
    # citer des obligations qui ne concernent pas le tenant.
    with conn.cursor() as cur:
        if force:
            cur.execute("""
                SELECT r.requirement_id, r.requirement_text, r.req_type,
                       r.qse_domain, r.citation_ref
                FROM requirements r
                WHERE EXISTS (
                    SELECT 1
                    FROM applicability_decisions ad
                    WHERE ad.requirement_id = r.requirement_id
                      AND ad.profile_id = %s
                      AND ad.status IN ('APPLICABLE', 'APPLICABLE_SOUS_CONDITIONS')
                )
                ORDER BY r.qse_domain
            """, (profile_id,))
        else:
            cur.execute("""
                SELECT r.requirement_id, r.requirement_text, r.req_type,
                       r.qse_domain, r.citation_ref
                FROM requirements r
                WHERE EXISTS (
                    SELECT 1
                    FROM applicability_decisions ad
                    WHERE ad.requirement_id = r.requirement_id
                      AND ad.profile_id = %s
                      AND ad.status IN ('APPLICABLE', 'APPLICABLE_SOUS_CONDITIONS')
                )
                  AND NOT EXISTS (
                      SELECT 1 FROM requirement_embeddings e
                      WHERE e.requirement_id = r.requirement_id
                  )
                ORDER BY r.qse_domain
            """, (profile_id,))
        reqs = cur.fetchall()

    print(f"  {len(reqs)} exigence(s) Ã  indexer")
    if not reqs:
        print("  Toutes les exigences sont dÃ©jÃ  indexÃ©es.")
        conn.close()
        return 0

    indexed = 0
    import time
    for i, req in enumerate(reqs, 1):
        req_id, req_text, req_type, domain, citation = req
        # Le chunk garde le domaine et le type pour rendre l'embedding plus
        # discriminant qu'un texte legal nu.
        chunk = f"[{domain or ''}] [{req_type}] {req_text}"

        print(f"  [{i:3d}/{len(reqs)}] Embedding... {chunk[:60]}")
        embedding = _get_openai_embedding(chunk, openai_client)
        if embedding is None:
            continue

        # Encodage literal pgvector: [x1,x2,...]
        pg_vector = _to_vector_literal(embedding)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO requirement_embeddings (requirement_id, chunk_text, embedding)
                VALUES (%s, %s, %s::vector(1536))
                ON CONFLICT (requirement_id) DO UPDATE SET
                    chunk_text = EXCLUDED.chunk_text,
                    embedding  = EXCLUDED.embedding,
                    created_at = now()
            """, (str(req_id), chunk, pg_vector))
        conn.commit()
        indexed += 1

        if i % 10 == 0:
            time.sleep(0.5)  # Respecter les rate limits d'embeddings

    conn.close()
    print(f"\n  {indexed} exigence(s) indexÃ©e(s) avec succÃ¨s")
    return indexed


# â”€â”€â”€ Recherche RAG â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _search_pgvector(
    conn: psycopg.Connection,
    profile_id: str,
    query_embedding: list[float],
    top_k: int = TOP_K,
) -> list[dict]:
    """
    Recherche RAG vectorielle par similarite cosine pgvector.

    C'est l'etape "R" de RAG: la question est deja embeddee, puis comparee aux
    exigences applicables indexees. Le filtre A2 evite les sources hors scope.
    """
    if not query_embedding:
        return []
    query_vector = _to_vector_literal(query_embedding)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT e.requirement_id, e.chunk_text,
                   (1 - (e.embedding <=> %s::vector(1536)))::float AS similarity,
                   r.requirement_text, r.req_type, r.qse_domain, r.citation_ref,
                   d.title AS doc_title, d.source
            FROM requirement_embeddings e
            JOIN requirements r ON r.requirement_id = e.requirement_id
            JOIN documents d ON d.doc_id = r.doc_id
            WHERE e.embedding IS NOT NULL
              AND EXISTS (
                    SELECT 1
                    FROM applicability_decisions ad
                    WHERE ad.requirement_id = e.requirement_id
                      AND ad.profile_id = %s
                      AND ad.status IN ('APPLICABLE', 'APPLICABLE_SOUS_CONDITIONS')
              )
            ORDER BY e.embedding <=> %s::vector(1536)
            LIMIT %s
        """, (query_vector, profile_id, query_vector, int(top_k)))
        rows = cur.fetchall()

    if not rows:
        return []

    return [
        {
            "requirement_id": str(r[0]),
            "chunk_text": r[1],
            "similarity": round(float(r[2] or 0.0), 4),
            "requirement_text": r[3],
            "req_type": r[4],
            "qse_domain": r[5],
            "citation_ref": r[6],
            "doc_title": r[7],
            "source": r[8],
        }
        for r in rows
    ]


def _search_fulltext_fallback(
    conn: psycopg.Connection,
    profile_id: str,
    query: str,
    top_k: int = TOP_K,
) -> list[dict]:
    """
    Recherche textuelle de secours (sans pgvector).
    Utilise ILIKE sur les mots-cles de la question.

    Ce fallback garde le chat utilisable si pgvector, l'extension ou l'API
    embeddings sont indisponibles.
    """
    keywords = [w.strip() for w in query.split() if len(w.strip()) > 3][:5]
    if not keywords:
        keywords = [query[:20]]

    conditions = " OR ".join([f"r.requirement_text ILIKE %s" for _ in keywords])

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT r.requirement_id, r.requirement_text, r.req_type,
                   r.qse_domain, r.citation_ref,
                   d.title AS doc_title, d.source
            FROM requirements r
            JOIN documents d ON d.doc_id = r.doc_id
            WHERE {conditions}
              AND EXISTS (
                    SELECT 1
                    FROM applicability_decisions ad
                    WHERE ad.requirement_id = r.requirement_id
                      AND ad.profile_id = %s
                      AND ad.status IN ('APPLICABLE', 'APPLICABLE_SOUS_CONDITIONS')
              )
            LIMIT %s
        """, tuple(f"%{kw}%" for kw in keywords) + (profile_id, top_k))

        rows = cur.fetchall()
        return [
            {
                "requirement_id": str(r[0]),
                "requirement_text": r[1],
                "req_type": r[2],
                "qse_domain": r[3],
                "citation_ref": r[4],
                "doc_title": r[5],
                "source": r[6],
                "similarity": 0.5,  # score arbitraire pour le fallback
            }
            for r in rows
        ]


SYSTEM_PROMPT_CHAT_V2 = """Tu es un expert reglementaire QHSE virtuel pour QALITAS.

Tu aides des utilisateurs terrain, QHSE, direction et auditeurs.

REGLES CRITIQUES:
1. ZERO hallucination: utilise uniquement le contexte fourni.
2. Distingue toujours les obligations reglementaires, l'etat actuel de l'entreprise, et les actions a engager.
3. N'invente jamais une preuve, un statut de conformite, un gap, une action corrective ou une source juridique.
4. Si une information n'est pas presente, dis-le explicitement.
5. Cite les sources ou les elements de contexte les plus utiles.
6. Pour les questions de type KPI, utilise les indicateurs officiels fournis dans le contexte et ne recompose pas toi-meme un taux a partir d'autres compteurs.
7. Le JSON doit toujours etre parseable: aucune chaine non fermee, aucun texte hors JSON.
8. Pour un format tableau, n'utilise pas de tableau Markdown. Ecris des lignes texte courtes dans "answer" sous la forme: Point | Statut | Action.

STYLE DE REPONSE:
1. Sois bref, decisionnel et professionnel: pas de long paragraphe compact.
2. Commence par la conclusion utile en une phrase.
3. Pour une question simple: 80 a 140 mots. Pour une priorisation ou un plan d'action: 180 a 220 mots maximum.
4. Dans "answer", utilise des retours a la ligne et des listes courtes:
   **Synthese**
   ...
   **Priorites**
   - ...
   **Actions**
   1. ...
5. Maximum 3 priorites et 4 actions dans le texte principal.
6. Mets les details de sources dans "source_citations", pas dans un paragraphe encombre.
7. Chaque action doit etre concrete: verbe d'action, objet, preuve attendue ou delai si disponible.
8. Ne mentionne jamais une erreur technique, un parseur JSON ou une exception dans "answer".

FORMAT JSON STRICT:
{
  "answer": "Reponse claire, structuree et operationnelle...",
  "obligation_type": "OBLIGATOIRE|RECOMMANDE|MIXTE",
  "has_uncertainty": false,
  "uncertainty_note": null,
  "source_citations": [
    {
      "article_ref": "...",
      "doc_title": "...",
      "excerpt": "...",
      "obligation": "Ce que cela impose ou demontre"
    }
  ],
  "recommended_actions": [
    "Action concrete 1",
    "Action concrete 2"
  ]
}
"""


def _load_company_context_summary_v2(conn: psycopg.Connection, profile_id: str) -> str:
    """Charge un resume compact multi-site pour le prompt chat.

    Ce contexte complete les sources reglementaires avec la realite du tenant:
    sites, processus, activites, produits et substances.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_name, sector, sub_sector, country, certifications,
                   headcount_total, main_activities
            FROM company_profiles
            WHERE profile_id = %s
            """,
            (profile_id,),
        )
        profile_row = cur.fetchone()
        if not profile_row:
            return "Contexte entreprise non disponible"

        cur.execute(
            """
            SELECT site_name, city, region, site_type, employee_count, main_activities
            FROM company_sites
            WHERE profile_id = %s
            ORDER BY created_at NULLS LAST, site_name
            """,
            (profile_id,),
        )
        site_rows = cur.fetchall()

        cur.execute(
            """
            SELECT cp.process_name, COALESCE(cs.site_name, '')
            FROM company_processes cp
            LEFT JOIN company_sites cs ON cs.site_id = cp.site_id
            WHERE cp.profile_id = %s
            ORDER BY cp.created_at NULLS LAST, cp.process_name
            """,
            (profile_id,),
        )
        process_rows = cur.fetchall()

        cur.execute(
            """
            SELECT COALESCE(ca.activity_name, ''), COALESCE(cp.process_name, ''), COALESCE(cs.site_name, '')
            FROM company_activities ca
            LEFT JOIN company_processes cp ON cp.process_id = ca.process_id
            LEFT JOIN company_sites cs ON cs.site_id = ca.site_id
            WHERE ca.profile_id = %s
            ORDER BY ca.created_at NULLS LAST, ca.activity_name, ca.process_name
            """,
            (profile_id,),
        )
        activity_rows = cur.fetchall()

        cur.execute(
            """
            SELECT designation, COALESCE(category, ''), COALESCE(site_name, '')
            FROM company_products
            WHERE profile_id = %s
              AND COALESCE(reference, '') NOT LIKE 'chemical:%%'
            ORDER BY created_at NULLS LAST, designation
            LIMIT 8
            """,
            (profile_id,),
        )
        product_rows = cur.fetchall()

        cur.execute(
            """
            SELECT designation
            FROM company_products
            WHERE profile_id = %s
              AND (
                    COALESCE(reference, '') LIKE 'chemical:%%'
                 OR UPPER(COALESCE(category, '')) = 'CHEMICAL'
                 OR UPPER(COALESCE(product_type, '')) = 'CHEMICAL'
              )
            ORDER BY created_at NULLS LAST, designation
            LIMIT 8
            """,
            (profile_id,),
        )
        chemical_rows = cur.fetchall()

    certifications = ", ".join(profile_row[4] or []) or "N/A"
    headcount_total = profile_row[5]
    if headcount_total is None:
        headcount_total = sum(int(row[4] or 0) for row in site_rows)

    site_summary = "; ".join(
        f"{row[0]} ({row[1] or 'ville N/A'}; effectif={row[4] or 'N/A'}; activites={row[5] or 'N/A'})"
        for row in site_rows[:5]
    ) or "aucun site renseigne"

    process_summary = "; ".join(
        f"{row[0]}{f' @ {row[1]}' if row[1] else ''}"
        for row in process_rows[:6]
    ) or "aucun processus renseigne"

    activity_summary = "; ".join(
        _truncate_text(
            " | ".join(
                part for part in [row[0] or "Activite", row[1] or "", row[2] or ""] if part
            ),
            80,
        )
        for row in activity_rows[:6]
        if any(str(part or "").strip() for part in row)
    ) or "aucune activite renseignee"

    product_summary = "; ".join(
        _truncate_text(
            f"{row[0]}{f' [{row[1]}]' if row[1] else ''}{f' @ {row[2]}' if row[2] else ''}",
            80,
        )
        for row in product_rows[:6]
    ) or "aucun produit renseigne"

    chemical_summary = ", ".join(
        _truncate_text(row[0], 40) for row in chemical_rows[:8] if str(row[0] or "").strip()
    ) or "aucune substance renseignee"

    return (
        f"Entreprise: {profile_row[0]} | Secteur: {profile_row[1] or 'N/A'}"
        f"{f' / {profile_row[2]}' if profile_row[2] else ''} | Pays: {profile_row[3] or 'N/A'}\n"
        f"Certifications: {certifications}\n"
        f"Effectif total: {headcount_total or 'N/A'} | Activites principales: {profile_row[6] or 'N/A'}\n"
        f"Sites ({len(site_rows)}): {site_summary}\n"
        f"Processus ({len(process_rows)}): {process_summary}\n"
        f"Activites ({len(activity_rows)}): {activity_summary}\n"
        f"Produits: {product_summary}\n"
        f"Substances / chimiques: {chemical_summary}"
    )


def _load_operational_chat_context(
    conn: psycopg.Connection,
    profile_id: str,
    requirement_ids: list[str],
) -> dict[str, Any]:
    """Charge le contexte A2/A3 utilise par le chat.

    Le RAG ne se limite pas aux textes reglementaires: il ajoute les decisions
    A2, les controles A3, gaps, actions et preuves pour repondre sur l'etat
    reel de l'entreprise.
    """
    req_ids = [str(req_id) for req_id in requirement_ids if str(req_id or "").strip()]
    context: dict[str, Any] = {
        "snapshot": {
            "applicability_counts": {},
            "compliance_counts": {},
            "gap_counts": {},
            "action_counts": {},
            "evidence_total": 0,
        },
        "requirements": [],
        "gaps": [],
        "actions": [],
        "proofs": [],
    }

    with conn.cursor() as cur:
        # Snapshot KPI global: utile pour les questions de type "combien",
        # "taux", "reste a faire", sans appeler le LLM inutilement.
        cur.execute(
            """
            SELECT status, COUNT(*)
            FROM applicability_decisions
            WHERE profile_id = %s
            GROUP BY status
            """,
            (profile_id,),
        )
        context["snapshot"]["applicability_counts"] = {
            str(status or "INCONNU"): int(total or 0) for status, total in cur.fetchall()
        }

        cur.execute(
            """
            SELECT compliance_status, COUNT(*)
            FROM compliance_checks
            WHERE profile_id = %s
            GROUP BY compliance_status
            """,
            (profile_id,),
        )
        context["snapshot"]["compliance_counts"] = {
            str(status or "INCONNU"): int(total or 0) for status, total in cur.fetchall()
        }

        cur.execute(
            """
            SELECT severity, COUNT(*)
            FROM gaps
            WHERE profile_id = %s
            GROUP BY severity
            """,
            (profile_id,),
        )
        context["snapshot"]["gap_counts"] = {
            str(severity or "INCONNU"): int(total or 0) for severity, total in cur.fetchall()
        }

        cur.execute(
            """
            SELECT state, COUNT(*)
            FROM corrective_actions
            WHERE profile_id = %s
            GROUP BY state
            """,
            (profile_id,),
        )
        context["snapshot"]["action_counts"] = {
            str(state or "INCONNU"): int(total or 0) for state, total in cur.fetchall()
        }

        cur.execute(
            "SELECT COUNT(*) FROM compliance_evidence WHERE profile_id = %s",
            (profile_id,),
        )
        context["snapshot"]["evidence_total"] = int(cur.fetchone()[0] or 0)

        app_counts = context["snapshot"].get("applicability_counts") or {}
        compliance_counts = context["snapshot"].get("compliance_counts") or {}
        applicable_total = int(app_counts.get("APPLICABLE") or 0) + int(app_counts.get("APPLICABLE_SOUS_CONDITIONS") or 0)
        non_applicable_total = int(app_counts.get("NON_APPLICABLE") or 0)
        uncertain_total = int(app_counts.get("INCERTAIN") or 0)
        total_checks = sum(int(value or 0) for value in compliance_counts.values())
        conforme_total = int(compliance_counts.get("CONFORME") or 0)
        partiel_total = int(compliance_counts.get("PARTIELLEMENT_CONFORME") or 0) + int(compliance_counts.get("PARTIEL") or 0)
        non_conforme_total = int(compliance_counts.get("NON_CONFORME") or 0)
        absence_preuve_total = int(compliance_counts.get("ABSENCE_DE_PREUVE") or 0)
        non_evalue_total = int(compliance_counts.get("NON_EVALUE") or 0)
        compliance_rate = round(conforme_total / total_checks, 3) if total_checks > 0 else None
        compliance_coverage_rate = round(total_checks / applicable_total, 3) if applicable_total > 0 else None
        unevaluated_applicable_total = max(0, applicable_total - total_checks)

        context["snapshot"].update(
            {
                "applicable_total": applicable_total,
                "non_applicable_total": non_applicable_total,
                "uncertain_total": uncertain_total,
                "total_checks": total_checks,
                "conforme_total": conforme_total,
                "partiel_total": partiel_total,
                "non_conforme_total": non_conforme_total,
                "absence_preuve_total": absence_preuve_total,
                "non_evalue_total": non_evalue_total,
                "compliance_rate": compliance_rate,
                "compliance_coverage_rate": compliance_coverage_rate,
                "unevaluated_applicable_total": unevaluated_applicable_total,
            }
        )

        # Exigences applicables reliees au contexte operationnel. Si la
        # question est specifique, on limite aux IDs retrouves par retrieval.
        requirement_sql = """
            SELECT ad.requirement_id::text,
                   ad.status,
                   COALESCE(ad.scope_level, 'ORGANIZATION'),
                   COALESCE(ad.scope_key, 'ORGANIZATION'),
                   COALESCE(ad.scope_label, 'ORGANIZATION'),
                   COALESCE(ad.scope_site, ''),
                   COALESCE(ad.scope_process, ''),
                   COALESCE(ad.scope_activity, ''),
                   COALESCE(ad.justification, ''),
                   COALESCE(ad.company_data_used, ''),
                   COALESCE(r.requirement_text, ''),
                   COALESCE(r.citation_ref, ''),
                   COALESCE(cc.compliance_status, 'NON_EVALUE'),
                   cc.compliance_score,
                   COALESCE(cc.expected_proofs, ''),
                   COALESCE(cc.found_proofs, ''),
                   COALESCE(cc.missing_proofs, ''),
                   COALESCE(cc.analysis_detail, ''),
                   cc.updated_at
            FROM applicability_decisions ad
            JOIN requirements r ON r.requirement_id = ad.requirement_id
            LEFT JOIN compliance_checks cc
                ON cc.decision_id = ad.decision_id
            WHERE ad.profile_id = %s
              AND ad.status IN ('APPLICABLE', 'APPLICABLE_SOUS_CONDITIONS')
        """
        requirement_params: list[Any] = [profile_id]
        if req_ids:
            requirement_sql += " AND ad.requirement_id = ANY(%s::uuid[])"
            requirement_params.append(req_ids)
        else:
            requirement_sql += """
              AND COALESCE(cc.compliance_status, 'NON_EVALUE') IN (
                    'ABSENCE_DE_PREUVE',
                    'NON_CONFORME',
                    'PARTIELLEMENT_CONFORME',
                    'PARTIEL',
                    'NON_EVALUE'
              )
            """
        requirement_sql += """
            ORDER BY CASE COALESCE(cc.compliance_status, 'NON_EVALUE')
                         WHEN 'ABSENCE_DE_PREUVE' THEN 0
                         WHEN 'NON_CONFORME' THEN 1
                         WHEN 'PARTIELLEMENT_CONFORME' THEN 2
                         WHEN 'PARTIEL' THEN 2
                         WHEN 'NON_EVALUE' THEN 3
                         ELSE 9
                     END,
                     ad.requirement_id,
                     CASE ad.scope_level
                         WHEN 'ORGANIZATION' THEN 0
                         WHEN 'SITE' THEN 1
                         WHEN 'PROCESS' THEN 2
                         WHEN 'ACTIVITY' THEN 3
                         ELSE 9
                     END,
                     ad.updated_at DESC
            LIMIT 18
        """
        cur.execute(requirement_sql, tuple(requirement_params))
        context["requirements"] = effective_applicability_rows([
            {
                "requirement_id": row[0],
                "status": row[1],
                "applicability_status": row[1],
                "scope_level": row[2],
                "scope_key": row[3],
                "scope_label": row[4],
                "scope_site": row[5],
                "scope_process": row[6],
                "scope_activity": row[7],
                "justification": row[8],
                "company_data_used": row[9],
                "requirement_text": row[10],
                "citation_ref": row[11],
                "compliance_status": row[12],
                "compliance_score": float(row[13]) if row[13] is not None else None,
                "expected_proofs": row[14],
                "found_proofs": row[15],
                "missing_proofs": row[16],
                "analysis_detail": row[17],
                "updated_at": row[18].isoformat() if row[18] else None,
            }
            for row in cur.fetchall()
        ])

        gap_sql = """
            SELECT g.requirement_id::text,
                   g.gap_type,
                   g.severity,
                   COALESCE(g.description, ''),
                   COALESCE(g.missing_proof, ''),
                   COALESCE(g.legal_impact, ''),
                   COALESCE(g.treatment_priority, ''),
                   COALESCE(g.scope_level, 'ORGANIZATION'),
                   COALESCE(g.scope_key, 'ORGANIZATION'),
                   COALESCE(g.scope_label, 'ORGANIZATION'),
                   COALESCE(g.scope_site, ''),
                   COALESCE(g.scope_process, ''),
                   COALESCE(g.scope_activity, ''),
                   g.created_at
            FROM gaps g
            WHERE g.profile_id = %s
        """
        gap_params: list[Any] = [profile_id]
        if req_ids:
            gap_sql += " AND g.requirement_id = ANY(%s::uuid[])"
            gap_params.append(req_ids)
        gap_sql += """
            ORDER BY CASE g.severity
                         WHEN 'CRITIQUE' THEN 0
                         WHEN 'MAJEURE' THEN 1
                         WHEN 'MINEURE' THEN 2
                         ELSE 9
                     END,
                     g.created_at DESC
            LIMIT 8
        """
        cur.execute(gap_sql, tuple(gap_params))
        context["gaps"] = [
            {
                "requirement_id": row[0],
                "gap_type": row[1],
                "severity": row[2],
                "description": row[3],
                "missing_proof": row[4],
                "legal_impact": row[5],
                "treatment_priority": row[6],
                "scope_level": row[7],
                "scope_key": row[8],
                "scope_label": row[9],
                "scope_site": row[10],
                "scope_process": row[11],
                "scope_activity": row[12],
                "created_at": row[13].isoformat() if row[13] else None,
            }
            for row in cur.fetchall()
        ]

        action_sql = """
            SELECT g.requirement_id::text,
                   COALESCE(ca.action_title, ''),
                   COALESCE(ca.action_description, ''),
                   COALESCE(ca.action_type, ''),
                   COALESCE(ca.state, ''),
                   COALESCE(ca.responsible, ''),
                   ca.due_date,
                   COALESCE(ca.expected_proof, ''),
                   COALESCE(ca.scope_level, 'ORGANIZATION'),
                   COALESCE(ca.scope_key, 'ORGANIZATION'),
                   COALESCE(ca.scope_label, 'ORGANIZATION'),
                   COALESCE(cs.site_name, ''),
                   COALESCE(cp.process_name, ''),
                   COALESCE(act.activity_name, ''),
                   ca.updated_at
            FROM corrective_actions ca
            LEFT JOIN gaps g ON g.gap_id = ca.gap_id
            LEFT JOIN company_sites cs ON cs.site_id = ca.site_id
            LEFT JOIN company_processes cp ON cp.process_id = ca.process_id
            LEFT JOIN company_activities act ON act.activity_id = ca.activity_id
            WHERE ca.profile_id = %s
        """
        action_params: list[Any] = [profile_id]
        if req_ids:
            action_sql += " AND g.requirement_id = ANY(%s::uuid[])"
            action_params.append(req_ids)
        action_sql += """
            ORDER BY CASE ca.state
                         WHEN 'PLANIFIEE' THEN 0
                         WHEN 'EN_COURS' THEN 1
                         WHEN 'REALISEE' THEN 2
                         ELSE 9
                     END,
                     ca.updated_at DESC
            LIMIT 8
        """
        cur.execute(action_sql, tuple(action_params))
        context["actions"] = [
            {
                "requirement_id": row[0],
                "action_title": row[1],
                "action_description": row[2],
                "action_type": row[3],
                "state": row[4],
                "responsible": row[5],
                "due_date": row[6].isoformat() if row[6] else None,
                "expected_proof": row[7],
                "scope_level": row[8],
                "scope_key": row[9],
                "scope_label": row[10],
                "scope_site": row[11],
                "scope_process": row[12],
                "scope_activity": row[13],
                "updated_at": row[14].isoformat() if row[14] else None,
            }
            for row in cur.fetchall()
        ]

        proof_sql = """
            SELECT ce.requirement_id::text,
                   COALESCE(ce.title, ''),
                   COALESCE(ce.evidence_type, ''),
                   COALESCE(ce.source_type, ''),
                   COALESCE(ce.scope_level, 'ORGANIZATION'),
                   COALESCE(ce.scope_key, 'ORGANIZATION'),
                   COALESCE(ce.scope_label, 'ORGANIZATION'),
                   COALESCE(cs.site_name, ''),
                   COALESCE(cp.process_name, ''),
                   COALESCE(ca.activity_name, ''),
                   COALESCE(ce.raw_text, ''),
                   ce.uploaded_at
            FROM compliance_evidence ce
            LEFT JOIN company_sites cs ON cs.site_id = ce.site_id
            LEFT JOIN company_processes cp ON cp.process_id = ce.process_id
            LEFT JOIN company_activities ca ON ca.activity_id = ce.activity_id
            WHERE ce.profile_id = %s
        """
        proof_params: list[Any] = [profile_id]
        if req_ids:
            proof_sql += " AND (ce.requirement_id = ANY(%s::uuid[]) OR ce.requirement_id IS NULL)"
            proof_params.append(req_ids)
        proof_sql += """
            ORDER BY CASE WHEN ce.requirement_id IS NULL THEN 1 ELSE 0 END,
                     ce.uploaded_at DESC
            LIMIT 8
        """
        cur.execute(proof_sql, tuple(proof_params))
        context["proofs"] = [
            {
                "requirement_id": row[0],
                "title": row[1],
                "evidence_type": row[2],
                "source_type": row[3],
                "scope_level": row[4],
                "scope_key": row[5],
                "scope_label": row[6],
                "scope_site": row[7],
                "scope_process": row[8],
                "scope_activity": row[9],
                "raw_text": row[10],
                "uploaded_at": row[11].isoformat() if row[11] else None,
            }
            for row in cur.fetchall()
        ]

    context["requirements"].sort(
        key=lambda item: (
            str(item.get("requirement_id") or ""),
            _scope_rank(str(item.get("scope_level") or "")),
            str(item.get("scope_key") or ""),
        )
    )
    return context


def _format_operational_chat_context(
    operational_context: dict[str, Any],
    retrieved_docs: list[dict],
) -> str:
    """Transforme le contexte A2/A3 en bloc texte lisible pour le prompt."""
    snapshot = operational_context.get("snapshot") or {}
    retrieved_by_id = {
        str(doc.get("requirement_id") or ""): doc
        for doc in retrieved_docs
        if str(doc.get("requirement_id") or "").strip()
    }

    lines = [
        "Snapshot global:",
        f"- Applicabilite A2: {_counts_to_text(snapshot.get('applicability_counts') or {})}",
        f"- Conformite A3: {_counts_to_text(snapshot.get('compliance_counts') or {})}",
        f"- Gaps: {_counts_to_text(snapshot.get('gap_counts') or {})}",
        f"- Actions: {_counts_to_text(snapshot.get('action_counts') or {})}",
        f"- Preuves disponibles: {int(snapshot.get('evidence_total') or 0)}",
    ]

    if snapshot.get("compliance_rate") is not None:
        lines.append(
            f"- KPI officiel A3: taux_conformite={_format_percent(_snapshot_float(snapshot, 'compliance_rate'))} "
            f"| verifications={_snapshot_int(snapshot, 'total_checks')} "
            f"| conformes={_snapshot_int(snapshot, 'conforme_total')} "
            f"| partiels={_snapshot_int(snapshot, 'partiel_total')} "
            f"| non_conformes={_snapshot_int(snapshot, 'non_conforme_total')} "
            f"| absences_preuve={_snapshot_int(snapshot, 'absence_preuve_total')}"
        )
    if _snapshot_int(snapshot, "applicable_total") > 0:
        lines.append(
            f"- KPI couverture perimetre applicable: applicable_A2={_snapshot_int(snapshot, 'applicable_total')} "
            f"| evaluees_A3={_snapshot_int(snapshot, 'total_checks')} "
            f"| couverture={_format_percent(_snapshot_float(snapshot, 'compliance_coverage_rate'))} "
            f"| applicables_non_evaluees={_snapshot_int(snapshot, 'unevaluated_applicable_total')}"
        )

    requirement_lines: list[str] = []
    for item in (operational_context.get("requirements") or [])[:12]:
        source = retrieved_by_id.get(str(item.get("requirement_id") or ""), {})
        ref = source.get("citation_ref") or source.get("doc_title") or item.get("requirement_id") or "N/A"
        scope_text = _format_scope_text(item)
        detail_parts = [
            f"{ref}",
            f"Perimetre: {scope_text}",
            f"A2: {item.get('applicability_status') or 'N/A'}",
            f"A3: {item.get('compliance_status') or 'NON_EVALUE'}",
        ]
        if item.get("missing_proofs"):
            detail_parts.append(f"preuves manquantes: {_truncate_text(item.get('missing_proofs'), 140)}")
        elif item.get("found_proofs"):
            detail_parts.append(f"preuves trouvees: {_truncate_text(item.get('found_proofs'), 140)}")
        elif item.get("analysis_detail"):
            detail_parts.append(f"analyse: {_truncate_text(item.get('analysis_detail'), 140)}")
        requirement_lines.append("- " + " | ".join(detail_parts))

    if requirement_lines:
        lines.append("Exigences reliees a la question:")
        lines.extend(requirement_lines)

    gap_lines = [
        "- "
        + " | ".join(
            part for part in [
                f"{item.get('severity') or 'N/A'}",
                _format_scope_text(item),
                _truncate_text(item.get("description"), 150),
                f"preuve manquante: {_truncate_text(item.get('missing_proof'), 90)}"
                if item.get("missing_proof") else "",
            ] if part
        )
        for item in (operational_context.get("gaps") or [])[:6]
    ]
    if gap_lines:
        lines.append("Ecarts ouverts / significatifs:")
        lines.extend(gap_lines)

    action_lines = [
        "- "
        + " | ".join(
            part for part in [
                f"{item.get('state') or 'N/A'}",
                _format_scope_text(item),
                _truncate_text(item.get("action_title"), 100),
                f"responsable: {item.get('responsible')}" if item.get("responsible") else "",
                f"echeance: {item.get('due_date')}" if item.get("due_date") else "",
                f"preuve attendue: {_truncate_text(item.get('expected_proof'), 90)}"
                if item.get("expected_proof") else "",
            ] if part
        )
        for item in (operational_context.get("actions") or [])[:6]
    ]
    if action_lines:
        lines.append("Actions correctives / preventives:")
        lines.extend(action_lines)

    proof_lines = [
        "- "
        + " | ".join(
            part for part in [
                _format_scope_text(item),
                item.get("title") or "Preuve",
                item.get("evidence_type") or "",
                _truncate_text(item.get("raw_text"), 140) if item.get("raw_text") else "",
            ] if part
        )
        for item in (operational_context.get("proofs") or [])[:6]
    ]
    if proof_lines:
        lines.append("Preuves recentes / disponibles:")
        lines.extend(proof_lines)

    return "\n".join(lines)


def _build_chat_prompt_v2(
    question: str,
    retrieved_docs: list[dict],
    company_context: str,
    operational_context: str,
    user_role: str = "expert",
    response_format: str = "synthesis",
) -> str:
    """Assemble le prompt final: question + sources RAG + contexte tenant."""
    role_desc = {
        "terrain": "operationnel terrain (langage simple, actions pratiques)",
        "expert": "responsable QHSE (langage technique, precisions reglementaires)",
        "direction": "direction (vue synthetique, risques et enjeux)",
        "auditeur": "auditeur (references exactes, preuves attendues, non-conformites potentielles)",
    }.get(user_role, "expert")
    format_desc = {
        "synthesis": "synthese courte avec priorites et actions",
        "checklist": "checklist operationnelle en liste numerotee",
        "audit": "lecture audit: constats, preuves attendues, risque de non-conformite",
        "table": "tableau texte robuste: lignes 'Point | Statut | Action', sans tableau Markdown",
    }.get(response_format, "synthese courte avec priorites et actions")

    docs_block = "\n\n".join(
        [
            f"--- Source {i + 1} ---\n"
            f"Reference: {d.get('citation_ref') or d.get('doc_title', 'N/A')}\n"
            f"Domaine: {d.get('qse_domain', 'N/A')} | Type: {d.get('req_type', 'N/A')}\n"
            f"Texte: {_truncate_text(d.get('requirement_text') or d.get('chunk_text', ''), 520)}"
            for i, d in enumerate(retrieved_docs)
        ]
    ) if retrieved_docs else "Aucune exigence reglementaire retrouvee dans le corpus injecte."

    return f"""PROFIL UTILISATEUR: {role_desc}
FORMAT ATTENDU: {format_desc}

QUESTION:
{question}

=== EXIGENCES REGLEMENTAIRES APPLICABLES ===
{docs_block}

=== CONTEXTE ENTREPRISE ===
{company_context}

=== ETAT OPERATIONNEL ACTUEL (A2/A3, gaps, actions, preuves) ===
{operational_context}

=== INSTRUCTIONS ===
Reponds en te basant UNIQUEMENT sur les sources fournies ci-dessus.
Quand tu parles d'obligations reglementaires, appuie-toi sur le corpus reglementaire.
Quand tu parles de la situation actuelle de l'entreprise, appuie-toi sur l'etat operationnel.
Quand la question porte sur un KPI (taux de conformite, nombre de non-conformites, couverture), utilise en priorite les KPI officiels presents dans le snapshot.
Ne recalcule jamais un taux de conformite en divisant le nombre de conformes par le nombre d'exigences applicables A2.
Ne confonds jamais:
- taux de conformite A3 = conformes / verifications A3
- couverture du perimetre applicable = verifications A3 / exigences applicables A2
Si l'information n'est pas disponible, dis-le explicitement.
Si les preuves ou donnees sont insuffisantes, indique ce qui manque.
Quand c'est pertinent, structure ta reponse autour de:
1. Exigence applicable
2. Etat actuel
3. Preuves disponibles / manquantes
4. Action recommandee

Style attendu dans le champ "answer":
- une synthese directe en premiere ligne;
- des listes courtes, jamais un seul bloc de texte;
- pas plus de 4 actions operationnelles;
- pas de repetition des citations detaillees, car elles doivent etre dans "source_citations";
- si une echeance, un site, un responsable ou une preuve attendue est present dans le contexte, fais-le apparaitre dans l'action.
- si le format attendu est un tableau, utilise des lignes courtes "Point | Statut | Action" et n'ajoute pas de ligne Markdown avec des tirets;
- n'inclus jamais une erreur technique, un nom d'exception ou un probleme JSON dans la reponse utilisateur.

Adapte ta reponse au profil: {role_desc}
Respecte le format attendu: {format_desc}
Reponds en JSON strict selon le format defini.
"""


def _build_default_citations(
    retrieved_docs: list[dict],
    operational_context: dict[str, Any],
) -> list[dict]:
    """Construit des citations de secours a partir des sources chargees."""
    citations = [
        {
            "requirement_id": d.get("requirement_id"),
            "article_ref": d.get("citation_ref") or d.get("doc_title") or "N/A",
            "doc_title": d.get("doc_title", ""),
            "excerpt": _truncate_text(d.get("requirement_text") or d.get("chunk_text", ""), 300),
            "obligation": d.get("req_type", ""),
        }
        for d in retrieved_docs[:5]
    ]
    if citations:
        return citations

    fallback: list[dict] = []
    for item in (operational_context.get("requirements") or [])[:3]:
        fallback.append(
            {
                "source_kind": "A3_REQUIREMENT",
                "requirement_id": item.get("requirement_id"),
                "article_ref": item.get("citation_ref") or f"A3 {item.get('compliance_status') or 'NON_EVALUE'}",
                "doc_title": _format_scope_text(item),
                "excerpt": _truncate_text(
                    item.get("missing_proofs")
                    or item.get("found_proofs")
                    or item.get("analysis_detail")
                    or item.get("justification"),
                    280,
                ),
                "obligation": item.get("applicability_status") or "",
            }
        )
    for item in (operational_context.get("gaps") or [])[:2]:
        fallback.append(
            {
                "source_kind": "GAP",
                "requirement_id": item.get("requirement_id"),
                "article_ref": f"Gap {item.get('severity') or 'N/A'}",
                "doc_title": _format_scope_text(item),
                "excerpt": _truncate_text(item.get("description"), 280),
                "obligation": item.get("gap_type") or "",
            }
        )
    for item in (operational_context.get("proofs") or [])[:2]:
        fallback.append(
            {
                "source_kind": "EVIDENCE",
                "requirement_id": item.get("requirement_id"),
                "article_ref": item.get("title") or "Preuve",
                "doc_title": _format_scope_text(item),
                "excerpt": _truncate_text(item.get("raw_text"), 280),
                "obligation": item.get("evidence_type") or "",
            }
        )
    return fallback[:5]


def _build_llm_error_fallback_response(
    retrieved_docs: list[dict],
    operational_context: dict[str, Any],
    response_format: str,
) -> ChatResponseLLM:
    """Construit une reponse utilisateur propre quand le JSON LLM est invalide."""
    snapshot = operational_context.get("snapshot") or {}
    retrieved_count = len(retrieved_docs or [])
    context_req_count = len(operational_context.get("requirements") or [])
    gap_count = len(operational_context.get("gaps") or [])
    action_count = len(operational_context.get("actions") or [])
    proof_count = len(operational_context.get("proofs") or [])
    evidence_total = int(snapshot.get("evidence_total") or proof_count or 0)
    absence_count = _snapshot_int(snapshot, "absence_preuve_total")
    non_conforme_count = _snapshot_int(snapshot, "non_conforme_total")

    actions = [
        "Controler les exigences A3 en absence de preuve ou non conformes.",
        "Rattacher les justificatifs disponibles au bon site, processus ou exigence.",
    ]
    if gap_count > 0:
        actions.insert(0, "Traiter les ecarts ouverts en priorite selon leur gravite.")
    if action_count > 0:
        actions.append("Verifier l'avancement des actions correctives deja planifiees.")
    actions = actions[:4]

    if response_format == "table":
        answer = (
            "**Tableau de synthese**\n"
            "- Point | Statut | Action\n"
            f"- Sources reglementaires | {retrieved_count} exigence(s) retrouvee(s) | Controler les citations affichees\n"
            f"- Controle A3 | {context_req_count} exigence(s) chargee(s), {absence_count} absence(s) de preuve, {non_conforme_count} non-conformite(s) | Prioriser les points bloquants\n"
            f"- Preuves | {evidence_total} preuve(s) disponible(s) | Identifier les justificatifs manquants et les rattacher\n"
            f"- Ecarts et actions | {gap_count} ecart(s), {action_count} action(s) | Planifier ou mettre a jour les actions correctives"
        )
    elif response_format == "checklist":
        answer = (
            "**Checklist de controle**\n"
            f"1. Verifier les {absence_count} cas en absence de preuve et les {non_conforme_count} non-conformite(s) A3.\n"
            f"2. Revoir les {gap_count} ecart(s) ouverts et leurs preuves attendues.\n"
            f"3. Rattacher les {evidence_total} preuve(s) disponibles aux exigences concernees.\n"
            "4. Relancer l'analyse A3 apres import ou mise a jour des justificatifs."
        )
    elif response_format == "audit":
        answer = (
            "**Lecture audit**\n"
            "La generation IA complete n'a pas ete finalisee, mais le contexte QALITAS permet un controle initial.\n\n"
            "**Constats a verifier**\n"
            f"- Exigences chargees: {context_req_count}; sources reglementaires retrouvees: {retrieved_count}.\n"
            f"- Absences de preuve A3: {absence_count}; non-conformites: {non_conforme_count}; ecarts ouverts: {gap_count}.\n\n"
            "**Preuves attendues**\n"
            "Verifier les justificatifs rattaches, les preuves manquantes et les actions correctives associees."
        )
    else:
        answer = (
            "**Synthese**\n"
            "Je n'ai pas pu finaliser la generation IA complete, mais les donnees QALITAS chargees permettent un premier controle.\n\n"
            "**A controler**\n"
            f"- Sources reglementaires retrouvees: {retrieved_count}.\n"
            f"- Exigences A3 chargees: {context_req_count}; absences de preuve: {absence_count}; non-conformites: {non_conforme_count}.\n"
            f"- Preuves disponibles: {evidence_total}; ecarts ouverts: {gap_count}; actions: {action_count}.\n\n"
            "**Actions**\n"
            "1. Identifier les exigences en absence de preuve ou non conformes.\n"
            "2. Ajouter ou rattacher les justificatifs manquants.\n"
            "3. Reexecuter le controle A3 pour confirmer le passage en conforme."
        )

    citations = _build_default_citations([], operational_context)
    if not citations:
        citations = _build_default_citations(retrieved_docs, operational_context)

    return ChatResponseLLM(
        answer=answer,
        obligation_type="MIXTE",
        has_uncertainty=True,
        uncertainty_note="Generation IA incomplete; reponse de secours fondee sur le contexte QALITAS charge.",
        source_citations=citations,
        recommended_actions=actions,
    )




# â”€â”€â”€ Pipeline de chat â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def chat(
    question: str,
    tenant_id: str,
    session_id: str | None = None,
    user_role: str = "expert",
    response_format: str = "synthesis",
    persist_history: bool = True,
) -> dict:
    """
    Traite une question utilisateur et retourne une rÃ©ponse sourcÃ©e.
    Point d'entrÃ©e principal pour l'API et le mode interactif.

    Pipeline RAG:
      1. retrouver le tenant et la session;
      2. repondre directement si la question est deterministe;
      3. embedder la question et chercher les exigences applicables;
      4. charger le contexte operationnel A2/A3;
      5. generer la reponse avec le LLM;
      6. verifier/normaliser les citations puis sauvegarder l'historique.
    """
    conn = _get_conn(tenant_id)
    # Profil
    with conn.cursor() as cur:
        cur.execute(
            "SELECT profile_id FROM company_profiles WHERE tenant_id=%s", (tenant_id,)
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            return {"error": f"Tenant inconnu: {tenant_id}"}
        profile_id = str(row[0])

    # Session + persistance (dÃ©sactivable pour les rÃ´les lecture seule)
    if persist_history:
        if not session_id:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO chat_sessions (profile_id, user_role)
                    VALUES (%s, %s) RETURNING session_id
                """, (profile_id, user_role))
                session_id = str(cur.fetchone()[0])
            conn.commit()
        else:
            # VÃ©rifier que la session appartient bien au tenant demandeur
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM chat_sessions cs
                    JOIN company_profiles cp ON cp.profile_id = cs.profile_id
                    WHERE cs.session_id = %s AND cp.tenant_id = %s
                """, (session_id, tenant_id))
                if not cur.fetchone():
                    conn.close()
                    return {"error": "Session introuvable ou n'appartient pas Ã  ce tenant"}

        # Sauvegarde de la question
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_messages (session_id, role, content)
                VALUES (%s, 'user', %s)
            """, (session_id, question))
        conn.commit()
    else:
        session_id = None

    # Contexte entreprise multi-site injecte dans le prompt final.
    company_ctx = _load_company_context_summary_v2(conn, profile_id)

    # Court-circuit deterministic: definitions, glossaire, aides simples.
    # Ces reponses evitent un retrieval/LLM quand la question ne demande pas
    # un raisonnement sur le corpus du tenant.
    quick_response = _build_deterministic_assistant_response(question)
    if quick_response is not None:
        quick_response.source_citations = _verify_source_citations(
            quick_response.source_citations,
            [],
            {},
        )
        quick_response = _normalize_chat_response_shape(quick_response)
        if persist_history and session_id:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO chat_messages
                        (session_id, role, content, source_requirement_ids,
                         has_uncertainty, uncertainty_note, llm_model)
                    VALUES (%s, 'assistant', %s, %s, %s, %s, %s)
                """, (
                    session_id,
                    quick_response.answer,
                    None,
                    quick_response.has_uncertainty,
                    quick_response.uncertainty_note,
                    "deterministic_assistant",
                ))
                cur.execute("""
                    UPDATE chat_sessions SET last_activity_at = now()
                    WHERE session_id = %s
                """, (session_id,))
            conn.commit()
        conn.close()
        return {
            "session_id": session_id,
            "question": question,
            "answer": quick_response.answer,
            "obligation_type": quick_response.obligation_type,
            "has_uncertainty": quick_response.has_uncertainty,
            "uncertainty_note": quick_response.uncertainty_note,
            "source_citations": quick_response.source_citations,
            "recommended_actions": quick_response.recommended_actions,
            "retrieved_sources": 0,
            "retrieval_top_k": 0,
            "search_mode": "deterministic",
            "llm_model": "deterministic_assistant",
            "context_snapshot": {},
        }

    # Court-circuit contexte entreprise: questions sur sites, activites,
    # effectifs, produits, substances, etc.
    company_response = _build_company_context_response(conn, profile_id, question)
    if company_response is not None:
        company_response.source_citations = _verify_source_citations(
            company_response.source_citations,
            [],
            {},
        )
        company_response = _normalize_chat_response_shape(company_response)
        if persist_history and session_id:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO chat_messages
                        (session_id, role, content, source_requirement_ids,
                         has_uncertainty, uncertainty_note, llm_model)
                    VALUES (%s, 'assistant', %s, %s, %s, %s, %s)
                """, (
                    session_id,
                    company_response.answer,
                    None,
                    company_response.has_uncertainty,
                    company_response.uncertainty_note,
                    "deterministic_company_context",
                ))
                cur.execute("""
                    UPDATE chat_sessions SET last_activity_at = now()
                    WHERE session_id = %s
                """, (session_id,))
            conn.commit()
        conn.close()
        return {
            "session_id": session_id,
            "question": question,
            "answer": company_response.answer,
            "obligation_type": company_response.obligation_type,
            "has_uncertainty": company_response.has_uncertainty,
            "uncertainty_note": company_response.uncertainty_note,
            "source_citations": company_response.source_citations,
            "recommended_actions": company_response.recommended_actions,
            "retrieved_sources": 0,
            "retrieval_top_k": 0,
            "search_mode": "company_context",
            "llm_model": "deterministic_company_context",
            "context_snapshot": {},
        }

    # Recherche RAG: vector search si pgvector + embeddings sont disponibles,
    # sinon fallback textuel pour garder le service utilisable.
    retrieved = []
    actual_search_mode = "fulltext_fallback"
    retrieval_top_k = _top_k_for_question(question)

    try:
        _ensure_pgvector_schema(conn)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"  [WARN] Schema pgvector non pret, fallback textuel: {e}")

    if _pgvector_available(conn):
        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        if openai_key:
            try:
                from openai import OpenAI
                oc = OpenAI(api_key=openai_key)
                embedding = _get_openai_embedding(question, oc)
                if embedding:
                    retrieved = _search_pgvector(conn, profile_id, embedding, retrieval_top_k)
                    if retrieved:
                        actual_search_mode = "vector_search"
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                print(f"  [WARN] vector search Ã©chouÃ©, fallback textuel: {e}")

    if not retrieved:
        # Fallback: recherche textuelle
        try:
            conn.rollback()
        except Exception:
            pass
        retrieved = _search_fulltext_fallback(conn, profile_id, question, retrieval_top_k)
        actual_search_mode = "fulltext_fallback"

    # Les IDs retrouves pilotent le contexte operationnel: sur question
    # specifique on charge les controles/gaps/preuves de ces exigences; sur
    # question generale on charge un contexte A3 plus global.
    requirement_ids = [
        str(item.get("requirement_id") or "")
        for item in retrieved
        if str(item.get("requirement_id") or "").strip()
    ]
    operational_requirement_ids = [] if _is_operational_context_question(question) else requirement_ids
    operational_context = _load_operational_chat_context(conn, profile_id, operational_requirement_ids)
    operational_ctx_text = _format_operational_chat_context(operational_context, retrieved)

    # Generer la reponse: certaines questions KPI/preuves manquantes sont
    # repondues deterministiquement apres chargement du snapshot A2/A3.
    response_model_used: str | None = None
    deterministic_response = _build_deterministic_assistant_response(question)
    deterministic_kind = "deterministic_assistant" if deterministic_response is not None else None
    if deterministic_response is None:
        deterministic_response = _build_deterministic_kpi_response(question, operational_context)
        deterministic_kind = "deterministic_kpi" if deterministic_response is not None else None
    if deterministic_response is None:
        deterministic_response = _build_missing_proofs_response(question, operational_context, response_format)
        deterministic_kind = "deterministic_missing_proofs" if deterministic_response is not None else None
    if deterministic_response is not None:
        response = deterministic_response
        response_model_used = deterministic_kind
    else:
        llm = get_llm_client()
        user_prompt = _build_chat_prompt_v2(
            question,
            retrieved,
            company_ctx,
            operational_ctx_text,
            user_role,
            response_format,
        )
        try:
            raw = llm.call_json(SYSTEM_PROMPT_CHAT_V2, user_prompt, max_tokens=1400)
            response = ChatResponseLLM(**raw)
            response_model_used = llm.last_model_used
        except Exception:
            response = _build_llm_error_fallback_response(
                retrieved,
                operational_context,
                response_format,
            )
            response_model_used = llm.last_model_used

    # Si le LLM n'a retourne aucune citation, on injecte les sources disponibles.
    # Puis _verify_source_citations retire les citations non ancrees dans les
    # exigences recuperees ou le contexte operationnel.
    if not response.source_citations and response_model_used != "deterministic_assistant":
        response.source_citations = _build_default_citations(retrieved, operational_context)
    response.source_citations = _verify_source_citations(
        response.source_citations,
        retrieved,
        operational_context,
    )
    response = _normalize_chat_response_shape(response)

    # Extraire les IDs de requirements citees (verifiees si possible)
    source_req_ids = [
        str(item.get("requirement_id") or "")
        for item in response.source_citations
        if str(item.get("requirement_id") or "").strip()
    ] or [
        str(d.get("requirement_id") or "")
        for d in retrieved[:5]
        if str(d.get("requirement_id") or "").strip()
    ]

    # Sauvegarde de la rÃ©ponse (si persistance active)
    if persist_history and session_id:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_messages
                    (session_id, role, content, source_requirement_ids,
                     has_uncertainty, uncertainty_note, llm_model)
                VALUES (%s, 'assistant', %s, %s, %s, %s, %s)
            """, (
                session_id,
                response.answer,
                source_req_ids or None,
                response.has_uncertainty,
                response.uncertainty_note,
                response_model_used,
            ))
            cur.execute("""
                UPDATE chat_sessions SET last_activity_at = now()
                WHERE session_id = %s
            """, (session_id,))
        conn.commit()
    conn.close()

    return {
        "session_id": session_id,
        "question": question,
        "answer": response.answer,
        "obligation_type": response.obligation_type,
        "has_uncertainty": response.has_uncertainty,
        "uncertainty_note": response.uncertainty_note,
        "source_citations": response.source_citations,
        "recommended_actions": response.recommended_actions,
        "retrieved_sources": len(retrieved),
        "retrieval_top_k": retrieval_top_k,
        "search_mode": actual_search_mode,
        "llm_model": response_model_used,
        "context_snapshot": operational_context.get("snapshot") or {},
    }


def get_session_history(session_id: str, tenant_id: str) -> list[dict]:
    """Retourne l'historique d'une session de chat (vÃ©rifie l'appartenance tenant)."""
    conn = _get_conn(tenant_id)
    with conn.cursor() as cur:
        # ContrÃ´le tenant : la session doit appartenir au tenant demandeur.
        # On valide via le profil (chat_sessions.profile_id -> company_profiles.tenant_id)
        # pour rester compatible avec le schÃ©ma DB actuel (sans colonne tenant_id dans chat_sessions).
        cur.execute(
            """
            SELECT 1
            FROM chat_sessions cs
            JOIN company_profiles cp ON cp.profile_id = cs.profile_id
            WHERE cs.session_id = %s
              AND cp.tenant_id = %s
            """,
            (session_id, tenant_id),
        )
        if not cur.fetchone():
            conn.close()
            return []

        cur.execute("""
            SELECT role, content, has_uncertainty, source_requirement_ids,
                   llm_model, created_at
            FROM chat_messages
            WHERE session_id = %s
            ORDER BY created_at
        """, (session_id,))
        rows = cur.fetchall()
    conn.close()
    return [
        {
            "role": r[0],
            "content": r[1],
            "has_uncertainty": r[2],
            "source_req_ids": [str(x) for x in (r[3] or [])],
            "llm_model": r[4],
            "created_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]


# â”€â”€â”€ Mode interactif â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def interactive_chat(tenant_id: str, user_role: str = "expert") -> None:
    """Mode chat interactif en ligne de commande."""
    print(f"\n=== Agent 4 â€” Chat Expert QALITAS ({tenant_id}) ===")
    print(f"Role: {user_role}")
    print("Tapez 'exit' pour quitter, 'history' pour voir l'historique\n")

    session_id = None

    while True:
        try:
            question = input("Vous: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAu revoir.")
            break

        if not question:
            continue
        if question.lower() == "exit":
            break
        if question.lower() == "history" and session_id:
            history = get_session_history(session_id, tenant_id)
            for msg in history:
                print(f"\n[{msg['role'].upper()}] {msg['content'][:200]}")
            continue

        print("\nRecherche et analyse en cours...")
        result = chat(question, tenant_id, session_id, user_role)

        if "error" in result:
            print(f"Erreur: {result['error']}")
            continue

        session_id = result["session_id"]

        print(f"\nAgent: {result['answer']}")
        if result.get("recommended_actions"):
            print("\nActions recommandees:")
            for action in result["recommended_actions"]:
                print(f"  - {action}")
        if result.get("has_uncertainty"):
            print(f"\n[ATTENTION] {result.get('uncertainty_note', 'Incertitude detectee')}")
        print(f"\n[Sources: {result['retrieved_sources']} | Mode: {result['search_mode']} | Modele: {result['llm_model']}]\n")


# â”€â”€â”€ CLI â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent 4 â€” Chat Expert RAG")
    parser.add_argument("--tenant", required=True, help="Tenant ID")
    parser.add_argument("--role", default="expert",
                        choices=["terrain", "expert", "direction", "auditeur"])
    parser.add_argument("--index", action="store_true",
                        help="Indexer les embeddings des exigences applicables")
    parser.add_argument("--force", action="store_true",
                        help="Forcer la rÃ©-indexation mÃªme si dÃ©jÃ  fait")
    parser.add_argument("--chat", action="store_true",
                        help="Mode chat interactif")
    parser.add_argument("--ingest-audits", action="store_true",
                        help="Importer les rapports d'audit PDF")
    parser.add_argument("--question", default=None,
                        help="Poser une question unique (sans mode interactif)")
    args = parser.parse_args()

    if args.ingest_audits:
        print(f"\n=== Ingestion rapports d'audit ({args.tenant}) ===\n")
        ingest_audit_reports(args.tenant)

    elif args.index:
        index_requirements(args.tenant, force=args.force)

    elif args.question:
        result = chat(args.question, args.tenant, user_role=args.role)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    elif args.chat:
        interactive_chat(args.tenant, args.role)

    else:
        parser.print_help()
