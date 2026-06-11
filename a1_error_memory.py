import json
import re
from typing import Any

from a1_shared_helpers import normalize_requirement_key, normalize_spaces


_TRIGGER_TOKEN_RE = re.compile(r"[a-z0-9àâäéèêëîïôöùûüç'-]{3,}", re.IGNORECASE)
_TRIGGER_STOPWORDS = {
    "les",
    "des",
    "dans",
    "avec",
    "pour",
    "sur",
    "par",
    "une",
    "du",
    "de",
    "la",
    "le",
    "au",
    "aux",
    "est",
    "sont",
    "etre",
    "etre",
    "qui",
    "que",
    "dont",
    "ainsi",
    "tout",
    "toute",
    "tous",
    "toutes",
    "article",
    "articles",
    "présente",
    "presente",
    "présent",
    "present",
    "cette",
    "cet",
    "ces",
}
_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
_DEFAULT_SIGNATURE_MIN_HITS = 2
_ERROR_MEMORY_TABLE = "a1_error_memory"
_ERROR_FAMILY_FIX_RULES = {
    "STRUCTURAL_FUSION": "RETRY_SPLIT",
    "SNIPPET_TRUNCATION": "REBUILD_SNIPPET",
    "MISSING_COMPONENT": "REGROUND_FROM_SOURCE",
    "WEAK_GROUNDING": "REGROUND_FROM_SOURCE",
    "TYPE_DRIFT": "RETYPE_FROM_SOURCE",
    "SUBJECT_DRIFT": "RESUBJECT_FROM_CONTEXT",
    "HUMAN_EDIT_REWRITE": "REWRITE_FROM_HUMAN_FEEDBACK",
    "DUPLICATE_REGISTRY_ENTRY": "DEDUP_BEFORE_PROMOTE",
    "OUT_OF_SCOPE_HUMAN_REJECT": "DROP_OR_VALIDATE",
    "NORMATIVE_STRENGTH_DRIFT": "RETYPE_FROM_SOURCE",
}


def create_error_memory_store(*, signature_min_hits: int = _DEFAULT_SIGNATURE_MIN_HITS) -> dict[str, Any]:
    return {
        "exact": {},
        "signature": {},
        "signals_total": 0,
        "signature_min_hits": max(1, int(signature_min_hits)),
    }


def _normalized_reasons(reasons: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for reason in reasons or []:
        value = str(reason or "").strip().upper()
        if value and value not in out:
            out.append(value)
    return out


def _trigger_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for token in _TRIGGER_TOKEN_RE.findall((text or "").lower()):
        if token in _TRIGGER_STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def build_error_memory_trigger(
    *,
    requirement_text: str,
    req_type: str,
    snippet: str = "",
) -> dict[str, str]:
    normalized_req = normalize_requirement_key(requirement_text or "")
    normalized_src = normalize_requirement_key(snippet or "")
    source_text = normalized_req or normalized_src
    tokens = _trigger_tokens(source_text)
    head_tokens = tokens[:6]
    signature_parts = tokens[:3] if len(tokens) >= 3 else head_tokens[:]

    req_type_up = (req_type or "AUTRE").strip().upper() or "AUTRE"
    exact_pattern = f"{req_type_up}|{normalized_req[:220] or normalized_src[:220]}".strip("|")
    signature_pattern = f"{req_type_up}|{' '.join(signature_parts[:6])}".strip("|")

    return {
        "exact_pattern": exact_pattern,
        "signature_pattern": signature_pattern,
    }


def suggest_fix_rule(error_family: str) -> str:
    family = str(error_family or "").strip().upper()
    return str(_ERROR_FAMILY_FIX_RULES.get(family) or "").strip().upper()


def classify_error_family(
    *,
    filter_name: str = "",
    reasons: list[str] | tuple[str, ...] | None = None,
    postcall: dict[str, Any] | None = None,
) -> tuple[str, str, str] | None:
    reason_codes = _normalized_reasons(reasons)
    filter_up = str(filter_name or "").strip().upper()
    postcall = dict(postcall or {})

    if "COMPLETENESS_FUSED_ACTION_CHAIN" in reason_codes:
        return "STRUCTURAL_FUSION", "FORCE_VALIDATE", "HIGH"
    if "COMPLETENESS_DANGLING_TAIL" in reason_codes or "COMPLETENESS_DANGLING_TAIL_DROP" in reason_codes:
        return "SNIPPET_TRUNCATION", "FORCE_VALIDATE", "HIGH"
    if any(code.startswith("COMPLETENESS_MISSING_") for code in reason_codes) or "MISSING_COMPONENT" in reason_codes:
        return "MISSING_COMPONENT", "FORCE_VALIDATE", "HIGH"
    if (
        any(code.startswith("GROUNDING_") for code in reason_codes)
        or any(code.startswith("LOW_SNIPPET_OVERLAP") for code in reason_codes)
        or str(postcall.get("grounding_verdict") or "").strip().upper() in {"SOFT_FAIL", "HARD_FAIL"}
    ):
        return "WEAK_GROUNDING", "FORCE_VALIDATE", "HIGH"
    if any(code.startswith("TYPE_") for code in reason_codes) or not bool(postcall.get("type_match", True)):
        return "TYPE_DRIFT", "FORCE_VALIDATE", "MEDIUM"
    if (
        any(code.startswith("SUBJECT_") for code in reason_codes)
        or not bool(postcall.get("subject_consistent", True))
    ):
        return "SUBJECT_DRIFT", "FORCE_VALIDATE", "MEDIUM"
    if filter_up in {
        "LOW_VALUE",
        "NON_ACTIONABLE_SCOPE",
        "INVENTED_NON_NORMATIVE",
        "OUT_OF_SCOPE_INDIVIDUAL",
        "POSTCALL_RUNTIME",
    }:
        return "NON_NORMATIVE_FALSE_POSITIVE", "OBSERVE_ONLY", "MEDIUM"
    if (
        "DESCRIPTIVE_WITHOUT_NORMATIVE_MARKER" in reason_codes
        or "OUT_OF_SCOPE_INDIVIDUAL_ACT" in reason_codes
        or "OUT_OF_SCOPE_PUBLICATION" in reason_codes
    ):
        return "NON_NORMATIVE_FALSE_POSITIVE", "OBSERVE_ONLY", "MEDIUM"
    return None


def build_error_memory_signal(
    *,
    requirement_text: str,
    req_type: str,
    snippet: str = "",
    reasons: list[str] | tuple[str, ...] | None = None,
    filter_name: str = "",
    postcall: dict[str, Any] | None = None,
    article_label: str = "",
    status: str = "",
    decision: str = "",
) -> dict[str, Any] | None:
    family_info = classify_error_family(
        filter_name=filter_name,
        reasons=reasons,
        postcall=postcall,
    )
    if not family_info:
        return None

    error_family, memory_action, severity = family_info
    trigger = build_error_memory_trigger(
        requirement_text=requirement_text,
        req_type=req_type,
        snippet=snippet,
    )
    exact_pattern = str(trigger.get("exact_pattern") or "").strip()
    signature_pattern = str(trigger.get("signature_pattern") or "").strip()
    if not exact_pattern and not signature_pattern:
        return None

    return {
        "error_family": error_family,
        "memory_action": memory_action,
        "severity": severity,
        "fix_rule": suggest_fix_rule(error_family),
        "req_type": (req_type or "AUTRE").strip().upper() or "AUTRE",
        "filter": str(filter_name or "").strip().upper(),
        "reasons": _normalized_reasons(reasons),
        "trigger_pattern": exact_pattern,
        "signature_pattern": signature_pattern,
        "article_label": str(article_label or "").strip(),
        "status": str(status or "").strip().upper(),
        "decision": str(decision or "").strip().upper(),
        "text_preview": normalize_spaces(requirement_text)[:220],
        "snippet_preview": normalize_spaces(snippet)[:220],
    }


def build_human_validation_feedback_signal(
    *,
    decision: str,
    requirement_text: str,
    req_type: str,
    snippet: str = "",
    rejection_reason: str = "",
    comment: str = "",
    corrected_text: str = "",
    article_label: str = "",
    status: str = "",
) -> dict[str, Any] | None:
    decision_up = str(decision or "").strip().upper()
    if decision_up not in {"EDIT", "REJECT", "FLAG"}:
        return None

    rejection_reason_up = str(rejection_reason or "").strip().upper()
    comment_text = normalize_spaces(comment or "")[:220]
    original_text = normalize_spaces(requirement_text or "")[:220]
    corrected_preview = normalize_spaces(corrected_text or "")[:220]

    if decision_up == "EDIT":
        error_family = "HUMAN_EDIT_REWRITE"
        severity = "HIGH"
        memory_action = "FORCE_VALIDATE"
        fix_rule = "REWRITE_FROM_HUMAN_FEEDBACK"
        reasons = ["HUMAN_EDITED"]
        prompt_patch = (
            f"Avant validation finale, préférer la reformulation humaine: {corrected_preview}"
            if corrected_preview
            else "Avant validation finale, reformuler plus clairement à partir de la source."
        )
    else:
        family_by_reason = {
            "TEXTE_INCORRECT": ("HUMAN_EDIT_REWRITE", "REWRITE_FROM_HUMAN_FEEDBACK"),
            "HORS_PERIMETRE": ("OUT_OF_SCOPE_HUMAN_REJECT", "DROP_OR_VALIDATE"),
            "DOUBLON": ("DUPLICATE_REGISTRY_ENTRY", "DEDUP_BEFORE_PROMOTE"),
            "FORCE_NORMATIVE_FAUSSE": ("NORMATIVE_STRENGTH_DRIFT", "RETYPE_FROM_SOURCE"),
            "INCOMPLET": ("MISSING_COMPONENT", "REGROUND_FROM_SOURCE"),
            "AUTRE": ("HUMAN_EDIT_REWRITE", "REWRITE_FROM_HUMAN_FEEDBACK"),
        }
        error_family, fix_rule = family_by_reason.get(
            rejection_reason_up or "AUTRE",
            ("HUMAN_EDIT_REWRITE", "REWRITE_FROM_HUMAN_FEEDBACK"),
        )
        severity = "HIGH" if decision_up == "REJECT" else "MEDIUM"
        memory_action = "FORCE_VALIDATE"
        reasons = [rejection_reason_up] if rejection_reason_up else []
        prompt_patch = (
            f"Erreur relevée par validation humaine: {comment_text}"
            if comment_text
            else f"Éviter cette sortie: {rejection_reason_up or 'AUTRE'}."
        )

    trigger = build_error_memory_trigger(
        requirement_text=requirement_text or corrected_text,
        req_type=req_type,
        snippet=snippet,
    )
    if not str(trigger.get("exact_pattern") or "").strip():
        return None

    return {
        "error_family": error_family,
        "memory_action": memory_action,
        "severity": severity,
        "fix_rule": fix_rule,
        "req_type": (req_type or "AUTRE").strip().upper() or "AUTRE",
        "filter": "HUMAN_VALIDATION_FEEDBACK",
        "reasons": reasons,
        "trigger_pattern": str(trigger.get("exact_pattern") or "").strip(),
        "signature_pattern": str(trigger.get("signature_pattern") or "").strip(),
        "article_label": str(article_label or "").strip(),
        "status": str(status or "").strip().upper(),
        "decision": decision_up,
        "text_preview": original_text,
        "snippet_preview": normalize_spaces(snippet or "")[:220],
        "prompt_patch": prompt_patch,
        "human_comment": comment_text,
        "corrected_text": corrected_preview,
    }


def register_error_memory_signal(
    store: dict[str, Any],
    signal: dict[str, Any],
    *,
    persisted: bool = False,
) -> None:
    if not store or not signal:
        return

    def _register_bucket(bucket_name: str, pattern: str) -> None:
        normalized_pattern = str(pattern or "").strip()
        if not normalized_pattern:
            return
        bucket = store.setdefault(bucket_name, {})
        families = bucket.setdefault(normalized_pattern, {})
        family = str(signal.get("error_family") or "").strip().upper()
        if not family:
            return
        slot = families.setdefault(
            family,
            {
                "count": 0,
                "persisted_count": 0,
                "runtime_count": 0,
                "memory_action": str(signal.get("memory_action") or "OBSERVE_ONLY").strip().upper(),
                "severity": str(signal.get("severity") or "MEDIUM").strip().upper(),
                "fix_rule": str(signal.get("fix_rule") or suggest_fix_rule(family)).strip().upper(),
                "trigger_pattern": str(signal.get("trigger_pattern") or ""),
                "signature_pattern": str(signal.get("signature_pattern") or ""),
                "last_signal": {},
            },
        )
        increment = max(1, int(signal.get("count") or 1))
        persisted_increment = max(0, int(signal.get("persisted_count") or (increment if persisted else 0)))
        runtime_increment = max(0, int(signal.get("runtime_count") or (0 if persisted else increment)))
        slot["count"] = int(slot.get("count") or 0) + increment
        if persisted:
            slot["persisted_count"] = int(slot.get("persisted_count") or 0) + max(1, persisted_increment or increment)
        else:
            slot["runtime_count"] = int(slot.get("runtime_count") or 0) + max(1, runtime_increment or increment)
        slot["last_signal"] = dict(signal)

    _register_bucket("exact", str(signal.get("trigger_pattern") or ""))
    _register_bucket("signature", str(signal.get("signature_pattern") or ""))
    store["signals_total"] = int(store.get("signals_total") or 0) + 1


def find_error_memory_hit(
    store: dict[str, Any],
    *,
    requirement_text: str,
    req_type: str,
    snippet: str = "",
) -> dict[str, Any] | None:
    if not store:
        return None

    trigger = build_error_memory_trigger(
        requirement_text=requirement_text,
        req_type=req_type,
        snippet=snippet,
    )
    signature_min_hits = max(1, int(store.get("signature_min_hits") or _DEFAULT_SIGNATURE_MIN_HITS))
    candidates: list[dict[str, Any]] = []

    exact_bucket = dict((store.get("exact") or {}).get(str(trigger.get("exact_pattern") or ""), {}))
    for family, data in exact_bucket.items():
        if str(data.get("memory_action") or "").strip().upper() != "FORCE_VALIDATE":
            continue
        count = int(data.get("count") or 0)
        if count < 1:
            continue
        candidates.append(
            {
                "error_family": family,
                "memory_action": str(data.get("memory_action") or "FORCE_VALIDATE").strip().upper(),
                "severity": str(data.get("severity") or "MEDIUM").strip().upper(),
                "fix_rule": str(data.get("fix_rule") or suggest_fix_rule(family)).strip().upper(),
                "count": count,
                "persisted_count": int(data.get("persisted_count") or 0),
                "runtime_count": int(data.get("runtime_count") or 0),
                "match_kind": "EXACT",
                "trigger_pattern": str(data.get("trigger_pattern") or trigger.get("exact_pattern") or ""),
                "signature_pattern": str(data.get("signature_pattern") or trigger.get("signature_pattern") or ""),
                "last_signal": dict(data.get("last_signal") or {}),
            }
        )

    signature_bucket = dict((store.get("signature") or {}).get(str(trigger.get("signature_pattern") or ""), {}))
    for family, data in signature_bucket.items():
        if str(data.get("memory_action") or "").strip().upper() != "FORCE_VALIDATE":
            continue
        count = int(data.get("count") or 0)
        if count < signature_min_hits:
            continue
        candidates.append(
            {
                "error_family": family,
                "memory_action": str(data.get("memory_action") or "FORCE_VALIDATE").strip().upper(),
                "severity": str(data.get("severity") or "MEDIUM").strip().upper(),
                "fix_rule": str(data.get("fix_rule") or suggest_fix_rule(family)).strip().upper(),
                "count": count,
                "persisted_count": int(data.get("persisted_count") or 0),
                "runtime_count": int(data.get("runtime_count") or 0),
                "match_kind": "SIGNATURE",
                "trigger_pattern": str(data.get("trigger_pattern") or trigger.get("exact_pattern") or ""),
                "signature_pattern": str(data.get("signature_pattern") or trigger.get("signature_pattern") or ""),
                "last_signal": dict(data.get("last_signal") or {}),
            }
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            _SEVERITY_RANK.get(str(item.get("severity") or "MEDIUM").upper(), 0),
            int(item.get("count") or 0),
            int(item.get("persisted_count") or 0),
            1 if str(item.get("match_kind") or "") == "EXACT" else 0,
        ),
        reverse=True,
    )
    return candidates[0]


def error_memory_table_exists(cur: Any) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema='public'
          AND table_name=%s
        LIMIT 1
        """,
        (_ERROR_MEMORY_TABLE,),
    )
    return cur.fetchone() is not None


def ensure_error_memory_table(cur: Any) -> None:
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_ERROR_MEMORY_TABLE} (
            memory_id BIGSERIAL PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            error_family TEXT NOT NULL,
            req_type TEXT NOT NULL DEFAULT 'AUTRE',
            trigger_pattern TEXT NOT NULL,
            signature_pattern TEXT NOT NULL DEFAULT '',
            bad_output TEXT NOT NULL DEFAULT '',
            snippet_preview TEXT NOT NULL DEFAULT '',
            fix_rule TEXT NOT NULL DEFAULT '',
            prompt_patch TEXT NOT NULL DEFAULT '',
            memory_action TEXT NOT NULL DEFAULT 'OBSERVE_ONLY',
            severity TEXT NOT NULL DEFAULT 'MEDIUM',
            filter_name TEXT NOT NULL DEFAULT '',
            reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
            article_label TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL DEFAULT '',
            sample_doc_id TEXT NOT NULL DEFAULT '',
            doc_ids TEXT[] NOT NULL DEFAULT '{{}}',
            hit_count INTEGER NOT NULL DEFAULT 1,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_replayed_at TIMESTAMPTZ NULL,
            replay_count INTEGER NOT NULL DEFAULT 0,
            replay_notes TEXT NOT NULL DEFAULT '',
            source_event_type TEXT NOT NULL DEFAULT 'A1_ERROR_MEMORY_SIGNAL',
            source_event_keys TEXT[] NOT NULL DEFAULT '{{}}',
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            CONSTRAINT a1_error_memory_uq UNIQUE (tenant_id, error_family, trigger_pattern)
        )
        """
    )
    cur.execute(
        f"""
        ALTER TABLE {_ERROR_MEMORY_TABLE}
        ADD COLUMN IF NOT EXISTS source_event_keys TEXT[] NOT NULL DEFAULT '{{}}'
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{_ERROR_MEMORY_TABLE}_tenant_last_seen
        ON {_ERROR_MEMORY_TABLE}(tenant_id, last_seen_at DESC)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{_ERROR_MEMORY_TABLE}_tenant_signature
        ON {_ERROR_MEMORY_TABLE}(tenant_id, signature_pattern)
        """
    )
    cur.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{_ERROR_MEMORY_TABLE}_tenant_doc
        ON {_ERROR_MEMORY_TABLE}(tenant_id, sample_doc_id)
        """
    )


def build_error_memory_record(
    *,
    tenant_id: str,
    doc_id: str = "",
    signal: dict[str, Any],
    source_event_type: str = "A1_ERROR_MEMORY_SIGNAL",
    source_event_key: str = "",
    prompt_patch: str = "",
) -> dict[str, Any]:
    signal = dict(signal or {})
    normalized_event_key = str(source_event_key or signal.get("source_event_key") or "").strip()
    return {
        "tenant_id": str(tenant_id or "").strip(),
        "sample_doc_id": str(doc_id or "").strip(),
        "error_family": str(signal.get("error_family") or "").strip().upper(),
        "req_type": str(signal.get("req_type") or "AUTRE").strip().upper() or "AUTRE",
        "trigger_pattern": str(signal.get("trigger_pattern") or "").strip(),
        "signature_pattern": str(signal.get("signature_pattern") or "").strip(),
        "bad_output": normalize_spaces(signal.get("text_preview") or ""),
        "snippet_preview": normalize_spaces(signal.get("snippet_preview") or ""),
        "fix_rule": str(signal.get("fix_rule") or "").strip().upper(),
        "prompt_patch": str(signal.get("prompt_patch") or prompt_patch or "").strip(),
        "memory_action": str(signal.get("memory_action") or "OBSERVE_ONLY").strip().upper(),
        "severity": str(signal.get("severity") or "MEDIUM").strip().upper(),
        "filter_name": str(signal.get("filter") or "").strip().upper(),
        "reasons": _normalized_reasons(signal.get("reasons") or []),
        "article_label": str(signal.get("article_label") or "").strip(),
        "status": str(signal.get("status") or "").strip().upper(),
        "decision": str(signal.get("decision") or "").strip().upper(),
        "hit_count": max(1, int(signal.get("count") or 1)),
        "source_event_type": str(source_event_type or "A1_ERROR_MEMORY_SIGNAL").strip().upper(),
        "source_event_keys": [normalized_event_key] if normalized_event_key else [],
        "payload": signal,
    }


def persist_error_memory_signal(
    cur: Any,
    *,
    tenant_id: str,
    doc_id: str = "",
    signal: dict[str, Any] | None,
    source_event_type: str = "A1_ERROR_MEMORY_SIGNAL",
    source_event_key: str = "",
    prompt_patch: str = "",
) -> bool:
    if not signal or not tenant_id:
        return False
    record = build_error_memory_record(
        tenant_id=tenant_id,
        doc_id=doc_id,
        signal=signal,
        source_event_type=source_event_type,
        source_event_key=source_event_key,
        prompt_patch=prompt_patch,
    )
    if not record["error_family"] or not record["trigger_pattern"]:
        return False

    cur.execute(
        f"""
        INSERT INTO {_ERROR_MEMORY_TABLE} (
            tenant_id,
            error_family,
            req_type,
            trigger_pattern,
            signature_pattern,
            bad_output,
            snippet_preview,
            fix_rule,
            prompt_patch,
            memory_action,
            severity,
            filter_name,
            reasons,
            article_label,
            status,
            decision,
            sample_doc_id,
            doc_ids,
            hit_count,
            source_event_type,
            source_event_keys,
            payload
        )
        VALUES (
            %(tenant_id)s,
            %(error_family)s,
            %(req_type)s,
            %(trigger_pattern)s,
            %(signature_pattern)s,
            %(bad_output)s,
            %(snippet_preview)s,
            %(fix_rule)s,
            %(prompt_patch)s,
            %(memory_action)s,
            %(severity)s,
            %(filter_name)s,
            %(reasons)s::jsonb,
            %(article_label)s,
            %(status)s,
            %(decision)s,
            %(sample_doc_id)s,
            CASE
                WHEN %(sample_doc_id)s <> '' THEN ARRAY[%(sample_doc_id)s]::text[]
                ELSE ARRAY[]::text[]
            END,
            %(hit_count)s,
            %(source_event_type)s,
            %(source_event_keys)s::text[],
            %(payload)s::jsonb
        )
        ON CONFLICT (tenant_id, error_family, trigger_pattern)
        DO UPDATE SET
            req_type = EXCLUDED.req_type,
            signature_pattern = EXCLUDED.signature_pattern,
            bad_output = CASE
                WHEN length(EXCLUDED.bad_output) >= length({_ERROR_MEMORY_TABLE}.bad_output)
                THEN EXCLUDED.bad_output ELSE {_ERROR_MEMORY_TABLE}.bad_output
            END,
            snippet_preview = CASE
                WHEN length(EXCLUDED.snippet_preview) >= length({_ERROR_MEMORY_TABLE}.snippet_preview)
                THEN EXCLUDED.snippet_preview ELSE {_ERROR_MEMORY_TABLE}.snippet_preview
            END,
            fix_rule = CASE
                WHEN EXCLUDED.fix_rule <> '' THEN EXCLUDED.fix_rule ELSE {_ERROR_MEMORY_TABLE}.fix_rule
            END,
            prompt_patch = CASE
                WHEN EXCLUDED.prompt_patch <> '' THEN EXCLUDED.prompt_patch ELSE {_ERROR_MEMORY_TABLE}.prompt_patch
            END,
            memory_action = EXCLUDED.memory_action,
            severity = EXCLUDED.severity,
            filter_name = EXCLUDED.filter_name,
            reasons = EXCLUDED.reasons,
            article_label = CASE
                WHEN EXCLUDED.article_label <> '' THEN EXCLUDED.article_label ELSE {_ERROR_MEMORY_TABLE}.article_label
            END,
            status = EXCLUDED.status,
            decision = EXCLUDED.decision,
            sample_doc_id = CASE
                WHEN EXCLUDED.sample_doc_id <> '' THEN EXCLUDED.sample_doc_id ELSE {_ERROR_MEMORY_TABLE}.sample_doc_id
            END,
            doc_ids = CASE
                WHEN EXCLUDED.sample_doc_id = '' THEN {_ERROR_MEMORY_TABLE}.doc_ids
                WHEN EXCLUDED.sample_doc_id = ANY({_ERROR_MEMORY_TABLE}.doc_ids) THEN {_ERROR_MEMORY_TABLE}.doc_ids
                ELSE array_append({_ERROR_MEMORY_TABLE}.doc_ids, EXCLUDED.sample_doc_id)
            END,
            hit_count = CASE
                WHEN cardinality(EXCLUDED.source_event_keys) = 0
                    THEN {_ERROR_MEMORY_TABLE}.hit_count + GREATEST(1, EXCLUDED.hit_count)
                WHEN {_ERROR_MEMORY_TABLE}.source_event_keys && EXCLUDED.source_event_keys
                    THEN {_ERROR_MEMORY_TABLE}.hit_count
                ELSE {_ERROR_MEMORY_TABLE}.hit_count + GREATEST(1, EXCLUDED.hit_count)
            END,
            last_seen_at = CASE
                WHEN cardinality(EXCLUDED.source_event_keys) = 0
                    THEN NOW()
                WHEN {_ERROR_MEMORY_TABLE}.source_event_keys && EXCLUDED.source_event_keys
                    THEN {_ERROR_MEMORY_TABLE}.last_seen_at
                ELSE NOW()
            END,
            source_event_type = EXCLUDED.source_event_type,
            source_event_keys = CASE
                WHEN cardinality(EXCLUDED.source_event_keys) = 0
                    THEN {_ERROR_MEMORY_TABLE}.source_event_keys
                WHEN {_ERROR_MEMORY_TABLE}.source_event_keys && EXCLUDED.source_event_keys
                    THEN {_ERROR_MEMORY_TABLE}.source_event_keys
                ELSE {_ERROR_MEMORY_TABLE}.source_event_keys || EXCLUDED.source_event_keys
            END,
            payload = EXCLUDED.payload
        """,
        {
            **record,
            "reasons": json.dumps(record["reasons"], ensure_ascii=False),
            "payload": json.dumps(record["payload"], ensure_ascii=False),
        },
    )
    return True


def load_persisted_error_memory_signals(cur: Any, tenant_id: str, *, limit: int = 400) -> list[dict[str, Any]]:
    if not tenant_id or limit <= 0:
        return []
    cur.execute(
        f"""
        SELECT
            error_family,
            req_type,
            trigger_pattern,
            signature_pattern,
            bad_output,
            snippet_preview,
            fix_rule,
            prompt_patch,
            memory_action,
            severity,
            filter_name,
            reasons,
            article_label,
            status,
            decision,
            sample_doc_id,
            doc_ids,
            hit_count,
            payload
        FROM {_ERROR_MEMORY_TABLE}
        WHERE tenant_id=%s
        ORDER BY last_seen_at DESC
        LIMIT %s
        """,
        (tenant_id, int(limit)),
    )
    signals: list[dict[str, Any]] = []
    for row in cur.fetchall():
        payload = row[18]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        payload = dict(payload or {})
        payload.update(
            {
                "error_family": str(row[0] or payload.get("error_family") or "").strip().upper(),
                "req_type": str(row[1] or payload.get("req_type") or "AUTRE").strip().upper(),
                "trigger_pattern": str(row[2] or payload.get("trigger_pattern") or "").strip(),
                "signature_pattern": str(row[3] or payload.get("signature_pattern") or "").strip(),
                "text_preview": normalize_spaces(row[4] or payload.get("text_preview") or ""),
                "snippet_preview": normalize_spaces(row[5] or payload.get("snippet_preview") or ""),
                "fix_rule": str(row[6] or payload.get("fix_rule") or "").strip().upper(),
                "prompt_patch": str(row[7] or payload.get("prompt_patch") or "").strip(),
                "memory_action": str(row[8] or payload.get("memory_action") or "OBSERVE_ONLY").strip().upper(),
                "severity": str(row[9] or payload.get("severity") or "MEDIUM").strip().upper(),
                "filter": str(row[10] or payload.get("filter") or "").strip().upper(),
                "reasons": _normalized_reasons(row[11] or payload.get("reasons") or []),
                "article_label": str(row[12] or payload.get("article_label") or "").strip(),
                "status": str(row[13] or payload.get("status") or "").strip().upper(),
                "decision": str(row[14] or payload.get("decision") or "").strip().upper(),
                "sample_doc_id": str(row[15] or payload.get("sample_doc_id") or "").strip(),
                "doc_ids": [str(v) for v in (row[16] or []) if str(v or "").strip()],
                "count": max(1, int(row[17] or 1)),
                "persisted_count": max(1, int(row[17] or 1)),
                "runtime_count": 0,
            }
        )
        signals.append(payload)
    return signals


def list_error_memory_replay_targets(
    cur: Any,
    *,
    tenant_id: str,
    limit: int = 10,
    error_family: str = "",
    error_families: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    if not tenant_id or limit <= 0:
        return []
    families = [
        str(value or "").strip().upper()
        for value in (error_families or [])
        if str(value or "").strip()
    ]
    if str(error_family or "").strip():
        family_value = str(error_family or "").strip().upper()
        if family_value not in families:
            families.append(family_value)
    sql = f"""
        SELECT
            doc_id,
            COUNT(*) AS families_count,
            MAX(last_seen_at) AS last_seen_at
        FROM (
            SELECT unnest(doc_ids) AS doc_id, last_seen_at
            FROM {_ERROR_MEMORY_TABLE}
            WHERE tenant_id=%s
              AND cardinality(doc_ids) > 0
              {{family_filter}}
        ) t
        WHERE doc_id IS NOT NULL AND doc_id <> ''
        GROUP BY doc_id
        ORDER BY MAX(last_seen_at) DESC, COUNT(*) DESC, doc_id ASC
        LIMIT %s
    """
    params: list[Any] = [tenant_id]
    family_filter = ""
    if families:
        family_filter = " AND error_family = ANY(%s::text[])"
        params.append(families)
    params.append(int(limit))
    cur.execute(sql.replace("{family_filter}", family_filter), params)
    return [
        {
            "doc_id": str(row[0] or "").strip(),
            "families_count": int(row[1] or 0),
            "last_seen_at": row[2],
        }
        for row in cur.fetchall()
        if str(row[0] or "").strip()
    ]


def mark_error_memory_replayed(
    cur: Any,
    *,
    tenant_id: str,
    doc_id: str,
    replay_notes: str = "",
) -> None:
    if not tenant_id or not doc_id:
        return
    cur.execute(
        f"""
        UPDATE {_ERROR_MEMORY_TABLE}
        SET
            last_replayed_at = NOW(),
            replay_count = replay_count + 1,
            replay_notes = %s
        WHERE tenant_id=%s
          AND (%s = sample_doc_id OR %s = ANY(doc_ids))
        """,
        (str(replay_notes or "")[:500], tenant_id, doc_id, doc_id),
    )
