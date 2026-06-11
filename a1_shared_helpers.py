import re
import html
from typing import Optional

_PUBLICATION_BOILERPLATE_RE = re.compile(
    r"(?i)\b(?:l[‘’]|le|la)\s+pr[ée]sent(?:e)?\s+(?:arr[êe]t[ée]?(?:\s+conjoint)?|d[ée]cret(?:-loi)?|loi)\s+sera\s+"
    r"(?:publi[ée]e?|ex[ée]cut[ée]e?)\b"
)
_INDIVIDUAL_ACT_NAME_RE = re.compile(
    r"(?i)\b(?:monsieur|madame|m\.|mme\.?)\s+[a-zàâäéèêëîïôöùûüç][a-zàâäéèêëîïôöùûüç'\-]{1,}"
)
_INDIVIDUAL_ACT_MARKERS = [
    "est nommé",
    "sont nommés",
    "est nommee",
    "sont nommes",
    "est désigné",
    "sont désignés",
    "est designe",
    "sont designes",
    "chargé des fonctions de",
    "chargee des fonctions de",
    "par délégation",
    "par delegation",
    "est habilité à signer",
    "est habilite a signer",
    "délégation de signature",
    "delegation de signature",
    "acceptation de démission",
    "acceptation de demission",
    "délégation de signature accordée à monsieur",
    "délégation de signature accordée à madame",
    "delegation de signature accordee a monsieur",
    "delegation de signature accordee a madame",
]

_OCR_HARD_BREAK_RE = re.compile(
    r"(?i)\s+(?=(?:"
    r"article\s+(?:premier|1er|unique|\d+)(?:\s*\(?\s*(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies|undecies|duodecies)\s*\)?)?\s*[-:–—]"
    r"|art\.?\s*\d+(?:[-\.]\d+)*(?:\s*\(?\s*(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies|undecies|duodecies)\s*\)?)?\s*[-:–—]"
    r"|tunis,\s*le\b"
    r"|le\s+ministre\b"
    r"|la\s+ministre\b"
    r"|la\s+cheffe\s+du\s+gouvernement\b"
    r"|monsieur\s+[a-zàâäéèêëîïôöùûüç'’-]+"
    r"|madame\s+[a-zàâäéèêëîïôöùûüç'’-]+"
    r"|n[°o]\s*\d+\b.*journal\s+officiel"
    r"|page\s+\d+\b.*journal\s+officiel"
    r"))"
)
_PAGE_HEADER_LINE_RE = re.compile(
    r"(?i)^\s*(?:n[°o]\s*\d+\b.*journal\s+officiel|page\s+\d+\b.*journal\s+officiel)\s*$"
)
_SIGNATURE_LINE_RE = re.compile(
    r"(?i)^\s*(?:tunis,\s*le\b|le\s+ministre\b|la\s+ministre\b|la\s+cheffe\s+du\s+gouvernement\b|arr[êe]tent\s*:|arr[êe]te\s*:|vu\b)\s*"
)
_PERSON_PREFIX_LINE_RE = re.compile(r"(?i)^\s*(?:monsieur|madame|mme\.?|m\.)\b")
_TITLECASE_NAME_LINE_RE = re.compile(
    r"^(?:[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,})(?:\s+[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,}){1,3}$"
)
_INLINE_SIGNATORY_SEGMENT_RE = re.compile(
    r"(?i)\b(?:le|la)\s+ministre\b[^.;:\n]{0,120}\b(?:[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,}\s+){1,3}[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,}\b"
)
_INLINE_GOV_SEGMENT_RE = re.compile(
    r"(?i)\bla\s+cheffe\s+du\s+gouvernement\b[^.;:\n]{0,100}\b(?:[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,}\s+){1,3}[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,}\b"
)
_INLINE_PERSON_SEGMENT_RE = re.compile(
    r"(?i)\b(?:monsieur|madame|mme\.?|m\.)\s+[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,}"
    r"(?:\s+[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,}){0,3}[^.;:\n]{0,120}"
)
_INLINE_NAME_BETWEEN_ARTICLES_RE = re.compile(
    r"(?i)\b(par\s+les\s+)(?:[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,}\s+){1,4}(articles?\b)"
)
_INLINE_NAME_BETWEEN_CREDITS_RE = re.compile(
    r"(?i)\b(cr[ée]dits?\s+)(?:[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,}\s+){1,4}(de\s+campagne\b)"
)
_INLINE_ORPHAN_VU_BEFORE_SUSVISE_RE = re.compile(r"(?i)\bvu\s+(?=susvis[ée]e?\b)")
_INLINE_PROPER_NOUN_LIST_RE = re.compile(
    r"(?:\b[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,}"
    r"(?:\s+[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,}){0,2}\b,\s*){3,}"
    r"\b[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,}"
    r"(?:\s+[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ][a-zàâäéèêëîïôöùûüç'’-]{2,}){0,2}\b"
)
_INLINE_BUREAU_ADMIN_NOISE_RE = re.compile(
    r"(?i)\b(bureau\s+d['’]ordre)\s+"
    r"(?:(?!\bde\s+l['’]administration\b).){20,220}"
    r"\b(de\s+l['’]administration\b)"
)
_INLINE_INTERESTS_GOVERNORATES_NOISE_RE = re.compile(
    r"(?i)\b(prend\s+en\s+charge)\s+"
    r"(?:[^.;:\n]{1,80},\s*){2,}"
    r"[^.;:\n]{1,80}\s+et\s+"
    r"(?=\s*int[ée]r[êe]ts?)"
)
_TUNIS_DATE_RE = re.compile(
    r"(?i)\btunis,\s*le\s+\d{1,2}\s+[a-zàâäéèêëîïôöùûüç]+\s+\d{4}\b"
)
_NORMATIVE_KEEP_HINT_RE = re.compile(
    r"(?i)\b(?:doit(?:vent)?|est\s+tenu(?:e|s|es)?\s+de|interdit(?:e|es|s)?|"
    r"b[ée]n[ée]fici\w*|communiqu\w*|transmett\w*|adress\w*|rej(?:ete|eté|eter)\w*|"
    r"prend\s+en\s+charge|entra[iî]ne\w*|proc[eè]d\w*|redevien\w*|"
    r"fait\s+foi|fix(?:e|ent|ée?s?)\w*|sous\s+r[ée]serve\s+de|[àa]\s+condition\s+de)\b"
)


def normalize_spaces(text: str) -> str:
    text = (text or "").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


_OCR_ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]+")
_OCR_WORD_DIGIT_RE = re.compile(
    r"(?:(?<=[A-Za-zÀ-ÿ])\d+(?=[A-Za-zÀ-ÿ])|(?<=[A-Za-zÀ-ÿ]{3})\d+\b|\b\d+(?=[A-Za-zÀ-ÿ]{3}))"
)
_OCR_FUSED_MODAL_BOUNDARY_RE = re.compile(
    r"(?i)(?<=[a-z])"
    r"(?=(?:est\s+tenu(?:e|s|es)?(?:\s+de|\b)|sont\s+tenus?(?:\s+de|\b)|doit(?:vent)?\b|"
    r"interdit(?:e|es|s)?\b|peu(?:t|vent)\b|communiqu\w+\b|transmett?\w+\b|adress\w+\b))"
)
_OCR_HEADING_INLINE_NOISE_RE = re.compile(
    r"(?i)(?<=[a-z])\s+"
    r"(?:chapitre|section|titre|livre)\s+[ivxlcdm0-9]+\s*[-.:]?\s+(?=[a-z])"
)
_OCR_DUPLICATE_MODAL_PREFIX_RE = re.compile(r"(?i)\b(?:do|de|d)\s+(doit(?:vent)?)\b")
_OCR_ARTICLE_PREFIX_NOISE_RE = re.compile(r"(?i)^\s*(?:art\.?|article)\s*[-:'’\"]+\s*")
_LEADING_SNIPPET_NOISE_RE = re.compile(r"^[\]\[(){}:;,\.-]+\s*")
_OCR_MIDWORD_UPPER_RE = re.compile(r"\b[a-zàâäéèêëîïôöùûüç]{2,}[A-Z]{2,}[a-zàâäéèêëîïôöùûüç]+\b")
_OCR_DUPLICATED_TOKEN_RE = re.compile(r"(?i)\b([a-zàâäéèêëîïôöùûüç]{4,})\1\b")
_OCR_SUSPICIOUS_TOKEN_RE = re.compile(
    r"(?:"
    r"[A-Za-zÀ-ÿ]{3,}\d+"
    r"|"
    r"\d+[A-Za-zÀ-ÿ]{3,}"
    r"|"
    r"[a-zàâäéèêëîïôöùûüç]{2,}[A-Z]{2,}[a-zàâäéèêëîïôöùûüç]+"
    r"|"
    r"([A-Za-zÀ-ÿ]{4,})\1"
    r")"
)


def has_ocr_artifact_signals(text: str) -> bool:
    value = text or ""
    if not value.strip():
        return False
    return any(
        pattern.search(value)
        for pattern in (
            _OCR_ARABIC_CHAR_RE,
            _OCR_WORD_DIGIT_RE,
            _OCR_FUSED_MODAL_BOUNDARY_RE,
            _OCR_HEADING_INLINE_NOISE_RE,
            _OCR_DUPLICATE_MODAL_PREFIX_RE,
            _OCR_MIDWORD_UPPER_RE,
            _OCR_DUPLICATED_TOKEN_RE,
        )
    ) or bool(_LEADING_SNIPPET_NOISE_RE.search(value))


def ocr_artifact_score(text: str) -> float:
    value = normalize_spaces(text)
    if not value:
        return 0.0
    tokens = re.findall(r"[A-Za-zÀ-ÿ0-9']+", value)
    if not tokens:
        return 0.0
    suspicious = sum(1 for token in tokens if _OCR_SUSPICIOUS_TOKEN_RE.search(token))
    return round(suspicious / max(len(tokens), 1), 4)


def has_heavy_ocr_artifact_signals(text: str) -> bool:
    value = normalize_spaces(text)
    if not value:
        return False
    score = ocr_artifact_score(value)
    if score >= 0.18:
        return True
    return has_ocr_artifact_signals(value) and score >= 0.10


def repair_common_ocr_artifacts(text: str) -> str:
    value = normalize_spaces(text)
    if not value:
        return ""
    value = _OCR_ARABIC_CHAR_RE.sub(" ", value)
    value = _OCR_WORD_DIGIT_RE.sub("", value)
    value = _OCR_DUPLICATE_MODAL_PREFIX_RE.sub(r"\1", value)
    value = _OCR_DUPLICATED_TOKEN_RE.sub(r"\1", value)
    value = _OCR_HEADING_INLINE_NOISE_RE.sub(" ", value)
    value = _OCR_FUSED_MODAL_BOUNDARY_RE.sub(" ", value)
    value = _OCR_ARTICLE_PREFIX_NOISE_RE.sub("", value)
    value = _LEADING_SNIPPET_NOISE_RE.sub("", value)
    value = re.sub(r"\s+", " ", value).strip(" ,;:-")
    return value


_REQUIREMENT_LEADING_ARTICLE_RE = re.compile(
    r"(?i)^\s*(?:art\.?|article)(?:\s*(?:n\s*)?(?:premier|1er|unique|\d+(?:[-.]\d+)*)\s*)?\s*[-:'’\"]+\s*"
)
_REQUIREMENT_LEADING_PAGE_RE = re.compile(r"(?i)^\s*page\s+\d+\s*[-:–—]?\s*")
_REQUIREMENT_LEADING_JORT_META_RE = re.compile(
    r"(?i)^\s*(?:n[°o]\s*\d+\s*)?"
    r"journal\s+officiel\s+de\s+la\s+r[ée]publique\s+tunisienne\s*[-:–—]?\s*"
)


def strip_requirement_leading_noise(text: str) -> str:
    value = normalize_spaces(text)
    for _ in range(3):
        updated = _REQUIREMENT_LEADING_PAGE_RE.sub("", value)
        updated = _REQUIREMENT_LEADING_JORT_META_RE.sub("", updated)
        updated = _REQUIREMENT_LEADING_ARTICLE_RE.sub("", updated)
        updated = normalize_spaces(updated)
        if updated == value:
            break
        value = updated
    return value


def decode_html_entities(text: str) -> str:
    return html.unescape(normalize_spaces(text))


_OVERLAP_MARKER_RE = re.compile(r"\[\s*\.\.\.\s*\]")
_SNIPPET_CANDIDATE_SPLIT_RE = re.compile(r"(?<=[\.;:])\s+|\n+")
_SNIPPET_CONTINUATION_SPLIT_RE = re.compile(
    r",\s+(?=(?:(?:et|ou)\s+)?(?:sans\b|sauf\b|si\b|lorsque\b|en\s+cas\s+de\b|"
    r"à\s+condition\s+de\b|a\s+condition\s+de\b|sous\s+r[ée]serve\b|"
    r"ne\s+peu(?:t|vent)\b|doit(?:vent)?\b|est\s+tenu(?:e|s|es)?\s+de\b|"
    r"sont\s+tenus\s+de\b|peut\b|peuvent\b|communiqu\w+\b|transmett\w+\b|"
    r"adress\w+\b|conclu\w+\b|prend\s+en\s+charge\b|(?:est|sont)\s+présent\w+\b|"
    r"(?:est|sont)\s+fix\w+\b))"
)
_SNIPPET_INLINE_CONTINUATION_RE = re.compile(
    r"\s+(?=(?:et|ou)\s+(?:sans\b|sauf\b|si\b|lorsque\b|en\s+cas\s+de\b|"
    r"à\s+condition\s+de\b|a\s+condition\s+de\b|sous\s+r[ée]serve\b))"
)
_SNIPPET_INTRO_CUE_RE = re.compile(
    r"(?i)\b(?:sans|sauf|si\b|lorsque\b|en\s+cas\s+de\b|"
    r"à\s+condition\s+de\b|a\s+condition\s+de\b|sous\s+r[ée]serve\b)\b"
)
_SNIPPET_INTRO_MARKERS = [
    "doit",
    "doivent",
    "est tenu de",
    "est tenue de",
    "sont tenus de",
    "peut",
    "peuvent",
    "s'effectuer",
    "communique",
    "communiquent",
    "transmet",
    "transmettent",
    "adresse",
    "adressent",
    "conclut",
    "concluent",
    "prend en charge",
    "sont presentes",
    "sont présentées",
    "est presente",
    "est présentée",
]
_DANGLING_SNIPPET_END_RE = re.compile(
    r"(?i)\b(?:contre|par|pour|de|du|des|d['’]|au|aux|a|à|sur|sous|dans|avec|et|ou)$"
)


def clean_source_snippet(text: str) -> str:
    text = _OVERLAP_MARKER_RE.sub(" ", normalize_spaces(text))
    text = strip_requirement_leading_noise(text)
    text = repair_common_ocr_artifacts(text)
    text = re.sub(r"(?i)\bil\s+il\b", "il", text)
    text = re.sub(r"(?i)\belle\s+elle\b", "elle", text)
    text = _LEADING_SNIPPET_NOISE_RE.sub("", text)
    return text.strip()
def _extract_snippet_intro(sentence: str) -> str:
    cleaned = normalize_spaces(sentence).strip(" ,;:-")
    if not cleaned:
        return ""
    match = _SNIPPET_INTRO_CUE_RE.search(cleaned)
    if not match:
        return ""
    intro = cleaned[: match.start()].strip(" ,;:-")
    if len(intro) < 18:
        return ""
    if not contains_any(intro, _SNIPPET_INTRO_MARKERS):
        return ""
    return intro


def _merge_intro_with_clause(intro: str, clause: str) -> str:
    clause_text = normalize_spaces(clause).strip(" ,;:-")
    if not clause_text:
        return ""
    clause_text = re.sub(r"(?i)^(?:et|ou)\s+", "", clause_text).strip()
    if not intro:
        return clause_text
    if clause_text.lower().startswith(intro.lower()):
        return clause_text
    return f"{intro} {clause_text}".strip()


def _expand_snippet_candidates(text: str) -> list[str]:
    cleaned = clean_source_snippet(text)
    if not cleaned:
        return []

    def _expand_structural_clause_candidates(sentence: str) -> list[str]:
        structural_candidates: list[str] = []
        normalized_sentence = normalize_spaces(sentence).strip(" ,;:-")
        if not normalized_sentence:
            return []

        repeated_aux_match = _REPEATED_AUXILIARY_CHAIN_RE.match(normalized_sentence)
        if repeated_aux_match:
            subject = normalize_spaces(repeated_aux_match.group(1))
            aux_1 = normalize_spaces(repeated_aux_match.group(2))
            pred_1 = normalize_spaces(repeated_aux_match.group(3).strip(" .;"))
            aux_2 = normalize_spaces(repeated_aux_match.group(4))
            pred_2 = normalize_spaces(repeated_aux_match.group(5).strip(" .;"))
            if subject and pred_1 and pred_2 and pred_1.lower() != pred_2.lower():
                structural_candidates.extend(
                    [
                        f"{subject} {aux_1} {pred_1}.",
                        f"{subject} {aux_2} {pred_2}.",
                    ]
                )

        modal_chain_match = _MODAL_INFINITIVE_CHAIN_RE.match(normalized_sentence)
        if modal_chain_match:
            subject_modal = normalize_spaces(modal_chain_match.group(1))
            split_items = _split_modal_infinitive_chain(subject_modal, modal_chain_match.group(2))
            if len(split_items) >= 2:
                structural_candidates.extend(split_items)

        return _dedupe_split_requirements(structural_candidates)

    candidates: list[str] = [cleaned]
    sentence_parts = [
        normalize_spaces(part).strip(" ,;:-")
        for part in _SNIPPET_CANDIDATE_SPLIT_RE.split(cleaned)
        if normalize_spaces(part).strip(" ,;:-")
    ]
    for sentence in sentence_parts:
        candidates.append(sentence)
        candidates.extend(_expand_structural_clause_candidates(sentence))
        comma_parts = [
            normalize_spaces(part).strip(" ,;:-")
            for part in _SNIPPET_CONTINUATION_SPLIT_RE.split(sentence)
            if normalize_spaces(part).strip(" ,;:-")
        ]
        clause_parts: list[str] = []
        for part in comma_parts:
            inline_parts = [
                normalize_spaces(piece).strip(" ,;:-")
                for piece in _SNIPPET_INLINE_CONTINUATION_RE.split(part)
                if normalize_spaces(piece).strip(" ,;:-")
            ]
            clause_parts.extend(inline_parts or [part])
        intro = _extract_snippet_intro(clause_parts[0] if clause_parts else sentence)
        for idx, clause in enumerate(clause_parts):
            candidates.append(clause)
            candidates.extend(_expand_structural_clause_candidates(clause))
            if idx > 0 and intro:
                merged = _merge_intro_with_clause(intro, clause)
                if merged:
                    candidates.append(merged)
            if idx + 1 < len(clause_parts):
                joined = normalize_spaces(f"{clause}; {clause_parts[idx + 1]}").strip(" ,;:-")
                if joined:
                    candidates.append(joined)
    return candidates


def extract_best_citation_snippet(
    requirement_text: str,
    source_snippet: str,
    chunk_text: str = "",
    *,
    max_chars: int = 280,
) -> str:
    """
    Choisit un extrait court et bien ancre pour la citation:
    - privilegie la clause/sous-phrase la plus proche lexicalement de l'exigence
    - evite de stocker toute l'unite quand un seul segment suffit
    - nettoie les marqueurs de recouvrement de chunks
    """
    req = strip_requirement_leading_noise(normalize_spaces(requirement_text))
    src = clean_source_snippet(source_snippet)
    chunk = clean_source_snippet(chunk_text)

    if not src and not chunk:
        return ""

    req_lower = req.lower()
    need_condition = any(cue in req_lower for cue in _COND_OR_EXCEPTION_CUES)

    def _truncate(text: str) -> str:
        value = normalize_spaces(text).strip(" ,;:-")
        adaptive_max_chars = max_chars
        if len(req) >= int(max_chars * 0.82):
            adaptive_max_chars = min(420, max(max_chars + 40, len(req) + 32))
        if len(value) <= adaptive_max_chars:
            return value

        forward_candidates = [
            pos
            for pos in (
                value.find("; ", max_chars),
                value.find(". ", max_chars),
                value.find(": ", max_chars),
                value.find(", ", max_chars),
            )
            if pos > 0 and pos <= adaptive_max_chars
        ]
        if forward_candidates:
            return value[: min(forward_candidates)].strip(" ,;:-")

        cut_candidates = [
            value.rfind("; ", 0, adaptive_max_chars),
            value.rfind(". ", 0, adaptive_max_chars),
            value.rfind(": ", 0, adaptive_max_chars),
            value.rfind(", ", 0, adaptive_max_chars),
            value.rfind(" ", 0, adaptive_max_chars),
        ]
        cut_at = max(cut_candidates)
        if cut_at >= int(adaptive_max_chars * 0.55):
            return value[:cut_at].strip(" ,;:-")
        fallback = value[:adaptive_max_chars].rsplit(" ", 1)[0].strip(" ,;:-")
        return fallback or value[:adaptive_max_chars].strip(" ,;:-")

    def _candidate_score(text: str) -> float:
        candidate = normalize_spaces(text).strip(" ,;:-")
        if len(candidate) < 15:
            return -1.0
        score = _ground_overlap(req, candidate)
        cand_lower = candidate.lower()
        if need_condition and any(cue in cand_lower for cue in _COND_OR_EXCEPTION_CUES):
            score += 0.12
        if contains_any(cand_lower, ["doit", "doivent", "interdit", "sauf", "lorsque", "en cas de"]):
            score += 0.04
        if len(req) >= 40:
            length_ratio = len(candidate) / max(len(req), 1)
            if length_ratio > 1.8:
                score -= 0.05
            if length_ratio > 2.4:
                score -= 0.08
            if length_ratio < 0.35:
                score -= 0.04
        if _DANGLING_SNIPPET_END_RE.search(candidate):
            score -= 0.12
        if len(candidate) > max_chars:
            score -= 0.03
        return round(score, 4)

    candidates: list[str] = []
    for base in [src, chunk]:
        candidates.extend(_expand_snippet_candidates(base))

    seen: set[str] = set()
    ranked: list[tuple[float, str]] = []
    for candidate in candidates:
        normalized_candidate = normalize_spaces(candidate)
        if not normalized_candidate or normalized_candidate in seen:
            continue
        seen.add(normalized_candidate)
        ranked.append((_candidate_score(normalized_candidate), normalized_candidate))

    ranked.sort(key=lambda item: (-item[0], len(item[1])))
    best_score, best_text = ranked[0] if ranked else (0.0, src or chunk)
    fallback = src or chunk
    selected = best_text if best_score >= 0.18 else fallback
    return _truncate(selected)


def sanitize_ocr_noise_for_extraction(text: str) -> str:
    """
    Nettoie le bruit OCR fréquent des PDF juridiques:
    - signatures ministérielles / dates de signature
    - en-têtes & pieds de page JORT
    - collisions de colonnes (hard-break avant marqueurs juridiques forts)

    Conserve de préférence le contenu normatif.
    """
    raw = text or ""
    if not raw.strip():
        return ""

    prepared = normalize_spaces(raw).replace("\n", " ")
    prepared = _OCR_HARD_BREAK_RE.sub("\n", prepared)
    prepared = re.sub(r"\n{2,}", "\n", prepared)

    kept_lines: list[str] = []
    for line in prepared.split("\n"):
        cleaned = repair_common_ocr_artifacts(line)
        if not cleaned:
            continue

        low = cleaned.lower()
        has_normative_hint = bool(_NORMATIVE_KEEP_HINT_RE.search(cleaned))
        is_page_noise = bool(_PAGE_HEADER_LINE_RE.match(cleaned))
        is_signature_line = bool(_SIGNATURE_LINE_RE.match(cleaned))
        is_person_line = bool(_PERSON_PREFIX_LINE_RE.match(cleaned))
        is_name_only_line = bool(_TITLECASE_NAME_LINE_RE.match(cleaned))

        if is_page_noise:
            continue
        if (is_signature_line or is_person_line or is_name_only_line) and not has_normative_hint:
            continue
        if low.startswith("n° ") and "journal officiel" in low:
            continue
        if low.startswith("page ") and "journal officiel" in low:
            continue

        kept_lines.append(cleaned)

    if not kept_lines:
        return ""

    compact = " ".join(kept_lines)
    compact = _INLINE_SIGNATORY_SEGMENT_RE.sub(" ", compact)
    compact = _INLINE_GOV_SEGMENT_RE.sub(" ", compact)
    compact = _INLINE_PERSON_SEGMENT_RE.sub(" ", compact)
    compact = _INLINE_NAME_BETWEEN_ARTICLES_RE.sub(r"\1\2", compact)
    compact = _INLINE_NAME_BETWEEN_CREDITS_RE.sub(r"\1\2", compact)
    compact = _INLINE_ORPHAN_VU_BEFORE_SUSVISE_RE.sub(" ", compact)
    compact = _INLINE_BUREAU_ADMIN_NOISE_RE.sub(r"\1 \2", compact)
    compact = _INLINE_INTERESTS_GOVERNORATES_NOISE_RE.sub(r"\1 ", compact)
    compact = re.sub(
        r"([a-zàâäéèêëîïôöùûüç])([A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ])",
        r"\1 \2",
        compact,
    )
    compact = repair_common_ocr_artifacts(compact)
    compact = _INLINE_PROPER_NOUN_LIST_RE.sub(" ", compact)
    compact = _TUNIS_DATE_RE.sub(" ", compact)
    compact = re.sub(r"\s+", " ", compact).strip(" ,;:-")
    return compact


def normalize_requirement_key(text: str) -> str:
    text = repair_common_ocr_artifacts(strip_requirement_leading_noise(text)).lower()
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"(?i)\bl['â€™](?=[a-zÃ Ã¢Ã¤Ã©Ã¨ÃªÃ«Ã®Ã¯Ã´Ã¶Ã¹Ã»Ã¼Ã§])", "", text)
    text = re.sub(r"[^\w\sàâäéèêëîïôöùûüç\-]", "", text)
    text = re.sub(r"(?i)\bl'(?=[a-zàâäéèêëîïôöùûüç])", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


_DEDUP_ASCII_FOLD_TABLE = str.maketrans(
    {
        "à": "a",
        "â": "a",
        "ä": "a",
        "é": "e",
        "è": "e",
        "ê": "e",
        "ë": "e",
        "î": "i",
        "ï": "i",
        "ô": "o",
        "ö": "o",
        "ù": "u",
        "û": "u",
        "ü": "u",
        "ç": "c",
    }
)


def build_doc_level_dedup_key(
    requirement_text: str,
    source_snippet: str,
    req_type: str,
) -> tuple[str, str, str]:
    """
    Cle de deduplication cross-article:
    - req_type normalise
    - requirement_text normalise
    - source_snippet normalise

    On garde le snippet dans la cle pour eviter de fusionner deux exigences
    lexicalement proches mais provenant de contextes juridiques differents.
    """
    normalized_type = normalize_spaces(req_type or "").upper() or "AUTRE"
    normalized_req = normalize_requirement_key(requirement_text).translate(_DEDUP_ASCII_FOLD_TABLE)
    normalized_src = normalize_requirement_key(source_snippet).translate(_DEDUP_ASCII_FOLD_TABLE)
    return (normalized_type, normalized_req, normalized_src)


def build_doc_level_relaxed_requirement_key(
    requirement_text: str,
    req_type: str,
) -> tuple[str, str]:
    """
    Cle de deduplication doc-level plus relaxe:
    - req_type normalise
    - requirement_text normalise/fold accents

    Utilisee pour capter les doublons cross-article quand le snippet varie
    legerement a cause du bruit OCR ou d'un recouvrement de chunks.
    """
    normalized_type = normalize_spaces(req_type or "").upper() or "AUTRE"
    normalized_req = normalize_requirement_key(requirement_text).translate(_DEDUP_ASCII_FOLD_TABLE)
    return (normalized_type, normalized_req)


def contains_any(text: str, markers: list[str]) -> bool:
    lowered = normalize_spaces(text).lower()
    return any(marker in lowered for marker in markers)


_LEGAL_RISK_MARKERS = [
    "doit",
    "doivent",
    "est tenu de",
    "tenu de",
    "est tenue de",
    "tenus de",
    "interdit",
    "est interdit",
    "sont interdits",
    "peut",
    "peuvent",
    "sauf",
    "lorsque",
    "si ",
    "en cas de",
    "a condition de",
    "a condition que",
    "a moins que",
    "sous reserve",
    "sous reserve de",
    "beneficie",
    "beneficient",
    "bénéficie",
    "bénéficient",
    "prend en charge",
    "communique",
    "communiquent",
    "transmet",
    "transmettent",
    "adresse",
    "adressent",
    "ouvre droit",
    "incombe",
    "incombent",
    "fait foi",
    "fixe",
    "fixent",
    "est rejetee",
    "est rejetée",
    "sont rejetees",
    "sont rejetées",
    "entraîne",
    "entraine",
    "entraînent",
    "entrainent",
    "exclusion",
    "annulation",
    "est puni",
    "sont punis",
    "est passible",
]
_LONG_SAFETY_SPLIT_RE = re.compile(r"(?<=[\.;:])\s+|(?=\s*(?:\d+\)|[a-z]\)))", re.IGNORECASE)


def has_legal_risk_markers(text: str) -> bool:
    lowered = normalize_spaces(text).lower()
    lowered = lowered.replace("à", "a").replace("é", "e").replace("è", "e")
    lowered = lowered.replace("ê", "e").replace("î", "i").replace("ï", "i")
    lowered = lowered.replace("ô", "o").replace("ö", "o").replace("û", "u").replace("ü", "u")
    return any(marker in lowered for marker in _LEGAL_RISK_MARKERS)


def should_enable_long_article_safety(
    text: str,
    *,
    units_count: int = 0,
    char_threshold: int = 1800,
    units_threshold: int = 4,
) -> bool:
    cleaned = normalize_spaces(text or "")
    if not cleaned:
        return False
    if len(cleaned) >= max(300, int(char_threshold)):
        return True
    if int(units_count) >= max(2, int(units_threshold)):
        return True
    return has_legal_risk_markers(cleaned)


def _split_for_long_article_safety(text: str, max_chars: int) -> list[str]:
    compact = normalize_spaces(text)
    if not compact:
        return []
    if len(compact) <= max_chars:
        return [compact]

    parts = [p.strip() for p in _LONG_SAFETY_SPLIT_RE.split(compact) if p and p.strip()]
    if len(parts) <= 1:
        return [compact]

    out: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current} {part}".strip() if current else part
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            out.append(current)
        current = part
    if current:
        out.append(current)

    return out or [compact]


# =========================
# Segmentation helpers
# =========================
ARTICLE_HEADER_RE = re.compile(
    r"(?i)^\s*(?:\[\s*)?"
    r"(Article\s+(premier|1er|\d+)|Art\.?\s*\d+(?:[-\.]\d+)*(?:\s*(bis|ter|quater|quinquies))?)"
    r"\s*[\]\-:–—]*\s*"
)
_ARTICLE_CODE_HINT_RE = re.compile(
    r"(?i)\b(?:article|art\.?)\s*(?:n\s*)?(premier|1er|\d+(?:[-\.]\d+)*)\b"
)
_FOREIGN_ARTICLE_HEADER_RE = re.compile(
    r"(?i)(?<![a-zàâäéèêëîïôöùûüç'’])(?:article|art\.?)\s*(?:n\s*)?"
    r"(premier|1er|\d+(?:[-\.]\d+)*)\s*[-:–—]"
)
_FOREIGN_ARTICLE_BARE_NUMBER_RE = re.compile(
    r"(?i)(?<!\w)(\d{1,3})\s*-\s*(?=(?:est|sont|le|la|les|si|tout|toute|peuvent|ne\s+peuvent|article|art\.))"
)


def strip_article_header(text: str) -> str:
    return ARTICLE_HEADER_RE.sub("", normalize_spaces(text)).strip()


def _normalize_article_code_token(value: str) -> str:
    token = normalize_spaces(value or "").lower().replace(".", "-")
    if token == "1er":
        token = "premier"
    if re.fullmatch(r"\d+", token):
        token = str(int(token))
    return token


def _extract_article_main_numeric(code: str) -> str | None:
    token = _normalize_article_code_token(code)
    if not token:
        return None
    if re.fullmatch(r"\d+", token):
        return token
    if re.fullmatch(r"\d+(?:-\d+)*", token):
        return token.split("-", 1)[0]
    return None


def _resolve_target_article_code(article_label: str | None, article_code: str | None) -> str:
    code = _normalize_article_code_token(article_code or "")
    if code:
        return code
    m = _ARTICLE_CODE_HINT_RE.search(article_label or "")
    if not m:
        return ""
    return _normalize_article_code_token(m.group(1) or "")


def trim_cross_article_noise(
    text: str,
    *,
    article_label: str | None = None,
    article_code: str | None = None,
    min_cut_offset: int = 80,
) -> str:
    """
    Coupe le texte lorsqu'un marqueur clair d'un autre article apparaît
    à l'intérieur de l'unité OCR (contamination inter-articles).
    """
    source = normalize_spaces(text or "")
    if not source:
        return ""
    target = _resolve_target_article_code(article_label, article_code)
    if not target:
        return source

    target_num = _extract_article_main_numeric(target)
    cut_positions: list[int] = []

    for m in _FOREIGN_ARTICLE_HEADER_RE.finditer(source):
        code = _normalize_article_code_token(m.group(1) or "")
        if not code or code == target:
            continue
        if m.start() >= min_cut_offset:
            cut_positions.append(m.start())

    for m in _FOREIGN_ARTICLE_BARE_NUMBER_RE.finditer(source):
        left_ctx = source[max(0, m.start() - 24) : m.start()].lower()
        if re.search(r"(?:article|art\.?)\s*$", left_ctx):
            continue
        marker_num = str(int(m.group(1) or "0"))
        if target_num and marker_num == target_num:
            continue
        if m.start() >= min_cut_offset:
            cut_positions.append(m.start())

    if not cut_positions:
        return source

    cut_at = min(cut_positions)
    trimmed = normalize_spaces(source[:cut_at]).strip(" ,;:-")
    return trimmed or source


def split_into_legal_units(
    text: str,
    *,
    article_label: str | None = None,
    article_code: str | None = None,
    long_article_safety: bool = False,
    max_unit_chars: int = 900,
) -> list[str]:
    txt = sanitize_ocr_noise_for_extraction(strip_article_header(text))
    txt = trim_cross_article_noise(
        txt,
        article_label=article_label,
        article_code=article_code,
    )
    if not txt:
        return []

    # Préserve le ":" pour les cas usuels, mais force un saut quand une liste
    # inline démarre juste après (": - item ...").
    txt = re.sub(r":\s*(?=-\s+\w)", ":\n", txt)
    rough_parts = re.split(r"(?<=[\.\;])\s+", txt)
    units: list[str] = []

    subject_pattern = (
        r"(?i)\s+\bet\s+(?=("
        r"il|elle|le salarié|le travailleur|l['’]employeur|l['’]entreprise|"
        r"la convention collective|le registre|cet avis|ce registre"
        r")\b)"
    )

    for part in rough_parts:
        part = part.strip()
        if not part:
            continue

        subparts = re.split(subject_pattern, part)
        rebuilt: list[str] = []
        i = 0

        while i < len(subparts):
            current = subparts[i].strip()

            if i + 2 < len(subparts):
                subject = subparts[i + 1].strip()
                remainder = subparts[i + 2].strip()

                if current:
                    rebuilt.append(current)
                if subject or remainder:
                    rebuilt.append(f"{subject} {remainder}".strip())
                i += 3
            else:
                if current:
                    rebuilt.append(current)
                i += 1

        for sub in rebuilt:
            sub = normalize_spaces(sub)
            if not sub:
                continue
            if long_article_safety:
                for split_part in _split_for_long_article_safety(sub, max_chars=max(180, int(max_unit_chars))):
                    if len(split_part) >= 15:
                        units.append(split_part)
            elif len(sub) >= 15:
                units.append(sub)

    return units


# =========================
# Rule typing / subject handling
# =========================
def infer_main_subject(chunk_text: str) -> Optional[str]:
    txt = repair_common_ocr_artifacts(normalize_spaces(chunk_text)).lower()

    if "la convention collective" in txt:
        return "La convention collective"
    if "cet avis" in txt:
        return "Cet avis"
    if "ce registre" in txt:
        return "Ce registre"
    if "le registre" in txt:
        return "Le registre"
    if "le salarié" in txt or "le travailleur" in txt:
        return "Le salarié"
    if "l'employeur" in txt or "l’employeur" in txt:
        return "L'employeur"
    if "l'entreprise" in txt or "l’entreprise" in txt:
        return "L'entreprise"
    if "le jury" in txt:
        return "Le jury"
    if "les candidats" in txt:
        return "Les candidats"
    if "le candidat" in txt:
        return "Le candidat"
    if "le directeur général" in txt or "le directeur general" in txt:
        return "Le directeur général"
    if "le ministre" in txt:
        return "Le ministre"
    if "la commission" in txt:
        return "La commission"
    if "l'institut" in txt or "l’institut" in txt:
        return "L'institut"
    if "l'agence" in txt or "l’agence" in txt:
        return "L'agence"
    if "les membres" in txt:
        return "Les membres"
    if re.search(r"\bemployeur\s+(?:doit|est\s+tenu|ne\s+doit|ne\s+peut)", txt):
        return "L'employeur"
    if re.search(r"\bentreprise\s+(?:doit|est\s+tenue|ne\s+doit|ne\s+peut)", txt):
        return "L'entreprise"
    if re.search(r"\b(?:salarie|travailleur)\s+(?:doit|est\s+tenu|ne\s+doit|ne\s+peut)", txt):
        return "Le salarié"
    if re.search(r"\bregistre\s+(?:doit|est\s+tenu|est\s+conserve)", txt):
        return "Le registre"
    if re.search(r"\bcandidats\s+(?:doivent|peuvent|ne\s+doivent|ne\s+peuvent)", txt):
        return "Les candidats"
    if re.search(r"\bcandidat\s+(?:doit|peut|ne\s+doit|ne\s+peut)", txt):
        return "Le candidat"
    if re.search(r"\bcommission\s+(?:doit|est\s+tenue|peut)", txt):
        return "La commission"
    if re.search(r"\bjury\s+(?:doit|est\s+charge|peut)", txt):
        return "Le jury"
    if re.search(r"\bministre\s+(?:doit|peut|est\s+charge)", txt):
        return "Le ministre"

    return None


_QSE_SUBDOMAIN_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    # QHSE
    (
        "QHSE",
        "SECURITE_TRAVAIL",
        (
            "securite au travail",
            "sante et securite",
            "equipement de protection",
            "accident du travail",
            "danger grave",
            "incendie",
            "evacuation",
        ),
    ),
    (
        "QHSE",
        "HYGIENE",
        (
            "hygiene",
            "salubrite",
            "assainissement",
            "nettoyage",
            "sanitaire",
        ),
    ),
    (
        "QHSE",
        "ENVIRONNEMENT",
        (
            "environnement",
            "dechet",
            "dechets",
            "pollution",
            "effluent",
            "emission",
            "rejet",
        ),
    ),
    (
        "QHSE",
        "RISQUES_PROFESSIONNELS",
        (
            "risque professionnel",
            "risques professionnels",
            "maladie professionnelle",
            "exposition au risque",
            "evaluation des risques",
            "evaluation du risque",
        ),
    ),
    (
        "QHSE",
        "PREVENTION",
        (
            "prevention",
            "prevenir",
            "mesures preventives",
            "plan de prevention",
            "dispositif preventif",
        ),
    ),
    # Ressources humaines
    (
        "RESSOURCES_HUMAINES",
        "RECRUTEMENT",
        (
            "concours",
            "candidature",
            "dossier de candidature",
            "demandes de candidature",
            "date de cloture",
            "inscrire leurs candidatures",
            "concours interne",
            "concours sur dossiers",
            "ouvert au concours",
            "concours est ouvert",
            "recrutement",
            "embauche",
            "admis",
            "anciennete dans le grade",
            "anciennete generale",
            "postes a pourvoir",
            "priorite est accordee",
            "plus age",
            "redeploiement",
            "specialites",
        ),
    ),
    (
        "RESSOURCES_HUMAINES",
        "FORMATION",
        (
            "formation",
            "stage",
            "apprentissage",
            "qualification",
            "renforcement des competences",
        ),
    ),
    (
        "RESSOURCES_HUMAINES",
        "CONDITIONS_TRAVAIL",
        (
            "temps de travail",
            "horaire de travail",
            "conditions de travail",
            "conge",
            "remuneration",
            "salaire",
            "repos hebdomadaire",
        ),
    ),
    (
        "RESSOURCES_HUMAINES",
        "GESTION_PERSONNEL",
        (
            "gestion du personnel",
            "personnel",
            "employe",
            "travailleur",
            "agent",
            "evaluation du personnel",
            "mobilite du personnel",
        ),
    ),
    # Gouvernance
    (
        "GOUVERNANCE",
        "RESPONSABILITES",
        (
            "responsable de",
            "est charge de",
            "sont charges de",
            "repond de",
            "incombe",
            "doit assurer",
            "doit evaluer",
            "doit proceder",
            "doit classer",
            "doit proposer",
            "doit decerner",
        ),
    ),
    (
        "GOUVERNANCE",
        "ORGANISATION",
        (
            "commission",
            "jury",
            "conseil",
            "composition",
            "compose de",
            "composee de",
            "composees de",
            "quorum",
            "membres",
            "organe",
        ),
    ),
    (
        "GOUVERNANCE",
        "CONTROLE",
        (
            "controle",
            "audit",
            "inspection",
            "verification",
            "supervision",
            "surveillance",
        ),
    ),
    (
        "GOUVERNANCE",
        "CONFORMITE_INTERNE",
        (
            "conformite interne",
            "non-conformite",
            "dispositif interne",
            "regle interne",
            "procedure interne",
        ),
    ),
    # Juridique general
    (
        "JURIDIQUE_GENERAL",
        "INTERDICTIONS",
        (
            "interdit",
            "est interdit",
            "ne doit pas",
            "ne peuvent pas",
            "est rejetee",
            "rejetee obligatoirement",
            "prohibe",
            "fraude",
            "tentative de fraude",
            "exclusion immediate",
            "annulation de l'epreuve",
        ),
    ),
    (
        "JURIDIQUE_GENERAL",
        "CONDITIONS_REGLEMENTAIRES",
        (
            "a condition de",
            "sous reserve de",
            "lorsque",
            "en cas de",
            "condition d eligibility",
            "conditions d eligibilite",
        ),
    ),
    (
        "JURIDIQUE_GENERAL",
        "SANCTIONS",
        (
            "est puni",
            "sont punis",
            "amende",
            "sanction",
            "passible",
            "penalite",
        ),
    ),
    (
        "JURIDIQUE_GENERAL",
        "OBLIGATIONS_LEGALES",
        (
            "doit",
            "doivent",
            "est tenu de",
            "sont tenus de",
            "obligatoire",
            "est fixe",
            "est fixee",
            "s'engage a",
            "s'engagent a",
        ),
    ),
    # Technique operationnel
    (
        "TECHNIQUE_OPERATIONNEL",
        "PROCEDURES",
        (
            "procedure",
            "procedures",
            "modalites",
            "processus",
            "protocole",
            "etapes",
        ),
    ),
    (
        "TECHNIQUE_OPERATIONNEL",
        "MAINTENANCE",
        (
            "maintenance",
            "entretien",
            "reparation",
            "controle technique",
            "maintenance preventive",
        ),
    ),
    (
        "TECHNIQUE_OPERATIONNEL",
        "EXPLOITATION",
        (
            "exploitation",
            "operation",
            "fonctionnement",
            "mise en service",
            "mise en exploitation",
        ),
    ),
    (
        "TECHNIQUE_OPERATIONNEL",
        "INSTALLATIONS",
        (
            "installation",
            "equipement",
            "infrastructure",
            "reseau",
            "dispositif technique",
        ),
    ),
    # Administratif
    (
        "ADMINISTRATIF",
        "DECLARATION",
        (
            "declaration",
            "declare",
            "doit notifier",
            "doit informer",
            "doit transmettre",
            "signaler",
        ),
    ),
    (
        "ADMINISTRATIF",
        "AUTORISATION",
        (
            "autorisation",
            "agrement",
            "homologation",
            "permis",
            "visa administratif",
        ),
    ),
    (
        "ADMINISTRATIF",
        "REGISTRE",
        (
            "registre",
            "registre special",
            "livre de bord",
            "journal de",
            "tenue du registre",
        ),
    ),
    (
        "ADMINISTRATIF",
        "DOCUMENTATION",
        (
            "dossier",
            "document",
            "pieces justificatives",
            "piece justificative",
            "formulaire",
            "proces-verbal",
            "rapport",
            "certificat",
            "pv",
            "bureau d ordre",
            "enregistree au bureau d ordre",
            "enregistrees au bureau d ordre",
            "enregistree au bureau d'ordre",
            "enregistrees au bureau d'ordre",
        ),
    ),
]

_QSE_REQ_TYPE_FALLBACK: dict[str, tuple[str, str]] = {
    "REGISTRE": ("ADMINISTRATIF", "REGISTRE"),
    "DECLARATION": ("ADMINISTRATIF", "DECLARATION"),
    "CONTROLE": ("GOUVERNANCE", "CONTROLE"),
    "RESPONSABILITE": ("GOUVERNANCE", "RESPONSABILITES"),
    "INTERDICTION": ("JURIDIQUE_GENERAL", "INTERDICTIONS"),
    "CONDITION": ("JURIDIQUE_GENERAL", "CONDITIONS_REGLEMENTAIRES"),
    "EXCEPTION": ("JURIDIQUE_GENERAL", "CONDITIONS_REGLEMENTAIRES"),
    "OBLIGATION": ("JURIDIQUE_GENERAL", "OBLIGATIONS_LEGALES"),
    "AUTRE": ("JURIDIQUE_GENERAL", "OBLIGATIONS_LEGALES"),
}


def classify_qse_domain_subdomain(
    requirement_text: str,
    req_type: str,
    citation_snippet: str = "",
    chunk_text: str = "",
) -> tuple[str, str, str]:
    """
    Classifieur deterministe domaine/sous-domaine pour l'agent A1.
    Retourne:
    - qse_domain
    - qse_sub_domain
    - mapping_strategy: rule | type_fallback | default_fallback
    """
    requirement_clean = normalize_spaces(requirement_text or "")
    combined = normalize_spaces(
        f"{requirement_text or ''} || {citation_snippet or ''} || {chunk_text or ''}"
    )
    normalized = combined.lower().translate(_DEDUP_ASCII_FOLD_TABLE)
    req_normalized = requirement_clean.lower().translate(_DEDUP_ASCII_FOLD_TABLE)
    req_type_upper = normalize_spaces(req_type or "").upper() or "AUTRE"

    if not requirement_clean or len(requirement_clean) < 20:
        return "UNMAPPED", "UNMAPPED", "noise_guard"

    noisy_requirement_cues = (
        "non immatricule",
        "superficie de cette parcelle",
        "ben mohamed",
        "bent ",
        "gouvernorat de",
    )
    if any(cue in req_normalized for cue in noisy_requirement_cues):
        return "UNMAPPED", "UNMAPPED", "noise_guard"

    req_words = re.findall(r"[a-zàâäéèêëîïôöùûüç]{2,}", req_normalized)
    req_digits = re.findall(r"\d+", req_normalized)
    if req_words and len(req_digits) / len(req_words) > 0.33 and req_type_upper == "AUTRE":
        return "UNMAPPED", "UNMAPPED", "noise_guard"

    if is_low_value_requirement_text(requirement_clean) and req_type_upper == "AUTRE":
        return "UNMAPPED", "UNMAPPED", "noise_guard"

    institutional_subjects = ("jury", "commission", "conseil", "comite", "organe")
    institutional_context = any(subj in normalized for subj in institutional_subjects)
    recruitment_priority_markers = (
        "concours",
        "candidat",
        "candidature",
        "redeploiement",
        "anciennete",
        "postes a pourvoir",
    )
    documentation_priority_markers = (
        "bureau d ordre",
        "enregistree au bureau d ordre",
        "enregistrees au bureau d ordre",
        "enregistree au bureau d'ordre",
        "enregistrees au bureau d'ordre",
        "pieces justificatives",
        "formulaire",
    )
    hard_interdiction_markers = (
        "fraude",
        "tentative de fraude",
        "exclusion immediate",
        "annulation de l'epreuve",
        "est rejetee",
        "rejetee obligatoirement",
        "interdit",
    )
    if any(marker in req_normalized for marker in hard_interdiction_markers):
        return "JURIDIQUE_GENERAL", "INTERDICTIONS", "rule"
    if any(marker in req_normalized for marker in documentation_priority_markers):
        return "ADMINISTRATIF", "DOCUMENTATION", "rule"
    if "cahier des charges" in normalized and (
        "modalites" in normalized or "transport" in normalized
    ):
        return "TECHNIQUE_OPERATIONNEL", "PROCEDURES", "rule"
    if ("societe communautaire" in normalized or "societes communautaires" in normalized) and (
        "compose de" in normalized or "composee de" in normalized or "composees de" in normalized
    ):
        return "GOUVERNANCE", "ORGANISATION", "rule"
    if req_type_upper == "CONDITION" and (
        "beneficie" in req_normalized
        or "ayant subi" in req_normalized
        or "en cas de" in req_normalized
        or "lorsque" in req_normalized
        or "a condition de" in req_normalized
    ):
        return "JURIDIQUE_GENERAL", "CONDITIONS_REGLEMENTAIRES", "rule"

    obligation_clause_cues = ("doit", "doivent", "est tenu de", "sont tenus de", "s'engage a", "s'engagent a")
    condition_clause_cues = ("en cas de", "si ", "lorsque", "a condition de", "sous reserve de")
    if req_type_upper in {"OBLIGATION", "DECLARATION"} and any(
        cue in req_normalized for cue in obligation_clause_cues
    ):
        if not any(cue in req_normalized for cue in condition_clause_cues):
            if not institutional_context and not any(cue in req_normalized for cue in recruitment_priority_markers):
                return "JURIDIQUE_GENERAL", "OBLIGATIONS_LEGALES", "rule"

    if not institutional_context and any(marker in normalized for marker in recruitment_priority_markers):
        return "RESSOURCES_HUMAINES", "RECRUTEMENT", "rule"
    if req_type_upper == "CONDITION" and "anciennete" in normalized and (
        "grade" in normalized or "plus age" in normalized
    ):
        return "RESSOURCES_HUMAINES", "RECRUTEMENT", "rule"

    governance_org_markers = (
        "il est cree",
        "est cree",
        "composition",
        "compose de",
        "composee de",
        "composees de",
        "quorum",
        "membres",
        "commission",
        "conseil",
    )
    governance_control_markers = (
        "audit",
        "inspection",
        "verification",
        "supervision",
        "surveillance",
    )
    governance_responsibility_verbs = (
        "est charge de",
        "sont charges de",
        "est charge",
        "sont charges",
        "doit assurer",
        "doit proposer",
        "doit classer",
        "doit proceder",
        "doit decerner",
        "repond de",
        "incombe",
    )
    if institutional_context and any(marker in normalized for marker in governance_org_markers):
        return "GOUVERNANCE", "ORGANISATION", "rule"
    if institutional_context and any(
        verb in normalized for verb in governance_responsibility_verbs
    ):
        return "GOUVERNANCE", "RESPONSABILITES", "rule"
    if institutional_context and any(marker in normalized for marker in governance_control_markers):
        return "GOUVERNANCE", "CONTROLE", "rule"

    governance_operational_cues = governance_control_markers + governance_responsibility_verbs
    best_match: tuple[int, str, str] | None = None
    for domain, sub_domain, markers in _QSE_SUBDOMAIN_RULES:
        hits = sum(1 for marker in markers if marker and marker in normalized)
        if hits <= 0:
            continue
        score = hits
        hinted = _QSE_REQ_TYPE_FALLBACK.get(req_type_upper)
        if hinted and hinted[0] == domain:
            score += 1
        if domain == "ADMINISTRATIF" and req_type_upper in {"DECLARATION", "REGISTRE"}:
            score += 1
        if domain == "JURIDIQUE_GENERAL" and req_type_upper in {"INTERDICTION", "CONDITION", "EXCEPTION"}:
            score += 1
        if domain == "GOUVERNANCE" and institutional_context:
            score += 1
        if (
            domain == "RESSOURCES_HUMAINES"
            and institutional_context
            and any(marker in normalized for marker in governance_operational_cues)
        ):
            score -= 2
        if best_match is None or score > best_match[0]:
            best_match = (score, domain, sub_domain)

    if best_match is not None:
        return best_match[1], best_match[2], "rule"

    if req_type_upper in _QSE_REQ_TYPE_FALLBACK:
        domain, sub_domain = _QSE_REQ_TYPE_FALLBACK[req_type_upper]
        return domain, sub_domain, "type_fallback"

    if req_type_upper == "AUTRE":
        return "UNMAPPED", "UNMAPPED", "default_unmapped"

    return "JURIDIQUE_GENERAL", "OBLIGATIONS_LEGALES", "default_fallback"


def normalize_subject_from_context(
    requirement_text: str,
    citation_snippet: str,
    chunk_text: str,
) -> str:
    subject = infer_main_subject(chunk_text)
    if not subject:
        return normalize_spaces(requirement_text)

    req = normalize_spaces(requirement_text)
    snippet = normalize_spaces(citation_snippet).lower()

    if snippet.startswith("elle "):
        req = re.sub(r"(?i)^elle\b", subject, req, count=1)
    elif snippet.startswith("il "):
        req = re.sub(r"(?i)^il\b", subject, req, count=1)

    bare_subject_patterns = {
        "L'employeur": r"(?i)^employeur\b",
        "L'entreprise": r"(?i)^entreprise\b",
        "Le salarié": r"(?i)^(?:salarie|travailleur)\b",
        "Le registre": r"(?i)^registre\b",
        "Le candidat": r"(?i)^candidat\b",
        "Les candidats": r"(?i)^candidats\b",
        "La commission": r"(?i)^commission\b",
        "Le jury": r"(?i)^jury\b",
        "Le ministre": r"(?i)^ministre\b",
    }
    subject_pattern = bare_subject_patterns.get(subject)
    if subject_pattern:
        req = re.sub(subject_pattern, subject, req, count=1)

    return req


def has_unresolved_subject_reference(requirement_text: str, chunk_text: str) -> bool:
    req = normalize_spaces(requirement_text)
    if not re.match(r"(?i)^(il|elle|ils|elles)\b", req):
        return False
    return infer_main_subject(chunk_text) is None


def normalize_req_type(requirement_text: str, source_snippet: str, llm_req_type: str) -> str:
    req = normalize_spaces(requirement_text).lower()
    src = normalize_spaces(source_snippet).lower()
    combined = f"{req} || {src}"
    llm_type = (llm_req_type or "").strip().upper()

    responsibility_markers = [
        "répond de",
        "repond de",
        "responsable de",
        "est responsable de",
        "imputables à sa faute",
        "imputables a sa faute",
    ]
    declaration_markers = [
        "doit notifier",
        "doit déclarer",
        "doit declarer",
        "doit informer",
        "doit transmettre",
        "doit adresser",
        "doit communiquer",
        "doivent communiquer",
        "communiquer chaque année",
        "communiquent chaque année",
        "transmettre chaque année",
        "adresser chaque année",
        "informer chaque année",
        "fournir chaque année",
        "demandes de candidature",
        "demande de candidature",
        "doivent être enregistrées",
        "doivent etre enregistrees",
        "doit être accompagnée",
        "doit etre accompagnee",
        "doit être accompagné",
        "doit etre accompagne",
        "est fixée conformément",
        "est fixee conformement",
        "est fixé conformément",
        "est fixe conformement",
        "sont recouvrées",
        "sont recouvrees",
        "sont affectées",
        "sont affectees",
        "sur la base de titres",
    ]
    declaration_reporting_verbs = [
        "communiqu",
        "transmett",
        "adress",
        "fourn",
        "inform",
        "déclar",
        "declar",
        "notifi",
    ]
    declaration_targets = [
        "ministère",
        "ministere",
        "autorité",
        "autorités",
        "autorite",
        "autorites",
        "inspection",
        "administration",
        "service compétent",
        "service competent",
        "autorités compétentes",
        "autorites competentes",
        "organisme",
        "office",
        "agence",
    ]
    declaration_payload_markers = [
        "information",
        "informations",
        "données",
        "donnees",
        "renseignement",
        "renseignements",
        "rapport",
        "déclaration",
        "declaration",
    ]
    condition_effect_markers = [
        "peut bénéficier",
        "peuvent bénéficier",
        "peut beneficier",
        "peuvent beneficier",
        "bénéficie",
        "bénéficient",
        "beneficie",
        "beneficient",
        "peut participer",
        "peuvent participer",
        "est ouvert aux",
        "sont ouverts aux",
        "ouvre droit",
        "ouvrent droit",
    ]
    effect_obligation_markers = [
        "prend en charge",
        "peut bénéficier",
        "peuvent bénéficier",
        "peut beneficier",
        "peuvent beneficier",
        "peut participer",
        "peuvent participer",
        "bénéficie",
        "bénéficient",
        "beneficie",
        "beneficient",
        "est ouvert aux",
        "sont ouverts aux",
        "ouvre droit",
        "ouvrent droit",
        "est fixé",
        "est fixe",
        "est fixée",
        "est fixee",
        "sont fixés",
        "sont fixes",
        "sont fixées",
        "sont fixees",
        "fixé conformément",
        "fixe conformement",
        "fixée conformément",
        "fixee conformement",
        "fixés conformément",
        "fixes conformement",
        "fixées conformément",
        "fixees conformement",
    ]
    exception_markers = [
        "sauf",
        "cependant",
        "toutefois",
        "sous réserve de",
        "sous reserve de",
        "sous réserve que",
        "sous reserve que",
        "par dérogation",
        "par derogation",
        "ne répond que",
        "n'en répond que",
        "n’en répond que",
        "ne repond que",
    ]
    condition_markers = [
        "si ",
        "lorsque",
        "en cas de",
        "à condition de",
        "a condition de",
        "à condition que",
        "a condition que",
        "sous réserve que",
        "sous reserve que",
    ]
    interdiction_markers = [
        "interdit",
        "est interdit",
        "sont interdits",
        "ne doit pas",
        "ne doivent pas",
        "est rejeté",
        "est rejetee",
        "sont rejetés",
        "sont rejetes",
        "rejeté obligatoirement",
        "rejete obligatoirement",
    ]
    obligation_markers = [
        "doit",
        "doivent",
        "est tenu de",
        "sont tenus de",
        "tenu de",
        "tenus de",
        "obligé de",
        "oblige de",
        "prend en charge",
        "bénéficie",
        "bénéficient",
        "beneficie",
        "beneficient",
        "est fixé",
        "est fixe",
        "est fixée",
        "est fixee",
        "sont fixés",
        "sont fixes",
        "sont fixées",
        "sont fixees",
        "sera tenu à la disposition",
        "sera tenue à la disposition",
    ]

    if contains_any(combined, responsibility_markers):
        return "RESPONSABILITE"
    if contains_any(combined, exception_markers):
        return "EXCEPTION"
    if contains_any(combined, interdiction_markers):
        return "INTERDICTION"

    has_condition = contains_any(combined, condition_markers)
    has_obligation = contains_any(combined, obligation_markers)
    has_declaration = contains_any(combined, declaration_markers)
    has_reporting_verb = contains_any(combined, declaration_reporting_verbs)
    has_reporting_target = contains_any(combined, declaration_targets)
    has_reporting_payload = contains_any(combined, declaration_payload_markers)
    has_eligibility_scope = contains_any(
        combined,
        [
            "peut participer",
            "peuvent participer",
            "peut beneficier",
            "peuvent beneficier",
            "peut beneficier",
            "peuvent beneficier",
            "est ouvert aux",
            "sont ouverts aux",
            "ouvre droit",
            "ouvrent droit",
        ],
    )
    has_eligibility_criteria = contains_any(
        combined,
        [
            "titulaire",
            "titulaires",
            "anciennet",
            "diplom",
            "licence",
            "maitrise",
            "maitrise",
            "justifiant",
            "remplissant les conditions",
            "sur dossiers",
        ],
    )

    if "registre" in combined:
        register_markers = [
            "tenir un registre",
            "tenir le registre",
            "registre doit être présenté",
            "registre doit être presenté",
            "registre doit être tenu",
            "registre doit etre tenu",
            "registre est tenu",
            "est tenu et conserv",
            "est conservé",
            "est conserve",
            "conservé pendant",
            "conserve pendant",
            "présenté à toute réquisition",
            "presente a toute requisition",
            "numéroté",
            "numerote",
            "paraphé",
            "paraphe",
            "consigné",
            "consigne",
            "à la disposition",
            "a la disposition",
        ]
        if llm_type == "REGISTRE" or contains_any(combined, register_markers):
            return "REGISTRE"

    if has_declaration:
        return "DECLARATION"
    if has_reporting_verb and (
        has_reporting_target
        or has_reporting_payload
        or "chaque année" in combined
        or "annuellement" in combined
    ):
        return "DECLARATION"
    if has_eligibility_scope and (has_condition or has_eligibility_criteria):
        return "CONDITION"
    if contains_any(combined, effect_obligation_markers):
        if has_condition or contains_any(combined, condition_effect_markers):
            return "CONDITION"
        return "OBLIGATION"

    if "déclar" in combined:
        return "DECLARATION"

    if has_condition and has_obligation:
        return "OBLIGATION"
    if has_condition:
        return "CONDITION"
    if has_obligation:
        return "OBLIGATION"

    if "contrôle" in combined or "controle" in combined:
        return "CONTROLE"

    if "avis doit indiquer" in req or req.startswith("cet avis doit indiquer"):
        return "OBLIGATION"

    if "exemplaire" in req and ("à la disposition" in req or "a la disposition" in req):
        return "OBLIGATION"

    allowed = {
        "OBLIGATION",
        "INTERDICTION",
        "RESPONSABILITE",
        "EXCEPTION",
        "CONDITION",
        "DECLARATION",
        "CONTROLE",
        "REGISTRE",
        "AUTRE",
    }
    return llm_type if llm_type in allowed else "AUTRE"


_EMPTY_LLM_FALLBACK_TRIGGER_RE = re.compile(
    r"(?i)\b(?:doit(?:vent)?|est\s+tenu(?:e|s|es)?(?:\s+de|\b)|interdit(?:e|es|s)?|"
    r"benef\w*|prend\s+en\s+charge|communiqu\w*|transmett?\w*|adress\w*|"
    r"notifi\w*|fourn\w*|sous\s+reser\w*|condition\b|fix\w*|entrai\w*)\b"
)

_STRUCTURAL_SUPERVISION_RE = re.compile(
    r"(?i)\best\s+supervis[ée]e?s?\s+par\s+(?:un|une|le|la)\s+"
    r"(?:jury|commission|comit[ée])\b"
)
_STRUCTURAL_COMPOSITION_FIXED_RE = re.compile(
    r"(?i)\b(?:composition\b[^.;:]{0,160}\bfix[ée]e?s?\b|"
    r"fix[ée]e?s?\b[^.;:]{0,160}\bcomposition\b)"
)


def build_empty_llm_fallback_requirements(unit_text: str) -> list[dict]:
    """
    Fallback conservateur: quand le LLM renvoie vide sur une unite clairement
    normative, on reinjecte une exigence candidate fondee sur le texte source.
    """
    source = sanitize_ocr_noise_for_extraction(unit_text or "")
    source = normalize_spaces(strip_article_header(source))
    source = repair_common_ocr_artifacts(source)
    if len(source) < 40:
        return []
    if not _EMPTY_LLM_FALLBACK_TRIGGER_RE.search(source):
        return []

    candidates: list[str] = []
    for candidate in _expand_snippet_candidates(source):
        cleaned = repair_common_ocr_artifacts(strip_requirement_leading_noise(candidate))
        cleaned = normalize_spaces(cleaned).strip(" ,;:-")
        if len(cleaned) < 20:
            continue
        if _EMPTY_LLM_FALLBACK_TRIGGER_RE.search(cleaned):
            candidates.append(cleaned)

    if not candidates:
        candidates = [source]

    def _score(candidate: str) -> tuple[float, int]:
        low = candidate.lower()
        score = 0.0
        if _EMPTY_LLM_FALLBACK_TRIGGER_RE.search(candidate):
            score += 1.4
        if re.search(r"(?i)\b(?:doit(?:vent)?|est\s+tenu(?:e|s|es)?\s+de|interdit(?:e|es|s)?|"
                     r"communiqu\w+|transmett?\w+|adress\w+|notifi\w+|fourn\w+|"
                     r"registre|tenir\s+un\s+registre)\b", candidate):
            score += 1.0
        if has_ocr_artifact_signals(candidate):
            score -= 0.45
        if re.search(r"(?i)\b(?:chapitre|section|titre|livre)\b", low):
            score -= 0.35
        return (round(score, 4), -len(candidate))

    best_candidate = max(candidates, key=_score)
    req_type = normalize_req_type(
        requirement_text=best_candidate,
        source_snippet=source,
        llm_req_type="AUTRE",
    )
    if req_type == "AUTRE":
        req_type = "OBLIGATION"

    return [{"req_type": req_type, "requirement_text": best_candidate}]


def build_structural_framing_requirements(
    unit_text: str,
    existing_requirements: list[object] | None = None,
) -> list[dict]:
    """
    Fallback structurel: ajoute une exigence "cadre" si le texte source
    contient une règle de supervision/gouvernance non couverte par la sortie LLM.
    """
    source = normalize_spaces(strip_article_header(unit_text or ""))
    if len(source) < 40:
        return []
    if not _STRUCTURAL_SUPERVISION_RE.search(source):
        return []

    existing_texts: list[str] = []
    for item in existing_requirements or []:
        req_text = ""
        if hasattr(item, "requirement_text"):
            req_text = str(getattr(item, "requirement_text") or "")
        elif isinstance(item, dict):
            req_text = str(item.get("requirement_text") or "")
        req_text = normalize_spaces(req_text)
        if req_text:
            existing_texts.append(req_text)

    existing_blob = normalize_spaces(" || ".join(existing_texts)).lower()
    if _STRUCTURAL_SUPERVISION_RE.search(existing_blob):
        return []

    best_sentence = ""
    for sentence in re.split(r"(?<=[\.;!?])\s+", source):
        candidate = normalize_spaces(sentence).strip(" -")
        if len(candidate) < 30:
            continue
        if _STRUCTURAL_SUPERVISION_RE.search(candidate):
            best_sentence = candidate
            break

    if not best_sentence:
        best_sentence = source
    if best_sentence and best_sentence[-1] not in {".", ";", ":"}:
        best_sentence = f"{best_sentence}."

    req_type = normalize_req_type(
        requirement_text=best_sentence,
        source_snippet=source,
        llm_req_type="OBLIGATION",
    )
    if req_type == "AUTRE":
        # Supervision institutionnelle = règle normative exploitable.
        req_type = "OBLIGATION"

    if (
        "composition" in source.lower()
        and "fix" in source.lower()
        and not _STRUCTURAL_COMPOSITION_FIXED_RE.search(best_sentence)
        and _STRUCTURAL_COMPOSITION_FIXED_RE.search(source)
    ):
        best_sentence = source
        if best_sentence and best_sentence[-1] not in {".", ";", ":"}:
            best_sentence = f"{best_sentence}."

    return [{"req_type": req_type, "requirement_text": best_sentence}]


# =========================
# Quality / risk guards
# =========================
def has_missing_legal_condition(requirement_text: str, citation_snippet: str) -> bool:
    req = normalize_spaces(requirement_text).lower()
    src = normalize_spaces(citation_snippet).lower()

    markers = [
        "sauf",
        "si ",
        "lorsque",
        "en cas de",
        "sous réserve de",
        "sous reserve de",
        "à condition de",
        "a condition de",
        "péril en la demeure",
        "peril en la demeure",
    ]
    return any(marker in src and marker not in req for marker in markers)


def has_missing_scope_context(requirement_text: str, citation_snippet: str) -> bool:
    req = normalize_spaces(requirement_text).lower()
    src = normalize_spaces(citation_snippet).lower()

    scope_markers = [
        "dans les établissements",
        "dans l'établissement",
        "dans les locaux",
        "sur les lieux",
        "au sein de",
        "à l'intérieur de",
        "a l'interieur de",
        "dans les cas où",
        "dans les cas ou",
        "où s'effectuent",
        "où s'effectue",
        "où se fait",
    ]
    return any(marker in src and marker not in req for marker in scope_markers)


def has_subject_mismatch(requirement_text: str, chunk_text: str) -> bool:
    req = normalize_spaces(requirement_text).lower()
    src = normalize_spaces(chunk_text).lower()

    req_has_employeur = "employeur" in req
    req_has_salarie = "salarié" in req or "travailleur" in req
    req_has_entreprise = "entreprise" in req
    req_has_convention = "convention collective" in req
    req_has_registre = "registre" in req

    src_has_employeur = "employeur" in src
    src_has_salarie = "salarié" in src or "travailleur" in src
    src_has_entreprise = "entreprise" in src
    src_has_convention = "convention collective" in src
    src_has_registre = "registre" in src

    if req_has_employeur and not src_has_employeur and src_has_salarie:
        return True
    if req_has_salarie and not src_has_salarie and src_has_employeur:
        return True
    if req_has_entreprise and not src_has_entreprise and src_has_salarie:
        return True
    if req_has_convention and not src_has_convention and (src_has_salarie or src_has_employeur):
        return True
    if req_has_registre and not src_has_registre and not ("registre" in requirement_text.lower() and "registre" in chunk_text.lower()):
        return True

    return False


def is_low_value_requirement_text(text: str) -> bool:
    text = strip_requirement_leading_noise(text)
    text = repair_common_ocr_artifacts(text)
    # Seuil abaissé de 15 → 20 chars et < 3 mots → < 4 mots pour réduire les faux positifs
    # sur des CONDITION/EXCEPTION courtes mais normativement valides
    if not text or len(text) < 20:
        return True
    if len(text.split()) < 4:
        return True

    if re.search(r"(?i)\bjournal\s+officiel\s+de\s+la\s+r[ée]publique\s+tunisienne\b", text):
        return True

    bad_exact = {
        "affichage",
        "notification",
        "déclaration",
        "declaration",
        "registre",
        "contrôle",
        "controle",
    }
    if text.lower() in bad_exact:
        return True

    # Phase 1: OCR truncation — starts with an isolated accented char (mid-sentence fragment)
    # e.g. "é sera accordée selon l'ancienneté..."
    if re.match(r"^[àâäéèêëîïôöùûüç]\s", text.strip()):
        return True

    # Phase 1: Cadastral / OCR table noise — Tunisian proper-name chains or numbered lists
    # e.g. "6- 4-Hedi ben Mohamed...", "Ali ben Mohsen ben Salah..."
    if re.match(r"^\s*\d+\s*[-–]\s*\d+\s*[-–]", text.strip()):
        return True
    if re.search(r"(?i)\bben\s+\w{2,}\s+(?:ben|bou|ou[a-z]{1,})\b", text):
        return True

    if not re.search(
        r"(?i)\b("
        r"doit|doivent|est|sont|interdit|répond|repond|sera|seront|"
        r"peut|peuvent|tenu|tenus|responsable|présenté|presente|"
        r"b[eé]n[eé]ficie|b[eé]n[eé]ficient|entra[iî]ne|entra[iî]nent|"
        r"communique|communiquent|ouvre|ouvrent|incombe|incombent|"
        r"prend\s+en\s+charge|sous\s+r[ée]serve\s+de|[àa]\s+condition\s+de|"
        r"proc[eè]de|proc[eè]dent|redevient|redeviennent|"
        r"fait\s+foi|fixe|fixent|fix[ée]e?s?"
        r")\b",
        text,
    ):
        return True

    return False


def is_partial_exception_requirement(requirement_text: str, citation_snippet: str) -> bool:
    req = normalize_spaces(requirement_text).lower()
    src = normalize_spaces(citation_snippet).lower()

    if req.startswith("sauf si ") or req.startswith("sauf "):
        # Une EXCEPTION autonome est valide si elle contient un sujet + verbe explicites
        # Exemples valides : "Sauf si le salarié justifie d'une incapacité médicale..."
        #                    "Sauf accord du salarié, l'employeur ne peut pas..."
        has_explicit_subject = bool(re.search(
            r"\b(employeur|salarié|salarie|candidat|jury|commission|agent|organisme|"
            r"entreprise|établissement|etablissement|directeur|ministre|titulaire)\b",
            req
        ))
        has_normative_verb = bool(re.search(
            r"\b(peut|doit|doivent|est tenu|sont tenus|interdit|obligatoire|"
            r"justifie|autorisé|autorise|accordé|accorde)\b",
            req
        ))
        # Si l'exception est autonome (sujet + verbe), ne pas rejeter
        if has_explicit_subject and has_normative_verb:
            return False

        if "sauf si" in src:
            prefix = src.split("sauf si", 1)[0].strip(" ,;:")
            if len(prefix) >= 20:
                return True
        if "sauf " in src:
            prefix = src.split("sauf ", 1)[0].strip(" ,;:")
            if len(prefix) >= 20:
                return True

    return False


def is_invented_from_non_normative_snippet(requirement_text: str, citation_snippet: str) -> bool:
    req = normalize_spaces(requirement_text).lower()
    src = normalize_spaces(citation_snippet).lower()

    src_normative_markers = [
        "doit",
        "doivent",
        "est tenu de",
        "sont tenus de",
        "interdit",
        "est interdit",
        "sont interdits",
        "sera tenu",
        "sera tenue",
        "doit être",
        "doivent être",
        "répond de",
        "responsable de",
        "s'imposent",
        "s’imposent",
        "peut",
        "peuvent",
    ]
    req_normative_markers = [
        "doit",
        "doivent",
        "interdit",
        "est interdit",
        "sera tenu",
        "sera tenue",
        "répond de",
        "responsable de",
    ]

    return contains_any(req, req_normative_markers) and not contains_any(src, src_normative_markers)


def is_non_actionable_scope_requirement(requirement_text: str, citation_snippet: str) -> bool:
    req = normalize_spaces(requirement_text).lower()
    src = normalize_spaces(citation_snippet).lower()

    if "doit être inclus dans le champ d'application" in req:
        return True

    scope_markers = [
        "dans tout établissement compris dans le champ d'application",
        "dans les établissements soumis à l'application",
        "en ce qui concerne",
        "sont étendues aux catégories",
        "sont etendues aux categories",
    ]
    strong_normative_markers = [
        "doit",
        "doivent",
        "est tenu de",
        "sont tenus de",
        "doit être affiché",
        "doit être affiche",
        "doivent être indiqués",
        "doivent etre indiques",
        "sera tenu à la disposition",
        "sera tenue à la disposition",
        "s'imposent",
        "s’imposent",
    ]

    if contains_any(src, scope_markers):
        if "champ d'application" in req and not contains_any(req, strong_normative_markers):
            return True

    return False


def is_publication_boilerplate_requirement(
    requirement_text: str,
    citation_snippet: str = "",
) -> bool:
    combined = normalize_spaces(f"{requirement_text} || {citation_snippet}")
    return bool(_PUBLICATION_BOILERPLATE_RE.search(combined))


def is_individual_act_requirement(
    requirement_text: str,
    citation_snippet: str = "",
) -> bool:
    req = normalize_spaces(requirement_text).lower()
    src = normalize_spaces(citation_snippet).lower()
    combined = f"{req} || {src}"

    has_named_person = bool(_INDIVIDUAL_ACT_NAME_RE.search(combined))
    has_individual_markers = contains_any(combined, _INDIVIDUAL_ACT_MARKERS)
    has_general_normative_markers = contains_any(
        combined,
        [
            "doit",
            "doivent",
            "est tenu de",
            "sont tenus de",
            "interdit",
            "en cas de",
            "lorsque",
            "sauf",
            "communique",
            "communiquent",
            "transmet",
            "transmettent",
            "adresse",
            "adressent",
            "prend en charge",
            "beneficie",
            "beneficient",
            "bénéficie",
            "bénéficient",
        ],
    )
    has_institutional_subject = contains_any(
        combined,
        [
            "direction generale",
            "direction générale",
            "ministere",
            "ministère",
            "administration",
            "etablissement",
            "établissement",
            "agriculteurs",
            "fonds national",
        ],
    )
    jort_institutional_cues = [
        "jury",
        "concours",
        "candidat",
        "commission",
        "délibération",
        "deliberation",
        "notation",
        "classement",
        "postes à pourvoir",
        "postes a pourvoir",
        "ancienneté dans le grade",
        "anciennete dans le grade",
    ]
    if contains_any(combined, jort_institutional_cues):
        return False

    if has_named_person and has_individual_markers and not has_general_normative_markers and not has_institutional_subject:
        return True
    return False


def is_out_of_scope_individual_requirement(
    requirement_text: str,
    citation_snippet: str = "",
) -> bool:
    return is_publication_boilerplate_requirement(
        requirement_text=requirement_text,
        citation_snippet=citation_snippet,
    ) or is_individual_act_requirement(
        requirement_text=requirement_text,
        citation_snippet=citation_snippet,
    )


# =========================
# Definition / classification guards
# =========================
def is_definition_like_article(text: str) -> bool:
    txt = strip_article_header(text).lower()
    first_window = txt[:250]

    definition_start_patterns = [
        r"^\s*on entend par\b",
        r"^\s*est considér",
        r"^\s*sont considér",
        r"^\s*ont considér",
        r"^\s*n['’]?est pas considér",
        r"^\s*ne sont pas considér",
        r"^\s*est réput",
        r"^\s*sont réput",
        r"^\s*est assimil",
        r"^\s*sont assimil",
        r"^\s*n['’]?est pas assimil",
        r"^\s*ne sont pas assimil",
        r"^\s*est un accord\b",
        r"^\s*est une convention\b",
    ]
    strong_normative_markers = [
        "doit",
        "doivent",
        "est tenu de",
        "sont tenus de",
        "interdit",
        "est interdit",
        "sont interdits",
        "répond de",
        "repond de",
        "responsable de",
        "est responsable de",
        "doit être",
        "doit etre",
        "doivent être",
        "doivent etre",
        "sera",
        "seront",
    ]

    starts_like_definition = any(re.search(pattern, first_window) for pattern in definition_start_patterns)
    has_strong_normative = contains_any(txt, strong_normative_markers)

    return starts_like_definition and not has_strong_normative


def is_definition_like_unit(text: str) -> bool:
    txt = normalize_spaces(text).lower()

    normative_markers = [
        "doit",
        "doivent",
        "est tenu de",
        "sont tenus de",
        "interdit",
        "est interdit",
        "sont interdits",
        "répond de",
        "responsable de",
        "est responsable de",
        "sera tenu",
        "doit être écrit",
        "doit être ecrit",
        "doit être affiché",
        "doit être affiche",
        "doit être tenu",
        "doit etre tenu",
        "peut",
        "peuvent",
        "bénéficie",
        "beneficie",
        "prend en charge",
        "entraîne",
        "entraine",
        "fait foi",
        "fixe",
        "fixent",
    ]

    definition_patterns = [
        r"\bon entend par\b",
        r"\best une convention\b",
        r"\best un accord\b",
        r"\best considér",
        r"\bsont considér",
        r"\bn['’]?est pas considér",
        r"\bne sont pas considér",
        r"\best réput",
        r"\bsont réput",
        r"\best assimil",
        r"\bsont assimil",
        r"\bn['’]?est pas assimil",
        r"\bne sont pas assimil",
    ]
    if any(re.search(pattern, txt) for pattern in definition_patterns):
        if contains_any(txt, normative_markers):
            return False
        return True

    if "appelée" in txt and "s'engage à" in txt:
        return True
    if "conclu entre" in txt or "conclue entre" in txt:
        return True

    if (
        ("est un accord" in txt or "est une convention" in txt or "conclu entre" in txt or "conclue entre" in txt)
        and not contains_any(txt, normative_markers)
    ):
        return True

    return False


def is_scope_extension_classification_unit(unit_text: str, chunk_text: str) -> bool:
    src = normalize_spaces(unit_text).lower()
    ctx = normalize_spaces(chunk_text).lower()
    src_no_enum = re.sub(r"^\s*(?:[-•*]|\d+\)|[a-z]\))\s*", "", src).strip()

    article_context_markers = [
        "les dispositions du présent code sont étendues",
        "les dispositions du present code sont etendues",
        "catégories de travailleurs ci-après",
        "categories de travailleurs ci-apres",
        "personnes visées à l'alinéa précédent",
        "personnes visees a l'alinea precedent",
    ]
    descriptive_start_markers = [
        "les personnes qui",
        "les personnes dont",
        "lorsque ces personnes exercent leur profession",
        "dans une entreprise industrielle ou commerciale",
        "pour le compte d'une seule entreprise",
    ]
    strong_normative_markers = [
        "sera toujours responsable",
        "ne sera responsable",
        "ne sont responsables",
        "est responsable",
        "sont responsables",
        "doit",
        "doivent",
        "interdit",
        "est interdit",
        "sont interdits",
        "tenu de",
        "tenus de",
        "peut",
        "peuvent",
        "beneficie",
        "bénéficie",
        "prend en charge",
        "entraîne",
        "entraine",
        "fait foi",
        "fixe",
        "fixent",
        "est rejetée",
        "est rejetee",
    ]
    pure_classification_markers = [
        "sont assimilées à",
        "sont assimilees a",
        "est assimilé à",
        "est assimile a",
        "ne leur est applicable que dans la mesure où",
        "ne leur est applicable que dans la mesure ou",
        "sont étendues aux catégories",
        "sont etendues aux categories",
    ]

    if contains_any(ctx, article_context_markers):
        if any(src_no_enum.startswith(marker) for marker in descriptive_start_markers):
            if not contains_any(src_no_enum, strong_normative_markers):
                return True

    if contains_any(src_no_enum, pure_classification_markers):
        if not contains_any(src_no_enum, strong_normative_markers):
            return True

    return False


# =========================
# Requirement text normalization
# =========================
_VAGUE_OBLIGATION_PREFIX_RE = re.compile(
    r"(?i)^(?:il\s+convient\s+de|il\s+y\s+a\s+lieu\s+de|il\s+est\s+n[ée]cessaire\s+de|il\s+est\s+requis\s+de|il\s+faut)\s+"
)
_LEADING_INFINITIVE_OBLIGATION_RE = re.compile(
    r"(?i)^(informer|avertir|notifier|transmettre|adresser|fournir|tenir|conserver|"
    r"enregistrer|proc[ée]der|classer|[ée]valuer|proposer|respecter|pr[ée]senter|"
    r"justifier|communiquer|renseigner)\b"
)
_WEAK_DETAIL_TAIL_RE = re.compile(
    r"(?i),\s*(?:notamment|en particulier|et ce|pour ce faire)\b[^.;:]*"
)
_COND_OR_EXCEPTION_CUES = (
    "à condition",
    "a condition",
    "sous réserve",
    "sous reserve",
    "sauf",
    "en cas de",
    "lorsque",
    "si ",
)
_DOIT_MODAL_PATTERN = r"(?:doit|doivent)"
_SPLITTABLE_REQ_TYPES = {"OBLIGATION", "DECLARATION", "REGISTRE", "CONTROLE"}
_STRUCTURAL_MODAL_PATTERN = (
    r"(?:"
    r"doit|doivent|peut|peuvent|"
    r"est\s+tenu(?:e|s|es)?\s+de|sont\s+tenu(?:s|es)?\s+de|"
    r"est\s+h(?:abilit|abilit)[ée]?\s+[àa]|sont\s+h(?:abilit|abilit)[ée]s?\s+[àa]|"
    r"est\s+charg[ée]?\s+de|sont\s+charg[ée]s?\s+de"
    r")"
)
_COORDINATED_INFINITIVE_SPLIT_RE = re.compile(
    r"(?i)\s+\bet\s+(?=(?:[àa]\s+|de\s+)?[a-zàâäéèêëîïôöùûüç'-]{3,}(?:er|ir|re|oir)\b)"
)
_COMMA_COORDINATED_INFINITIVE_SPLIT_RE = re.compile(
    r"(?i)\s*,\s*(?=(?:[àa]\s+|de\s+)?(?:etre|être|[a-zàâäéèêëîïôöùûüç'-]{3,}(?:er|ir|re|oir))\b)"
)
_LEADING_INFINITIVE_LINK_RE = re.compile(r"(?i)^(?:[àa]\s+|de\s+)")
_REPEATED_AUXILIARY_CHAIN_RE = re.compile(
    r"(?i)^(.+?)\s+(est|sont)\s+(.+?)\s+\bet\s+(est|sont)\s+(.+)$"
)
_COMPOSITION_SEMICOLON_CHAIN_RE = re.compile(
    r"(?i)^(.+?\bcompos\w+\s+de)\s+(.+?)\s*;\s*(?:et\s+)?de\s+(.+)$"
)
_MODAL_INFINITIVE_CHAIN_RE = re.compile(
    rf"(?i)^(.+?{_STRUCTURAL_MODAL_PATTERN})\s+(.+)$"
)
_REPEATED_MODAL_SEGMENT_SPLIT_RE = re.compile(
    rf"(?i)\s*(?:,|;|\bet\b)\s+(?=(?:{_STRUCTURAL_MODAL_PATTERN})\b)"
)
_DOCUMENTARY_INTRO_RE = re.compile(
    r"(?i)^(.+?\b(?:doit|doivent)\s+"
    r"(?:indiquer|mentionner|comprendre|comporter|contenir|enumerer|énumérer|"
    r"etre\s+accompagn[ée]?\s+de|être\s+accompagn[ée]?\s+de|"
    r"etre\s+assorti\s+de|être\s+assorti\s+de))\s+(.+)$"
)
_INLINE_HEADING_FRAGMENT_RE = re.compile(
    r"(?i)(?:^|(?<=[\.;:])\s*)"
    r"(?:jours?\s+f[ée]ri[ée]s?(?:,\s*ch[oô]m[ée]s?\s+et\s+pay[ée]s?)?"
    r"|chapitre\s+[ivxlcdm0-9]+|section\s+[ivxlcdm0-9]+|titre\s+[ivxlcdm0-9]+|livre\s+[ivxlcdm0-9]+)"
    r"\s*,\s*(?=(?:le|la|les|un|une|l['’]|si|lorsque|en\s+cas|dans\s+le\s+cas)\b)"
)


def _lowercase_first_alpha(text: str) -> str:
    if not text:
        return text
    for idx, ch in enumerate(text):
        if ch.isalpha():
            return text[:idx] + ch.lower() + text[idx + 1 :]
    return text


def _dedupe_split_requirements(parts: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = normalize_spaces(part).strip()
        if not normalized:
            continue
        key = re.sub(r"\s+", " ", normalized).strip(" .;,:").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(normalized.rstrip(".") + ".")
    return deduped


def _normalize_split_item(item: str, subject_modal: str) -> str:
    cleaned = normalize_spaces(re.sub(r"^(?:-|\d+[.)]?|[a-z][.)])\s*", "", item.strip(" .;")))
    if not cleaned:
        return ""
    if re.search(rf"(?i)\b{_STRUCTURAL_MODAL_PATTERN}\b", cleaned):
        return cleaned.rstrip(".") + "."
    if re.search(r"(?i)(?:à|a|de)\s*$", subject_modal):
        cleaned = _LEADING_INFINITIVE_LINK_RE.sub("", cleaned)
    return f"{subject_modal} {_lowercase_first_alpha(cleaned)}.".strip()


def _split_modal_infinitive_chain(subject_modal: str, body: str) -> list[str]:
    body_clean = normalize_spaces(body).strip(" .;")
    if not body_clean:
        return []

    parts = [
        normalize_spaces(seg.strip(" .;"))
        for seg in re.split(r"\s*;\s*|\s*(?:\n|•)\s*", body_clean)
        if seg.strip(" .;")
    ]
    if len(parts) < 2:
        coordinated_parts = [
            normalize_spaces(seg.strip(" .;"))
            for seg in _COORDINATED_INFINITIVE_SPLIT_RE.split(body_clean)
            if seg.strip(" .;")
        ]
        if len(coordinated_parts) >= 2:
            parts = coordinated_parts

    if len(parts) < 2:
        comma_parts = [
            normalize_spaces(seg.strip(" .;"))
            for seg in _COMMA_COORDINATED_INFINITIVE_SPLIT_RE.split(body_clean)
            if seg.strip(" .;")
        ]
        if len(comma_parts) >= 2:
            parts = comma_parts

    if len(parts) < 2:
        return []

    rebuilt = [_normalize_split_item(part, subject_modal) for part in parts]
    rebuilt = [part for part in rebuilt if part]
    return _dedupe_split_requirements(rebuilt)


def _split_documentary_semicolon_chain(requirement_text: str) -> list[str]:
    req = normalize_spaces(requirement_text).strip(" .;")
    if ";" not in req:
        return []

    parts = [normalize_spaces(part.strip(" .;")) for part in req.split(";") if part.strip(" .;")]
    if len(parts) < 2:
        return []

    intro_match = _DOCUMENTARY_INTRO_RE.match(parts[0])
    if not intro_match:
        return []

    intro = normalize_spaces(intro_match.group(1))
    first_item = normalize_spaces(intro_match.group(2))
    if not intro or not first_item:
        return []

    rebuilt = [f"{intro} {_lowercase_first_alpha(first_item)}."]
    for part in parts[1:]:
        clean = normalize_spaces(re.sub(r"(?i)^et\s+", "", part).strip(" .;"))
        if not clean:
            continue
        if re.search(rf"(?i)\b{_STRUCTURAL_MODAL_PATTERN}\b", clean):
            rebuilt.append(clean.rstrip(".") + ".")
        else:
            rebuilt.append(f"{intro} {_lowercase_first_alpha(clean)}.")
    rebuilt = _dedupe_split_requirements(rebuilt)
    return rebuilt if len(rebuilt) >= 2 else []


def _split_repeated_modal_chain(requirement_text: str) -> list[str]:
    req = normalize_spaces(requirement_text).strip(" .;")
    lead_match = re.match(rf"(?i)^(.+?)\s+({_STRUCTURAL_MODAL_PATTERN})\s+(.+)$", req)
    if not lead_match:
        return []

    subject = normalize_spaces(lead_match.group(1))
    first_modal = normalize_spaces(lead_match.group(2))
    remainder = normalize_spaces(lead_match.group(3))
    if not subject or not first_modal or not remainder:
        return []

    modal_segments = [
        normalize_spaces(seg.strip(" .;"))
        for seg in _REPEATED_MODAL_SEGMENT_SPLIT_RE.split(f"{first_modal} {remainder}")
        if seg.strip(" .;")
    ]
    if len(modal_segments) < 2:
        return []

    rebuilt: list[str] = []
    for seg in modal_segments:
        if re.match(rf"(?i)^(?:{_STRUCTURAL_MODAL_PATTERN})\b", seg):
            rebuilt.append(f"{subject} {seg}.")
    rebuilt = _dedupe_split_requirements(rebuilt)
    return rebuilt if len(rebuilt) >= 2 else []


def normalize_requirement_text_by_type(
    requirement_text: str,
    source_snippet: str,
    req_type: str,
    chunk_text: str,
) -> str:
    req = repair_common_ocr_artifacts(strip_requirement_leading_noise(normalize_spaces(requirement_text)))
    src = repair_common_ocr_artifacts(strip_requirement_leading_noise(normalize_spaces(source_snippet)))
    rt = (req_type or "").strip().upper()
    req = normalize_subject_from_context(req, src, chunk_text)

    if rt == "RESPONSABILITE":
        req = re.sub(r"(?i)^(.+?)\s+est tenu de répondre de\b", r"\1 répond de", req)

    if rt == "INTERDICTION":
        if has_missing_scope_context(req, src):
            req = src
        req = re.sub(r"\s+", " ", req).strip()

    if rt == "EXCEPTION":
        src_clean = re.sub(r"(?i)^\s*(cependant|toutefois)\s*,\s*", "", src).strip()
        req_lower = req.lower()
        src_lower = src_clean.lower()

        if ("lorsque" in src_lower or "si " in src_lower or "en cas de" in src_lower) and not (
            "lorsque" in req_lower or "si " in req_lower or "en cas de" in req_lower
        ):
            req = src_clean
        else:
            req = re.sub(r"(?i)^\s*(cependant|toutefois)\s*,\s*", "", req).strip()

        subject = infer_main_subject(chunk_text)
        if subject:
            req = re.sub(r"(?i)\bune personne\b", subject.lower(), req)
            req = re.sub(r"(?i)\bcette personne\b", subject.lower(), req)
            req = re.sub(r"(?i)\bil n['’]en\b", f"{subject} n'en", req, count=1)
            req = re.sub(r"(?i)\bil ne\b", f"{subject} ne", req, count=1)
            req = re.sub(r"(?i)\bil répond\b", f"{subject} répond", req, count=1)
            req = re.sub(r"(?i)\bil repond\b", f"{subject} répond", req, count=1)
            req = re.sub(r"(?i)\belle n['’]en\b", f"{subject} n'en", req, count=1)
            req = re.sub(r"(?i)\belle ne\b", f"{subject} ne", req, count=1)

        req = re.sub(r",\s+Le salarié\b", ", le salarié", req)
        req = re.sub(r",\s+L'employeur\b", ", l'employeur", req)
        req = re.sub(r",\s+L'entreprise\b", ", l'entreprise", req)
        req = req.strip()
        if req:
            req = req[0].upper() + req[1:]
        return req.strip()

    if rt == "OBLIGATION":
        src_lower = src.lower()
        req_lower = req.lower()
        subject = infer_main_subject(chunk_text)

        if "lorsque" in src_lower and "lorsque" not in req_lower:
            match = re.search(r"(?i)(lorsque[^,]+),?\s*(.*)", src)
            if match:
                condition_part = match.group(1).strip()
                req = f"{condition_part}, {req}"

        if subject and re.match(r"(?i)^(attendre|avertir|informer|prévenir|prevenir|restituer)\b", req.strip()):
            req = f"{subject} doit {req.strip()}"

        if subject:
            match = re.match(
                r"(?i)^(lorsque[^,]+,\s*)(attendre|avertir|informer|prévenir|prevenir|restituer)\b(.*)$",
                req.strip(),
            )
            if match:
                prefix = match.group(1).strip()
                verb = match.group(2).strip()
                rest = match.group(3).strip()
                req = f"{prefix} {subject} doit {verb} {rest}".strip()
                req = re.sub(r"\s+", " ", req)

            req = re.sub(r"(?i)^(lorsque[^,]+,\s*)il doit\b", rf"\1{subject} doit", req, count=1)
            req = re.sub(r"(?i)^il doit\b", f"{subject} doit", req, count=1)

        req = re.sub(
            r"(?i)s['’]il n['’]?y a péril en la demeure",
            "s'il n'y a pas péril en la demeure",
            req,
        )
        req = re.sub(r",\s+Le salarié\b", ", le salarié", req)
        req = re.sub(r",\s+L'employeur\b", ", l'employeur", req)
        req = re.sub(r",\s+L'entreprise\b", ", l'entreprise", req)

    return req.strip()


def refine_requirement_text_quality(
    requirement_text: str,
    source_snippet: str,
    req_type: str,
    chunk_text: str,
    *,
    max_chars: int = 320,
) -> str:
    """
    Améliore la lisibilité métier sans perdre l'ancrage juridique:
    - réécrit quelques formulations trop vagues ("il convient de ...")
    - compacte les détails faibles qui allongent inutilement
    - coupe proprement uniquement si le tail ne contient pas de condition critique.
    """
    req = repair_common_ocr_artifacts(strip_requirement_leading_noise(normalize_spaces(requirement_text)))
    src = repair_common_ocr_artifacts(strip_requirement_leading_noise(normalize_spaces(source_snippet)))
    rt = (req_type or "").strip().upper()

    if not req:
        return req

    subject = infer_main_subject(chunk_text) or infer_main_subject(src)
    req = normalize_subject_from_context(req, src, chunk_text)

    if subject and rt in {"OBLIGATION", "DECLARATION", "REGISTRE", "CONTROLE", "RESPONSABILITE"}:
        req = _VAGUE_OBLIGATION_PREFIX_RE.sub(f"{subject} doit ", req)
        req = re.sub(r"(?i)^doit\b", f"{subject} doit", req, count=1)
        req = re.sub(r"(?i)^doivent\b", f"{subject} doivent", req, count=1)
        if _LEADING_INFINITIVE_OBLIGATION_RE.match(req):
            req = f"{subject} doit {_lowercase_first_alpha(req)}"

    req = _INLINE_HEADING_FRAGMENT_RE.sub("", req).strip()

    if not any(cue in req.lower() for cue in _COND_OR_EXCEPTION_CUES):
        req = _WEAK_DETAIL_TAIL_RE.sub("", req)
    req = normalize_spaces(req).strip(" ;,")

    if len(req) > max_chars and rt not in {"CONDITION", "EXCEPTION"}:
        cut_candidates = [req.rfind("; ", 0, max_chars), req.rfind(". ", 0, max_chars), req.rfind(", ", 0, max_chars)]
        cut_at = max(cut_candidates)
        if cut_at >= int(max_chars * 0.6):
            tail = req[cut_at + 1 :].lower()
            if not any(cue in tail for cue in _COND_OR_EXCEPTION_CUES):
                req = req[: cut_at + 1].strip(" ;,")

    req = normalize_spaces(req)
    if req and req[-1] not in ".!?":
        req = f"{req}."

    return req


def split_fused_obligation(requirement_text: str, req_type: str) -> list[str]:
    req = normalize_spaces(requirement_text)

    if req_type not in _SPLITTABLE_REQ_TYPES:
        return [req]

    documentary_semicolon_split = _split_documentary_semicolon_chain(req)
    if len(documentary_semicolon_split) >= 2:
        return documentary_semicolon_split

    match = re.match(
        r"(?i)^(lorsque[^,]+,\s*)([^,]*?)doit\s+en avertir l'employeur\s+et\s+attendre\s+ses\s+instructions\s+(s['’]il n['’]?y a pas péril en la demeure\.?)$",
        req,
    )
    if match:
        prefix = match.group(1).strip()
        before = match.group(2).strip()
        condition = match.group(3).strip()

        subject = before if before else "le salarié"
        r1 = f"{prefix} {subject} doit en avertir l'employeur."
        r2 = f"{prefix} {subject} doit attendre ses instructions {condition}"
        return _dedupe_split_requirements([normalize_spaces(r1), normalize_spaces(r2)])

    repeated_aux_match = _REPEATED_AUXILIARY_CHAIN_RE.match(req)
    if repeated_aux_match:
        subject = normalize_spaces(repeated_aux_match.group(1))
        aux_1 = normalize_spaces(repeated_aux_match.group(2))
        pred_1 = normalize_spaces(repeated_aux_match.group(3).strip(" .;"))
        aux_2 = normalize_spaces(repeated_aux_match.group(4))
        pred_2 = normalize_spaces(repeated_aux_match.group(5).strip(" .;"))
        if subject and pred_1 and pred_2 and pred_1.lower() != pred_2.lower():
            return _dedupe_split_requirements(
                [
                    f"{subject} {aux_1} {pred_1}.",
                    f"{subject} {aux_2} {pred_2}.",
                ]
            )

    repeated_modal_split = _split_repeated_modal_chain(req)
    if len(repeated_modal_split) >= 2:
        return repeated_modal_split

    composition_chain_match = _COMPOSITION_SEMICOLON_CHAIN_RE.match(req)
    if composition_chain_match:
        subject_modal = normalize_spaces(composition_chain_match.group(1))
        first_part = normalize_spaces(composition_chain_match.group(2).strip(" .;"))
        second_part = normalize_spaces(composition_chain_match.group(3).strip(" .;"))
        if subject_modal and first_part and second_part:
            return _dedupe_split_requirements(
                [
                    f"{subject_modal} {first_part}.",
                    f"{subject_modal} {second_part}.",
                ]
            )

    # Cas général: "Le jury doit: proposer...; évaluer...; classer..."
    colon_match = re.match(rf"(?i)^(.+?{_STRUCTURAL_MODAL_PATTERN})\s*:\s*(.+)$", req)
    if colon_match:
        subject_modal = normalize_spaces(colon_match.group(1))
        items_part = colon_match.group(2).strip(" .;")
        if items_part:
            split_items = _split_modal_infinitive_chain(subject_modal, items_part)
            if len(split_items) >= 2:
                return split_items

    # Cas général: obligation longue chaînée par ';'
    if ";" in req:
        semicolon_parts = [normalize_spaces(part.strip(" .;")) for part in req.split(";") if part.strip(" .;")]
        if len(semicolon_parts) >= 2:
            first = semicolon_parts[0]
            subject_modal_match = re.match(rf"(?i)^(.+?{_STRUCTURAL_MODAL_PATTERN})\s+(.+)$", first)
            if subject_modal_match:
                subject_modal = normalize_spaces(subject_modal_match.group(1))
                rebuilt = [first.rstrip(".") + "."]
                for part in semicolon_parts[1:]:
                    if re.search(rf"(?i)\b{_STRUCTURAL_MODAL_PATTERN}\b", part):
                        rebuilt.append(part.rstrip(".") + ".")
                    else:
                        rebuilt.append(_normalize_split_item(part, subject_modal))
                rebuilt = _dedupe_split_requirements(rebuilt)
                if len(rebuilt) >= 2:
                    return rebuilt

    modal_chain_match = _MODAL_INFINITIVE_CHAIN_RE.match(req)
    if modal_chain_match:
        subject_modal = normalize_spaces(modal_chain_match.group(1))
        split_items = _split_modal_infinitive_chain(subject_modal, modal_chain_match.group(2))
        if len(split_items) >= 2:
            return split_items

    return [req]


_GROUND_TOKEN_RE = re.compile(r"[a-zàâäéèêëîïôöùûüç0-9]{3,}", re.IGNORECASE)
_GROUND_STOPWORDS = {
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
    "été",
    "ete",
    "qui",
    "que",
    "dont",
    "ainsi",
    "tout",
    "toute",
    "tous",
    "toutes",
    "present",
    "présent",
    "presente",
    "présente",
    "article",
    "articles",
    "loi",
    "decret",
    "arrête",
    "arrete",
}


def _ground_tokens(text: str) -> set[str]:
    tokens = set()
    for tok in _GROUND_TOKEN_RE.findall((text or "").lower()):
        if tok in _GROUND_STOPWORDS:
            continue
        tokens.add(tok)
    return tokens


def _ground_overlap(req_text: str, src_text: str) -> float:
    req_toks = _ground_tokens(req_text)
    src_toks = _ground_tokens(src_text)
    if not req_toks or not src_toks:
        return 0.0
    return len(req_toks & src_toks) / len(req_toks)


def _matches_split_source_subrequirement(req_text: str, src_text: str, req_type: str) -> bool:
    rt = (req_type or "").strip().upper()
    if rt not in _SPLITTABLE_REQ_TYPES:
        return False

    split_parts = split_fused_obligation(src_text, rt)
    if len(split_parts) < 2:
        return False

    req_norm = normalize_spaces(req_text).strip(" .;").lower()
    if not req_norm:
        return False

    for part in split_parts:
        part_norm = normalize_spaces(part).strip(" .;").lower()
        if not part_norm:
            continue
        if req_norm == part_norm or req_norm in part_norm:
            return True
        if _ground_overlap(req_text, part) >= 0.72:
            return True
    return False


def ground_requirement_to_source(
    requirement_text: str,
    source_snippet: str,
    req_type: str,
) -> str:
    """
    Ancrage qualité:
    - si la formulation extraite perd des éléments juridiques critiques
      (condition/portée) ou diverge trop du snippet source,
      on préfère la formulation source nettoyée.
    """
    req = strip_requirement_leading_noise(normalize_spaces(requirement_text))
    src = strip_requirement_leading_noise(normalize_spaces(source_snippet))
    rt = (req_type or "").strip().upper()

    if not req or not src:
        return req

    if is_out_of_scope_individual_requirement(req, src):
        return req

    src_has_normative_signal = has_legal_risk_markers(src)

    if rt in {
        "OBLIGATION",
        "INTERDICTION",
        "RESPONSABILITE",
        "EXCEPTION",
        "CONDITION",
        "DECLARATION",
        "CONTROLE",
        "REGISTRE",
        "AUTRE",
    } and src_has_normative_signal:
        if _matches_split_source_subrequirement(req, src, rt):
            return req
        if rt == "CONDITION":
            src_lower = src.lower()
            req_lower = req.lower()
            participation_scope = ("peuvent participer" in src_lower) or ("peut participer" in src_lower)
            if participation_scope:
                def _criteria_count(text: str) -> int:
                    count = 0
                    if "titulaire" in text or "titulaires" in text:
                        count += 1
                    if "anciennet" in text:
                        count += 1
                    if (
                        "diplom" in text
                        or "licence" in text
                        or "maitrise" in text
                        or "maîtrise" in text
                    ):
                        count += 1
                    return count

                src_criteria = _criteria_count(src_lower)
                req_criteria = _criteria_count(req_lower)
                if src_criteria >= 2 and req_criteria < src_criteria:
                    return src
        if has_missing_legal_condition(req, src):
            return src
        if has_missing_scope_context(req, src):
            return src
        overlap = _ground_overlap(req, src)
        if overlap < 0.35 and len(src) <= max(320, int(len(req) * 2.4)):
            return src

    return req


# =========================
# Citation / confidence / status
# =========================
def format_citation_ref(article_label: str, start_page: Optional[int], end_page: Optional[int]) -> str:
    sp = (start_page if start_page is not None else 0) + 1
    ep = (end_page if end_page is not None else sp - 1) + 1
    return f"{article_label} (p.{sp}-{ep})"


def classify_confidence(
    requirement_text: str,
    snippet: str,
    chunk_text: str,
    req_type: str,
) -> float:
    """
    Heuristique conservatrice.
    Ce score sert au tri interne, pas à une validation juridique automatique.
    """
    req = normalize_spaces(requirement_text)
    src = normalize_spaces(snippet)
    chunk = normalize_spaces(chunk_text)

    req_lower = req.lower()
    src_lower = src.lower()
    chunk_lower = chunk.lower()
    req_type_upper = (req_type or "").strip().upper()

    score = 0.58

    if len(req) >= 20:
        score += 0.05
    if len(req) >= 40:
        score += 0.04

    if len(src) >= 25:
        score += 0.05

    if src_lower and src_lower in chunk_lower:
        score += 0.08
    else:
        score -= 0.12

    if req_type_upper in {
        "OBLIGATION",
        "INTERDICTION",
        "RESPONSABILITE",
        "EXCEPTION",
        "CONDITION",
        "DECLARATION",
        "CONTROLE",
        "REGISTRE",
    }:
        score += 0.05

    strong_normative_markers = [
        "doit",
        "doivent",
        "interdit",
        "est interdit",
        "répond",
        "repond",
        "responsable",
        "sauf",
        "lorsque",
        "en cas de",
        "tenu de",
        "tenus de",
    ]
    if contains_any(req_lower, strong_normative_markers):
        score += 0.05

    if has_missing_legal_condition(req, src):
        score -= 0.10
    if has_subject_mismatch(req, chunk):
        score -= 0.12
    if has_missing_scope_context(req, src):
        score -= 0.08
    if is_invented_from_non_normative_snippet(req, src):
        score -= 0.15

    if has_heavy_ocr_artifact_signals(req):
        score -= 0.22
    elif has_ocr_artifact_signals(req):
        score -= 0.10

    if req.startswith(("Il ", "Elle ", "il ", "elle ")):
        score -= 0.06

    if is_non_actionable_scope_requirement(req, src):
        score -= 0.20
    if is_partial_exception_requirement(req, src):
        score -= 0.12
    if is_low_value_requirement_text(req):
        score -= 0.20

    return round(max(0.20, min(score, 0.97)), 2)


def compute_status(
    confidence: float,
    requirement_text: str,
    snippet: str,
    chunk_text: str,
    req_type: str = "",
) -> str:
    src = normalize_spaces(snippet)
    chunk = normalize_spaces(chunk_text)

    if not src or len(src) < 12:
        return "TO_VALIDATE"
    if normalize_spaces(src).lower() not in chunk.lower():
        return "TO_VALIDATE"
    if has_missing_legal_condition(requirement_text, src):
        return "TO_VALIDATE"
    if has_subject_mismatch(requirement_text, chunk):
        return "TO_VALIDATE"
    if has_missing_scope_context(requirement_text, src):
        return "TO_VALIDATE"
    if is_invented_from_non_normative_snippet(requirement_text, src):
        return "TO_VALIDATE"
    if has_unresolved_subject_reference(requirement_text, chunk):
        return "TO_VALIDATE"
    if has_heavy_ocr_artifact_signals(requirement_text):
        return "TO_VALIDATE"

    return "DRAFT" if confidence >= 0.86 else "TO_VALIDATE"

def expand_introductory_documentary_requirement(
    requirement_text: str,
    source_snippet: str,
    req_type: str,
) -> str:
    """
    Développe les formulations documentaires trop vagues du type :
    - "La notification doit comprendre les indications suivantes."
    - "Le dossier doit comprendre les pièces suivantes."
    - "La demande doit être accompagnée des pièces suivantes."
    en règle explicite complète si le snippet source contient la liste juste après.
    """
    req = normalize_spaces(requirement_text)
    src = normalize_spaces(source_snippet)
    rt = (req_type or "").strip().upper()

    if rt not in {"OBLIGATION", "DECLARATION", "REGISTRE", "CONTROLE"}:
        return req

    req_lower = req.lower()

    intro_patterns = [
        "doit comprendre les indications suivantes",
        "doit indiquer les indications suivantes",
        "doit comporter les indications suivantes",
        "doit contenir les indications suivantes",
        "doit comprendre les mentions suivantes",
        "doit contenir les mentions suivantes",
        "doit comprendre les pièces suivantes",
        "doit contenir les pièces suivantes",
        "doit être accompagné des pièces suivantes",
        "doit etre accompagne des pieces suivantes",
        "doit être accompagnée des pièces suivantes",
        "doit etre accompagnee des pieces suivantes",
        "doit être accompagné de la liste suivante",
        "doit etre accompagne de la liste suivante",
        "doit être accompagnée de la liste suivante",
        "doit etre accompagnee de la liste suivante",
    ]

    if not any(pat in req_lower for pat in intro_patterns):
        return req

    list_part = None

    if ":" in src:
        after_colon = src.split(":", 1)[1].strip(" .;")
        if after_colon:
            list_part = after_colon

    if not list_part:
        marker_patterns = [
            r"(?i)indications suivantes\s*[:\-–—]?\s*(.+)$",
            r"(?i)mentions suivantes\s*[:\-–—]?\s*(.+)$",
            r"(?i)pi[eè]ces suivantes\s*[:\-–—]?\s*(.+)$",
            r"(?i)liste suivante\s*[:\-–—]?\s*(.+)$",
        ]
        for pattern in marker_patterns:
            m = re.search(pattern, src)
            if m:
                candidate = m.group(1).strip(" .;")
                if candidate:
                    list_part = candidate
                    break

    if not list_part:
        return req

    list_part = normalize_spaces(list_part)

    subject = re.sub(r"(?i)\s+doit\s+.*$", "", req).strip()
    if not subject:
        return req

    if any(x in req_lower for x in ["indications suivantes", "mentions suivantes"]):
        return normalize_spaces(f"{subject} doit indiquer {list_part}.")

    if "pièces suivantes" in req_lower or "pieces suivantes" in req_lower:
        return normalize_spaces(f"{subject} doit comprendre {list_part}.")

    if "liste suivante" in req_lower:
        return normalize_spaces(f"{subject} doit être accompagné de {list_part}.")

    return req


def split_introductory_documentary_requirement(
    requirement_text: str,
    source_snippet: str,
    req_type: str,
    chunk_text: str = "",
) -> list[str]:
    """
    Scinde prudemment les exigences documentaires/listes quand le texte source
    contient une vraie enumeration exploitable.

    Exemple:
    - "La declaration doit comporter les indications suivantes : le nom ;
      l'adresse ; la date de naissance."
    =>
    - "La declaration doit indiquer le nom."
    - "La declaration doit indiquer l'adresse."
    - "La declaration doit indiquer la date de naissance."
    """
    req = normalize_spaces(requirement_text)
    rt = (req_type or "").strip().upper()
    if rt not in {"OBLIGATION", "DECLARATION", "REGISTRE", "CONTROLE"}:
        return [req]

    req_lower = req.lower()
    if not any(marker in req_lower for marker in ("indications suivantes", "mentions suivantes")):
        return [req]

    subject_match = re.match(
        r"(?i)^(.+?)\s+"
        r"(?:doit(?:vent)?|comporte(?:nt)?|comprend(?:ent)?|contient(?:ent)?|"
        r"indique(?:nt)?|mentionne(?:nt)?)\b",
        req,
    )
    subject = normalize_spaces((subject_match.group(1) if subject_match else "").strip(" -:;,."))
    if not subject:
        return [req]

    list_part = ""
    candidate_sources = [source_snippet, chunk_text]
    capture_patterns = [
        r"(?is)indications suivantes\s*[:\-–—]?\s*(.+)$",
        r"(?is)mentions suivantes\s*[:\-–—]?\s*(.+)$",
    ]
    for raw_candidate in candidate_sources:
        candidate = str(raw_candidate or "").strip()
        if not candidate:
            continue
        if ":" in candidate:
            after_colon = candidate.split(":", 1)[1].strip()
            if after_colon and (
                ";" in after_colon
                or "\n" in after_colon
                or re.search(r"(?i)(?:^|\s)(?:\d+[.)]|[a-z][.)]|[-•])\s+", after_colon)
            ):
                list_part = after_colon
                break
        for pattern in capture_patterns:
            match = re.search(pattern, candidate)
            if not match:
                continue
            extracted = match.group(1).strip()
            if extracted and (
                ";" in extracted
                or "\n" in extracted
                or re.search(r"(?i)(?:^|\s)(?:\d+[.)]|[a-z][.)]|[-•])\s+", extracted)
            ):
                list_part = extracted
                break
        if list_part:
            break

    if not list_part:
        return [req]

    list_part = re.split(r"(?<=[.!?])\s+", list_part, maxsplit=1)[0].strip()
    if not list_part:
        return [req]

    items_text = list_part
    items_text = re.sub(
        r"(?im)(?:^|[\n\r]+)\s*(?:[-•]|\d+[.)]|[a-z][.)])\s+",
        "; ",
        items_text,
    )
    items_text = re.sub(
        r"(?<=\S)\s+(?=(?:\d+[.)]|[a-z][.)]|[-•])\s+)",
        "; ",
        items_text,
    )
    items_text = items_text.replace("\r", "\n")
    if "\n" in items_text:
        items_text = re.sub(r"\n+", "; ", items_text)

    raw_items = [
        normalize_spaces(
            re.sub(r"^(?:-|\d+[.)]?|[a-z][.)])\s*", "", item.strip(" .;,:"))
        )
        for item in re.split(r"\s*;\s*", items_text)
        if item.strip(" .;,:")
    ]
    cleaned_items: list[str] = []
    for item in raw_items:
        if not item or len(item) < 3:
            continue
        item = re.sub(r"(?i)^et\s+", "", item).strip(" .;,:")
        if not item or len(item) < 3:
            continue
        if re.match(
            r"(?i)^(?:a compter|à compter|dans un delai|dans les|lorsque|en cas|si|au plus tard|durant|pendant)\b",
            item,
        ):
            continue
        cleaned_items.append(item)

    cleaned_items = _dedupe_split_requirements(
        [f"{subject} {'doivent' if re.match(r'(?i)^(?:les|ces|des|tous|toutes)\\b', subject) else 'doit'} "
         f"indiquer {_lowercase_first_alpha(item)}."
         for item in cleaned_items]
    )
    if 2 <= len(cleaned_items) <= 8:
        return cleaned_items
    return [req]
