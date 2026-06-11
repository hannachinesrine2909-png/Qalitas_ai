from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


POLICY_EXTRACT_FULL = "EXTRACT_FULL"
POLICY_EXTRACT_LIMITED = "EXTRACT_LIMITED"
POLICY_EXTRACT_LIMITED_DATA = "EXTRACT_LIMITED_DATA"
POLICY_DROP = "DROP"
POLICY_TO_VALIDATE_SOURCE_MISSING = "TO_VALIDATE_SOURCE_MISSING"

_VALID_POLICIES = {
    POLICY_EXTRACT_FULL,
    POLICY_EXTRACT_LIMITED,
    POLICY_EXTRACT_LIMITED_DATA,
    POLICY_DROP,
    POLICY_TO_VALIDATE_SOURCE_MISSING,
}

_SUMMARY_NOTICE_RE = re.compile(
    r"(?i)\b("
    r"sommaire|table\s+des\s+mati[eè]res|"
    r"avis\s+et\s+communications?|"
    r"situation\s+g[ée]n[ée]rale|"
    r"index\s+[ée]ditorial|"
    r"communiqu[ée]\s+officiel"
    r")\b"
)
_SUMMARY_ARTICLE_LABEL_RE = re.compile(
    r"(?i)\b("
    r"sommaire|avis\s+et\s+communications?|"
    r"table\s+des\s+mati[eè]res|index|"
    r"communiqu[ée]s?"
    r")\b"
)
_SUMMARY_LAYOUT_RE = re.compile(
    r"(?im)^\s*(?:-|\*|•)?\s*[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][^\n]{8,120}\s+\.{2,}\s*\d{1,4}\s*$"
)
_ARABIC_ONLY_RE = re.compile(
    r"(?i)\b("
    r"publi[ée]\s+uniquement\s+en\s+langue\s+arabe|"
    r"publi[ée]\s+uniquement\s+en\s+arabe|"
    r"texte\s+publi[ée]\s+en\s+arabe|"
    r"en\s+langue\s+arabe\s+uniquement|"
    r"uniquement\s+en\s+arabe"
    r")\b"
)
_STRONG_REGULATORY_TITLE_RE = re.compile(
    r"(?i)\b("
    r"code\s+du\s+travail|code\s+du\s+commerce|"
    r"loi(?:\s+n(?:[°o?]|â°)?\s*)?\d{1,4}(?:[-_/]\d{1,4})+|"
    r"d[ée]cret(?:-loi)?(?:\s+n(?:[°o?]|â°)?\s*)?\d{1,4}(?:[-_/]\d{1,4})+|"
    r"arr[êe]t[ée](?:\s+n(?:[°o?]|â°)?\s*)?\d{1,4}(?:[-_/]\d{1,4})+|"
    r"l\d{2,4}[-_]\d{1,4}\b|"
    r"d[_-]\d{2,4}[-_]\d{1,4}\b"
    r")\b"
)
_REGULATORY_TITLE_RE = re.compile(
    r"(?i)\b("
    r"code\s+du\s+travail|code\s+du\s+commerce|"
    r"loi\s+n[°o]|d[ée]cret(?:-loi)?(?:\s+n[°o])?|"
    r"arr[êe]t[ée]\s+fixant|fixant\s+les\s+modalit[ée]s|"
    r"cahier\s+des\s+charges|conditions?\s+d['’]application|"
    r"contributions?\s+tarifaires?|"
    r"obligations?\s+de|registre|proc[ée]dure"
    r")\b"
)
_CONCOURS_RE = re.compile(
    r"(?i)\b("
    r"concours|examen|candidature|jury|"
    r"date\s+de\s+cl[ôo]ture|nombre\s+de\s+postes?"
    r")\b"
)
_COMMISSION_RE = re.compile(
    r"(?i)\b("
    r"commission|quorum|proc[eè]s-verbal|inventaire|"
    r"conseil\s+d['’]administration|composition\s+de\s+la\s+commission"
    r")\b"
)
_ANNEX_DATA_RE = re.compile(
    r"(?i)\b("
    r"annexe|tableau|grille|tarifs?|bar[eè]me|"
    r"liste\s+des\s+postes|liste\s+des\s+sp[ée]cialit[ée]s|quotas?"
    r")\b"
)
_INDIVIDUAL_MARKER_RE = re.compile(
    r"(?i)\b("
    r"(?:est|sont)\s+(?:nomm[ée]e?s?|d[ée]sign[ée]e?s?)|"
    r"(?:est|sont)\s+charg[ée]e?s?\s+des\s+fonctions\s+de|"
    r"mise?\s+fin\s+aux?\s+fonctions|"
    r"cessation\s+de\s+fonctions|"
    r"promotion|r[ée]vocation|d[ée]mission|"
    r"d[ée]l[ée]gation\s+de\s+signature"
    r")\b"
)
_NAMED_PERSON_RE = re.compile(
    r"(?i)\b(?:monsieur|madame|mme\.?|m\.)\s+[a-zàâäéèêëîïôöùûüç][a-zàâäéèêëîïôöùûüç'\-]{1,}"
)
_INSTITUTIONAL_SUBJECT_RE = re.compile(
    r"(?i)\b("
    r"jury|commission|candidat(?:s)?|ministre|institut|agence|"
    r"conseil|direction|secr[ée]tariat|administration"
    r")\b"
)
_STRONG_INDIVIDUAL_CONTEXT_RE = re.compile(
    r"(?i)\b("
    r"en\s+remplacement\s+de|"
    r"mis(?:e)?\s+fin\s+aux?\s+fonctions|"
    r"est\s+nomm[ée]e?\s+directeur|"
    r"acceptation\s+de\s+d[ée]mission"
    r")\b"
)
_NORMATIVE_CORE_RE = re.compile(
    r"(?i)\b("
    r"doit|doivent|est\s+tenu(?:e|s|es)?(?:\s+de|\b)|"
    r"interdit(?:e|es|s)?|en\s+cas\s+de|lorsque|"
    r"sous\s+r[ée]serve\s+de|[àa]\s+condition\s+de|"
    r"est\s+fix[ée]e?|sont\s+fix[ée]e?s?|"
    r"est\s+rejet[ée]e?|sont\s+rejet[ée]e?s?"
    r")\b"
)
_STRONG_NORMATIVE_ARTICLE_RE = re.compile(
    r"(?i)\b("
    r"doit|doivent|est\s+tenu(?:e|s|es)?(?:\s+de|\b)|"
    r"sont\s+tenus?\s+de|interdit(?:e|es|s)?|"
    r"ne\s+doit(?:vent)?\s+pas|ne\s+peu(?:t|vent)\s+pas|"
    r"communiqu\w+|transmett?\w+|adress\w+|notifi\w+|"
    r"présent\w+|present\w+|tenir\s+un\s+registre|registre"
    r")\b"
)
_STANDARD_ARTICLE_LABEL_RE = re.compile(
    r"(?i)^\s*(?:art\.?|article)\s*(?:premier|1er|unique|\d+(?:[-.]\d+)*)\b"
)
_LIMITED_ACTIONABLE_CUES_RE = re.compile(
    r"(?i)\b("
    r"est\s+charg[ée]?\s+de|sont\s+charg[ée]s?\s+de|"
    r"proposer|[ée]valuer|classer|attribuer|d[ée]cerner|"
    r"est\s+fix[ée]e?|sont\s+fix[ée]e?s?|"
    r"nombre\s+de\s+postes?|date\s+de\s+cl[ôo]ture|"
    r"anciennet[ée]|priorit[ée]|pi[eè]ces?\s+suivantes?"
    r")\b"
)
_ARTICLE_META_SCOPE_RE = re.compile(
    r"(?i)\ble\s+pr[éee]sent\s+(?:arr[êe]t[ée]?|d[ée]cret|code|loi)\s+"
    r"(?:fixe|d[ée]finit|d[ée]termine|pr[ée]cise)\b|"
    r"\bfixe\s+les\s+conditions\s+d['’]application\b"
)


def _normalize(text: str) -> str:
    value = str(text or "").strip()
    value = (
        value.replace("Â°", "°")
        .replace("â€™", "'")
        .replace("’", "'")
        .replace("–", "-")
        .replace("—", "-")
        .replace("_", " ")
    )
    return re.sub(r"\s+", " ", value)


def _count_hits(pattern: re.Pattern[str], text: str) -> int:
    return len(pattern.findall(text or ""))


def _as_reason_tuple(values: Iterable[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    for v in values:
        item = str(v or "").strip().upper()
        if item and item not in ordered:
            ordered.append(item)
    return tuple(ordered)


@dataclass(frozen=True)
class QualificationDecision:
    policy: str
    document_type: str
    confidence: float
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "policy": self.policy,
            "document_type": self.document_type,
            "confidence": round(float(self.confidence), 4),
            "reason_codes": list(self.reason_codes),
        }


def _decision(policy: str, document_type: str, confidence: float, reasons: Iterable[str]) -> QualificationDecision:
    chosen = str(policy or "").strip().upper()
    if chosen not in _VALID_POLICIES:
        chosen = POLICY_EXTRACT_FULL
    return QualificationDecision(
        policy=chosen,
        document_type=str(document_type or "UNKNOWN").strip().upper() or "UNKNOWN",
        confidence=max(0.0, min(1.0, float(confidence))),
        reason_codes=_as_reason_tuple(reasons),
    )


def classify_document_policy(
    *,
    doc_title: str,
    doc_text_preview: str = "",
    article_count: int = 0,
) -> QualificationDecision:
    title = _normalize(doc_title).lower()
    preview = _normalize(doc_text_preview).lower()
    combined = _normalize(f"{title} {preview}").lower()
    reasons: list[str] = []

    if _SUMMARY_NOTICE_RE.search(combined):
        reasons.append("DOC_SUMMARY_NOTICE")
        return _decision(POLICY_DROP, "SUMMARY_NOTICE", 0.99, reasons)

    if _ARABIC_ONLY_RE.search(combined):
        reasons.append("DOC_SOURCE_ARABIC_ONLY")
        return _decision(POLICY_TO_VALIDATE_SOURCE_MISSING, "SOURCE_MISSING", 0.99, reasons)

    if _STRONG_REGULATORY_TITLE_RE.search(title):
        reasons.append("DOC_STRONG_REGULATORY_TITLE")
        return _decision(POLICY_EXTRACT_FULL, "REGULATORY_ACT", 0.93, reasons)

    individual_hits = _count_hits(_INDIVIDUAL_MARKER_RE, combined)
    person_hits = _count_hits(_NAMED_PERSON_RE, combined)
    normative_hits = _count_hits(_NORMATIVE_CORE_RE, combined)

    # Long legal acts with multiple normative cues should not be downgraded to limited
    if article_count >= 20 and normative_hits >= 2:
        reasons.append("DOC_LONG_NORMATIVE_ACT")
        return _decision(POLICY_EXTRACT_FULL, "REGULATORY_ACT", 0.82, reasons)

    institutional_hits = _count_hits(_INSTITUTIONAL_SUBJECT_RE, combined)
    if (
        individual_hits >= 1
        and person_hits >= 1
        and normative_hits <= 1
        and institutional_hits == 0
    ):
        reasons.extend(["DOC_INDIVIDUAL_ACT", "DOC_NAMED_PERSON"])
        return _decision(POLICY_DROP, "INDIVIDUAL_ACT", 0.92, reasons)
    if (
        individual_hits >= 2
        and normative_hits == 0
        and institutional_hits == 0
    ):
        reasons.append("DOC_INDIVIDUAL_ACT")
        return _decision(POLICY_DROP, "INDIVIDUAL_ACT", 0.88, reasons)

    if _ANNEX_DATA_RE.search(combined) and normative_hits <= 1 and article_count <= 10:
        reasons.append("DOC_ANNEX_TABLE_DATA")
        return _decision(POLICY_EXTRACT_LIMITED_DATA, "ANNEX_DATA", 0.84, reasons)

    if _REGULATORY_TITLE_RE.search(combined):
        reasons.append("DOC_REGULATORY_TITLE")
        return _decision(POLICY_EXTRACT_FULL, "REGULATORY_ACT", 0.86, reasons)

    if _CONCOURS_RE.search(combined):
        reasons.append("DOC_CONCOURS_PROCEDURAL")
        return _decision(POLICY_EXTRACT_LIMITED, "PROCEDURAL_ACT", 0.74, reasons)

    if _COMMISSION_RE.search(combined):
        reasons.append("DOC_COMMISSION_GOVERNANCE")
        return _decision(POLICY_EXTRACT_LIMITED, "GOVERNANCE_ACT", 0.71, reasons)

    if article_count > 0 and normative_hits > 0:
        reasons.append("DOC_DEFAULT_NORMATIVE")
        return _decision(POLICY_EXTRACT_FULL, "REGULATORY_ACT", 0.68, reasons)

    reasons.append("DOC_DEFAULT_CONSERVATIVE")
    return _decision(POLICY_EXTRACT_FULL, "UNKNOWN", 0.56, reasons)


def classify_article_policy(
    *,
    doc_title: str,
    article_label: str,
    article_text: str,
    document_policy: str = POLICY_EXTRACT_FULL,
) -> QualificationDecision:
    label = _normalize(article_label).lower()
    text = _normalize(article_text).lower()
    title = _normalize(doc_title).lower()
    preview = text[:2400]
    combined = _normalize(f"{title} {label} {preview}").lower()
    reasons: list[str] = []
    individual_hits = _count_hits(_INDIVIDUAL_MARKER_RE, combined)
    person_hits = _count_hits(_NAMED_PERSON_RE, combined)
    normative_hits = _count_hits(_NORMATIVE_CORE_RE, combined)
    strong_normative_hits = _count_hits(_STRONG_NORMATIVE_ARTICLE_RE, combined)
    institutional_hits = _count_hits(_INSTITUTIONAL_SUBJECT_RE, combined)
    strong_individual_context = bool(_STRONG_INDIVIDUAL_CONTEXT_RE.search(combined))
    standard_article_label = bool(_STANDARD_ARTICLE_LABEL_RE.search(label))

    if _SUMMARY_NOTICE_RE.search(combined):
        reasons.append("ARTICLE_SUMMARY_NOTICE")
        return _decision(POLICY_DROP, "SUMMARY_NOTICE", 0.99, reasons)
    if _SUMMARY_ARTICLE_LABEL_RE.search(label):
        reasons.append("ARTICLE_SUMMARY_NOTICE")
        return _decision(POLICY_DROP, "SUMMARY_NOTICE", 0.99, reasons)
    if _SUMMARY_LAYOUT_RE.search(article_text or "") and normative_hits == 0:
        reasons.append("ARTICLE_SUMMARY_NOTICE")
        return _decision(POLICY_DROP, "SUMMARY_NOTICE", 0.97, reasons)

    if _ARABIC_ONLY_RE.search(combined):
        reasons.append("ARTICLE_SOURCE_ARABIC_ONLY")
        return _decision(POLICY_TO_VALIDATE_SOURCE_MISSING, "SOURCE_MISSING", 0.99, reasons)
    if (
        _ARTICLE_META_SCOPE_RE.search(combined)
        and normative_hits == 0
        and strong_normative_hits == 0
    ):
        reasons.append("ARTICLE_META_SCOPE_ONLY")
        return _decision(POLICY_DROP, "META_SCOPE_ARTICLE", 0.9, reasons)
    if (
        person_hits >= 1
        and strong_individual_context
        and institutional_hits == 0
    ):
        reasons.extend(["ARTICLE_INDIVIDUAL_ACT", "ARTICLE_NAMED_PERSON"])
        return _decision(POLICY_DROP, "INDIVIDUAL_ACT", 0.96, reasons)
    if (
        strong_individual_context
        and normative_hits <= 1
        and institutional_hits == 0
    ):
        reasons.append("ARTICLE_INDIVIDUAL_ACT")
        return _decision(POLICY_DROP, "INDIVIDUAL_ACT", 0.92, reasons)
    if (
        individual_hits >= 1
        and person_hits >= 1
        and normative_hits <= 2
        and institutional_hits == 0
    ):
        reasons.extend(["ARTICLE_INDIVIDUAL_ACT", "ARTICLE_NAMED_PERSON"])
        return _decision(POLICY_DROP, "INDIVIDUAL_ACT", 0.95, reasons)
    if (
        individual_hits >= 2
        and normative_hits == 0
        and institutional_hits == 0
    ):
        reasons.append("ARTICLE_INDIVIDUAL_ACT")
        return _decision(POLICY_DROP, "INDIVIDUAL_ACT", 0.9, reasons)

    if _ANNEX_DATA_RE.search(combined) and normative_hits == 0:
        reasons.append("ARTICLE_ANNEX_TABLE_DATA")
        return _decision(POLICY_EXTRACT_LIMITED_DATA, "ANNEX_DATA", 0.87, reasons)
    if _ANNEX_DATA_RE.search(combined) and normative_hits > 0:
        if (
            standard_article_label
            and (
                strong_normative_hits >= 1
                or normative_hits >= 2
                or institutional_hits >= 1
            )
        ):
            reasons.append("ARTICLE_ANNEX_NORMATIVE_OVERRIDE")
            return _decision(POLICY_EXTRACT_FULL, "NORMATIVE_ARTICLE", 0.76, reasons)
        reasons.append("ARTICLE_ANNEX_MIXED_NORMATIVE")
        return _decision(POLICY_EXTRACT_LIMITED, "ANNEX_DATA_MIXED", 0.72, reasons)

    if _CONCOURS_RE.search(combined):
        if normative_hits >= 2:
            reasons.append("ARTICLE_CONCOURS_NORMATIVE")
            return _decision(POLICY_EXTRACT_FULL, "NORMATIVE_ARTICLE", 0.73, reasons)
        reasons.append("ARTICLE_CONCOURS_PROCEDURAL")
        return _decision(POLICY_EXTRACT_LIMITED, "PROCEDURAL_ACT", 0.7, reasons)

    if _COMMISSION_RE.search(combined) and normative_hits == 0:
        reasons.append("ARTICLE_COMMISSION_GOVERNANCE")
        return _decision(POLICY_EXTRACT_LIMITED, "GOVERNANCE_ACT", 0.72, reasons)
    if _COMMISSION_RE.search(combined) and normative_hits >= 1:
        reasons.append("ARTICLE_COMMISSION_NORMATIVE")
        return _decision(POLICY_EXTRACT_FULL, "NORMATIVE_ARTICLE", 0.71, reasons)

    if normative_hits > 0:
        reasons.append("ARTICLE_NORMATIVE")
        return _decision(POLICY_EXTRACT_FULL, "NORMATIVE_ARTICLE", 0.74, reasons)

    if str(document_policy or "").strip().upper() == POLICY_EXTRACT_LIMITED:
        if len(preview) >= 700:
            reasons.append("ARTICLE_INHERIT_DOC_LIMITED_SOFT")
            return _decision(POLICY_EXTRACT_FULL, "SOFT_FULL_ARTICLE", 0.59, reasons)
        reasons.append("ARTICLE_INHERIT_DOC_LIMITED")
        return _decision(POLICY_EXTRACT_LIMITED, "PROCEDURAL_ACT", 0.61, reasons)
    if str(document_policy or "").strip().upper() == POLICY_EXTRACT_LIMITED_DATA:
        reasons.append("ARTICLE_INHERIT_DOC_DATA")
        return _decision(POLICY_EXTRACT_LIMITED_DATA, "ANNEX_DATA", 0.61, reasons)

    reasons.append("ARTICLE_DEFAULT_FULL")
    return _decision(POLICY_EXTRACT_FULL, "UNKNOWN", 0.55, reasons)


def is_policy_extractible(policy: str) -> bool:
    p = str(policy or "").strip().upper()
    return p in {POLICY_EXTRACT_FULL, POLICY_EXTRACT_LIMITED, POLICY_EXTRACT_LIMITED_DATA}


def should_skip_standard_extraction(policy: str) -> bool:
    p = str(policy or "").strip().upper()
    return p in {
        POLICY_DROP,
        POLICY_TO_VALIDATE_SOURCE_MISSING,
        POLICY_EXTRACT_LIMITED_DATA,
    }


def limited_unit_is_actionable(unit_text: str) -> bool:
    normalized = _normalize(unit_text).lower()
    if _NORMATIVE_CORE_RE.search(normalized):
        return True
    if _LIMITED_ACTIONABLE_CUES_RE.search(normalized):
        return True
    if ":" in normalized and any(c in normalized for c in ("- ", "•", ";")):
        return True
    return False


def force_to_validate_by_policy(policy: str, status: str) -> str:
    p = str(policy or "").strip().upper()
    current = str(status or "").strip().upper() or "TO_VALIDATE"
    if p in {POLICY_EXTRACT_LIMITED, POLICY_EXTRACT_LIMITED_DATA} and current == "DRAFT":
        return "TO_VALIDATE"
    return current
