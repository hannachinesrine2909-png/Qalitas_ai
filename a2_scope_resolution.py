from __future__ import annotations

import unicodedata
from typing import Any, Iterable


def normalize_scope_text(value: Any) -> str:
    raw = str(value or "")
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_text.lower().split()).strip()


def is_legacy_scope_key(scope_key: Any) -> bool:
    return ":LEGACY:" in str(scope_key or "").upper()


def _scope_label(row: dict[str, Any]) -> str:
    return (
        str(row.get("scope_label") or "").strip()
        or str(row.get("scope_activity") or "").strip()
        or str(row.get("scope_process") or "").strip()
        or str(row.get("scope_site") or "").strip()
        or str(row.get("scope_key") or "ORGANIZATION").strip()
    )


def _scope_level(row: dict[str, Any]) -> str:
    return str(row.get("scope_level") or "ORGANIZATION").strip().upper() or "ORGANIZATION"


def scope_level_rank(scope_level: Any) -> int:
    value = str(scope_level or "ORGANIZATION").strip().upper()
    if value == "ORGANIZATION":
        return 0
    if value == "SITE":
        return 1
    if value == "PROCESS":
        return 2
    if value == "ACTIVITY":
        return 3
    return 9


def _scope_anchor_score(row: dict[str, Any]) -> int:
    scope_key = str(row.get("scope_key") or "ORGANIZATION")
    if scope_key == "ORGANIZATION":
        return 2
    if row.get("activity_id") or row.get("process_id") or row.get("site_id"):
        return 2
    if not is_legacy_scope_key(scope_key):
        return 1
    return 0


def _decision_score(row: dict[str, Any]) -> tuple[float, int, str]:
    confidence = row.get("confidence")
    try:
        conf_value = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        conf_value = 0.0
    updated_at = str(row.get("updated_at") or row.get("created_at") or "")
    return (
        conf_value + (0.35 if not is_legacy_scope_key(row.get("scope_key")) else 0.0),
        _scope_anchor_score(row),
        updated_at,
    )


def effective_applicability_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    items = [dict(row) for row in rows if isinstance(row, dict)]
    if not items:
        return []

    canonical_signatures: set[tuple[str, str, str]] = set()
    for row in items:
        req_id = str(row.get("requirement_id") or "").strip()
        if not req_id or is_legacy_scope_key(row.get("scope_key")):
            continue
        canonical_signatures.add((req_id, _scope_level(row), normalize_scope_text(_scope_label(row))))

    filtered: list[dict[str, Any]] = []
    for row in items:
        req_id = str(row.get("requirement_id") or "").strip()
        if not req_id:
            filtered.append(row)
            continue
        if is_legacy_scope_key(row.get("scope_key")):
            signature = (req_id, _scope_level(row), normalize_scope_text(_scope_label(row)))
            if signature in canonical_signatures:
                continue
        filtered.append(row)

    best_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in filtered:
        req_id = str(row.get("requirement_id") or "").strip()
        scope_key = str(row.get("scope_key") or "ORGANIZATION").strip() or "ORGANIZATION"
        compound_key = (req_id, scope_key)
        existing = best_by_key.get(compound_key)
        if existing is None or _decision_score(row) > _decision_score(existing):
            best_by_key[compound_key] = row

    output = list(best_by_key.values())
    output.sort(
        key=lambda row: (
            scope_level_rank(_scope_level(row)),
            normalize_scope_text(_scope_label(row)),
            str(row.get("status") or ""),
            -float(row.get("confidence") or 0.0) if str(row.get("confidence") or "").strip() else 0.0,
        )
    )
    return output
