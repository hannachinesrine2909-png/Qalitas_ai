from __future__ import annotations

from collections import defaultdict
import re
from typing import Any


_SPACES_RE = re.compile(r"\s+")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|[0-9]+[.)])\s+(.+?)\s*$")
_KV_RE = re.compile(r"^\s*(?P<label>[^:;|]{2,120}?)\s*[:\-]\s*(?P<value>.+?)\s*$")
_TABLE_SPLIT_RE = re.compile(r"\s{2,}|\t+|\s+\|\s+")
_NUMBER_UNIT_RE = re.compile(
    r"(?P<value>[0-9]+(?:[.,][0-9]+)?)\s*(?P<unit>%|dt|dinars?|jours?|heures?|mois|ans|postes?)\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"(?i)\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|[0-3]?\d\s+[a-zéûî]+(?:\s+\d{4})?)\b"
)


def _norm(text: str) -> str:
    return _SPACES_RE.sub(" ", (text or "").strip())


def _infer_object_type(label: str, value: str, unit: str) -> str:
    combined = _norm(f"{label} {value} {unit}").lower()
    if any(c in combined for c in ("tarif", "montant", "dinars", "dt", "frais", "coût", "cout")):
        return "tariff"
    if any(c in combined for c in ("quota", "poste", "plafond", "nombre")):
        return "quota"
    if any(c in combined for c in ("date", "délai", "delai", "jours", "heures", "mois", "ans")):
        return "deadline"
    if any(c in combined for c in ("région", "region", "spécialité", "specialite", "catégorie", "categorie")):
        return "classification"
    return "tabular_parameter"


def _extract_row(line: str) -> dict[str, str] | None:
    txt = _norm(line)
    if not txt:
        return None
    bullet = _BULLET_RE.match(txt)
    if bullet:
        txt = _norm(bullet.group(1))

    label = ""
    value = txt
    unit = ""
    m_kv = _KV_RE.match(txt)
    if m_kv:
        label = _norm(m_kv.group("label"))
        value = _norm(m_kv.group("value"))
    else:
        parts = [p.strip() for p in _TABLE_SPLIT_RE.split(txt) if p.strip()]
        if len(parts) >= 2:
            label = _norm(parts[0])
            value = _norm(parts[1])

    m_num = _NUMBER_UNIT_RE.search(value or txt)
    if m_num:
        unit = _norm(m_num.group("unit"))
    elif _DATE_RE.search(value or txt):
        unit = "date"

    if not label and not m_num and not _DATE_RE.search(value or txt):
        return None

    return {
        "label": label or txt[:90],
        "value": value,
        "unit": unit,
        "raw": txt,
    }


def parse_limited_data_objects(
    *,
    article_label: str,
    article_text: str,
    parent_legal_rule_ref: str = "",
    provenance: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    text = _norm(article_text)
    if not text:
        return []

    rows_by_type: dict[str, list[dict[str, str]]] = defaultdict(list)
    for line in (article_text or "").splitlines():
        row = _extract_row(line)
        if not row:
            continue
        obj_type = _infer_object_type(row.get("label", ""), row.get("value", ""), row.get("unit", ""))
        rows_by_type[obj_type].append(row)

    objects: list[dict[str, Any]] = []
    base_provenance = dict(provenance or {})
    base_provenance.setdefault("article_label", article_label or "")
    base_provenance.setdefault("parser", "limited_data_v1")

    for obj_type, rows in sorted(rows_by_type.items()):
        if not rows:
            continue
        units = sorted({r.get("unit", "").lower() for r in rows if r.get("unit")})
        objects.append(
            {
                "data_object_type": obj_type,
                "parent_legal_rule_ref": _norm(parent_legal_rule_ref or article_label or "") or "UNATTACHED",
                "table_schema": ["label", "value", "unit", "raw"],
                "rows": rows,
                "units": units,
                "provenance": base_provenance,
            }
        )
    return objects

