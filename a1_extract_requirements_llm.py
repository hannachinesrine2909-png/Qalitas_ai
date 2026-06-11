import argparse
from difflib import SequenceMatcher
import json
import os
import re
import time
from typing import Any
import unicodedata
from dotenv import load_dotenv
from pydantic import ValidationError
from tenant_db import connect_db

from a1_llm_extractor import LLMExtractor
from a1_error_memory import (
    build_error_memory_signal,
    create_error_memory_store,
    ensure_error_memory_table,
    error_memory_table_exists,
    find_error_memory_hit,
    load_persisted_error_memory_signals,
    persist_error_memory_signal,
    register_error_memory_signal,
)


load_dotenv()


from a1_postcall_quality import assess_postcall_requirement
from a1_precall_nlp import PRECALL_NLP_VERSION, classify_legal_unit, normalize_legal_text, segment_legal_units
from a1_shared_helpers import (
    build_empty_llm_fallback_requirements,
    build_structural_framing_requirements,
    decode_html_entities,
    clean_source_snippet,
    extract_best_citation_snippet,
    has_ocr_artifact_signals,
    sanitize_ocr_noise_for_extraction,
    has_legal_risk_markers,
    should_enable_long_article_safety,
    normalize_requirement_key,
    build_doc_level_dedup_key,
    build_doc_level_relaxed_requirement_key,
    split_into_legal_units,
    normalize_subject_from_context,
    normalize_req_type,
    is_low_value_requirement_text,
    is_partial_exception_requirement,
    is_invented_from_non_normative_snippet,
    is_non_actionable_scope_requirement,
    is_out_of_scope_individual_requirement,
    is_definition_like_article,
    is_definition_like_unit,
    is_scope_extension_classification_unit,
    normalize_requirement_text_by_type,
    refine_requirement_text_quality,
    repair_common_ocr_artifacts,
    expand_introductory_documentary_requirement,
    split_introductory_documentary_requirement,
    split_fused_obligation,
    format_citation_ref,
    ground_requirement_to_source,
    classify_confidence,
    classify_qse_domain_subdomain,
    compute_status,
    normalize_spaces,
    strip_article_header,
)
from a1_document_qualification_gate import (
    POLICY_DROP,
    POLICY_EXTRACT_FULL,
    POLICY_EXTRACT_LIMITED_DATA,
    POLICY_TO_VALIDATE_SOURCE_MISSING,
    QualificationDecision,
    classify_article_policy,
    classify_document_policy,
    force_to_validate_by_policy,
    limited_unit_is_actionable,
    should_skip_standard_extraction,
)
from a1_limited_data_parser import parse_limited_data_objects

# =========================
# Event helper
# =========================
def safe_insert_event(cur, tenant_id: str, doc_id: str, event_type: str, payload: dict) -> bool:
    payload_json = json.dumps(payload, ensure_ascii=False)

    statements = [
        (
            """
            INSERT INTO events(tenant_id, doc_id, event_type, payload_json)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (tenant_id, doc_id, event_type, payload_json),
        ),
        (
            """
            INSERT INTO events(tenant_id, doc_id, event_type, payload)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (tenant_id, doc_id, event_type, payload_json),
        ),
        (
            """
            INSERT INTO events(tenant_id, doc_id, event_type, event_payload)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (tenant_id, doc_id, event_type, payload_json),
        ),
    ]

    for sql, params in statements:
        try:
            with cur.connection.transaction():
                cur.execute(sql, params)
            return True
        except Exception:
            continue

    return False


def _parse_event_json_value(raw_value: Any) -> dict[str, Any] | None:
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _load_error_memory_signals(cur, tenant_id: str, *, limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not tenant_id:
        return []

    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public'
          AND table_name='events'
          AND column_name IN ('payload_json', 'payload', 'event_payload')
        """
    )
    available_columns = {str(row[0]) for row in cur.fetchall() if row and row[0]}

    for column_name in ("payload_json", "payload", "event_payload"):
        if column_name not in available_columns:
            continue
        cur.execute(
            f"""
            SELECT {column_name}
            FROM events
            WHERE tenant_id=%s
              AND event_type='A1_ERROR_MEMORY_SIGNAL'
              AND {column_name} IS NOT NULL
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (tenant_id, int(limit)),
        )
        rows = cur.fetchall()
        signals: list[dict[str, Any]] = []
        for row in rows:
            payload = _parse_event_json_value(row[0] if row else None)
            if isinstance(payload, dict):
                signals.append(payload)
        return signals

    return []


def _token_overlap_ratio(short_text: str, long_text: str) -> float:
    short_tokens = {t for t in short_text.split() if t}
    long_tokens = {t for t in long_text.split() if t}
    if not short_tokens:
        return 0.0
    return len(short_tokens & long_tokens) / len(short_tokens)


def _is_prefix_extended_duplicate(a: str, b: str, *, min_prefix_chars: int = 90) -> bool:
    aa = " ".join((a or "").split()).strip().lower()
    bb = " ".join((b or "").split()).strip().lower()
    if not aa or not bb:
        return False
    if aa == bb:
        return True
    short, long = (aa, bb) if len(aa) <= len(bb) else (bb, aa)
    if len(short) < max(20, int(min_prefix_chars)):
        return False
    strict_prefix_match = long.startswith(short + " ") or long == short
    if not strict_prefix_match:
        token_overlap = _token_overlap_ratio(short, long)
        seq_ratio = SequenceMatcher(None, short, long).ratio()
        if token_overlap >= 0.95 and len(long) >= int(len(short) * 1.12):
            return True
        if token_overlap >= 0.90 and seq_ratio >= 0.82:
            return True
        return False
    # Durci de 0.62 → 0.80 : deux exigences avec des fins différentes ne doivent pas être fusionnées
    if _token_overlap_ratio(short, long) < 0.80:
        return False
    # Vérification supplémentaire : les 40 derniers chars normalisés de la plus courte
    # doivent apparaître dans la plus longue (périmètre/portée différents → non-doublon)
    short_tail = short[-40:].strip() if len(short) >= 40 else short
    if short_tail and short_tail not in long:
        return False
    return True


def _is_high_overlap_duplicate(a: str, b: str, *, min_token_overlap: float = 0.88, min_seq_ratio: float = 0.82) -> bool:
    aa = normalize_requirement_key(a)
    bb = normalize_requirement_key(b)
    if not aa or not bb:
        return False
    if aa == bb:
        return True
    short, long = (aa, bb) if len(aa) <= len(bb) else (bb, aa)
    if len(short) < 40:
        return False
    if short in long and len(short) >= max(40, int(len(long) * 0.62)):
        return True
    token_overlap = _token_overlap_ratio(aa, bb)
    if token_overlap >= 0.95:
        return True
    if token_overlap < float(min_token_overlap):
        return False
    return SequenceMatcher(None, aa, bb).ratio() >= float(min_seq_ratio)


def _ascii_fold_runtime(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    return "".join(
        ch
        for ch in unicodedata.normalize("NFKD", raw)
        if not unicodedata.combining(ch)
    )


_PROMOTION_GENERIC_LEGAL_SUBJECT_RE = re.compile(
    r"^(?:l['’][a-z]|(?:le|la|les|tout|toute|tous|toutes)\s+[a-z])",
    re.IGNORECASE,
)


def _has_promotion_legal_subject(req_lower: str) -> bool:
    text = str(req_lower or "").strip().lower()
    if not text:
        return False

    explicit_prefixes = (
        "l'employeur", "le chef ", "tout travailleur", "le travailleur",
        "le salarie", "les salaries", "toute personne", "l'entreprise",
        "les entreprises", "le medecin", "le responsable", "les candidats",
        "le candidat", "le jury", "la commission", "le directeur",
        "le ministre", "l'organisme", "l'etablissement", "les membres",
        "tout employeur", "tout etablissement", "il est interdit",
        "est interdit", "toute demande", "tout dossier",
    )
    if any(text.startswith(prefix) for prefix in explicit_prefixes):
        return True

    # Les formulations demonstratives/meta ("Ces mesures...", "Le present arrete...")
    # restent volontairement en revue humaine.
    disallowed_prefixes = (
        "ce ", "cet ", "cette ", "ces ",
        "le present ", "la presente ", "les presents ", "les presentes ",
        "lors de ", "au cours de ", "en cas de ",
    )
    if any(text.startswith(prefix) for prefix in disallowed_prefixes):
        return False

    if not _PROMOTION_GENERIC_LEGAL_SUBJECT_RE.match(text):
        return False

    strong_normative_markers = (
        "doit ", "doivent ", "doit etre", "doivent etre",
        "est tenu", "sont tenus",
        "est interdit", "est interdite", "strictement interdit", "strictement interdite",
        "sont interdits", "sont interdites", "il est interdit",
        "ne peut ", "ne peuvent ", "peut ", "peuvent ",
        "peut obliger", "peuvent obliger",
        "peut prescrire", "peuvent prescrire",
        "est charge", "sont charges",
        "est soumis", "sont soumis", "est soumise", "sont soumises",
        "est attribue", "sont attribues", "n'est attribue", "ne sont attribues",
        "est autorise", "sont autorises", "n'est autorise", "ne sont autorises",
        "est admis", "sont admis", "n'est admis", "ne sont admis",
        "est passible", "sont passibles",
        "repond de", "repondent de",
        "doit indiquer", "doit comprendre", "doit comporter",
    )
    return any(marker in text for marker in strong_normative_markers)


def _looks_fragmentary_runtime_requirement(text: str) -> bool:
    req = normalize_spaces(strip_article_header(str(text or ""))).strip(" -:;,.")
    if len(req) < 28:
        return False
    if re.match(r"(?i)^(?:art\.?|article|\d+\s*[-:])", req):
        return True
    if re.match(r"^[a-zàâäéèêëîïôöùûüç]", req):
        return True
    first_clause = re.split(r"(?<=[\.;:])\s+", req, maxsplit=1)[0]
    first_window = first_clause[:120]
    first_window_key = _ascii_fold_runtime(normalize_requirement_key(first_window))
    first_tokens = [tok for tok in first_window_key.split() if tok]
    first_token = first_tokens[0] if first_tokens else ""
    allowed_short_start_tokens = {
        "l", "le", "la", "les", "un", "une", "ce", "ces", "cette", "si", "au", "aux",
        "du", "de", "des", "en", "dans", "il", "elle", "a", "d",
    }
    has_subject = bool(
        re.match(
            r"(?i)^(?:l|les|le|la|un|une|tout(?:e)?|ce|ces|cette|il|elle|"
            r"employeur|travailleur|salarie|entreprise|etablissement|"
            r"registre|conseil|commission|ministre|declaration)\b",
            first_window_key,
        )
    )
    if first_token and len(first_token) <= 2 and first_token not in allowed_short_start_tokens:
        return True
    if len(first_tokens) == 1 and first_token and len(first_token) <= 6 and len(req) > len(first_clause) + 18:
        return True
    if re.search(r"(?i)\bdoit\s+et\s+de\b", first_window_key):
        return True
    if has_subject:
        return False
    if re.match(
        r"(?i)^(?:tre|etre|fixe?e?s?|indique?e?s?|prevu(?:e|es)?|prolonge?e?|olonge?e?|charge?e?s?|habilite?e?s?)\b",
        first_window_key,
    ):
        return True
    if re.match(r"(?i)^(?:de|du|des|d|et|ou|aux|au|a|pour|avec|sur|sous|dans)\b", first_window_key):
        return True
    sentence_parts = [
        part.strip(" -:;,.")
        for part in re.split(r"(?<=[\.;:!?])\s+", req)
        if part.strip(" -:;,.")
    ]
    if len(sentence_parts) >= 2:
        first_sentence = sentence_parts[0]
        second_sentence = sentence_parts[1]
        first_score = _runtime_start_candidate_score(first_sentence)
        second_score = _runtime_start_candidate_score(second_sentence)
        first_sentence_key = _ascii_fold_runtime(normalize_requirement_key(first_sentence))
        first_sentence_tokens = [tok for tok in first_sentence_key.split() if tok]
        if (
            second_score >= max(6, first_score + 4)
            and (
                not re.match(
                    r"(?i)^(?:l|les|le|la|un|une|tout(?:e)?|ce|ces|cette|si|lorsque|en\s+cas|dans\s+le\s+cas)\b",
                    first_sentence_key,
                )
                or (
                    first_sentence_tokens
                    and len(first_sentence_tokens[0]) <= 6
                    and len(first_sentence_tokens) <= 2
                )
            )
        ):
            return True
    return False


def _looks_subject_verb_mismatch_requirement(text: str) -> bool:
    req = normalize_spaces(strip_article_header(str(text or ""))).strip(" -:;,.")
    if len(req) < 28:
        return False
    req_fold = _ascii_fold_runtime(req.lower())
    if re.match(
        r"^(?:un|une|le|la|l|ce|cette)\s+(?:registre|document|contrat|plan|conseil|proces-verbal|proces verbal)"
        r"(?:\s+[a-z-]+){0,6}\s+sont\b",
        req_fold,
    ):
        return True
    if re.match(
        r"^(?:les|ces)\s+(?:documents?|contrats?|plans?|membres?|autorites?)"
        r"(?:\s+[a-z-]+){0,6}\s+est\b",
        req_fold,
    ):
        return True
    return False


def _runtime_snippet_noise_score(snippet: str, requirement_text: str = "") -> float:
    snippet_clean = clean_source_snippet(decode_html_entities(snippet or ""))
    if not snippet_clean:
        return 10.0
    score = 0.0
    if re.match(r"^[a-zàâäéèêëîïôöùûüç]", snippet_clean):
        score += 1.2
    if has_ocr_artifact_signals(snippet_clean):
        score += 2.0
    if _looks_fragmentary_runtime_requirement(snippet_clean):
        score += 2.2
    if _looks_subject_verb_mismatch_requirement(snippet_clean):
        score += 2.0
    if re.search(
        r"(?i)(?:\b(?:doit(?:vent)?|est\s+tenu(?:e|s|es)?\s+de|sont\s+tenu(?:s|es)?\s+de)\s+"
        r"(?:informer|notifier|communiquer|transmettre|adresser|indiquer|presenter|présenter|soumettre|remettre)\b"
        r"|"
        r"\b(?:informer|notifier|communiquer|transmettre|adresser|indiquer|presenter|présenter|soumettre|remettre)\b"
        r")[\s\.,;:!?-]*$",
        _ascii_fold_runtime(snippet_clean),
    ):
        score += 1.9
    folded_key = _ascii_fold_runtime(normalize_requirement_key(snippet_clean))
    if re.match(r"^(?:olonge|tre|art loi|ent transportent|es de dechets)", folded_key):
        score += 2.2
    if len(snippet_clean) > 280:
        score += 0.5
    if requirement_text:
        req_key = normalize_requirement_key(requirement_text)
        overlap = max(
            _promotion_overlap_ratio(requirement_text, snippet_clean),
            _token_overlap_ratio(req_key, normalize_requirement_key(snippet_clean)),
        )
        if overlap < 0.40:
            score += 1.6
    return round(score, 4)


def _tighten_runtime_evidence_snippet(
    *,
    requirement_text: str,
    current_snippet: str,
    source_snippet: str,
    snippet_context_text: str,
    chunk_text: str,
) -> str:
    req_text = clean_source_snippet(decode_html_entities(requirement_text or ""))
    current = clean_source_snippet(decode_html_entities(current_snippet or ""))
    context = clean_source_snippet(
        decode_html_entities(snippet_context_text or source_snippet or chunk_text or current)
    )

    best = current
    best_score = _runtime_snippet_noise_score(best, req_text)
    best_overlap = max(
        _promotion_overlap_ratio(req_text, best),
        _token_overlap_ratio(normalize_requirement_key(req_text), normalize_requirement_key(best)),
    ) if best else 0.0

    rebuilt_candidates: list[str] = []
    for base_text in (context, source_snippet, chunk_text):
        candidate = extract_best_citation_snippet(
            requirement_text=req_text,
            source_snippet=base_text,
            chunk_text=context or chunk_text,
            max_chars=220,
        )
        candidate = clean_source_snippet(repair_common_ocr_artifacts(candidate))
        if candidate:
            rebuilt_candidates.append(candidate)

    repaired_current = clean_source_snippet(repair_common_ocr_artifacts(current))
    if repaired_current:
        rebuilt_candidates.append(repaired_current)

    for candidate in rebuilt_candidates:
        candidate_score = _runtime_snippet_noise_score(candidate, req_text)
        candidate_overlap = max(
            _promotion_overlap_ratio(req_text, candidate),
            _token_overlap_ratio(normalize_requirement_key(req_text), normalize_requirement_key(candidate)),
        )
        if candidate_overlap + 0.01 < best_overlap:
            continue
        if candidate_score + 0.2 < best_score:
            best = candidate
            best_score = candidate_score
            best_overlap = candidate_overlap
        elif candidate_score <= best_score + 0.05 and candidate_overlap > best_overlap + 0.05:
            best = candidate
            best_score = candidate_score
            best_overlap = candidate_overlap

    if req_text and not _looks_fragmentary_runtime_requirement(req_text):
        req_as_snippet_score = _runtime_snippet_noise_score(req_text, req_text)
        oversized_best = len(best or "") > max(180, int(len(req_text) * 1.45))
        req_key = normalize_requirement_key(req_text)
        best_key = normalize_requirement_key(best)
        req_anchor = " ".join(req_key.split()[:4]).strip()
        anchor_pos = best_key.find(req_anchor) if req_anchor else -1
        leading_noise_before_anchor = anchor_pos > 28
        if (
            best_score >= 3.0
            or (best_overlap < 0.45 and req_as_snippet_score < best_score)
            or (
                oversized_best
                and (best_overlap < 0.78 or leading_noise_before_anchor)
                and req_as_snippet_score <= best_score + 0.15
            )
        ):
            return req_text
    return best or current or req_text


def _split_runtime_granular_requirement(
    *,
    requirement_text: str,
    req_type: str,
    source_snippet: str,
    snippet_context_text: str,
    chunk_text: str,
) -> list[str]:
    documentary_splits = split_introductory_documentary_requirement(
        requirement_text=requirement_text,
        source_snippet=source_snippet or snippet_context_text,
        req_type=req_type,
        chunk_text=snippet_context_text or chunk_text,
    )
    if len(documentary_splits) >= 2:
        return documentary_splits
    return split_fused_obligation(requirement_text, req_type)


_PROMOTION_ALLOWED_TYPES = {
    "OBLIGATION",
    "INTERDICTION",
    "CONDITION",
    "EXCEPTION",
    "DECLARATION",
    "REGISTRE",
}
_RUNTIME_PRECALL_ALLOWED_MODES = {"OFF", "SHADOW", "CONSERVATIVE"}
_RUNTIME_BARE_ARTICLE_HEADER_RE = re.compile(
    r"(?im)(?:^|(?<=[\n\r.;:]))\s*"
    r"(?P<label>premier|1er|unique|\d{1,3}(?:[-.]\d+)*)"
    r"(?:\s+(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies|undecies|duodecies|nouveau))?"
    r"\s*(?:[-:.'’\"]+)\s*"
)
_RUNTIME_FRAGMENT_SPLIT_RE = re.compile(r"(?<=[\.;:?!])\s+|\n+|(?<=\s)•\s+")
_RUNTIME_STRONG_START_RE = re.compile(
    r"(?i)^(?:tout(?:e)?|les|le|la|l['’]|un(?:e)?|ce|ces|il|en\s+cas|dans\s+le\s+cas|"
    r"au\s+cours|sous\s+r[ée]serve|si|lorsque|durant)\b"
)
_RUNTIME_BAD_START_RE = re.compile(
    r"(?i)^(?:art\.?|article|\d{1,3}\s*[-:]|de|du|des|d['’]|et|ou|aux|au|a|à)\b"
)
_RUNTIME_MAJOR_HEADING_RE = re.compile(
    r"(?i)\b(?:livre|titre|chapitre|section|sous[-\s]section)\b"
)
_RUNTIME_HEADING_BOUNDARY_RE = re.compile(
    r"(?i)\b(?:livre|titre|chapitre|section|sous[-\s]section)\b\s*[.:-]?\s*[ivxlcdm0-9]+\b(?:\s*[-:])?"
)
_RUNTIME_ARTICLE_HEADER_RE = re.compile(
    r"(?i)\b(?:art\.?|article)\s*"
    r"(?:(?P<label>premier|1er|unique|\d+(?:[-.]\d+)*)"
    r"(?:\s+(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies|undecies|duodecies|nouveau))?"
    r"\s*(?:[-:.'’\"]+)|(?:[-:.'’\"]+))"
)
_RUNTIME_PAGE_NOISE_RE = re.compile(
    r"(?i)\b(?:journal\s+officiel\s+de\s+la\s+r[ée]publique\s+tunisienne|page\s+\d+\b|n[°o]\s*\d+\b)\b"
)
_RUNTIME_DANGLING_TAIL_RE = re.compile(
    r"(?i)\b(?:et|ou|par|pour|de|du|des|d['’]|a|à|au|aux|avec|contre|sur|sous|dans|les|la|le|soit|il)$"
)


def _promotion_overlap_ratio(requirement_text: str, source_snippet: str) -> float:
    req = " ".join((requirement_text or "").strip().lower().split())
    src = " ".join((source_snippet or "").strip().lower().split())
    if not req:
        return 0.0
    req_tokens = set(re.findall(r"[a-z0-9àâäéèêëîïôöùûüç]+", req))
    src_tokens = set(re.findall(r"[a-z0-9àâäéèêëîïôöùûüç]+", src))
    if not req_tokens:
        return 0.0
    return len(req_tokens & src_tokens) / len(req_tokens)


def _extract_action_anchor(text: str) -> str:
    normalized = normalize_requirement_key(text)
    if not normalized:
        return ""
    word_pattern = r"[a-z0-9àâäéèêëîïôöùûüç-]+"
    stop_tokens = {
        "la",
        "le",
        "les",
        "des",
        "du",
        "de",
        "d",
        "l",
        "un",
        "une",
        "et",
        "aux",
        "au",
    }
    excluded_heads = {
        "habilite",
        "habilites",
        "charge",
        "charges",
        "tenu",
        "tenus",
        "gestion",
        "fonctions",
        "mesures",
    }

    def _clean_candidate(raw_candidate: str) -> str:
        return " ".join(tok for tok in raw_candidate.split() if tok not in stop_tokens).strip()

    def _is_verb_like(token: str) -> bool:
        return bool(re.search(r"(?:er|ir|re|oir)$", token))

    for pattern in (
        rf"\b(?:à|a)\s+({word_pattern}(?:\s+{word_pattern}){{0,2}})",
        rf"\b(?:doit|doivent|peut|peuvent)\s+({word_pattern}(?:\s+{word_pattern}){{0,2}})",
    ):
        for match in re.finditer(pattern, normalized):
            candidate = _clean_candidate(match.group(1))
            if not candidate:
                continue
            head = candidate.split()[0]
            if head in excluded_heads:
                continue
            if _is_verb_like(head):
                return candidate

    for pattern in (
        rf"\b(?:à|a|de)\s+({word_pattern}(?:\s+{word_pattern}){{0,2}})",
        rf"\b(?:doit|doivent|est|sont|peut|peuvent)\s+({word_pattern}(?:\s+{word_pattern}){{0,2}})",
    ):
        for match in re.finditer(pattern, normalized):
            candidate = _clean_candidate(match.group(1))
            if candidate:
                return candidate
    return ""


def _has_ambiguous_modal_peut(requirement_text: str) -> bool:
    req = " ".join((requirement_text or "").strip().lower().split())
    if not re.search(r"\bpeu(?:t|vent)\b", req):
        return False
    normative_companions = [
        "à condition",
        "a condition",
        "sous réserve",
        "sous reserve",
        "ouvre droit",
        "ouvrent droit",
        "est ouvert aux",
        "sont ouverts aux",
        "bénéficie",
        "bénéficient",
        "beneficie",
        "beneficient",
        "participer",
        "justifiant",
        "titulaire",
        "titulaires",
        "anciennet",
        "diplom",
        "licence",
        "maitrise",
        "maîtrise",
    ]
    return not any(marker in req for marker in normative_companions)


def _cross_reference_search_text(article_text: str) -> str:
    return " ".join(strip_article_header(article_text or "").strip().split())


def _normalize_runtime_context_text(text: str) -> str:
    return decode_html_entities(clean_source_snippet(sanitize_ocr_noise_for_extraction(text or "")))


def _normalize_runtime_article_identity(raw_value: str) -> str:
    value = str(raw_value or "").strip().lower().replace(".", "-")
    if value in {"1er", "premier"}:
        return "1"
    return value


def _extract_runtime_article_identity(article_label: str) -> str:
    match = re.search(r"(?i)\b(premier|1er|unique|\d+(?:[-.]\d+)*)\b", str(article_label or ""))
    if not match:
        return ""
    return _normalize_runtime_article_identity(match.group(1))


def _runtime_article_identity_matches(found_identity: str, target_identity: str) -> bool:
    found = _normalize_runtime_article_identity(found_identity)
    target = _normalize_runtime_article_identity(target_identity)
    if not found or not target:
        return False
    if found == target:
        return True
    return found.lstrip("0") == target.lstrip("0")


def _runtime_start_candidate_score(text: str) -> int:
    candidate = normalize_spaces(str(text or "")).strip(" -:;,.")
    if len(candidate) < 24:
        return -100
    lowered_head = candidate[:140]
    score = 0
    if candidate[:1].isupper():
        score += 2
    if _RUNTIME_STRONG_START_RE.match(candidate):
        score += 5
    if re.search(
        r"(?i)\b(?:doit|doivent|est\s+tenu|sont\s+tenus|interdit|obligatoire|"
        r"peut|peuvent|habilit\w*|charg\w*|communiquer|transmettre|notifier|"
        r"conserver|présent\w+|present\w+|registre|déclar\w+|declar\w+)\b",
        lowered_head,
    ):
        score += 4
    if _RUNTIME_BAD_START_RE.match(candidate):
        score -= 8
    if re.match(r"^[a-zàâäéèêëîïôöùûüç]", candidate):
        score -= 4
    return score


def _trim_runtime_article_core_head(text: str, article_label: str = "") -> str:
    candidate = _normalize_runtime_context_text(text)
    if len(candidate) < 80:
        return candidate

    parts = [
        part.strip()
        for part in _RUNTIME_FRAGMENT_SPLIT_RE.split(candidate)
        if part and part.strip()
    ]
    if len(parts) <= 1:
        return candidate

    rebuilt: list[tuple[int, str]] = []
    cursor = 0
    lowered_candidate = candidate.lower()
    for part in parts[:10]:
        idx = lowered_candidate.find(part.lower(), cursor)
        if idx < 0:
            idx = cursor
        rebuilt.append((idx, part))
        cursor = max(idx + len(part), cursor)

    if not rebuilt:
        return candidate

    first_score = _runtime_start_candidate_score(rebuilt[0][1])
    best_idx = 0
    best_score = first_score
    for idx, (_, part) in enumerate(rebuilt[1:], start=1):
        part_score = _runtime_start_candidate_score(part)
        if part_score > best_score:
            best_idx = idx
            best_score = part_score

    if best_idx == 0:
        return candidate

    improvement = best_score - first_score
    if best_score < 6 or improvement < 5:
        return candidate

    start_pos = rebuilt[best_idx][0]
    prefix_text = candidate[:start_pos]
    prefix_headers = _iter_runtime_article_headers(prefix_text)
    has_strong_prefix_boundary = any(found_identity for _, _, found_identity in prefix_headers) or bool(
        _RUNTIME_HEADING_BOUNDARY_RE.search(prefix_text)
    )
    first_fragment = rebuilt[0][1]
    short_header_like_prefix = (
        first_score <= 0
        and len(normalize_spaces(first_fragment)) < 140
        and bool(re.match(r"(?i)^\s*(?:art\.?|article)\b", str(first_fragment or "")))
    )
    if not has_strong_prefix_boundary and not short_header_like_prefix:
        return candidate

    trimmed = candidate[start_pos:].strip(" -:;,.")
    return trimmed if len(trimmed) >= 60 else candidate


def _iter_runtime_article_headers(text: str) -> list[tuple[int, int, str]]:
    headers: list[tuple[int, int, str]] = []
    for match in _RUNTIME_ARTICLE_HEADER_RE.finditer(text):
        headers.append(
            (
                match.start(),
                match.end(),
                _extract_runtime_article_identity(match.group("label") or ""),
            )
        )
    for match in _RUNTIME_BARE_ARTICLE_HEADER_RE.finditer(text):
        span = (match.start(), match.end())
        if any(not (span[1] <= start or span[0] >= end) for start, end, _ in headers):
            continue
        headers.append(
            (
                span[0],
                span[1],
                _extract_runtime_article_identity(match.group("label") or ""),
            )
        )
    headers.sort(key=lambda item: (item[0], item[1]))
    return headers


def _find_runtime_article_start(text: str, article_label: str = "") -> int:
    headers = _iter_runtime_article_headers(text)
    if not headers:
        return 0
    target_identity = _extract_runtime_article_identity(article_label)
    if target_identity:
        for start, _, found_identity in headers:
            if _runtime_article_identity_matches(found_identity, target_identity):
                return start
    first_start, _, _ = headers[0]
    if first_start <= 220:
        return first_start
    if first_start <= 700 and _RUNTIME_HEADING_BOUNDARY_RE.search(text[:first_start]):
        return first_start
    return 0


def _score_runtime_article_window(
    window_text: str,
    *,
    article_label: str = "",
    start_pos: int = 0,
) -> tuple[int, int, int, str] | None:
    candidate = _trim_runtime_article_core_head(window_text, article_label=article_label).strip()
    if len(candidate) < 70:
        return None
    target_identity = _extract_runtime_article_identity(article_label)
    normative_hits = len(
        re.findall(
            r"(?i)\b(doit|doivent|tenu|tenus|interdit|habilit\w*|charg\w*|peut|peuvent|communiquer|"
            r"transmettre|adresser|notifier|conclure|concluent|fixera|elabore|élabore|"
            r"inspection|infractions?|registre|présent\w+|present\w+)\b",
            candidate,
        )
    )
    foreign_article_refs = len(
        re.findall(r"(?i)\b(?:article|art\.?)\s*\d{1,3}(?:[-.]\d+)*\b", candidate)
    )
    heading_mentions = len(_RUNTIME_MAJOR_HEADING_RE.findall(candidate))
    article_headers = _iter_runtime_article_headers(candidate)
    foreign_article_headers = 0
    generic_article_headers = 0
    matching_article_headers = 0
    for idx, (_, _, found_identity) in enumerate(article_headers):
        if idx == 0 and not found_identity:
            continue
        if target_identity and _runtime_article_identity_matches(found_identity, target_identity):
            matching_article_headers += 1
            continue
        if found_identity:
            foreign_article_headers += 1
        else:
            generic_article_headers += 1
    early_bonus = max(0, 8 - min(7, start_pos // 140))
    start_quality = _runtime_start_candidate_score(candidate)
    unanchored_length_penalty = 0
    if matching_article_headers == 0 and len(candidate) > 1500:
        unanchored_length_penalty = min(48, max(0, (len(candidate) - 1500) // 45))
    quality_score = (
        (normative_hits * 10)
        + (matching_article_headers * 10)
        + early_bonus
        + (start_quality * 4)
        - (foreign_article_refs * 2)
        - (foreign_article_headers * 24)
        - (generic_article_headers * 8)
        - (max(0, heading_mentions - 1) * 3)
        - unanchored_length_penalty
    )
    return (quality_score, normative_hits, len(candidate), candidate)


def _find_runtime_article_end(text: str, start_pos: int, article_label: str = "") -> int:
    target_identity = _extract_runtime_article_identity(article_label)
    candidate_positions: list[int] = []
    min_normative_chars = 110

    for match in _RUNTIME_HEADING_BOUNDARY_RE.finditer(text):
        if match.start() > start_pos + min_normative_chars:
            candidate_positions.append(match.start())

    for header_start, _, found_identity in _iter_runtime_article_headers(text):
        if header_start <= start_pos + 36:
            continue
        if target_identity and _runtime_article_identity_matches(found_identity, target_identity):
            continue
        if found_identity:
            if header_start > start_pos + 40:
                candidate_positions.append(header_start)
            continue
        if header_start > start_pos + 180:
            candidate_positions.append(header_start)

    for match in _RUNTIME_PAGE_NOISE_RE.finditer(text):
        if match.start() > start_pos + min_normative_chars:
            candidate_positions.append(match.start())

    for boundary in sorted(set(candidate_positions)):
        candidate = text[start_pos:boundary].strip()
        scored = _score_runtime_article_window(
            candidate,
            article_label=article_label,
            start_pos=start_pos,
        )
        if not candidate:
            continue
        if scored and (scored[1] > 0 or len(candidate) >= 180):
            return boundary
    return len(text)


def _build_runtime_article_core(article_text: str, article_label: str = "") -> str:
    prepared = _normalize_runtime_context_text(article_text)
    if not prepared:
        return ""
    target_identity = _extract_runtime_article_identity(article_label)
    anchored_has_target_match = any(
        _runtime_article_identity_matches(found_identity, target_identity)
        for _, _, found_identity in _iter_runtime_article_headers(prepared)
    )
    anchored_start = _find_runtime_article_start(prepared, article_label)
    anchored_end = _find_runtime_article_end(prepared, anchored_start, article_label)
    anchored_candidate = prepared[anchored_start:anchored_end].strip()
    anchored_score = _score_runtime_article_window(
        anchored_candidate,
        article_label=article_label,
        start_pos=anchored_start,
    )
    if anchored_candidate and anchored_score and (
        anchored_start > 0 or anchored_has_target_match
    ):
        return _trim_runtime_article_core_tail(
            _trim_runtime_article_core_head(anchored_candidate, article_label=article_label)
        )
    heading_matches = list(_RUNTIME_MAJOR_HEADING_RE.finditer(prepared))
    if not heading_matches:
        if anchored_candidate and anchored_score and anchored_end < len(prepared):
            return _trim_runtime_article_core_tail(
                _trim_runtime_article_core_head(anchored_candidate, article_label=article_label)
            )
        return _trim_runtime_article_core_tail(
            _trim_runtime_article_core_head(prepared, article_label=article_label)
        )
    if anchored_candidate and anchored_score and anchored_end < len(prepared):
        if all(match.start() >= anchored_end for match in heading_matches):
            return _trim_runtime_article_core_tail(
                _trim_runtime_article_core_head(anchored_candidate, article_label=article_label)
            )

    prefix_window: tuple[int, int, int, str] | None = None
    heading_windows: list[tuple[int, int, int, str]] = []
    first_heading_start = heading_matches[0].start()
    if first_heading_start >= 40:
        scored = _score_runtime_article_window(
            prepared[:first_heading_start],
            article_label=article_label,
            start_pos=0,
        )
        if scored:
            prefix_window = scored

    for idx in range(len(heading_matches)):
        start = heading_matches[idx].start()
        end = heading_matches[idx + 1].start() if idx + 1 < len(heading_matches) else len(prepared)
        if end <= start:
            continue
        scored = _score_runtime_article_window(
            prepared[start:end],
            article_label=article_label,
            start_pos=start,
        )
        if scored:
            heading_windows.append(scored)

    if heading_windows:
        best_heading = max(heading_windows, key=lambda item: (item[0], item[1], item[2]))
        if len(heading_windows) >= 2:
            if not prefix_window or best_heading[:3] >= prefix_window[:3]:
                return _trim_runtime_article_core_tail(best_heading[3])
        if best_heading[0] > 0:
            if not prefix_window or best_heading[:3] >= prefix_window[:3]:
                return _trim_runtime_article_core_tail(best_heading[3])
    if prefix_window and prefix_window[0] > 0:
        return _trim_runtime_article_core_tail(prefix_window[3])

    windows: list[tuple[int, int, int, str]] = []
    if prefix_window:
        windows.append(prefix_window)
    windows.extend(heading_windows)

    if not windows:
        return prepared

    best_quality, best_hits, best_len, best_window = max(windows, key=lambda item: (item[0], item[1], item[2]))
    if best_quality > 0 or best_hits > 0:
        return _trim_runtime_article_core_tail(
            _trim_runtime_article_core_head(best_window, article_label=article_label)
        )
    if len(windows) > 1 and best_len >= 120:
        return _trim_runtime_article_core_tail(
            _trim_runtime_article_core_head(best_window, article_label=article_label)
        )
    if best_len >= 180:
        return _trim_runtime_article_core_tail(
            _trim_runtime_article_core_head(best_window, article_label=article_label)
        )
    return _trim_runtime_article_core_tail(
        _trim_runtime_article_core_head(prepared, article_label=article_label)
    )


def _should_seed_runtime_article_core(article_text: str, article_core_text: str) -> bool:
    article_text_norm = _normalize_runtime_context_text(article_text)
    article_core_norm = _normalize_runtime_context_text(article_core_text)
    if not article_text_norm or not article_core_norm:
        return False
    if article_text_norm == article_core_norm:
        return False
    header_count = len(_iter_runtime_article_headers(article_text_norm))
    heading_count = len(_RUNTIME_HEADING_BOUNDARY_RE.findall(article_text_norm))
    if header_count >= 2:
        return True
    if heading_count >= 1 and len(article_text_norm) - len(article_core_norm) >= 80:
        return True
    overlap = max(
        _promotion_overlap_ratio(article_core_norm, article_text_norm),
        _token_overlap_ratio(
            normalize_requirement_key(article_core_norm),
            normalize_requirement_key(article_text_norm),
        ),
    )
    return overlap < 0.92 and len(article_text_norm) - len(article_core_norm) >= 120


def _select_runtime_article_source_window(
    *,
    article_label: str,
    article_text: str,
    ordered_chunks: list[tuple[int, str]],
) -> tuple[str, set[int], bool]:
    normalized_article_text = _normalize_runtime_context_text(article_text)
    normalized_chunks = [
        (int(chunk_no), _normalize_runtime_context_text(chunk_text))
        for chunk_no, chunk_text in ordered_chunks
        if _normalize_runtime_context_text(chunk_text)
    ]
    if not normalized_chunks:
        return normalized_article_text, set(), False

    all_chunk_nos = {chunk_no for chunk_no, _ in normalized_chunks}
    if len(normalized_chunks) == 1:
        only_chunk_text = normalized_chunks[0][1]
        source_text = normalized_article_text or only_chunk_text
        return source_text, all_chunk_nos, bool(
            normalized_article_text and normalize_spaces(source_text) != normalize_spaces(only_chunk_text)
        )

    candidates: list[tuple[float, int, int, int, int, str, set[int]]] = []

    def add_candidate(source_text: str, chunk_nos: set[int]) -> None:
        prepared = _normalize_runtime_context_text(source_text)
        if len(prepared) < 80:
            return
        core_text = _build_runtime_article_core(prepared, article_label=article_label)
        scored = _score_runtime_article_window(core_text, article_label=article_label, start_pos=0)
        if not scored:
            return
        quality_score, normative_hits, candidate_len, candidate_text = scored
        matching_headers = 0
        foreign_headers = 0
        for _, _, found_identity in _iter_runtime_article_headers(candidate_text):
            if found_identity and _runtime_article_identity_matches(found_identity, _extract_runtime_article_identity(article_label)):
                matching_headers += 1
            elif found_identity:
                foreign_headers += 1
        start_score = _runtime_start_candidate_score(candidate_text)
        span_penalty = max(0, len(chunk_nos) - 1) * 18
        unanchored_penalty = 0
        if matching_headers == 0 and candidate_len > 1200:
            unanchored_penalty = min(96, max(0, (candidate_len - 1200) // 20))
        tail_penalty = 0
        if _RUNTIME_DANGLING_TAIL_RE.search(candidate_text) or not re.search(r"[\.;:!?]\s*$", candidate_text):
            tail_penalty = 28
        adjusted_score = float(quality_score) - float(span_penalty + unanchored_penalty + tail_penalty)
        candidates.append(
            (
                adjusted_score,
                start_score,
                -foreign_headers,
                -len(chunk_nos),
                candidate_len,
                candidate_text,
                set(chunk_nos),
            )
        )

    if normalized_article_text:
        add_candidate(normalized_article_text, set(all_chunk_nos))

    chunk_count = len(normalized_chunks)
    if 1 < chunk_count <= 8:
        for end_idx in range(chunk_count):
            chunk_slice = normalized_chunks[: end_idx + 1]
            joined_text = " ".join(text for _, text in chunk_slice if text)
            add_candidate(joined_text, {chunk_no for chunk_no, _ in chunk_slice})

    if not candidates:
        source_text = normalized_article_text or " ".join(text for _, text in normalized_chunks)
        return source_text, set(all_chunk_nos), False

    best_score, _, _, _, _, best_source_text, best_chunk_nos = max(candidates)
    default_source = normalized_article_text or " ".join(text for _, text in normalized_chunks)
    default_chunk_nos = set(all_chunk_nos)
    target_identity = _extract_runtime_article_identity(article_label)
    best_has_target_anchor = any(
        _runtime_article_identity_matches(found_identity, target_identity)
        for _, _, found_identity in _iter_runtime_article_headers(best_source_text)
    )
    use_seed = (
        best_score > 0
        and (
            best_chunk_nos != default_chunk_nos
            or normalize_spaces(best_source_text) != normalize_spaces(default_source)
        )
    )
    if use_seed and len(best_chunk_nos) == 1 and len(normalized_chunks) > 2 and not best_has_target_anchor:
        use_seed = False
    return (best_source_text if use_seed else default_source), (best_chunk_nos if use_seed else default_chunk_nos), use_seed


def _trim_runtime_article_core_tail(text: str) -> str:
    candidate = (text or "").strip(" ,;:-")
    if len(candidate) < 80:
        return candidate
    if not _RUNTIME_DANGLING_TAIL_RE.search(candidate):
        return candidate

    tail_window_start = max(0, len(candidate) - 220)
    strong_cut_candidates = [
        candidate.rfind(": ", tail_window_start),
        candidate.rfind("; ", tail_window_start),
        candidate.rfind(". ", tail_window_start),
    ]
    for cut_at in strong_cut_candidates:
        if cut_at >= 80:
            return candidate[:cut_at].strip(" ,;:-")

    fallback_cut_candidates = [
        candidate.rfind(", ", tail_window_start),
        candidate.rfind(" et ", tail_window_start),
        candidate.rfind(" ou ", tail_window_start),
    ]
    for cut_at in fallback_cut_candidates:
        if cut_at >= 80:
            return candidate[:cut_at].strip(" ,;:-")
    return candidate


def _source_belongs_to_article_core(source_snippet: str, article_core_text: str) -> bool:
    source = _normalize_runtime_context_text(source_snippet)
    core = _normalize_runtime_context_text(article_core_text)
    if not source or not core:
        return True
    source_key = normalize_requirement_key(source)
    core_key = normalize_requirement_key(core)
    if source_key and source_key in core_key:
        return True
    overlap = max(
        _promotion_overlap_ratio(source, core),
        _token_overlap_ratio(source_key, core_key),
    )
    return overlap >= 0.58


def _localize_runtime_context(context_text: str, source_snippet: str, *, max_chars: int = 1100) -> str:
    context = _normalize_runtime_context_text(context_text)
    source = _normalize_runtime_context_text(source_snippet)
    if not context:
        return ""
    if not source:
        return context[:max_chars].strip()

    source_lower = source.lower()
    context_lower = context.lower()
    start_idx = context_lower.find(source_lower)

    if start_idx >= 0:
        heading_matches = list(_RUNTIME_MAJOR_HEADING_RE.finditer(context))
        window_start = 0
        window_end = len(context)
        for match in heading_matches:
            if match.start() < start_idx:
                window_start = match.start()
                continue
            if match.start() > start_idx:
                window_end = match.start()
                break
        localized = context[window_start:window_end].strip()
        if len(localized) <= max_chars:
            return localized
        local_start = max(0, start_idx - 280)
        local_end = min(len(context), start_idx + len(source) + 280)
        clipped = context[local_start:local_end].strip()
        return clipped if clipped else localized[:max_chars].strip()

    candidates = [
        part.strip()
        for part in re.split(r"(?<=[\.;:?!])\s+|\n+", context)
        if part and part.strip()
    ]
    if not candidates:
        return context[:max_chars].strip()
    best_candidate = max(
        candidates,
        key=lambda part: max(
            _promotion_overlap_ratio(source, part),
            _token_overlap_ratio(normalize_requirement_key(source), normalize_requirement_key(part)),
        ),
    )
    best_overlap = max(
        _promotion_overlap_ratio(source, best_candidate),
        _token_overlap_ratio(normalize_requirement_key(source), normalize_requirement_key(best_candidate)),
    )
    if best_overlap >= 0.42:
        return best_candidate[:max_chars].strip()
    return context[:max_chars].strip()


def _build_trusted_snippet_context(
    *,
    source_snippet: str,
    chunk_text: str,
    article_core_text: str,
) -> str:
    source = _normalize_runtime_context_text(source_snippet)
    chunk = _normalize_runtime_context_text(chunk_text)
    article_core = _normalize_runtime_context_text(article_core_text)
    if article_core and _source_belongs_to_article_core(source, article_core):
        return _localize_runtime_context(article_core, source)
    if chunk:
        return _localize_runtime_context(chunk, source)
    return source


def _build_runtime_llm_unit_text(
    *,
    unit_text: str,
    chunk_text: str,
    article_core_text: str,
) -> str:
    unit = _normalize_runtime_context_text(unit_text)
    if not unit:
        return ""
    article_core = _normalize_runtime_context_text(article_core_text)
    if article_core and _source_belongs_to_article_core(unit, article_core):
        localized = _localize_runtime_context(
            article_core,
            unit,
            max_chars=max(260, min(1200, len(unit) + 240)),
        )
        if localized:
            return localized
    chunk = _normalize_runtime_context_text(chunk_text)
    if chunk:
        localized = _localize_runtime_context(
            chunk,
            unit,
            max_chars=max(260, min(1200, len(unit) + 240)),
        )
        if localized:
            return localized
    return unit


def _read_runtime_precall_mode() -> str:
    raw = str(os.getenv("A1_PRECALL_RUNTIME_MODE", "CONSERVATIVE") or "").strip().upper()
    return raw if raw in _RUNTIME_PRECALL_ALLOWED_MODES else "CONSERVATIVE"


def _count_downstream_requirement_refs(
    cur,
    *,
    doc_id: str,
    article_ids: list[str] | None = None,
) -> int:
    if article_ids:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM compliance_checks cc
            JOIN requirements r ON r.requirement_id = cc.requirement_id
            WHERE r.doc_id=%s
              AND r.article_id = ANY(%s::uuid[])
            """,
            (doc_id, article_ids),
        )
    else:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM compliance_checks cc
            JOIN requirements r ON r.requirement_id = cc.requirement_id
            WHERE r.doc_id=%s
            """,
            (doc_id,),
        )
    return int((cur.fetchone() or [0])[0] or 0)


def _build_runtime_precall_units(
    chunk_text: str,
    *,
    article_label: str | None = None,
    max_unit_chars: int = 900,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    normalized_text, _ = normalize_legal_text(chunk_text or "")
    units, _ = segment_legal_units(
        normalized_text,
        article_label=article_label,
        max_unit_chars=max(180, int(max_unit_chars)),
    )
    runtime_units: list[dict[str, Any]] = []
    stats = {"HIGH": 0, "LOW": 0, "DROP": 0}
    for unit in units:
        unit_text = str((unit or {}).get("text") or "").strip()
        if not unit_text:
            continue
        source = str((unit or {}).get("source") or "unknown").strip() or "unknown"
        classification = classify_legal_unit(
            unit_text,
            article_label=article_label,
            source=source,
        )
        priority = str(classification.get("priority") or "DROP").strip().upper() or "DROP"
        if priority not in stats:
            priority = "DROP"
        stats[priority] += 1
        runtime_units.append(
            {
                "text": unit_text,
                "source": source,
                "priority": priority,
                "normative_score": float(classification.get("normative_score") or 0.0),
                "strong_normative": bool(classification.get("strong_normative")),
                "rule_hits": list(classification.get("rule_hits") or []),
            }
        )
    return normalized_text, runtime_units, stats


def _should_skip_unit_by_precall(
    *,
    mode: str,
    priority: str,
    unit_has_risk: bool,
    relaxed_by_safety: bool,
) -> tuple[bool, str]:
    mode_upper = str(mode or "").strip().upper()
    priority_upper = str(priority or "").strip().upper()
    if mode_upper == "OFF":
        return False, "PRECALL_OFF"
    if priority_upper != "DROP":
        return False, "PRECALL_PASS"
    if mode_upper == "SHADOW":
        return False, "PRECALL_DROP_SHADOW"
    if relaxed_by_safety or unit_has_risk:
        return False, "PRECALL_DROP_SHADOW_RISK"
    return True, "PRECALL_DROP_LOW_SIGNAL"


def _build_runtime_unit_cache_key(*, scope_ref: str = "", unit_text: str) -> str:
    unit_key = normalize_requirement_key(unit_text or "")
    if not unit_key:
        return ""
    scope_key = normalize_requirement_key(scope_ref or "")
    if scope_key:
        return f"{scope_key}|{unit_key}"
    return unit_key


def _dedupe_runtime_units(
    units: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    *,
    scope_ref: str = "",
) -> tuple[list[dict[str, Any]], int]:
    deduped_units: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    removed_total = 0
    for unit_meta in units or []:
        unit_text = str((unit_meta or {}).get("text") or "").strip()
        if not unit_text:
            continue
        cache_key = _build_runtime_unit_cache_key(scope_ref=scope_ref, unit_text=unit_text)
        if cache_key and cache_key in seen_keys:
            removed_total += 1
            continue
        if cache_key:
            seen_keys.add(cache_key)
        deduped_units.append(dict(unit_meta or {}))
    return deduped_units, removed_total


def _runtime_budget_guard_reason(
    *,
    llm_calls: int,
    total_tokens: int,
    estimated_cost_usd: float,
    max_llm_calls: int,
    max_total_tokens: int,
    max_estimated_cost_usd: float,
) -> str:
    if max_llm_calls > 0 and int(llm_calls) >= int(max_llm_calls):
        return "MAX_LLM_CALLS"
    if max_total_tokens > 0 and int(total_tokens) >= int(max_total_tokens):
        return "MAX_TOTAL_TOKENS"
    if max_estimated_cost_usd > 0 and float(estimated_cost_usd) >= float(max_estimated_cost_usd):
        return "MAX_ESTIMATED_COST_USD"
    return ""


def _maybe_promote_to_draft(
    *,
    status: str,
    req_type: str,
    confidence: float,
    requirement_text: str,
    source_snippet: str,
    postcall: dict[str, Any] | None,
    error_memory_hit: dict[str, Any] | None,
    article_policy: str,
    enabled: bool,
    min_confidence: float,
    min_overlap: float,
    min_snippet_chars: int,
    max_req_chars: int,
    max_snippet_chars: int,
    block_modal_peut: bool,
    require_clean_start: bool,
    min_quality_score: float,
    min_grounding_score: float,
    min_completeness_score: float,
    min_subject_consistency: float,
    require_postcall_draft: bool,
    block_fused_action_chain: bool,
) -> tuple[str, str, float]:
    current = str(status or "").strip().upper() or "TO_VALIDATE"
    if not enabled:
        return current, "PROMOTION_DISABLED", 0.0
    if current != "TO_VALIDATE":
        return current, "NOT_TO_VALIDATE", 0.0
    memory_hit = dict(error_memory_hit or {})
    memory_force_validate = str(memory_hit.get("memory_action") or "").strip().upper() == "FORCE_VALIDATE"
    memory_error_family = str(memory_hit.get("error_family") or "").strip().upper()
    if str(article_policy or "").strip().upper() != POLICY_EXTRACT_FULL:
        return current, "POLICY_NOT_FULL", 0.0
    if str(req_type or "").strip().upper() not in _PROMOTION_ALLOWED_TYPES:
        return current, "TYPE_NOT_ALLOWED", 0.0
    req_text = str(requirement_text or "").strip()
    if max_req_chars > 0 and len(req_text) > int(max_req_chars):
        return current, "REQUIREMENT_TOO_LONG", 0.0

    # --- Form quality guards (Phase 1) ---
    # Tolère les obligations courtes mais juridiquement propres.
    req_words = req_text.split()
    if len(req_words) < 6:
        return current, "TEXT_TOO_SHORT", 0.0
    if len(req_words) > 120:
        return current, "TEXT_TOO_LONG_WORDS", 0.0

    req_lower = _ascii_fold_runtime(req_text).lower()

    _LEGAL_SUBJECTS = (
        "l'employeur", "le chef ", "tout travailleur", "le travailleur",
        "le salarié", "les salariés", "toute personne", "l'entreprise",
        "les entreprises", "le médecin", "le responsable", "les candidats",
        "le candidat", "le jury", "la commission", "le directeur",
        "le ministre", "l'organisme", "l'établissement", "les membres",
        "tout employeur", "tout établissement", "il est interdit",
        "est interdit", "toute demande", "tout dossier",
    )
    _PROMOTION_LEGAL_SUBJECTS_ASCII = (
        "l'employeur", "le chef ", "tout travailleur", "le travailleur",
        "le salarie", "les salaries", "toute personne", "l'entreprise",
        "les entreprises", "le medecin", "le responsable", "les candidats",
        "le candidat", "le jury", "la commission", "le directeur",
        "le ministre", "l'organisme", "l'etablissement", "les membres",
        "tout employeur", "tout etablissement", "il est interdit",
        "est interdit", "toute demande", "tout dossier",
    )
    _has_legal_subject = _has_promotion_legal_subject(req_lower)
    if not _has_legal_subject:
        return current, "NO_LEGAL_SUBJECT", 0.0

    _NORMATIVE_VERBS = (
        "doit ", "est tenu", "est interdit", "ne peut pas", "ne peut ",
        "est obligé", "sont tenus", "doivent ", "doit être",
        "doit déclarer", "doit tenir", "doit présenter", "doit fournir",
        "répond de", "est habilité", "est chargé", "est rejeté",
        "est recouvré", "est affecté", "entraîne ", "sont rejetés",
        "doit être présenté", "doit comporter", "doit comprendre",
        "doit indiquer", "doit afficher", "est prouvée", "est fixé",
        "est fixée", "peut ", "peut bénéficier",
    )
    _PROMOTION_NORMATIVE_VERBS_ASCII = (
        "doit ", "est tenu", "est interdit", "est interdite", "strictement interdit",
        "strictement interdite", "ne peut pas", "ne peut ",
        "est oblige", "sont tenus", "doivent ", "doit etre",
        "doit declarer", "doit tenir", "doit presenter", "doit fournir",
        "repond de", "repondent de",
        "est habilite", "est charge", "sont charges", "est rejete",
        "est recouvre", "est affecte", "entraine ", "sont rejetes",
        "doit etre presente", "doit comporter", "doit comprendre",
        "doit indiquer", "doit afficher", "est prouvee", "est fixe",
        "est fixee", "peut ", "peuvent ",
        "peut obliger", "peuvent obliger",
        "peut prescrire", "peuvent prescrire",
        "est passible", "sont passibles",
        "peut beneficier",
    )
    _has_normative_verb = any(v in req_lower for v in _PROMOTION_NORMATIVE_VERBS_ASCII)
    if not _has_normative_verb:
        return current, "NO_NORMATIVE_VERB", 0.0
    # --- End form quality guards ---

    if float(confidence or 0.0) < float(min_confidence):
        return current, "LOW_CONFIDENCE", 0.0
    source = str(source_snippet or "").strip()
    snippet_len = len(source)
    if snippet_len < int(min_snippet_chars):
        return current, "SHORT_SNIPPET", 0.0
    if max_snippet_chars > 0 and snippet_len > int(max_snippet_chars):
        return current, "SNIPPET_TOO_LONG", 0.0
    if block_modal_peut and _has_ambiguous_modal_peut(req_text):
        return current, "MODAL_PEUT", 0.0
    if require_clean_start:
        if req_text and not req_text[0].isupper():
            return current, "NOISY_START", 0.0
        if re.match(r"(?i)^\s*article\s+\d+\s*\(nouveau\)", req_text):
            return current, "NOISY_START", 0.0
    runtime_postcall = dict(postcall or {})
    grounding_score = float(runtime_postcall.get("grounding_score") or 0.0)
    grounding_verdict = str(runtime_postcall.get("grounding_verdict") or "SOFT_FAIL").strip().upper()
    completeness_score = float(runtime_postcall.get("completeness_score") or 0.0)
    completeness_verdict = str(runtime_postcall.get("completeness_verdict") or "SOFT_FAIL").strip().upper()
    subject_consistency_score = float(runtime_postcall.get("subject_consistency_score") or 0.0)
    quality_score = float(runtime_postcall.get("quality_score") or 0.0)
    quality_decision = str(runtime_postcall.get("quality_decision") or "TO_VALIDATE").strip().upper()
    type_match = bool(runtime_postcall.get("type_match"))
    fused_action_chain = bool(runtime_postcall.get("fused_action_chain"))
    missing_condition = bool(runtime_postcall.get("missing_condition"))
    missing_exception = bool(runtime_postcall.get("missing_exception"))
    missing_scope = bool(runtime_postcall.get("missing_scope"))
    dangling_tail = bool(runtime_postcall.get("dangling_tail"))
    lexical_overlap = float(runtime_postcall.get("lexical_overlap") or 0.0)

    if block_fused_action_chain and fused_action_chain:
        return current, "FUSED_ACTION_CHAIN", lexical_overlap
    if missing_condition or missing_exception or missing_scope or dangling_tail:
        return current, "MISSING_COMPONENT", lexical_overlap
    if grounding_verdict != "PASS" or grounding_score < float(min_grounding_score):
        return current, "GROUNDING_TOO_WEAK", lexical_overlap
    if completeness_verdict != "PASS" or completeness_score < float(min_completeness_score):
        return current, "COMPLETENESS_NOT_PASS", lexical_overlap
    if not type_match:
        return current, "TYPE_MISMATCH_POSTCALL", lexical_overlap
    if subject_consistency_score < float(min_subject_consistency):
        return current, "SUBJECT_INCONSISTENT", lexical_overlap
    if quality_score < float(min_quality_score):
        return current, "QUALITY_TOO_LOW", lexical_overlap
    if require_postcall_draft and quality_decision != "DRAFT":
        return current, "POSTCALL_NOT_DRAFT", lexical_overlap

    overlap = max(_promotion_overlap_ratio(requirement_text, source_snippet), lexical_overlap)
    if overlap < float(min_overlap):
        return current, "LOW_OVERLAP", overlap
    if memory_force_validate:
        weak_grounding_recovered = (
            memory_error_family == "WEAK_GROUNDING"
            and float(confidence or 0.0) >= max(float(min_confidence), 0.85)
            and grounding_verdict == "PASS"
            and grounding_score >= max(float(min_grounding_score), 0.82)
            and completeness_verdict == "PASS"
            and completeness_score >= max(float(min_completeness_score), 0.90)
            and subject_consistency_score >= max(float(min_subject_consistency), 0.75)
            and quality_score >= max(float(min_quality_score), 0.88)
            and quality_decision == "DRAFT"
            and overlap >= max(float(min_overlap), 0.60)
        )
        if not weak_grounding_recovered:
            return current, "ERROR_MEMORY_HIT", overlap
    return "DRAFT", "PROMOTED_STRONG_SIGNAL", overlap


def _apply_error_memory_fix(
    *,
    requirement_text: str,
    req_type: str,
    source_snippet: str,
    evidence_snippet: str,
    snippet_context_text: str,
    chunk_text: str,
    memory_hit: dict[str, Any] | None,
) -> dict[str, Any]:
    memory_hit = dict(memory_hit or {})
    fix_rule = str(memory_hit.get("fix_rule") or "").strip().upper()
    req_text = decode_html_entities(str(requirement_text or "").strip())
    req_type_up = str(req_type or "AUTRE").strip().upper() or "AUTRE"
    source = clean_source_snippet(source_snippet or "")
    snippet = clean_source_snippet(evidence_snippet or "")
    context_source = clean_source_snippet(snippet_context_text or source or snippet or chunk_text)
    applied_rules: list[str] = []

    def _normalize_candidate(text_value: str, type_value: str) -> str:
        candidate = decode_html_entities(str(text_value or "").strip())
        candidate = normalize_subject_from_context(
            candidate,
            source or snippet or context_source,
            chunk_text,
        )
        candidate = normalize_requirement_text_by_type(
            requirement_text=candidate,
            source_snippet=source or context_source,
            req_type=type_value,
            chunk_text=chunk_text,
        )
        candidate = ground_requirement_to_source(
            requirement_text=candidate,
            source_snippet=source or context_source,
            req_type=type_value,
        )
        candidate = refine_requirement_text_quality(
            requirement_text=candidate,
            source_snippet=source or context_source,
            req_type=type_value,
            chunk_text=chunk_text,
        )
        return candidate

    if fix_rule == "RETRY_SPLIT":
        split_candidates: list[dict[str, str]] = []
        for candidate_source in (req_text, source or context_source):
            if not candidate_source:
                continue
            split_texts = _split_runtime_granular_requirement(
                requirement_text=candidate_source,
                req_type=req_type_up,
                source_snippet=source or context_source,
                snippet_context_text=snippet_context_text,
                chunk_text=chunk_text,
            )
            if len(split_texts) < 2:
                continue
            seen_split_keys: set[tuple[str, str]] = set()
            for split_text in split_texts:
                split_req_type = normalize_req_type(
                    requirement_text=split_text,
                    source_snippet=source or context_source,
                    llm_req_type=req_type_up,
                )
                normalized_split = _normalize_candidate(split_text, split_req_type)
                split_key = (split_req_type, normalize_requirement_key(normalized_split))
                if not split_key[1] or split_key in seen_split_keys:
                    continue
                seen_split_keys.add(split_key)
                split_candidates.append({"text": normalized_split, "req_type": split_req_type})
            if len(split_candidates) >= 2:
                applied_rules.append(fix_rule)
                return {
                    "applied": True,
                    "fix_rule": fix_rule,
                    "requirement_text": req_text,
                    "req_type": req_type_up,
                    "evidence_snippet": snippet,
                    "split_requirements": split_candidates,
                    "applied_rules": applied_rules,
                }

    if fix_rule == "REGROUND_FROM_SOURCE":
        primary_context = source or context_source
        regrounded = _normalize_candidate(req_text, req_type_up)
        if regrounded and regrounded != req_text:
            req_text = regrounded
            applied_rules.append(fix_rule)
        if not applied_rules and primary_context:
            best_split_text = ""
            best_split_score = 0.0
            action_anchor = _extract_action_anchor(req_text)
            split_candidates = _split_runtime_granular_requirement(
                requirement_text=primary_context,
                req_type=req_type_up,
                source_snippet=primary_context,
                snippet_context_text=snippet_context_text,
                chunk_text=chunk_text,
            )
            for split_text in split_candidates:
                split_req_type = normalize_req_type(
                    requirement_text=split_text,
                    source_snippet=primary_context,
                    llm_req_type=req_type_up,
                )
                normalized_split = _normalize_candidate(split_text, split_req_type)
                split_score = max(
                    _promotion_overlap_ratio(req_text, normalized_split),
                    _token_overlap_ratio(
                        normalize_requirement_key(req_text),
                        normalize_requirement_key(normalized_split),
                    ),
                )
                split_score += 0.45 * SequenceMatcher(
                    None,
                    normalize_requirement_key(req_text),
                    normalize_requirement_key(normalized_split),
                ).ratio()
                normalized_split_key = normalize_requirement_key(normalized_split)
                if action_anchor:
                    if action_anchor in normalized_split_key:
                        split_score += 0.70
                    elif action_anchor.split()[0] in normalized_split_key.split():
                        split_score += 0.35
                if split_score > best_split_score:
                    best_split_text = normalized_split
                    best_split_score = split_score
            if best_split_text and best_split_score >= 0.72:
                if best_split_text != req_text:
                    req_text = best_split_text
                rebuilt_split_snippet = extract_best_citation_snippet(
                    requirement_text=req_text,
                    source_snippet=primary_context,
                    chunk_text=snippet_context_text or chunk_text,
                )
                if rebuilt_split_snippet:
                    snippet = rebuilt_split_snippet if rebuilt_split_snippet != snippet else best_split_text
                applied_rules.append(fix_rule)
        if not applied_rules and primary_context:
            rebuilt_req = extract_best_citation_snippet(
                requirement_text=req_text,
                source_snippet=primary_context,
                chunk_text=snippet_context_text or chunk_text,
            )
            rebuilt_req = _normalize_candidate(rebuilt_req or primary_context, req_type_up)
            rebuilt_overlap = max(
                _promotion_overlap_ratio(req_text, rebuilt_req),
                _token_overlap_ratio(req_text, rebuilt_req),
            )
            if rebuilt_req and rebuilt_req != req_text and rebuilt_overlap >= 0.45:
                req_text = rebuilt_req
                applied_rules.append(fix_rule)

    if fix_rule == "RETYPE_FROM_SOURCE":
        corrected_type = normalize_req_type(
            requirement_text=req_text,
            source_snippet=source or context_source or snippet,
            llm_req_type=req_type_up,
        )
        if corrected_type != req_type_up:
            req_type_up = corrected_type
            applied_rules.append(fix_rule)
            req_text = _normalize_candidate(req_text, req_type_up)

    if fix_rule == "RESUBJECT_FROM_CONTEXT":
        corrected_req = normalize_subject_from_context(
            req_text,
            source or snippet or context_source,
            chunk_text,
        )
        corrected_req = _normalize_candidate(corrected_req, req_type_up)
        if corrected_req and corrected_req != req_text:
            req_text = corrected_req
            applied_rules.append(fix_rule)

    if fix_rule == "REBUILD_SNIPPET":
        rebuilt = extract_best_citation_snippet(
            requirement_text=req_text,
            source_snippet=context_source or source or snippet,
            chunk_text=snippet_context_text or chunk_text,
        )
        if rebuilt and rebuilt != snippet:
            snippet = rebuilt
            applied_rules.append(fix_rule)

    if applied_rules and fix_rule != "REBUILD_SNIPPET":
        rebuilt_snippet = extract_best_citation_snippet(
            requirement_text=req_text,
            source_snippet=context_source or source or snippet,
            chunk_text=snippet_context_text or chunk_text,
        )
        rebuilt_overlap = _promotion_overlap_ratio(req_text, rebuilt_snippet)
        current_overlap = _promotion_overlap_ratio(req_text, snippet)
        if (
            rebuilt_snippet
            and (
                not snippet
                or rebuilt_overlap > (current_overlap + 0.01)
                or (
                    rebuilt_overlap >= max(0.0, current_overlap - 0.01)
                    and len(rebuilt_snippet) < len(snippet)
                )
            )
        ):
            snippet = rebuilt_snippet

    return {
        "applied": bool(applied_rules),
        "fix_rule": fix_rule,
        "requirement_text": req_text,
        "req_type": req_type_up,
        "evidence_snippet": snippet,
        "split_requirements": [],
        "applied_rules": applied_rules,
    }


def _evaluate_runtime_requirement(
    *,
    requirement_text: str,
    req_type: str,
    source_snippet: str,
    snippet_context_text: str,
    chunk_text: str,
    evidence_snippet_override: str = "",
    normative_strength: str | None = None,
) -> dict[str, Any]:
    current_req_text = decode_html_entities(str(requirement_text or "").strip())
    current_req_type = str(req_type or "AUTRE").strip().upper() or "AUTRE"
    current_req_text = ground_requirement_to_source(
        requirement_text=current_req_text,
        source_snippet=source_snippet,
        req_type=current_req_type,
    )
    current_req_text = refine_requirement_text_quality(
        requirement_text=current_req_text,
        source_snippet=source_snippet,
        req_type=current_req_type,
        chunk_text=chunk_text,
    )
    evidence_snippet = clean_source_snippet(evidence_snippet_override or "")
    if not evidence_snippet:
        evidence_snippet = extract_best_citation_snippet(
            requirement_text=current_req_text,
            source_snippet=source_snippet,
            chunk_text=snippet_context_text,
        )
    confidence = classify_confidence(
        requirement_text=current_req_text,
        snippet=evidence_snippet,
        chunk_text=chunk_text,
        req_type=current_req_type,
    )
    initial_status = compute_status(
        confidence=confidence,
        requirement_text=current_req_text,
        snippet=evidence_snippet,
        chunk_text=chunk_text,
        req_type=current_req_type,
    )
    runtime_postcall = assess_postcall_requirement(
        requirement_text=current_req_text,
        req_type=current_req_type,
        snippet=evidence_snippet,
        chunk_text=chunk_text,
        confidence=confidence,
        status=initial_status,
        normative_strength=normative_strength,
    )
    current_req_text = decode_html_entities(
        str(runtime_postcall.get("adjusted_requirement_text") or current_req_text)
    )
    current_req_type = (
        str(runtime_postcall.get("adjusted_req_type") or current_req_type).strip().upper()
        or current_req_type
    )
    current_req_text = normalize_subject_from_context(
        current_req_text,
        evidence_snippet or source_snippet,
        chunk_text,
    )
    current_req_text = normalize_requirement_text_by_type(
        requirement_text=current_req_text,
        source_snippet=evidence_snippet or source_snippet,
        req_type=current_req_type,
        chunk_text=chunk_text,
    )
    current_req_text = refine_requirement_text_quality(
        requirement_text=current_req_text,
        source_snippet=evidence_snippet or source_snippet,
        req_type=current_req_type,
        chunk_text=chunk_text,
    )
    evidence_snippet = extract_best_citation_snippet(
        requirement_text=current_req_text,
        source_snippet=evidence_snippet or source_snippet,
        chunk_text=snippet_context_text,
    )
    postcall_decision = str(runtime_postcall.get("decision") or "KEEP").strip().upper()
    postcall_reasons = [
        str(reason)
        for reason in (runtime_postcall.get("reasons") or [])
        if str(reason or "").strip()
    ]
    grounding_score = round(float(runtime_postcall.get("grounding_score") or 0.0), 4)
    quality_score = round(float(runtime_postcall.get("quality_score") or 0.0), 4)
    status = (
        str(runtime_postcall.get("adjusted_status") or initial_status).strip().upper()
        or initial_status
    )
    return {
        "requirement_text": current_req_text,
        "req_type": current_req_type,
        "evidence_snippet": evidence_snippet,
        "confidence": confidence,
        "initial_status": initial_status,
        "runtime_postcall": runtime_postcall,
        "postcall_decision": postcall_decision,
        "postcall_reasons": postcall_reasons,
        "grounding_score": grounding_score,
        "quality_score": quality_score,
        "status": status,
    }


def _rewrite_runtime_requirement_from_structure(
    *,
    requirement_text: str,
    req_type: str,
    source_snippet: str,
    legal_subject: str = "",
    normative_verb: str = "",
    action_object: str = "",
    condition_text: str = "",
    exception_text: str = "",
    source_mode: str = "NON_PRECISE",
) -> dict[str, Any]:
    raw_text = decode_html_entities(normalize_spaces(requirement_text or ""))
    subject = decode_html_entities(normalize_spaces(legal_subject or ""))
    verb = decode_html_entities(normalize_spaces(normative_verb or ""))
    action = decode_html_entities(normalize_spaces(action_object or ""))
    condition = decode_html_entities(normalize_spaces(condition_text or ""))
    exception = decode_html_entities(normalize_spaces(exception_text or ""))
    mode = str(source_mode or "NON_PRECISE").strip().upper() or "NON_PRECISE"
    req_type_up = str(req_type or "AUTRE").strip().upper() or "AUTRE"

    if not raw_text:
        return {"applied": False, "requirement_text": "", "reason": "EMPTY"}
    if not subject or not verb or not action:
        return {"applied": False, "requirement_text": raw_text, "reason": "STRUCTURE_INCOMPLETE"}

    action_clean = action
    action_lower = action_clean.lower()
    verb_lower = verb.lower()
    subject_lower = subject.lower()
    if action_lower.startswith(subject_lower + " "):
        action_clean = action_clean[len(subject) :].strip(" ,;:-")
        action_lower = action_clean.lower()
    if action_lower.startswith(verb_lower + " "):
        action_clean = action_clean[len(verb) :].strip(" ,;:-")

    candidate = normalize_spaces(f"{subject} {verb} {action_clean}".strip())
    if condition:
        if condition.lower().startswith(("si ", "lorsque ", "en cas de ", "sous reserve de ", "a condition de ")):
            candidate = normalize_spaces(f"{condition}, {candidate}")
        elif condition.lower() not in candidate.lower():
            candidate = normalize_spaces(f"{candidate}, {condition}")
    if exception and exception.lower() not in candidate.lower():
        if exception.lower().startswith(("sauf ", "a l'exception", "à l'exception")):
            candidate = normalize_spaces(f"{candidate}, {exception}")
        else:
            candidate = normalize_spaces(f"{candidate}, sauf {exception}")

    candidate = normalize_requirement_text_by_type(
        requirement_text=candidate,
        source_snippet=source_snippet,
        req_type=req_type_up,
        chunk_text=source_snippet,
    )
    candidate = refine_requirement_text_quality(
        requirement_text=candidate,
        source_snippet=source_snippet,
        req_type=req_type_up,
        chunk_text=source_snippet,
    )
    candidate = decode_html_entities(normalize_spaces(candidate))

    raw_words = len(raw_text.split())
    candidate_words = len(candidate.split())
    raw_lower = raw_text.lower()
    rewrite_worthy = (
        mode in {"REFORMULE_LEGERE", "RECONSTRUCTION_CONTROLEE"}
        or raw_words > 45
        or any(
            marker in raw_lower
            for marker in (
                "conformement aux dispositions",
                "selon les modalites prevues",
                "selon les modalités prévues",
                "tel que defini",
                "tel que défini",
                "ci-apres",
                "ci-après",
            )
        )
    )
    if not rewrite_worthy:
        return {"applied": False, "requirement_text": raw_text, "reason": "RAW_ALREADY_ACCEPTABLE"}
    if candidate_words < 6 or candidate_words > 55:
        return {"applied": False, "requirement_text": raw_text, "reason": "CANDIDATE_OUT_OF_RANGE"}

    overlap = max(
        _promotion_overlap_ratio(raw_text, candidate),
        _token_overlap_ratio(normalize_requirement_key(raw_text), normalize_requirement_key(candidate)),
        _promotion_overlap_ratio(candidate, source_snippet),
    )
    if overlap < 0.45:
        return {"applied": False, "requirement_text": raw_text, "reason": "LOW_OVERLAP"}
    if len(candidate) > len(raw_text) + 12:
        return {"applied": False, "requirement_text": raw_text, "reason": "NOT_SHORTER"}

    return {
        "applied": candidate != raw_text,
        "requirement_text": candidate if candidate else raw_text,
        "reason": "STRUCTURED_REWRITE",
    }


# =========================
# Main pipeline
# =========================
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc_id", required=True, help="UUID du document")
    parser.add_argument("--limit", type=int, default=None, help="Limiter le nombre de chunks")
    parser.add_argument("--sleep_ms", type=int, default=0, help="Pause entre appels LLM")
    parser.add_argument("--article_label", default=None, help="Tester un seul article_label")
    parser.add_argument("--article_id", default=None, help="Tester un seul article_id")
    parser.add_argument("--tenant", default="", help="tenant_id pour activer le contexte RLS")
    parser.add_argument(
        "--force_full_rebuild",
        action="store_true",
        help="Supprimer toutes les exigences du document avant extraction, meme avec --limit.",
    )
    parser.add_argument(
        "--allow_ambiguous_article_label",
        action="store_true",
        help="Autorise explicitement un article_label qui correspond a plusieurs articles",
    )
    parser.add_argument(
        "--long_article_safety_mode",
        choices=["on", "off"],
        default=os.getenv("A1_LONG_ARTICLE_SAFETY_MODE", "on"),
        help="Mode de securite recall sur articles longs/conditionnels.",
    )
    parser.add_argument(
        "--long_article_safety_char_threshold",
        type=int,
        default=int(os.getenv("A1_LONG_ARTICLE_SAFETY_CHAR_THRESHOLD", "1800")),
        help="Seuil de caracteres pour activer le mode long article safety.",
    )
    parser.add_argument(
        "--long_article_safety_units_threshold",
        type=int,
        default=int(os.getenv("A1_LONG_ARTICLE_SAFETY_UNITS_THRESHOLD", "4")),
        help="Seuil d'unites juridiques pour activer le mode long article safety.",
    )
    parser.add_argument(
        "--precall_runtime_mode",
        choices=["off", "shadow", "conservative"],
        default=str(os.getenv("A1_PRECALL_RUNTIME_MODE", "conservative")).strip().lower() or "conservative",
        help="Branchement du precall NLP en runtime reel (conservateur par defaut).",
    )
    args = parser.parse_args()
    long_article_safety_enabled = args.long_article_safety_mode == "on"
    runtime_precall_mode = str(args.precall_runtime_mode or _read_runtime_precall_mode()).strip().upper()
    doc_level_dedup_enabled = os.getenv("A1_DEDUP_DOC_LEVEL_EXACT", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    doc_level_dedup_min_req_chars = max(
        0,
        int(os.getenv("A1_DEDUP_DOC_LEVEL_MIN_REQ_CHARS", "35")),
    )
    doc_level_text_dedup_enabled = os.getenv("A1_DEDUP_DOC_LEVEL_TEXT", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    doc_gate_enabled = os.getenv("A1_DOC_QUALIFICATION_GATE", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    article_prefix_dedup_enabled = os.getenv("A1_DEDUP_ARTICLE_PREFIX", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    article_prefix_dedup_min_chars = max(
        40,
        int(os.getenv("A1_DEDUP_ARTICLE_PREFIX_MIN_CHARS", "85")),
    )
    doc_level_prefix_dedup_enabled = os.getenv("A1_DEDUP_DOC_LEVEL_PREFIX", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    doc_level_prefix_dedup_min_chars = max(
        40,
        int(os.getenv("A1_DEDUP_DOC_LEVEL_PREFIX_MIN_CHARS", "90")),
    )
    promotion_enabled = os.getenv("A1_PROMOTION_TO_DRAFT_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    promotion_min_confidence = float(
        os.getenv(
            "A1_PROMOTION_TO_DRAFT_MIN_CONFIDENCE",
            os.getenv("A1_PROMOTION_LIMIT_CONF", "0.89"),
        )
    )
    promotion_min_overlap = float(os.getenv("A1_PROMOTION_TO_DRAFT_MIN_OVERLAP", "0.16"))
    promotion_min_snippet_chars = max(
        40,
        int(os.getenv("A1_PROMOTION_TO_DRAFT_MIN_SNIPPET_CHARS", "60")),
    )
    promotion_min_quality_score = float(os.getenv("A1_PROMOTION_TO_DRAFT_MIN_QUALITY", "0.84"))
    promotion_min_grounding_score = float(os.getenv("A1_PROMOTION_TO_DRAFT_MIN_GROUNDING", "0.72"))
    promotion_min_completeness_score = float(os.getenv("A1_PROMOTION_TO_DRAFT_MIN_COMPLETENESS", "0.85"))
    promotion_min_subject_consistency = float(
        os.getenv("A1_PROMOTION_TO_DRAFT_MIN_SUBJECT_CONSISTENCY", "0.60")
    )
    promotion_max_req_chars = max(
        120,
        int(os.getenv("A1_PROMOTION_TO_DRAFT_MAX_REQ_CHARS", "320")),
    )
    promotion_max_snippet_chars = max(
        promotion_min_snippet_chars,
        int(os.getenv("A1_PROMOTION_TO_DRAFT_MAX_SNIPPET_CHARS", "650")),
    )
    promotion_block_modal_peut = os.getenv("A1_PROMOTION_TO_DRAFT_BLOCK_MODAL_PEUT", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    promotion_require_clean_start = os.getenv(
        "A1_PROMOTION_TO_DRAFT_REQUIRE_CLEAN_START", "1"
    ).strip().lower() not in {
        "0",
        "false",
        "off",
    }
    promotion_require_postcall_draft = os.getenv(
        "A1_PROMOTION_TO_DRAFT_REQUIRE_POSTCALL_DRAFT", "1"
    ).strip().lower() not in {
        "0",
        "false",
        "off",
    }
    promotion_block_fused_action_chain = os.getenv(
        "A1_PROMOTION_TO_DRAFT_BLOCK_FUSED_ACTION_CHAIN", "1"
    ).strip().lower() not in {
        "0",
        "false",
        "off",
    }
    error_memory_enabled = os.getenv("A1_ERROR_MEMORY_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    error_memory_load_limit = max(0, int(os.getenv("A1_ERROR_MEMORY_LOAD_LIMIT", "400")))
    error_memory_signature_min_hits = max(1, int(os.getenv("A1_ERROR_MEMORY_SIGNATURE_MIN_HITS", "2")))
    runtime_unit_cache_enabled = os.getenv("A1_RUNTIME_UNIT_CACHE_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    runtime_unit_dedup_enabled = os.getenv("A1_RUNTIME_UNIT_DEDUP_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    runtime_budget_fallback_enabled = os.getenv("A1_RUNTIME_BUDGET_FALLBACK_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    runtime_budget_max_llm_calls = max(0, int(os.getenv("A1_RUNTIME_MAX_LLM_CALLS_PER_DOC", "0")))
    runtime_budget_max_total_tokens = max(0, int(os.getenv("A1_RUNTIME_MAX_TOTAL_TOKENS_PER_DOC", "0")))
    runtime_budget_max_estimated_cost_usd = max(
        0.0,
        float(os.getenv("A1_RUNTIME_MAX_ESTIMATED_COST_USD", "0")),
    )

    if args.article_label and args.article_id:
        raise RuntimeError("Utiliser soit --article_label soit --article_id, pas les deux.")

    dsn = os.getenv("PG_DSN")
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")

    seen_article_exact: set[tuple[str, str]] = set()
    seen_doc_exact: set[tuple[str, str, str]] = set()
    seen_article_prefix: dict[str, list[tuple[str, str]]] = {}
    seen_doc_relaxed_exact: set[tuple[str, str]] = set()
    seen_doc_prefix: dict[str, list[str]] = {}

    # Articles contenant des renvois inter-articles → leurs exigences restent en TO_VALIDATE
    _CROSS_REF_DETECT_RE = re.compile(
        r"(?i)\b(article\s+\d+|conform[eé]ment\s+aux\s+dispositions\s+de|"
        r"sous\s+r[eé]serve\s+de\s+l['’]?article|en\s+application\s+de\s+l['’]?article|"
        r"pr[eé]vu\s+[àa]\s+l['’]?article|vis[eé]\s+[àa]\s+l['’]?article)\b"
    )
    articles_with_cross_ref: set[str] = set()

    with connect_db(dsn, tenant_id=str(args.tenant or "").strip() or None) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tenant_id, title FROM documents WHERE doc_id=%s", (args.doc_id,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError("doc_id introuvable dans documents")

            tenant_id, doc_title = row
            error_memory_store = create_error_memory_store(
                signature_min_hits=error_memory_signature_min_hits
            )
            error_memory_storage_source = "disabled"
            error_memory_table_ready = False
            persisted_error_memory_signals: list[dict[str, Any]] = []
            if error_memory_enabled:
                try:
                    if error_memory_table_exists(cur):
                        error_memory_table_ready = True
                    else:
                        ensure_error_memory_table(cur)
                        error_memory_table_ready = True
                    persisted_error_memory_signals = load_persisted_error_memory_signals(
                        cur,
                        str(tenant_id),
                        limit=error_memory_load_limit,
                    )
                    error_memory_storage_source = "table"
                except Exception:
                    persisted_error_memory_signals = []
                    error_memory_table_ready = False
                if not persisted_error_memory_signals:
                    persisted_error_memory_signals = _load_error_memory_signals(
                        cur,
                        str(tenant_id),
                        limit=error_memory_load_limit,
                    )
                    if persisted_error_memory_signals:
                        error_memory_storage_source = "events_fallback"
                    elif error_memory_storage_source != "table":
                        error_memory_storage_source = "empty"
            for signal in persisted_error_memory_signals:
                register_error_memory_signal(error_memory_store, signal, persisted=True)

            sql = """
                SELECT
                    c.chunk_id,
                    c.chunk_no,
                    c.chunk_text,
                    a.article_id,
                    a.article_label,
                    a.article_code,
                    a.start_page,
                    a.end_page,
                    a.start_char,
                    a.livre,
                    a.titre,
                    a.chapitre,
                    a.section
                FROM chunks c
                JOIN articles a ON c.article_id = a.article_id
                WHERE c.doc_id=%s
            """
            params: list = [args.doc_id]

            if args.article_id:
                sql += " AND a.article_id=%s"
                params.append(args.article_id)

            if args.article_label:
                sql += " AND a.article_label=%s"
                params.append(args.article_label)

            sql += " ORDER BY a.start_page NULLS FIRST, a.start_char NULLS FIRST, c.chunk_no"

            if args.limit is not None:
                sql += " LIMIT %s"
                params.append(args.limit)

            cur.execute(sql, params)
            rows = cur.fetchall()

            if args.article_id and not rows:
                raise RuntimeError(f"Aucun chunk trouve pour article_id={args.article_id}")

            if args.article_label and not args.allow_ambiguous_article_label:
                matching_article_ids = sorted({str(r[3]) for r in rows})
                if len(matching_article_ids) > 1:
                    raise RuntimeError(
                        f"article_label ambigu ({args.article_label}): {len(matching_article_ids)} articles matches. "
                        "Utiliser --article_id pour cibler explicitement, ou --allow_ambiguous_article_label."
                    )

            article_ids_in_scope = sorted({str(r[3]) for r in rows})
            full_doc_rebuild = bool(args.force_full_rebuild) or (
                args.limit is None and args.article_label is None and args.article_id is None
            )

            article_text_by_id: dict[str, tuple[str, str]] = {}
            article_chunk_rows_by_id: dict[str, list[tuple[int, str]]] = {}
            article_runtime_core_by_id: dict[str, str] = {}
            article_runtime_source_text_by_id: dict[str, str] = {}
            article_runtime_allowed_chunk_nos_by_id: dict[str, set[int]] = {}
            article_runtime_core_seed_by_id: dict[str, bool] = {}
            article_runtime_source_seed_by_id: dict[str, bool] = {}
            for row in rows:
                article_chunk_rows_by_id.setdefault(str(row[3]), []).append((int(row[1]), str(row[2] or "")))
            if article_ids_in_scope:
                cur.execute(
                    """
                    SELECT article_id::text, COALESCE(article_label, ''), COALESCE(article_text, '')
                    FROM articles
                    WHERE doc_id=%s
                        AND article_id = ANY(%s::uuid[])
                    ORDER BY start_page NULLS FIRST, start_char NULLS FIRST
                    """,
                    (args.doc_id, article_ids_in_scope),
                )
                for aid, alabel, atext in cur.fetchall():
                    article_id_text = str(aid)
                    article_text_value = str(atext or "")
                    article_text_by_id[article_id_text] = (str(alabel or ""), article_text_value)
                    selected_source_text, selected_chunk_nos, selected_source_seed = _select_runtime_article_source_window(
                        article_label=str(alabel or ""),
                        article_text=article_text_value,
                        ordered_chunks=article_chunk_rows_by_id.get(article_id_text, []),
                    )
                    article_runtime_source_text_by_id[article_id_text] = selected_source_text or article_text_value
                    article_runtime_allowed_chunk_nos_by_id[article_id_text] = set(selected_chunk_nos or set())
                    article_runtime_source_seed_by_id[article_id_text] = bool(selected_source_seed)
                    article_core_text = _build_runtime_article_core(
                        selected_source_text or article_text_value,
                        article_label=str(alabel or ""),
                    )
                    article_runtime_core_by_id[article_id_text] = article_core_text
                    article_runtime_core_seed_by_id[article_id_text] = _should_seed_runtime_article_core(
                        selected_source_text or article_text_value,
                        article_core_text,
                    ) or bool(selected_source_seed)

            doc_preview_parts: list[str] = []
            for aid in article_ids_in_scope[:6]:
                alabel, atext = article_text_by_id.get(aid, ("", ""))
                sample = sanitize_ocr_noise_for_extraction(atext)[:600]
                if sample:
                    doc_preview_parts.append(f"{alabel} {sample}")
            doc_text_preview = " ".join(doc_preview_parts)
            if not doc_text_preview:
                row_preview = " ".join(
                    sanitize_ocr_noise_for_extraction(str(r[2] or ""))[:300]
                    for r in rows[:6]
                )
                doc_text_preview = row_preview

            if doc_gate_enabled:
                doc_gate = classify_document_policy(
                    doc_title=doc_title,
                    doc_text_preview=doc_text_preview,
                    article_count=len(article_ids_in_scope),
                )
            else:
                doc_gate = QualificationDecision(
                    policy=POLICY_EXTRACT_FULL,
                    document_type="DISABLED",
                    confidence=1.0,
                    reason_codes=("DOC_GATE_DISABLED",),
                )

            skip_all_by_doc_policy = doc_gate.policy in {POLICY_DROP, POLICY_TO_VALIDATE_SOURCE_MISSING}
            article_policy_cache: dict[str, QualificationDecision] = {}
            article_policy_counts: dict[str, int] = {}
            article_policy_reason_counts: dict[str, int] = {}
            article_drop_reason_counts: dict[str, int] = {}
            article_skip_ids: set[str] = set()
            article_policy_limited_ids: set[str] = set()
            article_core_seeded_ids: set[str] = set()

            downstream_refs_in_scope = _count_downstream_requirement_refs(
                cur,
                doc_id=str(args.doc_id),
                article_ids=None if full_doc_rebuild else article_ids_in_scope,
            )
            if downstream_refs_in_scope > 0:
                scope_label = "document complet" if full_doc_rebuild else "articles selectionnes"
                raise RuntimeError(
                    "Relance A1 bloquee: des exigences de ce "
                    f"{scope_label} sont deja referencees par A3 "
                    f"(compliance_checks={downstream_refs_in_scope}). "
                    "Utiliser un document clone / un tenant de test, ou purger les analyses aval avant rebuild."
                )

            if full_doc_rebuild:
                cur.execute("DELETE FROM requirements WHERE doc_id=%s", (args.doc_id,))
            elif article_ids_in_scope:
                cur.execute(
                    """
                    DELETE FROM requirements
                    WHERE doc_id=%s
                        AND article_id = ANY(%s::uuid[])
                    """,
                    (args.doc_id, article_ids_in_scope),
                )
                if args.article_label:
                    cur.execute(
                        """
                        DELETE FROM requirements
                        WHERE doc_id=%s
                            AND article_id IS NULL
                            AND citation_ref ILIKE %s
                        """,
                        (args.doc_id, f"{args.article_label}%"),
                    )

            cur.execute(
                "SELECT COALESCE(MAX(requirement_no), 0) FROM requirements WHERE doc_id=%s",
                (args.doc_id,),
            )
            requirement_no = cur.fetchone()[0]
            inserted_requirements_count = 0
            chunk_ok = 0
            chunk_empty = 0
            chunk_error = 0

            raw_llm_requirements = 0
            rejected_low_value = 0
            rejected_fragmentary = 0
            rejected_mismatch = 0
            rejected_scope = 0
            rejected_partial_exception = 0
            rejected_non_normative = 0
            rejected_out_of_scope = 0
            deduplicated = 0
            deduplicated_exact_article = 0
            deduplicated_exact_doc = 0
            deduplicated_prefix_article = 0
            deduplicated_prefix_doc = 0
            long_article_safety_chunks_total = 0
            long_article_safety_forced_units_total = 0
            long_article_safety_full_chunk_fallback_total = 0
            softened_filter_rejections_total = 0
            empty_llm_fallback_generated_total = 0
            article_dropped_by_policy_total = 0
            precall_runtime_chunks_total = 0
            precall_runtime_fallback_chunks_total = 0
            precall_runtime_units_high_total = 0
            precall_runtime_units_low_total = 0
            precall_runtime_units_drop_total = 0
            precall_runtime_drop_shadow_total = 0
            precall_runtime_drop_hard_total = 0
            units_dropped_limited_policy_total = 0
            limited_policy_forced_units_total = 0
            limited_data_articles_seen: set[str] = set()
            limited_data_objects_total = 0
            limited_data_unattached_total = 0
            limited_data_object_type_counts: dict[str, int] = {}
            limited_data_samples: list[dict] = []
            policy_forced_to_validate_total = 0
            promotion_reviewed_to_validate_total = 0
            promoted_to_draft_total = 0
            promotion_blocked_policy_total = 0
            promotion_blocked_type_total = 0
            promotion_blocked_low_conf_total = 0
            promotion_blocked_short_snippet_total = 0
            promotion_blocked_low_overlap_total = 0
            promotion_blocked_long_req_total = 0
            promotion_blocked_long_snippet_total = 0
            promotion_blocked_modal_peut_total = 0
            promotion_blocked_noisy_start_total = 0
            promotion_blocked_quality_total = 0
            promotion_blocked_grounding_total = 0
            promotion_blocked_completeness_total = 0
            promotion_blocked_subject_total = 0
            promotion_blocked_postcall_total = 0
            promotion_blocked_fused_total = 0
            promotion_blocked_missing_component_total = 0
            promotion_blocked_postcall_type_total = 0
            promotion_blocked_error_memory_total = 0
            promotion_overlap_sum = 0.0
            error_memory_loaded_total = len(persisted_error_memory_signals)
            error_memory_signals_total = 0
            error_memory_hits_total = 0
            error_memory_fix_applied_total = 0
            error_memory_persisted_total = 0
            error_memory_persist_failures_total = 0
            error_memory_family_counts: dict[str, int] = {}
            error_memory_fix_rule_counts: dict[str, int] = {}
            runtime_unit_response_cache: dict[str, dict[str, Any]] = {}
            runtime_unit_deduped_total = 0
            runtime_unit_cache_hits_total = 0
            runtime_unit_cache_store_total = 0
            runtime_budget_blocked_units_total = 0
            runtime_budget_fallback_units_total = 0
            article_core_filtered_units_total = 0
            qse_domain_counts: dict[str, int] = {}
            qse_sub_domain_counts: dict[str, int] = {}
            qse_mapping_strategy_counts: dict[str, int] = {}

            def record_error_memory_signal(signal: dict[str, Any] | None) -> None:
                nonlocal error_memory_signals_total, error_memory_persisted_total, error_memory_persist_failures_total
                if not error_memory_enabled or not signal:
                    return
                register_error_memory_signal(error_memory_store, signal, persisted=False)
                error_memory_signals_total += 1
                family = str(signal.get("error_family") or "UNKNOWN").strip().upper() or "UNKNOWN"
                error_memory_family_counts[family] = int(error_memory_family_counts.get(family) or 0) + 1
                if error_memory_table_ready:
                    try:
                        if persist_error_memory_signal(
                            cur,
                            tenant_id=str(tenant_id),
                            doc_id=str(args.doc_id or ""),
                            signal=signal,
                            source_event_type="A1_ERROR_MEMORY_SIGNAL",
                        ):
                            error_memory_persisted_total += 1
                    except Exception:
                        error_memory_persist_failures_total += 1
                safe_insert_event(cur, tenant_id, args.doc_id, "A1_ERROR_MEMORY_SIGNAL", signal)

            def record_error_memory_hit(
                *,
                memory_hit: dict[str, Any],
                requirement_text: str,
                req_type: str,
                article_label_value: str,
            ) -> None:
                nonlocal error_memory_hits_total
                if not error_memory_enabled or not memory_hit:
                    return
                error_memory_hits_total += 1
                family = str(memory_hit.get("error_family") or "UNKNOWN").strip().upper() or "UNKNOWN"
                error_memory_family_counts[family] = int(error_memory_family_counts.get(family) or 0) + 1
                safe_insert_event(cur, tenant_id, args.doc_id, "A1_ERROR_MEMORY_HIT", {
                    "error_family": family,
                    "memory_action": str(memory_hit.get("memory_action") or "FORCE_VALIDATE"),
                    "match_kind": str(memory_hit.get("match_kind") or ""),
                    "fix_rule": str(memory_hit.get("fix_rule") or ""),
                    "count": int(memory_hit.get("count") or 0),
                    "persisted_count": int(memory_hit.get("persisted_count") or 0),
                    "runtime_count": int(memory_hit.get("runtime_count") or 0),
                    "req_type": req_type,
                    "article_label": article_label_value,
                    "trigger_pattern": str(memory_hit.get("trigger_pattern") or ""),
                    "signature_pattern": str(memory_hit.get("signature_pattern") or ""),
                    "text_preview": str(requirement_text or "")[:160],
                })

            def record_error_memory_fix(
                *,
                memory_hit: dict[str, Any] | None,
                fix_rule: str,
                article_label_value: str,
                req_type_before: str,
                req_type_after: str,
                text_before: str,
                text_after: str,
                split_count: int = 0,
            ) -> None:
                nonlocal error_memory_fix_applied_total
                if not error_memory_enabled or not fix_rule:
                    return
                error_memory_fix_applied_total += 1
                fix_rule_up = str(fix_rule or "").strip().upper()
                error_memory_fix_rule_counts[fix_rule_up] = (
                    int(error_memory_fix_rule_counts.get(fix_rule_up) or 0) + 1
                )
                safe_insert_event(cur, tenant_id, args.doc_id, "A1_ERROR_MEMORY_FIX_APPLIED", {
                    "error_family": str((memory_hit or {}).get("error_family") or ""),
                    "memory_action": str((memory_hit or {}).get("memory_action") or ""),
                    "match_kind": str((memory_hit or {}).get("match_kind") or ""),
                    "fix_rule": fix_rule_up,
                    "article_label": article_label_value,
                    "req_type_before": req_type_before,
                    "req_type_after": req_type_after,
                    "text_before": str(text_before or "")[:240],
                    "text_after": str(text_after or "")[:240],
                    "split_count": int(split_count or 0),
                })

            extractor = LLMExtractor()
            # Phase 6 — boucle d'apprentissage : enrichir le prompt avec les
            # exemples validés par les experts (validations APPROVE en base).
            extractor.load_fewshot_examples(
                dsn=dsn,
                limit=8,
                min_confidence=0.82,
                tenant_id=str(tenant_id),
            )
            run_used_fallback = False
            providers_used: set[str] = set()
            models_used: set[str] = set()
            doc_policy_skip_logged = False

            for (
                chunk_id,
                chunk_no,
                chunk_text,
                article_id,
                article_label,
                article_code,
                start_page,
                end_page,
                start_char,
                livre,
                titre,
                chapitre,
                section,
            ) in rows:
                try:
                    if skip_all_by_doc_policy:
                        if not doc_policy_skip_logged:
                            print(
                                f"DOC_GATE skip extraction ({doc_gate.policy}) | reasons={list(doc_gate.reason_codes)}",
                                flush=True,
                            )
                            doc_policy_skip_logged = True
                        chunk_empty += 1
                        continue

                    cleaned_chunk_text = sanitize_ocr_noise_for_extraction(chunk_text or "")
                    if not cleaned_chunk_text:
                        chunk_empty += 1
                        continue

                    article_key = str(article_id)
                    cached_label, cached_text = article_text_by_id.get(article_key, ("", ""))
                    allowed_chunk_nos = article_runtime_allowed_chunk_nos_by_id.get(article_key) or set()
                    if allowed_chunk_nos and int(chunk_no) not in allowed_chunk_nos:
                        chunk_empty += 1
                        continue
                    article_source_text = (
                        article_runtime_source_text_by_id.get(article_key, "")
                        or cached_text
                        or cleaned_chunk_text
                    )
                    article_core_text = article_runtime_core_by_id.get(article_key, "") or article_source_text or cleaned_chunk_text
                    article_core_seed_mode = bool(article_runtime_core_seed_by_id.get(article_key))
                    article_source_seed_mode = bool(article_runtime_source_seed_by_id.get(article_key))
                    if (article_source_seed_mode or article_core_seed_mode) and article_core_text:
                        if article_key not in article_core_seeded_ids:
                            cleaned_chunk_text = article_source_text or article_core_text
                            article_core_seeded_ids.add(article_key)
                        else:
                            chunk_empty += 1
                            continue

                    # Détection renvoi inter-article (une fois par article)
                    if article_key not in articles_with_cross_ref and article_key not in seen_article_exact:
                        full_article_text = article_source_text or cached_text or cleaned_chunk_text
                        if _CROSS_REF_DETECT_RE.search(_cross_reference_search_text(full_article_text)):
                            articles_with_cross_ref.add(article_key)

                    if article_key not in article_policy_cache:
                        if doc_gate_enabled:
                            article_decision = classify_article_policy(
                                doc_title=doc_title,
                                article_label=article_label or cached_label,
                                article_text=cached_text or cleaned_chunk_text,
                                document_policy=doc_gate.policy,
                            )
                        else:
                            article_decision = QualificationDecision(
                                policy=POLICY_EXTRACT_FULL,
                                document_type="DISABLED",
                                confidence=1.0,
                                reason_codes=("ARTICLE_GATE_DISABLED",),
                            )
                        article_policy_cache[article_key] = article_decision
                        article_policy_counts[article_decision.policy] = int(
                            article_policy_counts.get(article_decision.policy) or 0
                        ) + 1
                        for reason_code in article_decision.reason_codes:
                            rc = str(reason_code or "").strip().upper()
                            if not rc:
                                continue
                            article_policy_reason_counts[rc] = int(article_policy_reason_counts.get(rc) or 0) + 1
                    article_gate = article_policy_cache[article_key]
                    article_policy = article_gate.policy

                    if should_skip_standard_extraction(article_policy):
                        for reason_code in article_gate.reason_codes:
                            rc = str(reason_code or "").strip().upper()
                            if not rc:
                                continue
                            article_drop_reason_counts[rc] = int(article_drop_reason_counts.get(rc) or 0) + 1

                        if (
                            article_policy == POLICY_EXTRACT_LIMITED_DATA
                            and article_key not in limited_data_articles_seen
                        ):
                            limited_data_articles_seen.add(article_key)
                            data_objects = parse_limited_data_objects(
                                article_label=(article_label or cached_label or article_code or ""),
                                article_text=(cached_text or cleaned_chunk_text or ""),
                                parent_legal_rule_ref=(article_label or cached_label or article_code or ""),
                                provenance={
                                    "doc_id": str(args.doc_id),
                                    "article_id": article_key,
                                    "article_label": str(article_label or cached_label or ""),
                                },
                            )
                            for data_obj in data_objects:
                                obj_type = str(data_obj.get("data_object_type") or "unknown").strip().lower() or "unknown"
                                limited_data_object_type_counts[obj_type] = int(
                                    limited_data_object_type_counts.get(obj_type) or 0
                                ) + 1
                                if str(data_obj.get("parent_legal_rule_ref") or "").strip().upper() == "UNATTACHED":
                                    limited_data_unattached_total += 1
                                limited_data_objects_total += 1
                                if len(limited_data_samples) < 8:
                                    limited_data_samples.append(
                                        {
                                            "data_object_type": obj_type,
                                            "parent_legal_rule_ref": data_obj.get("parent_legal_rule_ref"),
                                            "rows_count": len(data_obj.get("rows") or []),
                                            "units": list(data_obj.get("units") or []),
                                            "provenance": data_obj.get("provenance") or {},
                                        }
                                    )

                        if article_key not in article_skip_ids:
                            article_skip_ids.add(article_key)
                            article_dropped_by_policy_total += 1
                            print(
                                f"ARTICLE_GATE skipped {article_label} ({article_policy}) | reasons={list(article_gate.reason_codes)}",
                                flush=True,
                            )
                        chunk_empty += 1
                        continue
                    if article_policy == "EXTRACT_LIMITED":
                        article_policy_limited_ids.add(article_key)

                    legal_units_meta: list[dict[str, Any]] = []
                    if runtime_precall_mode != "OFF":
                        precall_chunk_text, precall_units_meta, precall_stats = _build_runtime_precall_units(
                            cleaned_chunk_text,
                            article_label=article_label,
                        )
                        if precall_chunk_text:
                            cleaned_chunk_text = precall_chunk_text
                        if precall_units_meta:
                            legal_units_meta = precall_units_meta
                            precall_runtime_chunks_total += 1
                            precall_runtime_units_high_total += int(precall_stats.get("HIGH") or 0)
                            precall_runtime_units_low_total += int(precall_stats.get("LOW") or 0)
                            precall_runtime_units_drop_total += int(precall_stats.get("DROP") or 0)

                    if not legal_units_meta:
                        legal_units = split_into_legal_units(
                            cleaned_chunk_text,
                            article_label=article_label,
                            article_code=article_code,
                            long_article_safety=long_article_safety_enabled,
                        )
                        legal_units_meta = [
                            {
                                "text": unit_text,
                                "source": "legacy_split",
                                "priority": "UNKNOWN",
                                "normative_score": 0.0,
                                "strong_normative": False,
                                "rule_hits": [],
                            }
                            for unit_text in legal_units
                            if str(unit_text or "").strip()
                        ]
                        if runtime_precall_mode != "OFF":
                            precall_runtime_fallback_chunks_total += 1

                    if not legal_units_meta:
                        chunk_empty += 1
                        continue

                    long_article_safety_active = long_article_safety_enabled and should_enable_long_article_safety(
                        cleaned_chunk_text,
                        units_count=len(legal_units_meta),
                        char_threshold=args.long_article_safety_char_threshold,
                        units_threshold=args.long_article_safety_units_threshold,
                    )
                    if long_article_safety_active:
                        long_article_safety_chunks_total += 1

                    if is_definition_like_article(cleaned_chunk_text):
                        if not (long_article_safety_active and has_legal_risk_markers(cleaned_chunk_text)):
                            print(f"SKIPPED definition-like chunk {chunk_no} | {article_label}", flush=True)
                            chunk_empty += 1
                            continue
                        print(
                            f"LONG_SAFETY override definition-like chunk {chunk_no} | {article_label}",
                            flush=True,
                        )

                    print(f"PASSED chunk {chunk_no} | {article_label}", flush=True)

                    units_to_process: list[dict[str, Any]] = []
                    for unit_meta in legal_units_meta:
                        unit_text = str((unit_meta or {}).get("text") or "").strip()
                        if not unit_text:
                            continue
                        if article_core_text and not _source_belongs_to_article_core(unit_text, article_core_text):
                            article_core_filtered_units_total += 1
                            continue
                        limited_actionable = limited_unit_is_actionable(unit_text)
                        if article_policy == "EXTRACT_LIMITED" and not limited_actionable:
                            units_dropped_limited_policy_total += 1
                            continue

                        precall_priority = str((unit_meta or {}).get("priority") or "UNKNOWN").strip().upper() or "UNKNOWN"
                        unit_has_risk = has_legal_risk_markers(unit_text)
                        unit_out_of_scope = is_out_of_scope_individual_requirement(
                            requirement_text=unit_text,
                            citation_snippet=unit_text,
                        )
                        relaxed_by_safety = (long_article_safety_active and unit_has_risk) or (
                            unit_has_risk and not unit_out_of_scope
                        )

                        dropped_by_definition = is_definition_like_unit(unit_text)
                        if dropped_by_definition and not relaxed_by_safety:
                            print(
                                f"SKIPPED definition-like unit | {article_label}: {unit_text[:80]}",
                                flush=True,
                            )
                            continue
                        if dropped_by_definition and relaxed_by_safety:
                            long_article_safety_forced_units_total += 1

                        dropped_by_scope_extension = is_scope_extension_classification_unit(
                            unit_text,
                            cleaned_chunk_text,
                        )
                        if dropped_by_scope_extension and not relaxed_by_safety:
                            print(
                                f"SKIPPED scope/classification unit | {article_label}: {unit_text[:80]}",
                                flush=True,
                            )
                            continue
                        if dropped_by_scope_extension and relaxed_by_safety:
                            long_article_safety_forced_units_total += 1

                        precall_skip, precall_reason = _should_skip_unit_by_precall(
                            mode=runtime_precall_mode,
                            priority=precall_priority,
                            unit_has_risk=unit_has_risk,
                            relaxed_by_safety=relaxed_by_safety,
                        )
                        if precall_skip:
                            precall_runtime_drop_hard_total += 1
                            print(
                                f"PRECALL skipped low-signal unit | {article_label}: {unit_text[:80]}",
                                flush=True,
                            )
                            continue
                        if precall_reason.startswith("PRECALL_DROP_SHADOW"):
                            precall_runtime_drop_shadow_total += 1

                        units_to_process.append(
                            {
                                "text": unit_text,
                                "priority": precall_priority,
                                "source": str((unit_meta or {}).get("source") or "unknown"),
                            }
                        )

                    if (
                        article_policy == "EXTRACT_LIMITED"
                        and not units_to_process
                        and has_legal_risk_markers(cleaned_chunk_text)
                    ):
                        units_to_process.append({
                            "text": article_core_text or article_source_text or cleaned_chunk_text,
                            "priority": "FALLBACK_LIMITED",
                            "source": "article_core",
                        })
                        limited_policy_forced_units_total += 1

                    if (
                        article_policy == "EXTRACT_LIMITED"
                        and not units_to_process
                        and str(cleaned_chunk_text or "").strip()
                        and len(str(cleaned_chunk_text or "").strip()) >= 140
                    ):
                        # Degrade to TO_VALIDATE path instead of silent drop on ambiguous limited articles.
                        units_to_process.append({
                            "text": article_core_text or article_source_text or cleaned_chunk_text,
                            "priority": "FALLBACK_LIMITED",
                            "source": "article_core",
                        })
                        limited_policy_forced_units_total += 1

                    if (
                        not units_to_process
                        and long_article_safety_active
                        and has_legal_risk_markers(cleaned_chunk_text)
                    ):
                        units_to_process.append({
                            "text": article_core_text or article_source_text or cleaned_chunk_text,
                            "priority": "FALLBACK_LONG_ARTICLE",
                            "source": "article_core",
                        })
                        long_article_safety_full_chunk_fallback_total += 1
                        print(
                            f"LONG_SAFETY fallback full chunk {chunk_no} | {article_label}",
                            flush=True,
                        )

                    if runtime_unit_dedup_enabled and units_to_process:
                        units_to_process, deduped_units_count = _dedupe_runtime_units(
                            units_to_process,
                            scope_ref=(article_key or article_label or article_code or ""),
                        )
                        runtime_unit_deduped_total += deduped_units_count

                    if not units_to_process:
                        chunk_empty += 1
                        continue

                    unit_had_requirements = False

                    for unit_meta in units_to_process:
                        unit_text = str((unit_meta or {}).get("text") or "").strip()
                        if not unit_text:
                            continue
                        if article_core_text and not _source_belongs_to_article_core(unit_text, article_core_text):
                            article_core_filtered_units_total += 1
                            continue
                        llm_unit_text = _build_runtime_llm_unit_text(
                            unit_text=unit_text,
                            chunk_text=cleaned_chunk_text,
                            article_core_text=article_core_text,
                        ) or unit_text
                        source_snippet = decode_html_entities(clean_source_snippet(llm_unit_text))
                        snippet_context_text = _build_trusted_snippet_context(
                            source_snippet=source_snippet,
                            chunk_text=cleaned_chunk_text,
                            article_core_text=article_core_text,
                        )
                        runtime_unit_cache_key = _build_runtime_unit_cache_key(
                            scope_ref=(article_key or article_label or article_code or ""),
                            unit_text=llm_unit_text,
                        )
                        parsed = None
                        parsed_items: list[Any] = []
                        cached_runtime_response = (
                            runtime_unit_response_cache.get(runtime_unit_cache_key)
                            if runtime_unit_cache_enabled and runtime_unit_cache_key
                            else None
                        )
                        if cached_runtime_response:
                            cached_response = cached_runtime_response.get("response")
                            if hasattr(cached_response, "model_copy"):
                                parsed = cached_response.model_copy(deep=True)
                            else:
                                parsed = cached_response
                            runtime_unit_cache_hits_total += 1
                            cached_provider = str(cached_runtime_response.get("provider") or "").strip()
                            cached_model = str(cached_runtime_response.get("model") or "").strip()
                            if cached_provider:
                                providers_used.add(cached_provider)
                            if cached_model:
                                models_used.add(cached_model)
                            run_used_fallback = run_used_fallback or bool(
                                cached_runtime_response.get("fallback_used")
                            )
                        else:
                            budget_reason = _runtime_budget_guard_reason(
                                llm_calls=int(extractor.usage_totals.get("llm_calls") or 0),
                                total_tokens=int(extractor.usage_totals.get("total_tokens") or 0),
                                estimated_cost_usd=float(
                                    extractor.usage_totals.get("estimated_cost_usd") or 0.0
                                ),
                                max_llm_calls=runtime_budget_max_llm_calls,
                                max_total_tokens=runtime_budget_max_total_tokens,
                                max_estimated_cost_usd=runtime_budget_max_estimated_cost_usd,
                            )
                            if budget_reason:
                                runtime_budget_blocked_units_total += 1
                                safe_insert_event(cur, tenant_id, args.doc_id, "A1_RUNTIME_BUDGET_GUARD", {
                                    "article_label": article_label,
                                    "article_id": article_key,
                                    "reason": budget_reason,
                                    "unit_preview": unit_text[:180],
                                    "llm_calls": int(extractor.usage_totals.get("llm_calls") or 0),
                                    "total_tokens": int(extractor.usage_totals.get("total_tokens") or 0),
                                    "estimated_cost_usd": round(
                                        float(extractor.usage_totals.get("estimated_cost_usd") or 0.0),
                                        8,
                                    ),
                                })
                                if runtime_budget_fallback_enabled:
                                    parsed_items = build_empty_llm_fallback_requirements(source_snippet)
                                    structural_fallback_items = build_structural_framing_requirements(
                                        source_snippet,
                                        existing_requirements=parsed_items,
                                    )
                                    if structural_fallback_items:
                                        existing_keys: set[str] = set()
                                        for existing_item in parsed_items:
                                            existing_text = (
                                                existing_item.requirement_text
                                                if hasattr(existing_item, "requirement_text")
                                                else str((existing_item or {}).get("requirement_text") or "")
                                            )
                                            if existing_text:
                                                existing_keys.add(normalize_requirement_key(existing_text))
                                        for extra_item in structural_fallback_items:
                                            extra_text = str((extra_item or {}).get("requirement_text") or "")
                                            extra_key = normalize_requirement_key(extra_text)
                                            if not extra_text or extra_key in existing_keys:
                                                continue
                                            parsed_items.append(extra_item)
                                            existing_keys.add(extra_key)
                                    if parsed_items:
                                        runtime_budget_fallback_units_total += 1
                            else:
                                parsed = extractor.extract(
                                    article_label=article_label,
                                    chunk_text=llm_unit_text,
                                )

                                providers_used.add(extractor.last_provider_used or "unknown")
                                models_used.add(extractor.last_model_used or "unknown")
                                run_used_fallback = run_used_fallback or extractor.last_fallback_used

                                if runtime_unit_cache_enabled and runtime_unit_cache_key and parsed is not None:
                                    runtime_unit_response_cache[runtime_unit_cache_key] = {
                                        "response": (
                                            parsed.model_copy(deep=True)
                                            if hasattr(parsed, "model_copy")
                                            else parsed
                                        ),
                                        "provider": extractor.last_provider_used or "",
                                        "model": extractor.last_model_used or "",
                                        "fallback_used": bool(extractor.last_fallback_used),
                                    }
                                    runtime_unit_cache_store_total += 1

                        if parsed is not None:
                            parsed_items = list(parsed.requirements or [])
                        if not parsed_items:
                            fallback_items = build_empty_llm_fallback_requirements(source_snippet)
                            if fallback_items:
                                parsed_items = fallback_items
                                empty_llm_fallback_generated_total += len(fallback_items)
                        structural_fallback_items = build_structural_framing_requirements(
                            source_snippet,
                            existing_requirements=parsed_items,
                        )
                        if structural_fallback_items:
                            existing_keys: set[str] = set()
                            for existing_item in parsed_items:
                                existing_text = (
                                    existing_item.requirement_text
                                    if hasattr(existing_item, "requirement_text")
                                    else str((existing_item or {}).get("requirement_text") or "")
                                )
                                if existing_text:
                                    existing_keys.add(normalize_requirement_key(existing_text))

                            for extra_item in structural_fallback_items:
                                extra_text = str((extra_item or {}).get("requirement_text") or "")
                                if not extra_text:
                                    continue
                                extra_key = normalize_requirement_key(extra_text)
                                if extra_key in existing_keys:
                                    continue
                                parsed_items.append(extra_item)
                                existing_keys.add(extra_key)
                                empty_llm_fallback_generated_total += 1
                        if not parsed_items:
                            continue

                        raw_llm_requirements += len(parsed_items)
                        unit_had_requirements = True

                        for item in parsed_items:
                            item_req_text = (
                                item.requirement_text
                                if hasattr(item, "requirement_text")
                                else str((item or {}).get("requirement_text") or "")
                            )
                            item_req_type = (
                                item.req_type
                                if hasattr(item, "req_type")
                                else str((item or {}).get("req_type") or "AUTRE")
                            )
                            item_normative_strength = (
                                item.normative_strength
                                if hasattr(item, "normative_strength")
                                else str((item or {}).get("normative_strength") or "IMPERATIF")
                            ) or "IMPERATIF"
                            item_legal_subject = (
                                item.legal_subject
                                if hasattr(item, "legal_subject")
                                else str((item or {}).get("legal_subject") or "")
                            )
                            item_normative_verb = (
                                item.normative_verb
                                if hasattr(item, "normative_verb")
                                else str((item or {}).get("normative_verb") or "")
                            )
                            item_action_object = (
                                item.action_object
                                if hasattr(item, "action_object")
                                else str((item or {}).get("action_object") or "")
                            )
                            item_condition_text = (
                                item.condition_text
                                if hasattr(item, "condition_text")
                                else str((item or {}).get("condition_text") or "")
                            )
                            item_exception_text = (
                                item.exception_text
                                if hasattr(item, "exception_text")
                                else str((item or {}).get("exception_text") or "")
                            )
                            item_source_mode = (
                                item.source_mode
                                if hasattr(item, "source_mode")
                                else str((item or {}).get("source_mode") or "NON_PRECISE")
                            ) or "NON_PRECISE"
                            rewrite_result = _rewrite_runtime_requirement_from_structure(
                                requirement_text=item_req_text,
                                req_type=item_req_type,
                                source_snippet=source_snippet,
                                legal_subject=item_legal_subject,
                                normative_verb=item_normative_verb,
                                action_object=item_action_object,
                                condition_text=item_condition_text,
                                exception_text=item_exception_text,
                                source_mode=item_source_mode,
                            )
                            item_req_text = str(rewrite_result.get("requirement_text") or item_req_text)
                            normalized_requirement_text = normalize_subject_from_context(
                                item_req_text,
                                source_snippet,
                                cleaned_chunk_text,
                            )
                            normalized_requirement_text = decode_html_entities(normalized_requirement_text)

                            normalized_req_type = normalize_req_type(
                                requirement_text=normalized_requirement_text,
                                source_snippet=source_snippet,
                                llm_req_type=item_req_type,
                            )

                            normalized_requirement_text = normalize_requirement_text_by_type(
                                requirement_text=normalized_requirement_text,
                                source_snippet=source_snippet,
                                req_type=normalized_req_type,
                                chunk_text=cleaned_chunk_text,
                            )
                            normalized_requirement_text = expand_introductory_documentary_requirement(
                                requirement_text=normalized_requirement_text,
                                source_snippet=source_snippet,
                                req_type=normalized_req_type,
                            )
                            expanded_requirements = _split_runtime_granular_requirement(
                                requirement_text=normalized_requirement_text,
                                req_type=normalized_req_type,
                                source_snippet=source_snippet,
                                snippet_context_text=snippet_context_text,
                                chunk_text=cleaned_chunk_text,
                            )
                            if article_id is None:
                                raise RuntimeError(
                                    "article_id NULL detecte pendant insertion requirement."
                                )

                            pending_requirements: list[dict[str, Any]] = [
                                {
                                    "text": req_text,
                                    "req_type": normalized_req_type,
                                    "normative_strength": item_normative_strength,
                                    "memory_fix_applied": False,
                                    "late_split_applied": False,
                                }
                                for req_text in expanded_requirements
                                if str(req_text or "").strip()
                            ]

                            while pending_requirements:
                                pending_requirement = pending_requirements.pop(0)
                                current_req_text = decode_html_entities(
                                    str(pending_requirement.get("text") or "").strip()
                                )
                                current_req_type = (
                                    str(pending_requirement.get("req_type") or normalized_req_type)
                                    .strip()
                                    .upper()
                                    or normalized_req_type
                                )
                                memory_fix_applied = bool(pending_requirement.get("memory_fix_applied"))
                                late_split_applied = bool(pending_requirement.get("late_split_applied"))
                                current_normative_strength = str(
                                    pending_requirement.get("normative_strength") or "IMPERATIF"
                                ).strip().upper() or "IMPERATIF"

                                evaluated_requirement = _evaluate_runtime_requirement(
                                    requirement_text=current_req_text,
                                    req_type=current_req_type,
                                    source_snippet=source_snippet,
                                    snippet_context_text=snippet_context_text,
                                    chunk_text=cleaned_chunk_text,
                                    normative_strength=current_normative_strength,
                                )
                                final_req_text = str(evaluated_requirement["requirement_text"])
                                normalized_req_type = str(evaluated_requirement["req_type"])
                                evidence_snippet = str(evaluated_requirement["evidence_snippet"])
                                confidence = float(evaluated_requirement["confidence"])
                                initial_status = str(evaluated_requirement["initial_status"])
                                runtime_postcall = dict(evaluated_requirement["runtime_postcall"])
                                postcall_decision = str(evaluated_requirement["postcall_decision"])
                                postcall_reasons = list(evaluated_requirement["postcall_reasons"])
                                grounding_score = float(evaluated_requirement["grounding_score"])
                                quality_score = float(evaluated_requirement["quality_score"])
                                status = str(evaluated_requirement["status"])

                                if not late_split_applied:
                                    late_split_candidates: list[dict[str, str]] = []
                                    late_split_parent_key = normalize_requirement_key(final_req_text)
                                    seen_late_split_keys: set[tuple[str, str]] = set()
                                    for split_text in _split_runtime_granular_requirement(
                                        requirement_text=final_req_text,
                                        req_type=normalized_req_type,
                                        source_snippet=evidence_snippet or source_snippet,
                                        snippet_context_text=snippet_context_text,
                                        chunk_text=cleaned_chunk_text,
                                    ):
                                        split_req_type = normalize_req_type(
                                            requirement_text=split_text,
                                            source_snippet=source_snippet,
                                            llm_req_type=normalized_req_type,
                                        )
                                        split_key = (
                                            split_req_type,
                                            normalize_requirement_key(split_text),
                                        )
                                        if (
                                            not split_key[1]
                                            or split_key[1] == late_split_parent_key
                                            or split_key in seen_late_split_keys
                                        ):
                                            continue
                                        seen_late_split_keys.add(split_key)
                                        late_split_candidates.append({
                                            "text": split_text,
                                            "req_type": split_req_type,
                                        })
                                    if len(late_split_candidates) >= 2:
                                        for split_item in reversed(late_split_candidates):
                                            pending_requirements.insert(0, {
                                                "text": str((split_item or {}).get("text") or ""),
                                                "req_type": str(
                                                    (split_item or {}).get("req_type") or normalized_req_type
                                                ),
                                                "memory_fix_applied": memory_fix_applied,
                                                "late_split_applied": True,
                                            })
                                        continue

                                memory_hit = (
                                    find_error_memory_hit(
                                        error_memory_store,
                                        requirement_text=final_req_text,
                                        req_type=normalized_req_type,
                                        snippet=evidence_snippet,
                                    )
                                    if error_memory_enabled
                                    else None
                                )

                                if memory_hit and not memory_fix_applied:
                                    memory_fix = _apply_error_memory_fix(
                                        requirement_text=final_req_text,
                                        req_type=normalized_req_type,
                                        source_snippet=source_snippet,
                                        evidence_snippet=evidence_snippet,
                                        snippet_context_text=snippet_context_text,
                                        chunk_text=cleaned_chunk_text,
                                        memory_hit=memory_hit,
                                    )
                                    if bool(memory_fix.get("applied")):
                                        fix_rule = str(memory_fix.get("fix_rule") or "").strip().upper()
                                        split_requirements = [
                                            split_item
                                            for split_item in (memory_fix.get("split_requirements") or [])
                                            if str((split_item or {}).get("text") or "").strip()
                                        ]
                                        if split_requirements:
                                            record_error_memory_fix(
                                                memory_hit=memory_hit,
                                                fix_rule=fix_rule,
                                                article_label_value=article_label,
                                                req_type_before=normalized_req_type,
                                                req_type_after=normalized_req_type,
                                                text_before=final_req_text,
                                                text_after=" | ".join(
                                                    str((split_item or {}).get("text") or "")
                                                    for split_item in split_requirements[:4]
                                                ),
                                                split_count=len(split_requirements),
                                            )
                                            for split_item in reversed(split_requirements):
                                                pending_requirements.insert(0, {
                                                    "text": str((split_item or {}).get("text") or ""),
                                                    "req_type": str(
                                                        (split_item or {}).get("req_type") or normalized_req_type
                                                    ),
                                                    "memory_fix_applied": True,
                                                    "late_split_applied": True,
                                                })
                                            continue

                                        fixed_req_text = decode_html_entities(
                                            str(memory_fix.get("requirement_text") or final_req_text)
                                        )
                                        fixed_req_type = (
                                            str(memory_fix.get("req_type") or normalized_req_type)
                                            .strip()
                                            .upper()
                                            or normalized_req_type
                                        )
                                        fixed_evidence_snippet = clean_source_snippet(
                                            str(memory_fix.get("evidence_snippet") or evidence_snippet)
                                        )

                                        if (
                                            fixed_req_text != final_req_text
                                            or fixed_req_type != normalized_req_type
                                            or fixed_evidence_snippet != evidence_snippet
                                        ):
                                            record_error_memory_fix(
                                                memory_hit=memory_hit,
                                                fix_rule=fix_rule,
                                                article_label_value=article_label,
                                                req_type_before=normalized_req_type,
                                                req_type_after=fixed_req_type,
                                                text_before=final_req_text,
                                                text_after=fixed_req_text,
                                            )
                                            evaluated_requirement = _evaluate_runtime_requirement(
                                                requirement_text=fixed_req_text,
                                                req_type=fixed_req_type,
                                                source_snippet=source_snippet,
                                                snippet_context_text=snippet_context_text,
                                                chunk_text=cleaned_chunk_text,
                                                evidence_snippet_override=fixed_evidence_snippet,
                                                normative_strength=current_normative_strength,
                                            )
                                            final_req_text = str(evaluated_requirement["requirement_text"])
                                            normalized_req_type = str(evaluated_requirement["req_type"])
                                            evidence_snippet = str(evaluated_requirement["evidence_snippet"])
                                            confidence = float(evaluated_requirement["confidence"])
                                            initial_status = str(evaluated_requirement["initial_status"])
                                            runtime_postcall = dict(evaluated_requirement["runtime_postcall"])
                                            postcall_decision = str(evaluated_requirement["postcall_decision"])
                                            postcall_reasons = list(evaluated_requirement["postcall_reasons"])
                                            grounding_score = float(
                                                evaluated_requirement["grounding_score"]
                                            )
                                            quality_score = float(
                                                evaluated_requirement["quality_score"]
                                            )
                                            status = str(evaluated_requirement["status"])
                                            memory_fix_applied = True
                                            memory_hit = (
                                                find_error_memory_hit(
                                                    error_memory_store,
                                                    requirement_text=final_req_text,
                                                    req_type=normalized_req_type,
                                                    snippet=evidence_snippet,
                                                )
                                                if error_memory_enabled
                                                else None
                                            )

                                if (
                                    memory_hit
                                    and str(memory_hit.get("memory_action") or "").strip().upper() == "FORCE_VALIDATE"
                                    and status not in {"REJECT", "TO_VALIDATE"}
                                ):
                                    status = "TO_VALIDATE"
                                    if "ERROR_MEMORY_FORCE_VALIDATE" not in postcall_reasons:
                                        postcall_reasons.append("ERROR_MEMORY_FORCE_VALIDATE")
                                    record_error_memory_hit(
                                        memory_hit=memory_hit,
                                        requirement_text=final_req_text,
                                        req_type=normalized_req_type,
                                        article_label_value=article_label,
                                    )

                                final_req_text = normalize_subject_from_context(
                                    final_req_text,
                                    evidence_snippet,
                                    cleaned_chunk_text,
                                )
                                final_req_text = normalize_requirement_text_by_type(
                                    requirement_text=final_req_text,
                                    source_snippet=evidence_snippet,
                                    req_type=normalized_req_type,
                                    chunk_text=cleaned_chunk_text,
                                )
                                final_req_text = refine_requirement_text_quality(
                                    requirement_text=final_req_text,
                                    source_snippet=evidence_snippet,
                                    req_type=normalized_req_type,
                                    chunk_text=cleaned_chunk_text,
                                )
                                evidence_snippet = clean_source_snippet(evidence_snippet)
                                evidence_snippet = _tighten_runtime_evidence_snippet(
                                    requirement_text=final_req_text,
                                    current_snippet=evidence_snippet,
                                    source_snippet=source_snippet,
                                    snippet_context_text=snippet_context_text,
                                    chunk_text=cleaned_chunk_text,
                                )

                                if postcall_decision == "DROP":
                                    record_error_memory_signal(
                                        build_error_memory_signal(
                                            requirement_text=final_req_text,
                                            req_type=normalized_req_type,
                                            snippet=evidence_snippet,
                                            reasons=postcall_reasons,
                                            filter_name="POSTCALL_RUNTIME",
                                            postcall=runtime_postcall,
                                            article_label=article_label,
                                            status=status,
                                            decision=postcall_decision,
                                        )
                                    )
                                    safe_insert_event(cur, tenant_id, args.doc_id, "REQUIREMENT_REJECTED", {
                                        "filter": "POSTCALL_RUNTIME",
                                        "req_type": normalized_req_type,
                                        "article_label": article_label,
                                        "text_preview": final_req_text[:120],
                                        "reasons": postcall_reasons[:8],
                                    })
                                    continue
                                soft_validate_override = False
                                recall_guard = has_legal_risk_markers(
                                    f"{final_req_text} {evidence_snippet}"
                                ) and not is_out_of_scope_individual_requirement(
                                    requirement_text=final_req_text,
                                    citation_snippet=evidence_snippet,
                                )
                                if is_low_value_requirement_text(final_req_text):
                                    if recall_guard:
                                        softened_filter_rejections_total += 1
                                        soft_validate_override = True
                                    else:
                                        rejected_low_value += 1
                                        record_error_memory_signal(
                                            build_error_memory_signal(
                                                requirement_text=final_req_text,
                                                req_type=normalized_req_type,
                                                snippet=evidence_snippet,
                                                reasons=postcall_reasons,
                                                filter_name="LOW_VALUE",
                                                postcall=runtime_postcall,
                                                article_label=article_label,
                                                status=status,
                                                decision="DROP",
                                            )
                                        )
                                        safe_insert_event(cur, tenant_id, args.doc_id, "REQUIREMENT_REJECTED", {
                                            "filter": "LOW_VALUE",
                                            "req_type": normalized_req_type,
                                            "article_label": article_label,
                                            "text_preview": final_req_text[:120],
                                        })
                                        continue
                                if _looks_fragmentary_runtime_requirement(final_req_text):
                                    rejected_fragmentary += 1
                                    record_error_memory_signal(
                                        build_error_memory_signal(
                                            requirement_text=final_req_text,
                                            req_type=normalized_req_type,
                                            snippet=evidence_snippet,
                                            reasons=postcall_reasons,
                                            filter_name="FRAGMENTARY_RUNTIME",
                                            postcall=runtime_postcall,
                                            article_label=article_label,
                                            status=status,
                                            decision="DROP",
                                        )
                                    )
                                    safe_insert_event(cur, tenant_id, args.doc_id, "REQUIREMENT_REJECTED", {
                                        "filter": "FRAGMENTARY_RUNTIME",
                                        "req_type": normalized_req_type,
                                        "article_label": article_label,
                                        "text_preview": final_req_text[:120],
                                    })
                                    continue
                                if _looks_subject_verb_mismatch_requirement(final_req_text):
                                    rejected_mismatch += 1
                                    record_error_memory_signal(
                                        build_error_memory_signal(
                                            requirement_text=final_req_text,
                                            req_type=normalized_req_type,
                                            snippet=evidence_snippet,
                                            reasons=postcall_reasons,
                                            filter_name="SUBJECT_VERB_MISMATCH",
                                            postcall=runtime_postcall,
                                            article_label=article_label,
                                            status=status,
                                            decision="DROP",
                                        )
                                    )
                                    safe_insert_event(cur, tenant_id, args.doc_id, "REQUIREMENT_REJECTED", {
                                        "filter": "SUBJECT_VERB_MISMATCH",
                                        "req_type": normalized_req_type,
                                        "article_label": article_label,
                                        "text_preview": final_req_text[:120],
                                    })
                                    continue
                                if is_non_actionable_scope_requirement(final_req_text, evidence_snippet):
                                    if recall_guard:
                                        softened_filter_rejections_total += 1
                                        soft_validate_override = True
                                    else:
                                        rejected_scope += 1
                                        record_error_memory_signal(
                                            build_error_memory_signal(
                                                requirement_text=final_req_text,
                                                req_type=normalized_req_type,
                                                snippet=evidence_snippet,
                                                reasons=postcall_reasons,
                                                filter_name="NON_ACTIONABLE_SCOPE",
                                                postcall=runtime_postcall,
                                                article_label=article_label,
                                                status=status,
                                                decision="DROP",
                                            )
                                        )
                                        safe_insert_event(cur, tenant_id, args.doc_id, "REQUIREMENT_REJECTED", {
                                            "filter": "NON_ACTIONABLE_SCOPE",
                                            "req_type": normalized_req_type,
                                            "article_label": article_label,
                                            "text_preview": final_req_text[:120],
                                        })
                                        continue
                                if is_partial_exception_requirement(final_req_text, evidence_snippet):
                                    if recall_guard:
                                        softened_filter_rejections_total += 1
                                        soft_validate_override = True
                                    else:
                                        rejected_partial_exception += 1
                                        record_error_memory_signal(
                                            build_error_memory_signal(
                                                requirement_text=final_req_text,
                                                req_type=normalized_req_type,
                                                snippet=evidence_snippet,
                                                reasons=postcall_reasons,
                                                filter_name="PARTIAL_EXCEPTION",
                                                postcall=runtime_postcall,
                                                article_label=article_label,
                                                status=status,
                                                decision="DROP",
                                            )
                                        )
                                        safe_insert_event(cur, tenant_id, args.doc_id, "REQUIREMENT_REJECTED", {
                                            "filter": "PARTIAL_EXCEPTION",
                                            "req_type": normalized_req_type,
                                            "article_label": article_label,
                                            "text_preview": final_req_text[:120],
                                        })
                                        continue
                                if is_invented_from_non_normative_snippet(final_req_text, evidence_snippet):
                                    if recall_guard:
                                        softened_filter_rejections_total += 1
                                        soft_validate_override = True
                                    else:
                                        rejected_non_normative += 1
                                        record_error_memory_signal(
                                            build_error_memory_signal(
                                                requirement_text=final_req_text,
                                                req_type=normalized_req_type,
                                                snippet=evidence_snippet,
                                                reasons=postcall_reasons,
                                                filter_name="INVENTED_NON_NORMATIVE",
                                                postcall=runtime_postcall,
                                                article_label=article_label,
                                                status=status,
                                                decision="DROP",
                                            )
                                        )
                                        safe_insert_event(cur, tenant_id, args.doc_id, "REQUIREMENT_REJECTED", {
                                            "filter": "INVENTED_NON_NORMATIVE",
                                            "req_type": normalized_req_type,
                                            "article_label": article_label,
                                            "text_preview": final_req_text[:120],
                                        })
                                        continue
                                if is_out_of_scope_individual_requirement(
                                    requirement_text=final_req_text,
                                    citation_snippet=evidence_snippet,
                                ):
                                    rejected_out_of_scope += 1
                                    record_error_memory_signal(
                                        build_error_memory_signal(
                                            requirement_text=final_req_text,
                                            req_type=normalized_req_type,
                                            snippet=evidence_snippet,
                                            reasons=postcall_reasons,
                                            filter_name="OUT_OF_SCOPE_INDIVIDUAL",
                                            postcall=runtime_postcall,
                                            article_label=article_label,
                                            status=status,
                                            decision="DROP",
                                        )
                                    )
                                    safe_insert_event(cur, tenant_id, args.doc_id, "REQUIREMENT_REJECTED", {
                                        "filter": "OUT_OF_SCOPE_INDIVIDUAL",
                                        "req_type": normalized_req_type,
                                        "article_label": article_label,
                                        "text_preview": final_req_text[:120],
                                    })
                                    continue

                                if status == "TO_VALIDATE":
                                    record_error_memory_signal(
                                        build_error_memory_signal(
                                            requirement_text=final_req_text,
                                            req_type=normalized_req_type,
                                            snippet=evidence_snippet,
                                            reasons=postcall_reasons,
                                            postcall=runtime_postcall,
                                            article_label=article_label,
                                            status=status,
                                            decision=postcall_decision,
                                        )
                                    )

                                article_dedup_key = (
                                    str(article_id),
                                    normalize_requirement_key(final_req_text),
                                )
                                if article_dedup_key in seen_article_exact:
                                    deduplicated += 1
                                    deduplicated_exact_article += 1
                                    continue

                                if article_prefix_dedup_enabled:
                                    article_prefix_bucket = seen_article_prefix.setdefault(str(article_id), [])
                                    normalized_final_for_prefix = article_dedup_key[1]
                                    is_prefix_dup = False
                                    for prev_type, prev_text in article_prefix_bucket:
                                        if prev_type != normalized_req_type:
                                            continue
                                        if _is_prefix_extended_duplicate(
                                            normalized_final_for_prefix,
                                            prev_text,
                                            min_prefix_chars=article_prefix_dedup_min_chars,
                                        ) or _is_high_overlap_duplicate(
                                            normalized_final_for_prefix,
                                            prev_text,
                                        ):
                                            is_prefix_dup = True
                                            break
                                    if is_prefix_dup:
                                        deduplicated += 1
                                        deduplicated_prefix_article += 1
                                        continue

                                if doc_level_dedup_enabled:
                                    doc_relaxed_key = build_doc_level_relaxed_requirement_key(
                                        requirement_text=final_req_text,
                                        req_type=normalized_req_type,
                                    )
                                    if (
                                        doc_level_text_dedup_enabled
                                        and len(doc_relaxed_key[1]) >= doc_level_dedup_min_req_chars
                                        and doc_relaxed_key in seen_doc_relaxed_exact
                                    ):
                                        deduplicated += 1
                                        deduplicated_exact_doc += 1
                                        continue
                                    if (
                                        doc_level_prefix_dedup_enabled
                                        and len(doc_relaxed_key[1]) >= doc_level_dedup_min_req_chars
                                    ):
                                        doc_prefix_bucket = seen_doc_prefix.setdefault(doc_relaxed_key[0], [])
                                        is_doc_prefix_dup = False
                                        for prev_text in doc_prefix_bucket:
                                            if _is_prefix_extended_duplicate(
                                                doc_relaxed_key[1],
                                                prev_text,
                                                min_prefix_chars=doc_level_prefix_dedup_min_chars,
                                            ):
                                                is_doc_prefix_dup = True
                                                break
                                        if is_doc_prefix_dup:
                                            deduplicated += 1
                                            deduplicated_prefix_doc += 1
                                            continue
                                    doc_dedup_key = build_doc_level_dedup_key(
                                        requirement_text=final_req_text,
                                        source_snippet=evidence_snippet,
                                        req_type=normalized_req_type,
                                    )
                                    if (
                                        len(doc_dedup_key[1]) >= doc_level_dedup_min_req_chars
                                        and doc_dedup_key in seen_doc_exact
                                    ):
                                        deduplicated += 1
                                        deduplicated_exact_doc += 1
                                        continue
                                    if (
                                        doc_level_text_dedup_enabled
                                        and len(doc_relaxed_key[1]) >= doc_level_dedup_min_req_chars
                                    ):
                                        seen_doc_relaxed_exact.add(doc_relaxed_key)
                                    if (
                                        doc_level_prefix_dedup_enabled
                                        and len(doc_relaxed_key[1]) >= doc_level_dedup_min_req_chars
                                    ):
                                        seen_doc_prefix.setdefault(doc_relaxed_key[0], []).append(doc_relaxed_key[1])
                                    if len(doc_dedup_key[1]) >= doc_level_dedup_min_req_chars:
                                        seen_doc_exact.add(doc_dedup_key)

                                seen_article_exact.add(article_dedup_key)
                                if article_prefix_dedup_enabled:
                                    seen_article_prefix.setdefault(str(article_id), []).append(
                                        (normalized_req_type, article_dedup_key[1])
                                    )

                                requirement_no += 1
                                inserted_requirements_count += 1
                                citation_ref = format_citation_ref(article_label, start_page, end_page)
                                qse_domain, qse_sub_domain, qse_mapping_strategy = classify_qse_domain_subdomain(
                                    requirement_text=final_req_text,
                                    req_type=normalized_req_type,
                                    citation_snippet=evidence_snippet,
                                    chunk_text=cleaned_chunk_text,
                                )
                                qse_domain_counts[qse_domain] = int(qse_domain_counts.get(qse_domain) or 0) + 1
                                qse_sub_domain_counts[qse_sub_domain] = int(
                                    qse_sub_domain_counts.get(qse_sub_domain) or 0
                                ) + 1
                                qse_mapping_strategy_counts[qse_mapping_strategy] = int(
                                    qse_mapping_strategy_counts.get(qse_mapping_strategy) or 0
                                ) + 1

                                if soft_validate_override and status != "REJECT":
                                    status = "TO_VALIDATE"
                                # Article avec renvoi inter-article → revue manuelle obligatoire
                                if article_key in articles_with_cross_ref and status not in ("REJECT", "TO_VALIDATE"):
                                    status = "TO_VALIDATE"
                                status_before_policy = status
                                status = force_to_validate_by_policy(article_policy, status)
                                if status_before_policy != status and status == "TO_VALIDATE":
                                    policy_forced_to_validate_total += 1
                                if status == "TO_VALIDATE":
                                    promotion_reviewed_to_validate_total += 1
                                    promoted_status, promotion_reason, promotion_overlap = _maybe_promote_to_draft(
                                        status=status,
                                        req_type=normalized_req_type,
                                        confidence=confidence,
                                        requirement_text=final_req_text,
                                        source_snippet=evidence_snippet,
                                        postcall=runtime_postcall,
                                        error_memory_hit=memory_hit,
                                        article_policy=article_policy,
                                        enabled=promotion_enabled,
                                        min_confidence=promotion_min_confidence,
                                        min_overlap=promotion_min_overlap,
                                        min_snippet_chars=promotion_min_snippet_chars,
                                        max_req_chars=promotion_max_req_chars,
                                        max_snippet_chars=promotion_max_snippet_chars,
                                        block_modal_peut=promotion_block_modal_peut,
                                        require_clean_start=promotion_require_clean_start,
                                        min_quality_score=promotion_min_quality_score,
                                        min_grounding_score=promotion_min_grounding_score,
                                        min_completeness_score=promotion_min_completeness_score,
                                        min_subject_consistency=promotion_min_subject_consistency,
                                        require_postcall_draft=promotion_require_postcall_draft,
                                        block_fused_action_chain=promotion_block_fused_action_chain,
                                    )
                                    if promoted_status == "DRAFT" and status != "DRAFT":
                                        status = "DRAFT"
                                        promoted_to_draft_total += 1
                                        promotion_overlap_sum += promotion_overlap
                                    elif promotion_reason == "POLICY_NOT_FULL":
                                        promotion_blocked_policy_total += 1
                                    elif promotion_reason == "TYPE_NOT_ALLOWED":
                                        promotion_blocked_type_total += 1
                                    elif promotion_reason == "LOW_CONFIDENCE":
                                        promotion_blocked_low_conf_total += 1
                                    elif promotion_reason == "SHORT_SNIPPET":
                                        promotion_blocked_short_snippet_total += 1
                                    elif promotion_reason == "LOW_OVERLAP":
                                        promotion_blocked_low_overlap_total += 1
                                    elif promotion_reason == "REQUIREMENT_TOO_LONG":
                                        promotion_blocked_long_req_total += 1
                                    elif promotion_reason == "SNIPPET_TOO_LONG":
                                        promotion_blocked_long_snippet_total += 1
                                    elif promotion_reason == "MODAL_PEUT":
                                        promotion_blocked_modal_peut_total += 1
                                    elif promotion_reason == "NOISY_START":
                                        promotion_blocked_noisy_start_total += 1
                                    elif promotion_reason == "QUALITY_TOO_LOW":
                                        promotion_blocked_quality_total += 1
                                    elif promotion_reason == "GROUNDING_TOO_WEAK":
                                        promotion_blocked_grounding_total += 1
                                    elif promotion_reason == "COMPLETENESS_NOT_PASS":
                                        promotion_blocked_completeness_total += 1
                                    elif promotion_reason == "SUBJECT_INCONSISTENT":
                                        promotion_blocked_subject_total += 1
                                    elif promotion_reason == "POSTCALL_NOT_DRAFT":
                                        promotion_blocked_postcall_total += 1
                                    elif promotion_reason == "FUSED_ACTION_CHAIN":
                                        promotion_blocked_fused_total += 1
                                    elif promotion_reason == "MISSING_COMPONENT":
                                        promotion_blocked_missing_component_total += 1
                                    elif promotion_reason == "TYPE_MISMATCH_POSTCALL":
                                        promotion_blocked_postcall_type_total += 1
                                    elif promotion_reason == "ERROR_MEMORY_HIT":
                                        promotion_blocked_error_memory_total += 1

                                cur.execute(
                                    """
                                    INSERT INTO requirements(
                                        doc_id,
                                        article_id,
                                        requirement_no,
                                        requirement_text,
                                        req_type,
                                        category,
                                        qse_domain,
                                        qse_sub_domain,
                                        citation_snippet,
                                        citation_ref,
                                        confidence,
                                        status,
                                        grounding_score,
                                        quality_score,
                                        normative_strength,
                                        extraction_source,
                                        extracted_at
                                    )
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                                    """,
                                    (
                                        args.doc_id,
                                        article_id,
                                        requirement_no,
                                        final_req_text,
                                        normalized_req_type,
                                        None,
                                        qse_domain,
                                        qse_sub_domain,
                                        evidence_snippet,
                                        citation_ref,
                                        confidence,
                                        status,
                                        grounding_score,
                                        quality_score,
                                        current_normative_strength,
                                        "llm_api",
                                    ),
                                )

                        if args.sleep_ms > 0:
                            time.sleep(args.sleep_ms / 1000)

                    if unit_had_requirements:
                        chunk_ok += 1
                    else:
                        chunk_empty += 1

                except ValidationError as exc:
                    chunk_error += 1
                    print(f"ERROR ValidationError on {article_label} / chunk {chunk_no}: {exc}", flush=True)

                except Exception as exc:
                    chunk_error += 1
                    print(f"ERROR sur {article_label} / chunk {chunk_no}: {exc}", flush=True)

            llm_usage_totals = {
                "llm_calls": int(extractor.usage_totals.get("llm_calls") or 0),
                "prompt_tokens": int(extractor.usage_totals.get("prompt_tokens") or 0),
                "completion_tokens": int(extractor.usage_totals.get("completion_tokens") or 0),
                "total_tokens": int(extractor.usage_totals.get("total_tokens") or 0),
                "estimated_cost_usd": round(
                    float(extractor.usage_totals.get("estimated_cost_usd") or 0.0),
                    8,
                ),
            }
            llm_cache_stats = {
                "cache_enabled": bool(extractor.cache_enabled),
                "cache_hits_total": int(extractor.cache_hits_total or 0),
                "cache_hits_strict_total": int(extractor.cache_hits_strict_total or 0),
                "cache_hits_relaxed_total": int(extractor.cache_hits_relaxed_total or 0),
                "cache_negative_hits_total": int(extractor.cache_negative_hits_total or 0),
                "cache_lookup_total": int(extractor.cache_lookup_total or 0),
                "cache_misses_total": int(extractor.cache_misses_total or 0),
                "cache_put_total": int(extractor.cache_put_total or 0),
                "cache_put_negative_total": int(extractor.cache_put_negative_total or 0),
            }
            llm_availability_totals = {
                key: (
                    round(float(value), 4) if isinstance(value, float) else int(value)
                )
                for key, value in dict(extractor.availability_totals or {}).items()
            }
            avg_tokens_per_llm_call = round(
                (
                    float(llm_usage_totals["total_tokens"]) / float(llm_usage_totals["llm_calls"])
                ) if llm_usage_totals["llm_calls"] else 0.0,
                2,
            )
            avg_cost_per_requirement = round(
                (
                    float(llm_usage_totals["estimated_cost_usd"]) / float(inserted_requirements_count)
                ) if inserted_requirements_count else 0.0,
                8,
            )

            event_payload = {
                "mode": "llm_api",
                "doc_title": doc_title,
                "article_label_filter": args.article_label,
                "article_id_filter": args.article_id,
                "force_full_rebuild": bool(args.force_full_rebuild),
                "primary_provider": extractor.primary_provider,
                "primary_model": extractor.primary_model,
                "fallback_provider": extractor.fallback_provider,
                "fallback_model": extractor.fallback_model,
                "providers_used": sorted(providers_used),
                "models_used": sorted(models_used),
                "fallback_used_during_run": run_used_fallback,
                "chunks_ok": chunk_ok,
                "chunks_empty": chunk_empty,
                "chunks_error": chunk_error,
                "raw_llm_requirements": raw_llm_requirements,
                "requirements_inserted": inserted_requirements_count,
                "qse_domain_counts": qse_domain_counts,
                "qse_sub_domain_counts": qse_sub_domain_counts,
                "qse_mapping_strategy_counts": qse_mapping_strategy_counts,
                "rejected_low_value": rejected_low_value,
                "rejected_fragmentary": rejected_fragmentary,
                "rejected_mismatch": rejected_mismatch,
                "rejected_scope": rejected_scope,
                "rejected_partial_exception": rejected_partial_exception,
                "rejected_non_normative": rejected_non_normative,
                "rejected_out_of_scope": rejected_out_of_scope,
                "deduplicated": deduplicated,
                "deduplicated_exact_article": deduplicated_exact_article,
                "deduplicated_exact_doc": deduplicated_exact_doc,
                "deduplicated_prefix_article": deduplicated_prefix_article,
                "deduplicated_prefix_doc": deduplicated_prefix_doc,
                "doc_level_dedup_enabled": doc_level_dedup_enabled,
                "doc_level_text_dedup_enabled": doc_level_text_dedup_enabled,
                "doc_level_dedup_min_req_chars": doc_level_dedup_min_req_chars,
                "article_prefix_dedup_enabled": article_prefix_dedup_enabled,
                "article_prefix_dedup_min_chars": article_prefix_dedup_min_chars,
                "doc_level_prefix_dedup_enabled": doc_level_prefix_dedup_enabled,
                "doc_level_prefix_min_chars": doc_level_prefix_dedup_min_chars,
                "long_article_safety_enabled": long_article_safety_enabled,
                "long_article_safety_chunks_total": long_article_safety_chunks_total,
                "long_article_safety_forced_units_total": long_article_safety_forced_units_total,
                "long_article_safety_full_chunk_fallback_total": long_article_safety_full_chunk_fallback_total,
                "precall_runtime_version": PRECALL_NLP_VERSION,
                "precall_runtime_mode": runtime_precall_mode,
                "precall_runtime_chunks_total": precall_runtime_chunks_total,
                "precall_runtime_fallback_chunks_total": precall_runtime_fallback_chunks_total,
                "precall_runtime_units_high_total": precall_runtime_units_high_total,
                "precall_runtime_units_low_total": precall_runtime_units_low_total,
                "precall_runtime_units_drop_total": precall_runtime_units_drop_total,
                "precall_runtime_drop_shadow_total": precall_runtime_drop_shadow_total,
                "precall_runtime_drop_hard_total": precall_runtime_drop_hard_total,
                "runtime_unit_cache_enabled": runtime_unit_cache_enabled,
                "runtime_unit_dedup_enabled": runtime_unit_dedup_enabled,
                "runtime_unit_deduped_total": runtime_unit_deduped_total,
                "runtime_unit_cache_hits_total": runtime_unit_cache_hits_total,
                "runtime_unit_cache_store_total": runtime_unit_cache_store_total,
                "runtime_budget_fallback_enabled": runtime_budget_fallback_enabled,
                "runtime_budget_max_llm_calls": runtime_budget_max_llm_calls,
                "runtime_budget_max_total_tokens": runtime_budget_max_total_tokens,
                "runtime_budget_max_estimated_cost_usd": runtime_budget_max_estimated_cost_usd,
                "runtime_budget_blocked_units_total": runtime_budget_blocked_units_total,
                "runtime_budget_fallback_units_total": runtime_budget_fallback_units_total,
                "softened_filter_rejections_total": softened_filter_rejections_total,
                "empty_llm_fallback_generated_total": empty_llm_fallback_generated_total,
                "llm_usage_totals": llm_usage_totals,
                "llm_cache_stats": llm_cache_stats,
                "llm_availability_totals": llm_availability_totals,
                "llm_avg_tokens_per_call": avg_tokens_per_llm_call,
                "llm_cost_per_inserted_requirement_usd": avg_cost_per_requirement,
                "doc_gate_enabled": doc_gate_enabled,
                "doc_gate_policy": doc_gate.policy,
                "doc_gate_document_type": doc_gate.document_type,
                "doc_gate_confidence": round(float(doc_gate.confidence), 4),
                "doc_gate_reason_codes": list(doc_gate.reason_codes),
                "doc_gate_skip_all": skip_all_by_doc_policy,
                "article_policy_counts": article_policy_counts,
                "article_policy_reason_counts": article_policy_reason_counts,
                "article_drop_reason_counts": article_drop_reason_counts,
                "article_dropped_by_policy_total": article_dropped_by_policy_total,
                "article_limited_total": len(article_policy_limited_ids),
                "units_dropped_limited_policy_total": units_dropped_limited_policy_total,
                "limited_policy_forced_units_total": limited_policy_forced_units_total,
                "limited_data_articles_total": len(limited_data_articles_seen),
                "limited_data_objects_total": limited_data_objects_total,
                "limited_data_object_type_counts": limited_data_object_type_counts,
                "limited_data_unattached_total": limited_data_unattached_total,
                "limited_data_samples": limited_data_samples,
                "policy_forced_to_validate_total": policy_forced_to_validate_total,
                "promotion_enabled": promotion_enabled,
                "promotion_min_confidence": promotion_min_confidence,
                "promotion_min_overlap": promotion_min_overlap,
                "promotion_min_snippet_chars": promotion_min_snippet_chars,
                "promotion_min_quality_score": promotion_min_quality_score,
                "promotion_min_grounding_score": promotion_min_grounding_score,
                "promotion_min_completeness_score": promotion_min_completeness_score,
                "promotion_min_subject_consistency": promotion_min_subject_consistency,
                "promotion_max_req_chars": promotion_max_req_chars,
                "promotion_max_snippet_chars": promotion_max_snippet_chars,
                "promotion_block_modal_peut": promotion_block_modal_peut,
                "promotion_require_clean_start": promotion_require_clean_start,
                "promotion_require_postcall_draft": promotion_require_postcall_draft,
                "promotion_block_fused_action_chain": promotion_block_fused_action_chain,
                "error_memory_enabled": error_memory_enabled,
                "error_memory_storage_source": error_memory_storage_source,
                "error_memory_load_limit": error_memory_load_limit,
                "error_memory_signature_min_hits": error_memory_signature_min_hits,
                "error_memory_loaded_total": error_memory_loaded_total,
                "error_memory_signals_total": error_memory_signals_total,
                "error_memory_hits_total": error_memory_hits_total,
                "error_memory_fix_applied_total": error_memory_fix_applied_total,
                "error_memory_persisted_total": error_memory_persisted_total,
                "error_memory_persist_failures_total": error_memory_persist_failures_total,
                "error_memory_family_counts": error_memory_family_counts,
                "error_memory_fix_rule_counts": error_memory_fix_rule_counts,
                "article_core_filtered_units_total": article_core_filtered_units_total,
                "promotion_reviewed_to_validate_total": promotion_reviewed_to_validate_total,
                "promoted_to_draft_total": promoted_to_draft_total,
                "promotion_blocked_policy_total": promotion_blocked_policy_total,
                "promotion_blocked_type_total": promotion_blocked_type_total,
                "promotion_blocked_low_conf_total": promotion_blocked_low_conf_total,
                "promotion_blocked_short_snippet_total": promotion_blocked_short_snippet_total,
                "promotion_blocked_low_overlap_total": promotion_blocked_low_overlap_total,
                "promotion_blocked_long_req_total": promotion_blocked_long_req_total,
                "promotion_blocked_long_snippet_total": promotion_blocked_long_snippet_total,
                "promotion_blocked_modal_peut_total": promotion_blocked_modal_peut_total,
                "promotion_blocked_noisy_start_total": promotion_blocked_noisy_start_total,
                "promotion_blocked_quality_total": promotion_blocked_quality_total,
                "promotion_blocked_grounding_total": promotion_blocked_grounding_total,
                "promotion_blocked_completeness_total": promotion_blocked_completeness_total,
                "promotion_blocked_subject_total": promotion_blocked_subject_total,
                "promotion_blocked_postcall_total": promotion_blocked_postcall_total,
                "promotion_blocked_fused_total": promotion_blocked_fused_total,
                "promotion_blocked_missing_component_total": promotion_blocked_missing_component_total,
                "promotion_blocked_postcall_type_total": promotion_blocked_postcall_type_total,
                "promotion_blocked_error_memory_total": promotion_blocked_error_memory_total,
                "promotion_overlap_avg_promoted": round(
                    (promotion_overlap_sum / promoted_to_draft_total) if promoted_to_draft_total else 0.0,
                    4,
                ),
            }

            event_inserted = safe_insert_event(
                cur=cur,
                tenant_id=tenant_id,
                doc_id=args.doc_id,
                event_type="REQUIREMENTS_EXTRACTED",
                payload=event_payload,
            )

            print("\n===== A1 Extraction Summary =====", flush=True)
            print(f"Document          : {doc_title}", flush=True)
            print(f"Chunks OK         : {chunk_ok}", flush=True)
            print(f"Chunks empty      : {chunk_empty}", flush=True)
            print(f"Chunks error      : {chunk_error}", flush=True)
            print(f"Doc gate policy   : {doc_gate.policy}", flush=True)
            print(f"Doc gate type     : {doc_gate.document_type}", flush=True)
            print(f"Doc gate reasons  : {list(doc_gate.reason_codes)}", flush=True)
            print(f"LLM raw outputs   : {raw_llm_requirements}", flush=True)
            print(f"Inserted          : {inserted_requirements_count}", flush=True)
            print(f"QSE domains       : {qse_domain_counts}", flush=True)
            print(f"QSE sub-domains   : {qse_sub_domain_counts}", flush=True)
            print(f"QSE map strategy  : {qse_mapping_strategy_counts}", flush=True)
            print(f"Rejected low value: {rejected_low_value}", flush=True)
            print(f"Rejected fragment.: {rejected_fragmentary}", flush=True)
            print(f"Rejected mismatch : {rejected_mismatch}", flush=True)
            print(f"Rejected scope    : {rejected_scope}", flush=True)
            print(f"Rejected except.  : {rejected_partial_exception}", flush=True)
            print(f"Rejected invented : {rejected_non_normative}", flush=True)
            print(f"Rejected oos      : {rejected_out_of_scope}", flush=True)
            print(f"Deduplicated      : {deduplicated}", flush=True)
            print(f"Dedup article-ex  : {deduplicated_exact_article}", flush=True)
            print(f"Dedup doc-exact   : {deduplicated_exact_doc}", flush=True)
            print(f"Dedup article-pre : {deduplicated_prefix_article}", flush=True)
            print(f"Dedup doc-pre     : {deduplicated_prefix_doc}", flush=True)
            print(f"Long safety chunks: {long_article_safety_chunks_total}", flush=True)
            print(f"Long safety forced: {long_article_safety_forced_units_total}", flush=True)
            print(f"Precall mode      : {runtime_precall_mode}", flush=True)
            print(f"Precall chunks    : {precall_runtime_chunks_total}", flush=True)
            print(f"Precall fallback  : {precall_runtime_fallback_chunks_total}", flush=True)
            print(f"Precall HIGH/LOW  : {precall_runtime_units_high_total}/{precall_runtime_units_low_total}", flush=True)
            print(f"Precall DROP seen : {precall_runtime_units_drop_total}", flush=True)
            print(f"Precall DROP hard : {precall_runtime_drop_hard_total}", flush=True)
            print(f"Precall DROP shdw : {precall_runtime_drop_shadow_total}", flush=True)
            print(f"Runtime dedup     : {runtime_unit_deduped_total}", flush=True)
            print(f"Runtime run-cache : {runtime_unit_cache_hits_total}/{runtime_unit_cache_store_total}", flush=True)
            print(f"Budget block/fb   : {runtime_budget_blocked_units_total}/{runtime_budget_fallback_units_total}", flush=True)
            print(f"Empty LLM fallback: {empty_llm_fallback_generated_total}", flush=True)
            print(f"Long safety backup: {long_article_safety_full_chunk_fallback_total}", flush=True)
            print(f"Softened rejects  : {softened_filter_rejections_total}", flush=True)
            print(
                "LLM calls/tokens  : "
                f"{llm_usage_totals['llm_calls']}/{llm_usage_totals['prompt_tokens']}/"
                f"{llm_usage_totals['completion_tokens']}/{llm_usage_totals['total_tokens']}",
                flush=True,
            )
            print(f"LLM est. cost USD : {llm_usage_totals['estimated_cost_usd']}", flush=True)
            print(
                "LLM cache hits    : "
                f"{llm_cache_stats['cache_hits_total']} "
                f"(strict={llm_cache_stats['cache_hits_strict_total']}, "
                f"relaxed={llm_cache_stats['cache_hits_relaxed_total']})",
                flush=True,
            )
            print(f"Err mem source    : {error_memory_storage_source}", flush=True)
            print(f"Err mem loaded    : {error_memory_loaded_total}", flush=True)
            print(f"Err mem signals   : {error_memory_signals_total}", flush=True)
            print(f"Err mem hits      : {error_memory_hits_total}", flush=True)
            print(f"Err mem fixes     : {error_memory_fix_applied_total}", flush=True)
            print(f"Err mem persisted : {error_memory_persisted_total}", flush=True)
            print(f"Err mem per.fail  : {error_memory_persist_failures_total}", flush=True)
            print(f"Err mem families  : {error_memory_family_counts}", flush=True)
            print(f"Err mem rules     : {error_memory_fix_rule_counts}", flush=True)
            print(f"Article core skip : {article_core_filtered_units_total}", flush=True)
            print(f"Article policy cnt: {article_policy_counts}", flush=True)
            print(f"Article reason cnt: {article_policy_reason_counts}", flush=True)
            print(f"Article drop rsn  : {article_drop_reason_counts}", flush=True)
            print(f"Article dropped   : {article_dropped_by_policy_total}", flush=True)
            print(f"Units drop limited: {units_dropped_limited_policy_total}", flush=True)
            print(f"Limited forced    : {limited_policy_forced_units_total}", flush=True)
            print(f"Limited data arts : {len(limited_data_articles_seen)}", flush=True)
            print(f"Limited data objs : {limited_data_objects_total}", flush=True)
            print(f"Limited data types: {limited_data_object_type_counts}", flush=True)
            print(f"Policy forced TV  : {policy_forced_to_validate_total}", flush=True)
            print(f"Promo reviewed TV : {promotion_reviewed_to_validate_total}", flush=True)
            print(f"Promo to DRAFT    : {promoted_to_draft_total}", flush=True)
            print(f"Promo block policy: {promotion_blocked_policy_total}", flush=True)
            print(f"Promo block type  : {promotion_blocked_type_total}", flush=True)
            print(f"Promo block conf  : {promotion_blocked_low_conf_total}", flush=True)
            print(f"Promo block snip  : {promotion_blocked_short_snippet_total}", flush=True)
            print(f"Promo block overlap: {promotion_blocked_low_overlap_total}", flush=True)
            print(f"Promo block reqlen: {promotion_blocked_long_req_total}", flush=True)
            print(f"Promo block sniplen: {promotion_blocked_long_snippet_total}", flush=True)
            print(f"Promo block modal : {promotion_blocked_modal_peut_total}", flush=True)
            print(f"Promo block noisy : {promotion_blocked_noisy_start_total}", flush=True)
            print(f"Promo block qual  : {promotion_blocked_quality_total}", flush=True)
            print(f"Promo block ground: {promotion_blocked_grounding_total}", flush=True)
            print(f"Promo block compl : {promotion_blocked_completeness_total}", flush=True)
            print(f"Promo block subj  : {promotion_blocked_subject_total}", flush=True)
            print(f"Promo block pcall : {promotion_blocked_postcall_total}", flush=True)
            print(f"Promo block fused : {promotion_blocked_fused_total}", flush=True)
            print(f"Promo block miss  : {promotion_blocked_missing_component_total}", flush=True)
            print(f"Promo block ptype : {promotion_blocked_postcall_type_total}", flush=True)
            print(f"Promo block mem   : {promotion_blocked_error_memory_total}", flush=True)
            print(f"Providers used    : {sorted(providers_used)}", flush=True)
            print(f"Models used       : {sorted(models_used)}", flush=True)
            print(f"Fallback used     : {run_used_fallback}", flush=True)
            print(f"Event inserted    : {event_inserted}", flush=True)


if __name__ == "__main__":
    main()
