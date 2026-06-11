import datetime as dt
import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Any


PRECALL_NLP_VERSION = "B1.0-B1.4-1.4.0"

_LIGATURE_REPLACEMENTS = {
    "œ": "oe",
    "Œ": "OE",
    "æ": "ae",
    "Æ": "AE",
    "ﬁ": "fi",
    "ﬂ": "fl",
}

_PUNCT_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2033": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
    }
)

_SOFT_HYPHEN_RE = re.compile("\u00ad")
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
_OCR_PIPE_RE = re.compile(r"(?<=\w)\|(?=\w)")
_HYPHEN_LINEBREAK_RE = re.compile(r"(?<=\w)[\-\u2010\u2011\u2012\u2013\u2014]\s*\n\s*(?=\w)")
_STANDALONE_PAGE_NUM_RE = re.compile(r"(?m)^\s*[-–—]?\s*\d{1,4}\s*[-–—]?\s*$")
_PAGE_HEADER_RE = re.compile(
    r"(?im)^\s*(journal\s+officiel|republique\s+tunisienne|jort)\b.*$"
)
_TRAILING_SPACE_LINE_RE = re.compile(r"(?m)[ \t]+$")
_SPACE_RUN_RE = re.compile(r"[ \t]{2,}")
_MULTI_BLANK_LINES_RE = re.compile(r"\n{3,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,;:.!?])")
_MISSING_SPACE_AFTER_PUNCT_RE = re.compile(r"([,;:.!?])(?=[^\s\n])")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n{2,}")
_LONG_UNIT_SPLIT_RE = re.compile(r"(?<=[\.;])\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[\.;!?])\s+(?=[A-Z0-9ÀÂÄÇÉÈÊËÎÏÔÖÙÛÜ\(])")
_DASH_SENTENCE_SPLIT_RE = re.compile(r"(?<=[\.;!?])\s+(?=[-â€“â€”]\s+[A-Z0-9\(])")
_ARTICLE_HEADER_RE = re.compile(
    r"(?i)^\s*(?:\[\s*)?(?:article\s+(?:premier|1er|\d+)|art\.?\s*\d+(?:[-\.]\d+)*)\s*[\]\-:–—]*\s*"
)
_LIST_LINE_RE = re.compile(
    r"(?im)^\s*(?:[-*•▪◦]|[a-z]\)|\d+\)|\d+\s*[-.:)]|[ivxlcdm]+\))\s+"
)
_LIST_ITEM_MARKER_RE = re.compile(
    r"(?im)(?:^|\n|\s)(?:[-*â€¢â–ªâ—¦]|[a-z]\)|\d+\)|\d+\s*[-.:)]|[ivxlcdm]+\))\s+"
)
_OCR_HARD_BOUNDARY_RE = re.compile(
    r"(?i)\s+(?=(?:par\s+arr\w+\b|vu\b|monsieur\b|madame\b|"
    r"le\s+ministre\b|la\s+ministre\b|la\s+cheffe\s+du\s+gouvernement\b|"
    r"arr\w+\s*:|article\s+premier\s*-\s|art\.?\s*\d+\s*-\s|"
    r"(?:chapitre|section|titre|livre)\s+[ivxlcdm0-9]+\b(?:\s*[-.:])?\s+(?=[A-Z0-9])))"
)
_OCR_ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]+")
_OCR_WORD_DIGIT_RE = re.compile(r"(?<=[A-Za-z])\d+(?=[A-Za-z]|\s)")
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
_ABBREVIATION_TOKENS = [
    ("Art.", "Art§"),
    ("art.", "art§"),
    ("M.", "M§"),
    ("Mme.", "Mme§"),
    ("Dr.", "Dr§"),
    ("etc.", "etc§"),
]

_NORMATIVE_RULES: list[tuple[str, re.Pattern[str], float]] = [
    ("MANDATORY_DOIT", re.compile(r"(?i)\bdoit(?:vent)?\b"), 1.4),
    ("MANDATORY_TENU", re.compile(r"(?i)\best\s+tenu(?:e|s|es)?\s+de\b"), 1.4),
    ("INTERDICTION", re.compile(r"(?i)\b(?:est|sont)\s+interdit(?:e|es|s)?\b|\bne\s+doit(?:vent)?\s+pas\b"), 1.6),
    ("CONDITION", re.compile(r"(?i)\blorsque\b|\ben\s+cas\s+de\b|\bsi\b|\bà\s+condition\s+de\b|\ba\s+condition\s+de\b"), 0.8),
    ("EXCEPTION", re.compile(r"(?i)\bsauf\b|\bà\s+l['’]exception\s+de\b|\ba\s+l['’]exception\s+de\b"), 0.9),
    ("SANCTION", re.compile(r"(?i)\best\s+puni(?:e|s|es)?\b|\bpuni(?:e|s|es)?\s+de\b"), 1.5),
    ("RESPONSABILITE", re.compile(r"(?i)\best\s+responsable\b|\brépond\s+de\b|\brepond\s+de\b"), 1.2),
    ("REGISTRE", re.compile(r"(?i)\bregistre\b"), 0.5),
    (
        "EFFECT_LEGAL",
        re.compile(
            r"(?i)\bb[ée]n[ée]fici\w*\b|\bprend\s+en\s+charge\b|\best\s+accord[ée]e?\b|"
            r"\bsont\s+fix[ée]es?\b|\bsont\s+recouvr[ée]es?\b|\bsont\s+affect[ée]es?\b|"
            r"\bentra[iî]ne\b|\bouvre\s+droit\b|\bcommuniqu\w*\b|\btransmett\w*\b|\badress\w*\b"
        ),
        0.95,
    ),
]

_DESCRIPTIVE_RULES: list[tuple[str, re.Pattern[str], float]] = [
    ("DEFINITION", re.compile(r"(?i)\bon\s+entend\s+par\b|\bdésigne\b|\bdesigne\b|\best\s+défini(?:e)?\s+comme\b"), 1.6),
    ("NOMINATION", re.compile(r"(?i)\best\s+nomm[ée]e?\b|\bsont\s+nomm[ée]s\b|\best\s+désign[ée]e?\b|\best\s+charge\b"), 1.5),
    (
        "SOMMAIRE",
        re.compile(
            r"(?im)^\s*(?:chapitre|section|titre|livre|sommaire)\b(?:\s+[ivxlcdm0-9]+)?(?:\s*[:.\-])?"
        ),
        1.3,
    ),
    ("REFERENCE_META", re.compile(r"(?i)\bla\s+présente\s+loi\b|\bla\s+presente\s+loi\b|\bjournal\s+officiel\b"), 1.0),
    ("ORGANISATION", re.compile(r"(?i)\bobjet\s+de\s+la\s+loi\b|\bdispositions\s+générales\b|\bdispositions\s+generales\b"), 0.9),
    (
        "PUBLICATION_BOILERPLATE",
        re.compile(
            r"(?i)\ble\s+pr[ée]sent\s+(?:arr[êe]t[ée]?(?:\s+conjoint)?|d[ée]cret|loi)\s+sera\s+publi[ée]\b"
        ),
        1.8,
    ),
    (
        "META_REGULATORY_INTRO",
        re.compile(
            r"(?i)\ble\s+pr[Ã©e]sent\s+(?:arr[Ãªe]t[Ã©e]?|d[Ã©e]cret|code|loi)\s+"
            r"(?:fixe|d[Ã©e]finit|d[Ã©e]termine|pr[Ã©e]cise)\b|"
            r"\bfixe\s+les\s+conditions\s+d['â€™]application\b"
        ),
        1.7,
    ),
    (
        "ACTE_INDIVIDUEL_NAME",
        re.compile(
            r"(?i)\b(?:monsieur|madame|m\.|mme\.?)\s+[a-zàâäéèêëîïôöùûüç][a-zàâäéèêëîïôöùûüç'\-]{1,}"
        ),
        0.6,
    ),
    (
        "ACTE_INDIVIDUEL_APPOINTMENT",
        re.compile(
            r"(?i)\b(?:est|sont)\s+(?:nomm[ée]e?s?|d[ée]sign[ée]e?s?)\b|"
            r"\bcharg[ée]e?s?\s+des\s+fonctions\s+de\b|"
            r"\bacceptation\s+de\s+d[ée]mission\b|"
            r"\bd[ée]l[ée]gation\s+de\s+signature\s+accord[ée]e?\s+[àa]\b"
        ),
        0.9,
    ),
    # Phase 1: organisational creation / concours opening / conformity boilerplate
    (
        "CREATION_ORG",
        re.compile(
            r"(?i)\bil\s+est\s+(?:cr[ée][ée]e?|institu[ée]e?|[ée]tabli[ée]?|ouvert)\b|"
            r"\best\s+organis[ée]e?\s+conform[ée]ment\s+aux\s+dispositions\s+du\s+pr[ée]sent\b|"
            r"\bconcours\b.{0,60}\best\s+ouvert\b"
        ),
        1.1,
    ),
]

_STRONG_NORMATIVE_RULES = {"MANDATORY_DOIT", "MANDATORY_TENU", "INTERDICTION", "SANCTION"}
_NON_ARTICLE_KEYWORDS = (
    "annexe",
    "sommaire",
    "nomination",
    "demission",
    "démission",
    "acceptation",
    "table",
    "tableau",
)
_ARTICLE_CODE_RE = re.compile(
    r"(?i)\b(?:article|art\.?)\s*(?:n\s*)?(premier|1er|unique|\d+(?:[-\.]\d+)*)(?:\s*\(?\s*(bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies|undecies|duodecies)\s*\)?)?"
)
_FOREIGN_ARTICLE_HEADER_RE = re.compile(
    r"(?i)(?<![a-zàâäéèêëîïôöùûüç'’])(?:article|art\.?)\s*(?:n\s*)?"
    r"(premier|1er|\d+(?:[-\.]\d+)*)\s*[-:–—]"
)
_FOREIGN_ARTICLE_BARE_NUMBER_RE = re.compile(
    r"(?i)(?<!\w)(\d{1,3})\s*-\s*(?=(?:est|sont|le|la|les|si|tout|toute|peuvent|ne\s+peuvent|article|art\.))"
)


def _extract_article_code_from_label(article_label: str | None) -> str | None:
    label = re.sub(r"\s+", " ", (article_label or "")).strip().lower()
    if not label:
        return None
    m = _ARTICLE_CODE_RE.search(label)
    if not m:
        return None
    main = (m.group(1) or "").lower().replace(".", "-")
    if main == "1er":
        main = "premier"
    suffix = (m.group(2) or "").lower()
    return f"{main}-{suffix}" if suffix else main


def _extract_article_main_numeric(code: str | None) -> str | None:
    token = (code or "").strip().lower().replace(".", "-")
    if not token:
        return None
    if token == "1er":
        token = "premier"
    if re.fullmatch(r"\d+", token):
        return str(int(token))
    if re.fullmatch(r"\d+(?:-\d+)*", token):
        return token.split("-", 1)[0]
    return None


def _trim_cross_article_noise(text: str, *, article_label: str | None = None, min_cut_offset: int = 80) -> str:
    source = (text or "").strip()
    if not source:
        return ""
    target = _extract_article_code_from_label(article_label)
    if not target:
        return source

    target_num = _extract_article_main_numeric(target)
    cut_positions: list[int] = []

    for m in _FOREIGN_ARTICLE_HEADER_RE.finditer(source):
        code = (m.group(1) or "").strip().lower().replace(".", "-")
        if code == "1er":
            code = "premier"
        if re.fullmatch(r"\d+", code):
            code = str(int(code))
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
    return source[:cut_at].strip(" ,;:-") or source


def _is_non_article_label(article_label: str | None) -> bool:
    lower = re.sub(r"\s+", " ", (article_label or "")).strip().lower()
    if not lower:
        return False
    if _extract_article_code_from_label(lower):
        return False
    return any(token in lower for token in _NON_ARTICLE_KEYWORDS)


def _now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _safe_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._-")
    return cleaned or "run"


def _sub_count(pattern: re.Pattern[str], repl: str, text: str) -> tuple[str, int]:
    updated, count = pattern.subn(repl, text)
    return updated, count


def normalize_legal_text(raw_text: str) -> tuple[str, dict[str, Any]]:
    text = raw_text or ""
    stats: dict[str, Any] = {
        "nfkc_applied": False,
        "ligature_replacements": 0,
        "soft_hyphen_removed": 0,
        "zero_width_removed": 0,
        "ocr_pipe_replacements": 0,
        "hyphen_linebreak_repairs": 0,
        "standalone_page_markers_removed": 0,
        "page_headers_removed": 0,
        "punct_translation_changes": 0,
        "space_before_punct_fixes": 0,
        "space_after_punct_fixes": 0,
        "space_run_collapses": 0,
        "blank_line_collapses": 0,
        "line_trailing_space_removed": 0,
        "ocr_arabic_chars_removed": 0,
        "ocr_word_digit_repairs": 0,
        "ocr_fused_modal_repairs": 0,
        "ocr_inline_heading_repairs": 0,
        "ocr_duplicate_modal_repairs": 0,
        "changed": False,
        "original_chars": len(text),
        "normalized_chars": len(text),
        "delta_chars": 0,
        "normalization_ratio": 1.0,
    }

    if not text:
        return "", stats

    original = text
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    nfkc = unicodedata.normalize("NFKC", text)
    stats["nfkc_applied"] = nfkc != text
    text = nfkc

    punct_translated = text.translate(_PUNCT_TRANSLATION)
    stats["punct_translation_changes"] = sum(1 for a, b in zip(text, punct_translated) if a != b)
    text = punct_translated

    text = text.replace("\u00a0", " ").replace("\u202f", " ")

    for src, dst in _LIGATURE_REPLACEMENTS.items():
        count = text.count(src)
        if count:
            text = text.replace(src, dst)
            stats["ligature_replacements"] += count

    text, count = _sub_count(_SOFT_HYPHEN_RE, "", text)
    stats["soft_hyphen_removed"] = count

    text, count = _sub_count(_ZERO_WIDTH_RE, "", text)
    stats["zero_width_removed"] = count

    text, count = _sub_count(_HYPHEN_LINEBREAK_RE, "", text)
    stats["hyphen_linebreak_repairs"] = count

    text, count = _sub_count(_OCR_PIPE_RE, "l", text)
    stats["ocr_pipe_replacements"] = count

    text, count = _sub_count(_OCR_ARABIC_CHAR_RE, " ", text)
    stats["ocr_arabic_chars_removed"] = count

    text, count = _sub_count(_OCR_WORD_DIGIT_RE, "", text)
    stats["ocr_word_digit_repairs"] = count

    text, count = _sub_count(_OCR_DUPLICATE_MODAL_PREFIX_RE, r"\1", text)
    stats["ocr_duplicate_modal_repairs"] = count

    text, count = _sub_count(_OCR_HEADING_INLINE_NOISE_RE, " ", text)
    stats["ocr_inline_heading_repairs"] = count

    text, count = _sub_count(_OCR_FUSED_MODAL_BOUNDARY_RE, " ", text)
    stats["ocr_fused_modal_repairs"] = count

    text, count = _sub_count(_STANDALONE_PAGE_NUM_RE, "", text)
    stats["standalone_page_markers_removed"] = count

    text, count = _sub_count(_PAGE_HEADER_RE, "", text)
    stats["page_headers_removed"] = count

    text, count = _sub_count(_TRAILING_SPACE_LINE_RE, "", text)
    stats["line_trailing_space_removed"] = count

    text, count = _sub_count(_SPACE_BEFORE_PUNCT_RE, r"\1", text)
    stats["space_before_punct_fixes"] = count

    text, count = _sub_count(_MISSING_SPACE_AFTER_PUNCT_RE, r"\1 ", text)
    stats["space_after_punct_fixes"] = count

    text, count = _sub_count(_SPACE_RUN_RE, " ", text)
    stats["space_run_collapses"] = count

    text, count = _sub_count(_MULTI_BLANK_LINES_RE, "\n\n", text)
    stats["blank_line_collapses"] = count

    text = text.strip()

    stats["changed"] = text != original
    stats["normalized_chars"] = len(text)
    stats["delta_chars"] = stats["original_chars"] - stats["normalized_chars"]
    if stats["original_chars"]:
        stats["normalization_ratio"] = round(stats["normalized_chars"] / stats["original_chars"], 4)

    return text, stats


def _split_long_unit(text: str, max_unit_chars: int) -> list[str]:
    if len(text) <= max_unit_chars:
        return [text]

    raw_parts = [p.strip() for p in _LONG_UNIT_SPLIT_RE.split(text) if p.strip()]
    if len(raw_parts) <= 1:
        return [text]

    merged: list[str] = []
    current = ""
    for part in raw_parts:
        candidate = f"{current} {part}".strip() if current else part
        if len(candidate) <= max_unit_chars:
            current = candidate
        else:
            if current:
                merged.append(current)
            current = part
    if current:
        merged.append(current)

    return merged or [text]


def _protect_abbreviations(text: str) -> str:
    protected = text
    for src, placeholder in _ABBREVIATION_TOKENS:
        protected = protected.replace(src, placeholder)
    return protected


def _restore_abbreviations(text: str) -> str:
    restored = text
    for src, placeholder in _ABBREVIATION_TOKENS:
        restored = restored.replace(placeholder, src)
    return restored


def _is_normative_list_block(paragraph_text: str) -> bool:
    txt = paragraph_text or ""
    lower = txt.lower()
    list_lines = _LIST_LINE_RE.findall(txt)
    semicolons = txt.count(";")
    has_colon = ":" in txt
    inline_list = bool(re.search(r":\s*-\s+\w", txt))
    inline_numbered_list = bool(re.search(r":\s*(?:\d+\)|[a-z]\)|[ivxlcdm]+\))\s+\w", txt, re.IGNORECASE))

    intro_markers = [
        "suiv",
        "comme suit",
        "notamment",
        "les mentions",
        "les informations",
        "les indications",
        "les éléments",
        "les elements",
        "les conditions",
        "les pièces",
        "les pieces",
        "les documents",
        "les critères",
        "les criteres",
        "les modalités",
        "les modalites",
        "doit contenir",
        "doit comporter",
        "doit être accompagné",
        "doit etre accompagne",
        "doit comprendre",
        "comprend",
        "comprenant",
        "comporte",
        "comportant",
        "incluant",
        "ci-après",
        "ci-apres",
        "à savoir",
        "a savoir",
        "sont les suivants",
        "sont les suivantes",
        "que sont",
    ]
    has_intro_marker = any(marker in lower for marker in intro_markers)

    if len(list_lines) >= 2:
        return True
    if inline_list:
        return True
    if inline_numbered_list:
        return True
    if has_colon and len(list_lines) >= 1:
        return True
    if has_colon and semicolons >= 2 and has_intro_marker:
        return True
    if semicolons >= 3 and has_intro_marker:  # Phase 4: inline semi-colon list without bullet markers
        return True
    return False


def _extract_list_intro(text: str) -> str:
    """Return the normative intro clause before the first list item or colon, for context propagation."""
    # First explicit list marker (bullet / numbered item)
    m = _LIST_LINE_RE.search(text)
    if m and m.start() > 5:
        candidate = text[:m.start()].strip().rstrip(": \t")
        if len(candidate) >= 15:
            return candidate
    # Trailing colon pattern at end of intro sentence
    m2 = re.search(r"(.{15,200}):\s*$", text[:250], re.DOTALL)
    if m2:
        return m2.group(1).strip()
    return ""


def _split_normative_list_block(text: str, *, max_unit_chars: int) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []

    intro = _extract_list_intro(raw)
    body = raw

    first_marker = _LIST_ITEM_MARKER_RE.search(raw)
    if first_marker:
        body = raw[first_marker.start() :].strip()
    elif intro:
        colon_idx = raw.find(":")
        if colon_idx >= 0:
            body = raw[colon_idx + 1 :].strip()

    flattened = re.sub(r"\s*\n+\s*", " ", body).strip()
    if not flattened:
        return []

    marked = _LIST_ITEM_MARKER_RE.sub(" ||| ", flattened)
    if "|||" not in marked and ";" in flattened:
        marked = flattened.replace(";", " ||| ")

    parts = [
        re.sub(r"\s+", " ", part).strip(" ,;:-")
        for part in marked.split("|||")
        if re.sub(r"\s+", " ", part).strip(" ,;:-")
    ]
    if not parts:
        return []

    emitted: list[str] = []
    seen: set[str] = set()
    for part in parts:
        candidate = part
        if intro and not part.lower().startswith(intro.lower()):
            joined = f"{intro}: {part}".strip()
            if len(joined) <= max_unit_chars:
                candidate = joined
        normalized_candidate = re.sub(r"\s+", " ", candidate).strip(" ,;:-")
        key = normalized_candidate.lower()
        if normalized_candidate and key not in seen:
            seen.add(key)
            emitted.append(normalized_candidate)
    return emitted


def _append_segmented_unit(
    *,
    buffer: list[dict[str, str]],
    raw_unit: str,
    source: str,
    min_unit_chars: int,
    max_unit_chars: int,
    list_intro: str = "",
) -> None:
    unit = re.sub(r"\s+", " ", raw_unit).strip()
    if not unit:
        return
    parts = _split_long_unit(unit, max_unit_chars=max_unit_chars)
    for i, part in enumerate(parts):
        compact = re.sub(r"\s+", " ", part).strip()
        if source == "sentence_split":
            compact = re.sub(r"^\s*[-â€“â€”]\s+", "", compact)
        # Phase 4: prepend normative intro to orphaned sub-parts so NLP scores them correctly
        if i > 0 and list_intro and len(list_intro) + len(compact) + 2 <= max_unit_chars:
            compact = f"{list_intro}: {compact}"
        if len(compact) >= min_unit_chars:
            buffer.append({"text": compact, "source": source})


def segment_legal_units(
    text: str,
    *,
    article_label: str | None = None,
    min_unit_chars: int = 20,
    max_unit_chars: int = 900,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return [], {
            "paragraphs_total": 0,
            "list_blocks_preserved": 0,
            "sentence_splits_total": 0,
            "units_from_list_blocks": 0,
            "units_from_sentence_split": 0,
            "units_from_paragraph_fallback": 0,
        }

    trimmed = _trim_cross_article_noise(raw.strip(), article_label=article_label)
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(trimmed) if p.strip()]
    if not paragraphs:
        paragraphs = [trimmed]

    units: list[dict[str, str]] = []
    stats = {
        "paragraphs_total": len(paragraphs),
        "list_blocks_preserved": 0,
        "sentence_splits_total": 0,
        "units_from_list_blocks": 0,
        "units_from_sentence_split": 0,
        "units_from_paragraph_fallback": 0,
    }

    for paragraph in paragraphs:
        # OCR fusion often concatenates several legal acts in one paragraph.
        # On split explicitement sur les frontières OCR sans injecter de point.
        paragraph_parts = [p.strip() for p in _OCR_HARD_BOUNDARY_RE.split(paragraph) if p.strip()]
        if not paragraph_parts:
            paragraph_parts = [paragraph]

        for part_paragraph in paragraph_parts:
            if _is_normative_list_block(part_paragraph):
                before = len(units)
                list_items = _split_normative_list_block(
                    part_paragraph,
                    max_unit_chars=max_unit_chars,
                )
                if list_items:
                    for list_item in list_items:
                        _append_segmented_unit(
                            buffer=units,
                            raw_unit=list_item,
                            source="list_block",
                            min_unit_chars=min_unit_chars,
                            max_unit_chars=max_unit_chars,
                        )
                else:
                    _append_segmented_unit(
                        buffer=units,
                        raw_unit=part_paragraph,
                        source="list_block",
                        min_unit_chars=min_unit_chars,
                        max_unit_chars=max_unit_chars,
                        list_intro=_extract_list_intro(part_paragraph),
                    )
                stats["list_blocks_preserved"] += 1
                stats["units_from_list_blocks"] += max(0, len(units) - before)
                continue

            protected = _protect_abbreviations(part_paragraph)
            sentence_parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(protected) if p.strip()]
            sentence_parts = [_restore_abbreviations(p) for p in sentence_parts]
            expanded_sentence_parts: list[str] = []
            for part in sentence_parts:
                dash_parts = [p.strip() for p in _DASH_SENTENCE_SPLIT_RE.split(part) if p.strip()]
                expanded_sentence_parts.extend(dash_parts or [part])
            sentence_parts = expanded_sentence_parts

            if len(sentence_parts) > 1:
                stats["sentence_splits_total"] += len(sentence_parts) - 1
                before = len(units)
                for part in sentence_parts:
                    _append_segmented_unit(
                        buffer=units,
                        raw_unit=part,
                        source="sentence_split",
                        min_unit_chars=min_unit_chars,
                        max_unit_chars=max_unit_chars,
                    )
                stats["units_from_sentence_split"] += max(0, len(units) - before)
                continue

            before = len(units)
            _append_segmented_unit(
                buffer=units,
                raw_unit=part_paragraph,
                source="paragraph_fallback",
                min_unit_chars=min_unit_chars,
                max_unit_chars=max_unit_chars,
            )
            stats["units_from_paragraph_fallback"] += max(0, len(units) - before)

    if not units and trimmed:
        units = [{"text": trimmed, "source": "fallback_raw"}]
        stats["units_from_paragraph_fallback"] += 1

    return units, stats


def classify_legal_unit(
    unit_text: str,
    *,
    article_label: str | None = None,
    source: str = "unknown",
    high_threshold: float = 0.68,
    low_threshold: float = 0.32,
) -> dict[str, Any]:
    text = (unit_text or "").strip()
    lower = text.lower()

    if not text:
        return {
            "priority": "DROP",
            "normative_score": 0.0,
            "normative_label": "DESCRIPTIVE_OR_NOISE",
            "rule_hits": ["EMPTY_UNIT"],
            "strong_normative": False,
        }

    norm_raw = 0.0
    desc_raw = 0.0
    rule_hits: list[str] = []
    strong_normative = False

    for rule_name, rule_re, weight in _NORMATIVE_RULES:
        if rule_re.search(lower):
            norm_raw += weight
            rule_hits.append(f"NORM:{rule_name}")
            if rule_name in _STRONG_NORMATIVE_RULES:
                strong_normative = True

    for rule_name, rule_re, weight in _DESCRIPTIVE_RULES:
        if rule_re.search(lower):
            desc_raw += weight
            rule_hits.append(f"DESC:{rule_name}")

    if source == "list_block":
        norm_raw += 0.35
        rule_hits.append("CTX:LIST_BLOCK")
    elif source == "sentence_split":
        norm_raw += 0.1
        rule_hits.append("CTX:SENTENCE_SPLIT")

    if ":" in text and ";" in text:
        norm_raw += 0.1
        rule_hits.append("CTX:COLON_SEMICOLON")

    if _is_non_article_label(article_label):
        # Les labels non standards (nomination, sommaire, annexe, etc.) sont
        # souvent hors périmètre exigences générales A1.
        desc_raw += 0.9
        rule_hits.append("CTX:NON_ARTICLE_LABEL")
        if not strong_normative:
            desc_raw += 0.45
            rule_hits.append("CTX:NON_ARTICLE_LABEL_SOFT_BLOCK")

    raw_score = norm_raw - desc_raw
    normative_score = 1.0 / (1.0 + math.exp(-raw_score))
    normative_score = round(normative_score, 4)

    has_norm = norm_raw > 0
    has_desc = desc_raw > 0
    if has_desc and not has_norm and normative_score < low_threshold:
        priority = "DROP"
    elif strong_normative and normative_score >= (low_threshold - 0.05):
        priority = "HIGH"
    elif normative_score >= high_threshold:
        priority = "HIGH"
    elif normative_score >= low_threshold:
        priority = "LOW"
    else:
        priority = "DROP"

    # Garde-fou pour unités mixtes OCR (normatif + bruit nominatif/sommaire):
    # ne pas perdre des obligations juridiques utiles quand le score est proche du seuil LOW.
    mixed_desc_hits = {
        "DESC:SOMMAIRE",
        "DESC:ACTE_INDIVIDUEL_NAME",
        "DESC:ACTE_INDIVIDUEL_APPOINTMENT",
    }
    has_effect_norm = "NORM:EFFECT_LEGAL" in rule_hits
    has_mixed_desc = any(hit in mixed_desc_hits for hit in rule_hits)
    near_low = normative_score >= max(0.0, low_threshold - 0.08)
    if priority == "DROP" and has_effect_norm and has_mixed_desc and (near_low or norm_raw >= 0.9):
        priority = "LOW"
        rule_hits.append("SAFEGUARD:MIXED_NORMATIVE_NEAR_LOW")

    if priority == "HIGH":
        label = "NORMATIVE_STRONG"
    elif priority == "LOW":
        label = "NORMATIVE_AMBIGUOUS"
    else:
        label = "DESCRIPTIVE_OR_NOISE"

    return {
        "priority": priority,
        "normative_score": normative_score,
        "normative_label": label,
        "rule_hits": rule_hits,
        "strong_normative": strong_normative,
    }


def build_precall_contract(
    *,
    case_id: str,
    article_label: str,
    raw_text: str,
    min_unit_chars: int = 20,
    max_unit_chars: int = 900,
    unit_preview_chars: int = 220,
    units_sample_size: int = 6,
    high_threshold: float = 0.68,
    low_threshold: float = 0.32,
) -> tuple[str, dict[str, Any]]:
    normalized_text, normalization = normalize_legal_text(raw_text)

    segmented_units, segmentation_stats = segment_legal_units(
        normalized_text,
        article_label=article_label,
        min_unit_chars=min_unit_chars,
        max_unit_chars=max_unit_chars,
    )
    units_text = [u["text"] for u in segmented_units if u.get("text")]
    unit_sources = [u.get("source", "unknown") for u in segmented_units]

    units: list[dict[str, Any]] = []
    classification_summary = {
        "units_high": 0,
        "units_low": 0,
        "units_drop": 0,
        "strong_normative_units": 0,
    }
    cursor = 0
    for idx, unit_text in enumerate(units_text, start=1):
        start = normalized_text.find(unit_text, cursor)
        if start < 0:
            start = cursor
        end = start + len(unit_text)
        cursor = end

        classification = classify_legal_unit(
            unit_text,
            article_label=article_label,
            source=unit_sources[idx - 1],
            high_threshold=high_threshold,
            low_threshold=low_threshold,
        )
        priority = classification["priority"]
        if priority == "HIGH":
            classification_summary["units_high"] += 1
        elif priority == "LOW":
            classification_summary["units_low"] += 1
        else:
            classification_summary["units_drop"] += 1
        if classification["strong_normative"]:
            classification_summary["strong_normative_units"] += 1

        units.append(
            {
                "unit_id": f"{case_id}::{article_label}::u{idx}",
                "priority": priority,
                "normative_score": classification["normative_score"],
                "normative_label": classification["normative_label"],
                "rule_hits": classification["rule_hits"][:10],
                "source": unit_sources[idx - 1],
                "start_char": start,
                "end_char": end,
                "char_count": len(unit_text),
                "text_preview": unit_text[:unit_preview_chars],
            }
        )

    quality_flags: list[str] = []
    if normalization["ocr_pipe_replacements"] >= 3:
        quality_flags.append("OCR_NOISE_HEAVY")
    if normalization["hyphen_linebreak_repairs"] >= 2:
        quality_flags.append("BROKEN_WORDS_REPAIRED")
    if normalization["page_headers_removed"] > 0 or normalization["standalone_page_markers_removed"] > 0:
        quality_flags.append("PAGE_NOISE_REMOVED")
    if normalization["normalization_ratio"] < 0.65:
        quality_flags.append("HIGH_COMPRESSION")

    contract = {
        "precall_version": PRECALL_NLP_VERSION,
        "generated_at_utc": _now_utc_iso(),
        "case_id": case_id,
        "article_label": article_label,
        "normalization": normalization,
        "segmentation": segmentation_stats,
        "classification": {
            "high_threshold": high_threshold,
            "low_threshold": low_threshold,
            **classification_summary,
        },
        "units_total": len(units),
        "units_sample": units[:units_sample_size],
        "quality_flags": quality_flags,
    }
    return normalized_text, contract


def _sum_normalization_changes(normalization: dict[str, Any]) -> int:
    change_keys = [
        "ligature_replacements",
        "soft_hyphen_removed",
        "zero_width_removed",
        "ocr_pipe_replacements",
        "hyphen_linebreak_repairs",
        "standalone_page_markers_removed",
        "page_headers_removed",
        "punct_translation_changes",
        "space_before_punct_fixes",
        "space_after_punct_fixes",
        "space_run_collapses",
        "blank_line_collapses",
        "line_trailing_space_removed",
    ]
    return sum(int(normalization.get(k) or 0) for k in change_keys)


def build_precall_report(*, results: list[dict[str, Any]]) -> dict[str, Any]:
    case_reports: list[dict[str, Any]] = []
    total_cases = 0
    cases_with_precall = 0
    total_units = 0
    total_units_sent_to_llm = 0
    total_units_dropped = 0
    total_original_chars = 0
    total_normalized_chars = 0
    cases_with_changes = 0
    total_normalization_events = 0
    total_paragraphs = 0
    total_list_blocks_preserved = 0
    total_sentence_splits = 0
    total_units_from_list_blocks = 0
    total_units_from_sentence_split = 0
    total_units_from_paragraph_fallback = 0
    total_units_high = 0
    total_units_low = 0
    total_units_drop = 0
    total_units_sent_high = 0
    total_units_sent_low = 0
    total_units_dropped_low_policy = 0
    total_units_dropped_priority_drop = 0
    total_strong_normative_units = 0
    total_units_shadow_drop_candidates = 0
    total_units_shadow_low_policy_candidates = 0
    mode_counts: dict[str, int] = {"shadow": 0, "soft": 0, "full": 0, "unknown": 0}

    for case_result in results:
        total_cases += 1
        precall = case_result.get("precall")
        if not isinstance(precall, dict):
            continue

        cases_with_precall += 1
        normalization = precall.get("normalization", {})
        gating = precall.get("gating", {})
        segmentation = precall.get("segmentation", {})
        classification = precall.get("classification", {})
        policy = precall.get("classification_policy", {})

        mode = str(policy.get("mode") or "unknown").strip().lower()
        if mode not in mode_counts:
            mode = "unknown"
        mode_counts[mode] += 1

        original_chars = int(normalization.get("original_chars") or 0)
        normalized_chars = int(normalization.get("normalized_chars") or 0)
        units_total = int(gating.get("units_total") or 0)
        units_sent = int(gating.get("units_sent_to_llm") or 0)
        units_dropped = int(gating.get("units_dropped_total") or 0)

        total_original_chars += original_chars
        total_normalized_chars += normalized_chars
        total_units += units_total
        total_units_sent_to_llm += units_sent
        total_units_dropped += units_dropped

        normalization_events = _sum_normalization_changes(normalization)
        total_normalization_events += normalization_events
        if normalization_events > 0:
            cases_with_changes += 1

        total_paragraphs += int(segmentation.get("paragraphs_total") or 0)
        total_list_blocks_preserved += int(segmentation.get("list_blocks_preserved") or 0)
        total_sentence_splits += int(segmentation.get("sentence_splits_total") or 0)
        total_units_from_list_blocks += int(segmentation.get("units_from_list_blocks") or 0)
        total_units_from_sentence_split += int(segmentation.get("units_from_sentence_split") or 0)
        total_units_from_paragraph_fallback += int(segmentation.get("units_from_paragraph_fallback") or 0)
        total_units_high += int(classification.get("units_high") or 0)
        total_units_low += int(classification.get("units_low") or 0)
        total_units_drop += int(classification.get("units_drop") or 0)
        total_units_sent_high += int(classification.get("units_sent_high") or 0)
        total_units_sent_low += int(classification.get("units_sent_low") or 0)
        total_units_dropped_low_policy += int(classification.get("units_dropped_low_policy") or 0)
        total_units_dropped_priority_drop += int(classification.get("units_dropped_priority_drop") or 0)
        total_strong_normative_units += int(classification.get("strong_normative_units") or 0)
        total_units_shadow_drop_candidates += int(classification.get("units_shadow_drop_candidates") or 0)
        total_units_shadow_low_policy_candidates += int(
            classification.get("units_shadow_low_policy_candidates") or 0
        )

        case_reports.append(
            {
                "case_id": case_result.get("case_id"),
                "status": case_result.get("status"),
                "article_label": case_result.get("article_label"),
                "quality_flags": precall.get("quality_flags", []),
                "normalization_events": normalization_events,
                "units_total": units_total,
                "units_sent_to_llm": units_sent,
                "units_dropped_total": units_dropped,
                "llm_gate_ratio": gating.get("llm_gate_ratio"),
                "normalization_ratio": normalization.get("normalization_ratio"),
                "paragraphs_total": segmentation.get("paragraphs_total"),
                "list_blocks_preserved": segmentation.get("list_blocks_preserved"),
                "sentence_splits_total": segmentation.get("sentence_splits_total"),
                "units_high": classification.get("units_high"),
                "units_low": classification.get("units_low"),
                "units_drop": classification.get("units_drop"),
                "units_sent_high": classification.get("units_sent_high"),
                "units_sent_low": classification.get("units_sent_low"),
                "units_dropped_low_policy": classification.get("units_dropped_low_policy"),
                "units_dropped_priority_drop": classification.get("units_dropped_priority_drop"),
                "strong_normative_units": classification.get("strong_normative_units"),
                "units_shadow_drop_candidates": classification.get("units_shadow_drop_candidates"),
                "units_shadow_low_policy_candidates": classification.get("units_shadow_low_policy_candidates"),
                "precall_mode": mode,
            }
        )

    llm_calls_reduction_pct = None
    if total_units > 0:
        llm_calls_reduction_pct = round((1.0 - (total_units_sent_to_llm / total_units)) * 100.0, 2)

    return {
        "precall_version": PRECALL_NLP_VERSION,
        "generated_at_utc": _now_utc_iso(),
        "summary": {
            "cases_total": total_cases,
            "cases_with_precall": cases_with_precall,
            "cases_with_normalization_changes": cases_with_changes,
            "total_normalization_events": total_normalization_events,
            "total_original_chars": total_original_chars,
            "total_normalized_chars": total_normalized_chars,
            "normalization_ratio_global": (
                round(total_normalized_chars / total_original_chars, 4)
                if total_original_chars
                else 1.0
            ),
            "units_total": total_units,
            "units_sent_to_llm": total_units_sent_to_llm,
            "units_dropped_total": total_units_dropped,
            "llm_calls_reduction_pct_estimated": llm_calls_reduction_pct,
            "paragraphs_total": total_paragraphs,
            "list_blocks_preserved_total": total_list_blocks_preserved,
            "sentence_splits_total": total_sentence_splits,
            "units_from_list_blocks_total": total_units_from_list_blocks,
            "units_from_sentence_split_total": total_units_from_sentence_split,
            "units_from_paragraph_fallback_total": total_units_from_paragraph_fallback,
            "units_high_total": total_units_high,
            "units_low_total": total_units_low,
            "units_drop_total": total_units_drop,
            "units_sent_high_total": total_units_sent_high,
            "units_sent_low_total": total_units_sent_low,
            "units_dropped_low_policy_total": total_units_dropped_low_policy,
            "units_dropped_priority_drop_total": total_units_dropped_priority_drop,
            "strong_normative_units_total": total_strong_normative_units,
            "units_shadow_drop_candidates_total": total_units_shadow_drop_candidates,
            "units_shadow_low_policy_candidates_total": total_units_shadow_low_policy_candidates,
            "shadow_filter_candidates_total": (
                total_units_shadow_drop_candidates + total_units_shadow_low_policy_candidates
            ),
            "precall_mode_distribution": mode_counts,
        },
        "cases": case_reports,
    }


def persist_precall_report(
    *,
    precall_report: dict[str, Any],
    outdir: str,
    timestamp: str,
    run_id: str,
) -> tuple[Path, Path]:
    root = Path(outdir).expanduser().resolve()
    precall_dir = root / "precall"
    precall_history_dir = root / "history" / "precall"

    precall_dir.mkdir(parents=True, exist_ok=True)
    precall_history_dir.mkdir(parents=True, exist_ok=True)

    safe_run_id = _safe_filename_component(run_id)
    latest_path = precall_dir / "precall_latest.json"
    history_path = precall_history_dir / f"precall_{timestamp}_{safe_run_id}.json"

    content = json.dumps(precall_report, ensure_ascii=False, indent=2)
    latest_path.write_text(content, encoding="utf-8")
    history_path.write_text(content, encoding="utf-8")

    return latest_path, history_path
