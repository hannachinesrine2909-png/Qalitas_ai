import os
import re
import json
import argparse
from bisect import bisect_right

import psycopg
from dotenv import load_dotenv
from a1_shared_helpers import sanitize_ocr_noise_for_extraction, strip_article_header
from tenant_db import connect_db

load_dotenv()

# =========================
# Regex robustes
# =========================

ARTICLE_SUFFIX_PATTERN = (
    r"bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies|"
    r"undecies|duodecies|nouveau"
)

ARRETER_VERB_PATTERN = r"arr[êe]te(?:nt)?"
ARRETER_MARKER_PATTERN = ARRETER_VERB_PATTERN + r"(?:\s+ce\s+qui\s+suit)?\s*[:;.]"

# Supporte notamment :
# - Article unique
# - Article premier
# - Article 1er
# - Article 70
# - Article 70 (bis)
# - Article 70 bis
# - Art. 6
# - Art. 6-2
# - Art. 14 ter
# - Art 14. quater
# - [Art. 9 - ...]
ARTICLE_RE = re.compile(
    r"""(?imx)
    ^\s*\[?\s*
    (
        Article\s+
        (
            unique
            | premier
            | 1er
            | \d+
        )
        (?:\s*\(?\s*
            (
                """
    + ARTICLE_SUFFIX_PATTERN
    + r"""
            )
        \s*\)?)?
        |
        Art\.?\s*
        (
            \d+(?:[-\.]\d+)*
        )
        (?:[\s\.]*\(?\s*
            (
                """
    + ARTICLE_SUFFIX_PATTERN
    + r"""
            )
        \s*\)?)?
    )
    \s*\]?\s*[-:–—.]?
    """
)

# Fallback OCR: certains corpus (notamment codes) portent des en-têtes
# réduits du type "23 - ..." sans le préfixe "Article/Art.".
ARTICLE_NUMERIC_RE = re.compile(
    r"""(?imx)
    ^\s*
    (
        \d{1,3}
        (?:\s*\(?\s*(
            """
    + ARTICLE_SUFFIX_PATTERN
    + r"""
        )\s*\)?)?
    )
    \s*[-:–—.]
    """
)

# Fallback OCR: permet de détecter un header article même s'il est collé
# au milieu d'une ligne (colonnes fusionnées / retours ligne perdus).
ARTICLE_INLINE_RE = re.compile(
    r"""(?ix)
    (?<!\w)
    (
        Article\s+(?:unique|premier|1er|\d+)
        (?:\s*\(?\s*(?:"""
    + ARTICLE_SUFFIX_PATTERN
    + r""")\s*\)?)?
        |
        Art\.?\s*\d+(?:[-\.]\d+)*
        (?:[\s\.]*\(?\s*(?:"""
    + ARTICLE_SUFFIX_PATTERN
    + r""")\s*\)?)?
    )
    \s*[-:–—]
    """
)

LIVRE_RE = re.compile(r"(?im)^\s*LIVRE\b.*$")
TITRE_RE = re.compile(r"(?im)^\s*Titre\b.*$")
CHAPITRE_RE = re.compile(r"(?im)^\s*Chapitre\b.*$")
SECTION_RE = re.compile(r"(?im)^\s*Section\b.*$")

ANNEXE_RE = re.compile(
    r"""(?imx)
    ^\s*
    (
        ANNEXE(?:S)?\b.*$
        | Tableau(?:x)?\b.*$
        | Liste\b.*$
    )
    """
)

# Début d'acte JORT
ACT_START_RE = re.compile(
    r"""(?imx)
    ^\s*
    (
        Loi\s+constitutionnelle\b.*$
        | Loi\s+organique\b.*$
        | Loi\s+d['’]orientation\b.*$
        | Décret[-\s]loi\b.*$
        | Décret\s+présidentiel\b.*$
        | Décret\s+gouvernemental\b.*$
        | Décret\b.*$
        | Arr[êe]t[ée]\b.*$
        | Décision(?:\s+n[°o]\s*\d+|\s+du\b|\s+portant\b|\s+relative\b|\s+fixant\b).*$ 
        | Circulaire(?:\s+n[°o]\s*\d+|\s+du\b|\s+portant\b|\s+relative\b|\s+fixant\b).*$ 
        | Convention(?:\s+n[°o]\s*\d+|\s+du\b|\s+portant\b|\s+relative\b|\s+fixant\b).*$ 
        | Règlement(?:\s+n[°o]\s*\d+|\s+du\b|\s+portant\b|\s+relative\b|\s+fixant\b).*$ 
        | Loi\b.*$
    )
    """
)

ACT_INLINE_RE = re.compile(
    r"""(?ix)
    (?<!\w)
    (
        Loi\s+constitutionnelle\b
        | Loi\s+organique\b
        | Loi\s+d['’]orientation\b
        | Décret[-\s]loi\b
        | Décret\s+présidentiel\b
        | Décret\s+gouvernemental\b
        | Décret\b
        | Arr[êe]t[ée]\b
        | Décision(?:\s+n[°o]\s*\d+|\s+du\b|\s+portant\b|\s+relative\b|\s+fixant\b)
        | Circulaire(?:\s+n[°o]\s*\d+|\s+du\b|\s+portant\b|\s+relative\b|\s+fixant\b)
        | Convention(?:\s+n[°o]\s*\d+|\s+du\b|\s+portant\b|\s+relative\b|\s+fixant\b)
        | Règlement(?:\s+n[°o]\s*\d+|\s+du\b|\s+portant\b|\s+relative\b|\s+fixant\b)
        | Loi\b
    )
    """
)

_CLAUSE_SPLIT_RE = re.compile(r"(?<=[\.;:])\s+")
_NORMATIVE_HINT_RE = re.compile(
    r"(?i)\b(?:doit(?:vent)?|est\s+tenu(?:e|s|es)?\s+de|interdit(?:e|es|s)?|"
    r"b[ée]n[ée]fici\w*|communiqu\w*|transmett\w*|adress\w*|rej(?:ete|eter|eté)\w*|"
    r"prend\s+en\s+charge|entra[iî]ne\w*|proc[eè]d\w*|redevien\w*|"
    r"fait\s+foi|fix(?:e|ent|ée?s?)\w*|sous\s+r[ée]serve\s+de|[àa]\s+condition\s+de)\b"
)
_INLINE_INTER_ACT_BRIDGE_RE = re.compile(
    r"(?i)\bpar\s+(?:arr[êe]t[ée]|d[ée]cret|loi|d[ée]cision)\b[^.;:\n]{0,220}?"
    r"(?=(?:\b(?:doit(?:vent)?|est\s+tenu(?:e|s|es)?\s+de|interdit(?:e|es|s)?|"
    r"b[ée]n[ée]fici\w*|communiqu\w*|transmett\w*|adress\w*|rej(?:ete|eter|eté)\w*|"
    r"prend\s+en\s+charge|entra[iî]ne\w*|sous\s+r[ée]serve\s+de|[àa]\s+condition\s+de)\b|[.;:]|$))"
)
_INLINE_INTER_ACT_TITLE_RE = re.compile(
    r"(?i)\b(?:arr[êe]t[ée]|d[ée]cret|loi|d[ée]cision|circulaire|convention|r[èe]glement)\s+"
    r"(?:n[°o]\s*\d+|du\s+\d{1,2}\s+[a-zàâäéèêëîïôöùûüç]+\s+\d{4}|"
    r"pr[ée]sidentiel|gouvernemental|organique|constitutionnelle)\b"
)
_INLINE_JORT_NOISE_RE = re.compile(
    r"(?i)\b(?:journal\s+officiel|tunis,\s*le|le\s+ministre\b|la\s+ministre\b|"
    r"la\s+cheffe\s+du\s+gouvernement\b|n[°o]\s*\d+\b|page\s+\d+\b)\b"
)
_TRUNCATED_LEGAL_SUFFIX_RE = re.compile(
    r"(?i)\b(?:du|de|des|d['’]|au|aux|dans|sur|sous|par|pour|et|ou|le|la|les|"
    r"present|présent|presente|présente|titre|article|gouvernorats|campagne)\s*$"
)
_REPAIR_SIGNAL_RE = re.compile(
    r"(?i)\b(?:r[ée]e?chelonn\w*|fonds\s+national\s+de\s+garantie|"
    r"prend\s+en\s+charge|int[ée]r[êe]ts?|sous\s+r[ée]serve\s+de|"
    r"[àa]\s+condition\s+de)\b"
)
_TOKEN_RE = re.compile(r"[a-zàâäéèêëîïôöùûüç0-9]{4,}", re.IGNORECASE)
_ANCHOR_STOPWORDS = {
    "article",
    "articles",
    "present",
    "presente",
    "presentes",
    "presents",
    "arrete",
    "decret",
    "loi",
    "decision",
    "circulaire",
    "convention",
    "reglement",
    "sont",
    "etre",
    "dans",
    "avec",
    "pour",
}

# =========================
# Utils
# =========================
def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u00a0", " ")
    s = s.replace("\xad", "")
    s = s.replace("￾", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def apply_ocr_hard_breaks(text: str) -> str:
    """
    Ajoute des retours ligne avant des marqueurs juridiques forts pour
    limiter la contamination inter-actes quand l'OCR fusionne des colonnes.
    """
    s = text or ""
    patterns = [
        r"(?i)\s+(?=(?:Article\s+(?:unique|premier|1er|\d+)(?:\s*\(?\s*(?:"
        + ARTICLE_SUFFIX_PATTERN
        + r")\s*\)?)?\s*[-:–—]))",
        r"(?i)\s+(?=(?:Art\.?\s*\d+(?:[-\.]\d+)*(?:[\s\.]*\(?\s*(?:"
        + ARTICLE_SUFFIX_PATTERN
        + r")\s*\)?)?\s*[-:–—]))",
        # Fallback OCR codes/lois : en-têtes numériques nus ("23 - ...")
        # quand "Article/Art." est perdu.
        r"(?i)(?<=[\.;:])\s+(?=(?:\d{1,3}\s*[-:–—]))",
        # Evite de couper au milieu d'une phrase normative ("sauf décision",
        # "du présent arrêté", etc.). On force un saut uniquement après une
        # ponctuation forte, ce qui reste utile pour les signatures collées OCR.
        r"(?i)(?<=[\n\.;:])\s+(?=(?:Tunis,\s*le\b|Le\s+ministre\b|La\s+ministre\b|La\s+Cheffe\s+du\s+Gouvernement\b|Vu\b|"
        + ARRETER_MARKER_PATTERN
        + r"))",
    ]
    for pat in patterns:
        s = re.sub(pat, "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s


def looks_like_article_text(text: str) -> bool:
    return bool(ARTICLE_RE.search(text or ""))


def strip_trailing_headings(article_text: str) -> str:
    """
    Supprime les headings structurels ou annexes collés en fin d'article
    qui appartiennent en réalité au bloc suivant.
    """
    lines = [line.rstrip() for line in article_text.splitlines()]
    cleaned = []

    for line in lines:
        stripped = line.strip()
        is_heading = (
            LIVRE_RE.match(stripped)
            or TITRE_RE.match(stripped)
            or CHAPITRE_RE.match(stripped)
            or SECTION_RE.match(stripped)
            or ANNEXE_RE.match(stripped)
        )
        if is_heading and cleaned:
            break
        if is_heading and not cleaned:
            cleaned.append(line)
            continue
        cleaned.append(line)

    return "\n".join(cleaned).strip()


_MAJOR_HEADING_INLINE_RE = re.compile(
    r"(?i)\b(?:livre|titre|chapitre|section|sous[-\s]section)\b[^\n]{0,180}"
)


def _trim_multi_heading_article_window(article_text: str) -> str:
    prepared = normalize_text(article_text or "")
    if not prepared:
        return ""

    heading_matches = list(_MAJOR_HEADING_INLINE_RE.finditer(prepared))
    if len(heading_matches) < 2:
        return prepared

    windows: list[tuple[int, int, str]] = []
    for idx in range(len(heading_matches) - 1):
        start = heading_matches[idx].start()
        end = heading_matches[idx + 1].start()
        if end <= start:
            continue
        window_text = prepared[start:end].strip()
        if len(window_text) < 120:
            continue
        normative_hits = len(_NORMATIVE_HINT_RE.findall(window_text.lower()))
        windows.append((normative_hits, len(window_text), window_text))

    if not windows:
        return prepared

    best_hits, best_len, best_window = max(windows, key=lambda item: (item[1], item[0]))
    if best_len >= 180:
        return best_window
    return prepared


def trim_article_text_noise(article_text: str) -> str:
    """
    Coupe la fin d'un article quand commencent des signatures, préambules
    ou débuts d'actes parasites collés par OCR.
    """
    if not article_text:
        return ""

    prepared = apply_ocr_hard_breaks(normalize_text(article_text))
    lines = [line.rstrip() for line in prepared.splitlines()]
    cleaned = []
    stop_res = [
        re.compile(r"(?i)^\s*tunis,\s*le\b"),
        re.compile(r"(?i)^\s*le\s+ministre\b"),
        re.compile(r"(?i)^\s*la\s+ministre\b"),
        re.compile(r"(?i)^\s*la\s+cheffe\s+du\s+gouvernement\b"),
        re.compile(r"(?i)^\s*vu\b"),
        re.compile(r"(?i)^\s*" + ARRETER_MARKER_PATTERN),
        re.compile(r"(?i)^\s*n[°o]\s*\d+\b.*journal\s+officiel"),
        # Limite l'arrêt aux vrais entêtes d'actes et évite les faux positifs
        # comme "sauf décision contraire" au milieu d'un article.
        re.compile(
            r"(?i)^\s*(?:arr[êe]t[ée]|d[ée]cret|loi|d[ée]cision|circulaire|convention|r[èe]glement)\s+"
            r"(?:n[°o]\s*\d+|du\b|de\s+la\b|de\s+l['’]|gouvernemental\b|pr[ée]sidentiel\b|organique\b|constitutionnelle\b)"
        ),
    ]

    page_noise_res = [
        re.compile(r"(?i)^\s*n[°o]\s*\d+\b.*journal\s+officiel"),
        re.compile(r"(?i)^\s*page\s+\d+\b.*journal\s+officiel"),
    ]
    vu_re = re.compile(r"(?i)^\s*vu\b")

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if any(rx.search(stripped) for rx in page_noise_res):
            continue
        if idx > 0 and any(rx.search(stripped) for rx in stop_res):
            if vu_re.match(stripped) and idx > 3:
                continue
            break
        cleaned.append(line)

    compact = "\n".join(cleaned).strip()
    compact = sanitize_ocr_noise_for_extraction(compact)
    compact = prune_inter_act_noise(compact)
    compact = _trim_multi_heading_article_window(compact)
    return strip_trailing_headings(compact)


def _extract_anchor_tokens(text: str, *, max_clauses: int = 2) -> set[str]:
    clauses = [normalize_text(c) for c in _CLAUSE_SPLIT_RE.split(text or "") if normalize_text(c)]
    anchor = " ".join(clauses[: max(1, int(max_clauses))])
    tokens: set[str] = set()
    for tok in _TOKEN_RE.findall(anchor.lower()):
        if tok in _ANCHOR_STOPWORDS:
            continue
        tokens.add(tok)
    return tokens


def prune_inter_act_noise(text: str) -> str:
    """
    Nettoie les fragments OCR "inter-actes" collés au milieu d'un article.
    Objectif: retirer les ponts parasites (ex: "Par arrêté ...") sans casser
    les phrases normatives utiles.
    """
    compact = normalize_text(text or "")
    if not compact:
        return ""

    compact = _INLINE_INTER_ACT_BRIDGE_RE.sub(" ", compact)
    compact = re.sub(r"\s{2,}", " ", compact).strip()
    clauses = [normalize_text(c) for c in _CLAUSE_SPLIT_RE.split(compact) if normalize_text(c)]
    if len(clauses) <= 1:
        return compact

    anchor_tokens = _extract_anchor_tokens(compact)
    kept: list[str] = []

    for idx, clause in enumerate(clauses):
        if not clause:
            continue

        low = clause.lower()
        has_normative = bool(_NORMATIVE_HINT_RE.search(clause))
        has_jort_noise = bool(_INLINE_JORT_NOISE_RE.search(clause))
        has_inter_act_title = bool(_INLINE_INTER_ACT_TITLE_RE.search(clause))
        clause_tokens = {
            tok for tok in _TOKEN_RE.findall(low) if tok not in _ANCHOR_STOPWORDS
        }
        overlap = len(clause_tokens & anchor_tokens)

        if has_jort_noise and not has_normative:
            continue
        if idx > 0 and has_inter_act_title and not has_normative and overlap == 0:
            continue
        if idx > 0 and len(clause) < 24 and not has_normative:
            continue

        kept.append(clause)

    if not kept:
        return compact

    out = " ".join(kept).strip()
    out = re.sub(r"\s{2,}", " ", out)
    return out


def _looks_truncated_article_tail(text: str) -> bool:
    compact = normalize_text(text).replace("\n", " ").strip()
    if not compact:
        return False
    if len(compact) < 120:
        return True
    if re.search(r"[.!?;:]\s*$", compact):
        return False
    low = compact.lower()
    if low.endswith(("du present", "du présent", "a l'article", "à l'article")):
        return True
    return bool(_TRUNCATED_LEGAL_SUFFIX_RE.search(low))


def _extract_normative_prefix_tail(prefix: str, *, max_chars: int = 480) -> str:
    clauses = [normalize_text(c) for c in _CLAUSE_SPLIT_RE.split(prefix or "") if normalize_text(c)]
    kept_rev: list[str] = []
    chars = 0
    for clause in reversed(clauses):
        low_clause = clause.lower()
        if ARTICLE_INLINE_RE.search(clause):
            continue
        if re.match(r"(?i)^\s*(?:art\.?\s*)?\d+\s*[-:]", low_clause):
            continue
        has_signal = bool(_NORMATIVE_HINT_RE.search(clause) or _REPAIR_SIGNAL_RE.search(clause))
        if not has_signal:
            continue
        kept_rev.append(clause)
        chars += len(clause)
        if chars >= max_chars or len(kept_rev) >= 3:
            break
    if not kept_rev:
        return ""
    kept = list(reversed(kept_rev))
    return " ".join(kept).strip()


def _truncate_on_other_article_header(text: str, target_code: str | None) -> str:
    compact = normalize_text(text)
    if not compact:
        return ""
    matches: list[re.Match[str]] = list(ARTICLE_INLINE_RE.finditer(compact))
    matches.extend(list(ARTICLE_NUMERIC_RE.finditer(compact)))
    matches.sort(key=lambda m: m.start())
    for m in matches:
        if m.start() < 120:
            continue
        _lbl, code = parse_article_label_code(m.group(0))
        if target_code and code == target_code:
            continue
        return compact[: m.start()].strip()
    return compact


def _repair_truncated_article_from_context(
    act_text: str,
    local_start: int,
    local_end: int,
    header_line: str,
    article_text: str,
) -> str:
    baseline = normalize_text(article_text)
    if not baseline:
        return baseline
    if not _looks_truncated_article_tail(baseline):
        return baseline

    header_norm = normalize_text(header_line)
    _label, target_code = parse_article_label_code(header_line)
    windows = [
        (max(0, local_start - 900), min(len(act_text), local_end + 700)),
        (max(0, local_start - 1700), min(len(act_text), local_end + 1300)),
        (max(0, local_start - 2500), min(len(act_text), local_end + 1800)),
    ]

    candidates: list[str] = [baseline]
    for start, end in windows:
        if end <= start:
            continue
        raw = normalize_text(act_text[start:end])
        if len(raw) < len(baseline) + 40:
            continue
        cleaned = sanitize_ocr_noise_for_extraction(raw)
        cleaned = prune_inter_act_noise(cleaned)
        cleaned = normalize_text(cleaned)
        if not cleaned:
            continue

        header_pos = cleaned.lower().find(header_norm.lower())
        if header_pos >= 0:
            prefix = cleaned[max(0, header_pos - 900) : header_pos]
            suffix = cleaned[header_pos + len(header_norm) :]
            prefix_tail = _extract_normative_prefix_tail(prefix)
            suffix_head = normalize_text(suffix[:260]).lower()
            suffix_has_legal_start = bool(
                re.search(
                    r"(?i)\b(en\s+cas\s+de|lorsque|si\s+|doit|doivent|peut|peuvent|est|sont|interdit|tenu\s+de)\b",
                    suffix_head,
                )
            )
            use_prefix_tail = bool(prefix_tail) and (
                len(suffix_head) < 120 or not suffix_has_legal_start
            )
            body = normalize_text(f"{prefix_tail} {suffix}".strip()) if use_prefix_tail else normalize_text(suffix)
            candidate = normalize_text(f"{header_norm}\n{body}".strip()) if body else header_norm
        else:
            candidate = normalize_text(f"{header_norm}\n{cleaned}")

        candidate = _truncate_on_other_article_header(candidate, target_code)
        candidate = trim_article_text_noise(candidate)
        candidate = normalize_text(candidate)
        if not candidate or len(candidate) < len(baseline):
            continue
        candidates.append(candidate)

    def _score(text: str) -> tuple[int, int]:
        low = normalize_text(text).lower()
        norm_hits = len(_NORMATIVE_HINT_RE.findall(low))
        repair_hits = len(_REPAIR_SIGNAL_RE.findall(low))
        noise_hits = len(_INLINE_JORT_NOISE_RE.findall(low))
        other_headers = 0
        for m in ARTICLE_INLINE_RE.finditer(low):
            _lbl, code = parse_article_label_code(m.group(0))
            if target_code and code == target_code:
                continue
            if m.start() >= 120:
                other_headers += 1
        end_ok = 1 if re.search(r"[.!?;:]\s*$", text.strip()) else 0
        length_score = min(16, len(low) // 120)
        score = (
            (repair_hits * 8)
            + (norm_hits * 4)
            + (end_ok * 2)
            + length_score
            - (noise_hits * 5)
            - (other_headers * 6)
        )
        return score, len(text)

    best = max(candidates, key=_score)
    baseline_score = _score(baseline)
    best_score = _score(best)
    if best_score[0] >= baseline_score[0] + 3 and best_score[1] >= baseline_score[1] + 40:
        return best
    if _looks_truncated_article_tail(baseline) and best_score[1] >= baseline_score[1] + 120 and best_score[0] >= baseline_score[0]:
        return best
    return baseline


def find_article_matches(act_text: str):
    """
    Détecte les headers d'article en combinant regex ligne + fallback inline.
    Retourne une liste triée de match spans (start, end, header_text).
    """
    candidates = []

    for m in ARTICLE_RE.finditer(act_text):
        header = normalize_text(m.group(0))
        if header:
            candidates.append((m.start(), m.end(), header))

    for m in ARTICLE_NUMERIC_RE.finditer(act_text):
        header = normalize_text(m.group(0))
        if header:
            candidates.append((m.start(), m.end(), header))

    for m in ARTICLE_INLINE_RE.finditer(act_text):
        header = normalize_text(m.group(0))
        if header:
            candidates.append((m.start(), m.end(), header))

    if not candidates:
        return []

    candidates.sort(key=lambda x: x[0])
    deduped = []
    for start, end, header in candidates:
        if deduped and abs(start - deduped[-1][0]) <= 3:
            # Préfère le header le plus riche (souvent ARTICLE_RE ligne).
            if len(header) > len(deduped[-1][2]):
                deduped[-1] = (start, end, header)
            continue
        deduped.append((start, end, header))

    return deduped


def build_full_text_with_page_offsets(cur, doc_id: str):
    """
    Reconstruit le texte complet depuis document_pages
    et crée un mapping char -> page_no.
    """
    cur.execute(
        """
        SELECT page_no, page_text
        FROM document_pages
        WHERE doc_id=%s
        ORDER BY page_no
        """,
        (doc_id,),
    )
    rows = cur.fetchall()

    if not rows:
        raise RuntimeError("Aucune page trouvée dans document_pages pour ce doc_id")

    pages = [(pno, normalize_text(ptxt)) for pno, ptxt in rows if ptxt is not None]

    full_parts = []
    page_start_offsets = []
    offset = 0
    sep = "\n\n"

    for pno, ptxt in pages:
        page_start_offsets.append((offset, pno))
        full_parts.append(ptxt)
        offset += len(ptxt) + len(sep)

    full_text = normalize_text(sep.join(full_parts))
    return full_text, page_start_offsets


def char_to_page(pos: int, page_start_offsets):
    if not page_start_offsets:
        return 0
    starts = [x[0] for x in page_start_offsets]
    i = bisect_right(starts, pos) - 1
    return page_start_offsets[i][1] if i >= 0 else 0


def extract_headings(full_text: str):
    markers = {"livre": [], "titre": [], "chapitre": [], "section": []}

    for m in LIVRE_RE.finditer(full_text):
        markers["livre"].append((m.start(), m.group(0).strip()))
    for m in TITRE_RE.finditer(full_text):
        markers["titre"].append((m.start(), m.group(0).strip()))
    for m in CHAPITRE_RE.finditer(full_text):
        markers["chapitre"].append((m.start(), m.group(0).strip()))
    for m in SECTION_RE.finditer(full_text):
        markers["section"].append((m.start(), m.group(0).strip()))

    for k in markers:
        markers[k].sort(key=lambda x: x[0])

    return markers


def last_heading_before(markers_list, pos):
    if not markers_list:
        return None
    starts = [x[0] for x in markers_list]
    idx = bisect_right(starts, pos) - 1
    return markers_list[idx][1] if idx >= 0 else None


# =========================
# JORT filters
# =========================
def is_individual_appointment_act(act_title: str, act_text: str) -> bool:
    """
    Exclut les actes individuels de nomination / désignation / cessation
    qui ne doivent pas alimenter A1.
    """
    title = normalize_text(act_title).lower()
    first_window = normalize_text(act_text[:1200]).lower()

    individual_markers = [
        "nomination",
        "nommer",
        "est nommé",
        "est nommée",
        "sont nommés",
        "sont nommées",
        "désignation",
        "désigner",
        "est désigné",
        "est désignée",
        "chargé de fonctions",
        "chargée de fonctions",
        "fin de fonctions",
        "cessation de fonctions",
        "mise à la retraite",
        "admission à la retraite",
        "délégation de signature",
        "delegation de signature",
        "affectation",
        "mutation",
    ]

    person_markers = [
        "monsieur",
        "madame",
        "mme",
        "m.",
        "dr ",
        "professeur ",
    ]

    # Si l'acte n'a pas d'article, il sera déjà ignoré plus loin.
    # Ici on cible surtout les actes avec "Article unique" mais purement individuels.
    has_individual_marker = any(marker in title or marker in first_window for marker in individual_markers)
    has_person_marker = any(marker in first_window for marker in person_markers)

    # Marqueurs qui suggèrent au contraire un acte normatif général
    general_normative_markers = [
        "conditions d'application",
        "conditions d’octroi",
        "conditions d'octroi",
        "modalités",
        "modalites",
        "concours interne",
        "modalités d'organisation",
        "modalites d'organisation",
        "fixant les modalités",
        "fixant les modalites",
        "cahier des charges",
        "statut particulier",
        "fixation",
        "barème",
        "bareme",
        "barèmes",
        "baremes",
        "taux",
        "liste des zones",
        "liste des bénéficiaires",
        "registre",
        "déclaration",
        "declaration",
        "contribution",
        "cotisation",
        "agrément des organismes",
        "agrement des organismes",
    ]
    has_general_normative = any(marker in first_window for marker in general_normative_markers)

    return has_individual_marker and has_person_marker and not has_general_normative


def contains_annex_after(pos: int, text: str):
    m = ANNEXE_RE.search(text, pos=pos)
    return m.start() if m else None


# =========================
# JORT / article splitting
# =========================
def split_jort_acts(full_text: str):
    """
    Découpe un numéro JORT en blocs d'actes.

    Correction clé :
    on ne perd plus le texte AVANT le premier acte détecté.
    Si ce préfixe contient des articles, on le garde.
    """
    matches = list(ACT_START_RE.finditer(full_text))

    if not matches:
        return [("DOCUMENT", 0, len(full_text), full_text)]

    acts = []

    first_start = matches[0].start()
    prefix_text = full_text[:first_start].strip()
    if len(prefix_text) >= 30 and looks_like_article_text(prefix_text):
        acts.append(("DOCUMENT_PREFIX", 0, first_start, prefix_text))

    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)

        line_end = full_text.find("\n", start)
        if line_end == -1:
            line_end = m.end()

        act_title = full_text[start:line_end].strip()
        act_text = full_text[start:end].strip()

        if len(act_text) >= 30:
            acts.append((act_title, start, end, act_text))

    # Si aucun bloc n'a d'article, fallback document complet
    if not any(looks_like_article_text(act_text) for _, _, _, act_text in acts):
        return [("DOCUMENT", 0, len(full_text), full_text)]

    merged_acts = []
    i = 0
    while i < len(acts):
        title, start, end, text = acts[i]
        has_article = bool(find_article_matches(text))
        if not has_article and i + 1 < len(acts):
            next_title, _next_start, next_end, next_text = acts[i + 1]
            if ARTICLE_RE.search(next_text[:300]) or ARTICLE_NUMERIC_RE.search(next_text[:300]):
                merged = f"{text}\n\n{next_text}".strip()
                merged_acts.append((title, start, next_end, merged))
                i += 2
                continue
        merged_acts.append((title, start, end, text))
        i += 1

    return merged_acts


def _base_article_number_from_header(header_line: str) -> int | None:
    _label, code = parse_article_label_code(header_line)
    if not code:
        return None
    m = re.match(r"^(\d+)", str(code))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _is_implausible_backtrack(current_header: str, next_header: str) -> bool:
    current_num = _base_article_number_from_header(current_header)
    next_num = _base_article_number_from_header(next_header)
    if current_num is None or next_num is None:
        return False
    # Dans un même acte, un retour arrière de numérotation est presque
    # toujours un bruit OCR (référence "article 23 -" collée au texte).
    return next_num < current_num


def split_articles(full_text: str):
    """
    Découpe full_text en articles.
    Retourne : (article_ref_line, start_char, end_char, article_text)
    """
    act_blocks = split_jort_acts(full_text)
    articles = []

    for act_title, act_start, act_end, act_text in act_blocks:
        matches = find_article_matches(act_text)
        if not matches:
            continue

        # Filtre JORT : exclure les actes individuels de nomination/désignation
        if is_individual_appointment_act(act_title, act_text):
            continue

        annex_positions = [m.start() for m in ANNEXE_RE.finditer(act_text)]

        for i, (local_start, header_end, header_line) in enumerate(matches):
            next_article_start = len(act_text)
            for j in range(i + 1, len(matches)):
                cand_start, _cand_end, cand_header = matches[j]
                if _is_implausible_backtrack(header_line, cand_header):
                    continue
                next_article_start = cand_start
                break

            next_annex_start = None
            for ann_pos in annex_positions:
                if ann_pos > local_start:
                    next_annex_start = ann_pos
                    break

            local_end = next_article_start
            if next_annex_start is not None:
                local_end = min(local_end, next_annex_start)

            global_start = act_start + local_start
            global_end = act_start + local_end

            body = act_text[min(header_end, local_end):local_end].strip()

            article_text = (header_line + "\n" + body).strip()
            article_text = trim_article_text_noise(article_text)
            article_text = _repair_truncated_article_from_context(
                act_text=act_text,
                local_start=local_start,
                local_end=local_end,
                header_line=header_line,
                article_text=article_text,
            )

            if article_text and len(article_text) >= 10:
                articles.append((header_line, global_start, global_end, article_text))

    if not articles:
        return [("Document", 0, len(full_text), full_text)]

    return articles


# =========================
# Chunking — détection listes numérotées avec en-tête normatif
# =========================

# Détecte une phrase d'en-tête qui introduit une liste obligatoire
# ex. "L'employeur doit :", "Les candidats sont tenus de :", "Le dossier comprend :"
_LIST_HEADER_RE = re.compile(
    r"(?i)\b(doit|doivent|est\s+tenu\s+de|sont\s+tenus\s+de|comprend|doit\s+comprendre|"
    r"doit\s+contenir|doit\s+être\s+accompagné|doit\s+indiquer|doit\s+comporter)\s*[:\-–]\s*$"
)

# Détecte le début d'un item de liste numérotée ou à tirets
_LIST_ITEM_RE = re.compile(
    r"^\s*(?:\d+[\)\.]\s*|[a-z][\)\.]\s*|[ivxlIVXL]+[\)\.]\s*|[-–•]\s+)"
)


def _is_list_header_sentence(sentence: str) -> bool:
    """Retourne True si la phrase introduit une liste obligatoire."""
    s = sentence.strip()
    return bool(_LIST_HEADER_RE.search(s))


def _split_preserving_list_blocks(text: str, max_chars: int = 1200) -> list[str]:
    """
    Découpe le texte en chunks en préservant les blocs [en-tête normatif + items de liste].
    Un en-tête du type "L'employeur doit : 1) ... 2) ..." ne sera jamais coupé entre
    l'en-tête et ses items si le bloc tient dans max_chars.
    Retourne une liste de chunks (str).
    """
    lines = [l.strip() for l in re.split(r"\n", text) if l.strip()]
    if not lines:
        return []

    blocks: list[str] = []
    current_block: list[str] = []
    in_list = False

    for line in lines:
        if _is_list_header_sentence(line):
            if current_block:
                blocks.append(" ".join(current_block))
                current_block = []
            current_block = [line]
            in_list = True
        elif in_list and _LIST_ITEM_RE.match(line):
            current_block.append(line)
        else:
            if in_list:
                blocks.append(" ".join(current_block))
                current_block = [line]
                in_list = False
            else:
                current_block.append(line)

    if current_block:
        blocks.append(" ".join(current_block))

    # Maintenant découpe les blocs trop longs avec le splitter standard
    result = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if len(block) <= max_chars:
            result.append(block)
        else:
            result.extend(split_long_paragraph_safely(block, max_chars=max_chars))
    return result


def split_long_paragraph_safely(p: str, max_chars: int = 1200):
    p = normalize_text(p).replace("\n", " ").strip()
    chunks = []

    while len(p) > max_chars:
        window = p[: max_chars + 1]

        strong_cut = max(
            window.rfind(". "),
            window.rfind("; "),
            window.rfind("? "),
            window.rfind("! "),
        )
        medium_cut = window.rfind(": ")
        weak_cut = max(window.rfind(", "), window.rfind(" "))

        cut = -1
        if strong_cut >= int(max_chars * 0.35):
            cut = strong_cut + 1
        elif medium_cut >= int(max_chars * 0.45):
            cut = medium_cut + 1
        else:
            cut = weak_cut

        if cut < int(max_chars * 0.5):
            cut = window.rfind(" ")
        if cut <= 0:
            cut = max_chars

        chunk = p[:cut].strip()
        if chunk:
            chunks.append(chunk)

        p = p[cut:].strip()

    if p:
        chunks.append(p)

    return chunks


def chunk_text(text: str, max_chars: int = 1200):
    # Phase 2 : avant de découper en paragraphes, on préserve les blocs
    # [en-tête normatif + liste numérotée] pour ne pas séparer "doit :" de ses items
    if _LIST_HEADER_RE.search(normalize_text(text)):
        return _split_preserving_list_blocks(normalize_text(text), max_chars=max_chars)

    paras = [
        normalize_text(p).replace("\n", " ").strip()
        for p in re.split(r"\n\s*\n", text)
        if p.strip()
    ]

    chunks = []
    buf = ""

    for p in paras:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = (buf + "\n\n" + p).strip()
        else:
            if buf:
                chunks.append(buf)

            if len(p) <= max_chars:
                buf = p
            else:
                long_parts = split_long_paragraph_safely(p, max_chars=max_chars)
                chunks.extend(long_parts)
                buf = ""

    if buf:
        chunks.append(buf)

    return chunks


def chunk_text_with_overlap(text: str, *, max_chars: int = 1200, overlap_chars: int = 180):
    chunks = chunk_text(text, max_chars=max_chars)
    if len(chunks) <= 1 or overlap_chars <= 0:
        return chunks

    def _safe_overlap_tail(value: str) -> str:
        normalized = normalize_text(value)
        if len(normalized) <= overlap_chars:
            return normalized
        start = max(0, len(normalized) - overlap_chars)
        if start > 0 and normalized[start - 1].isalnum() and normalized[start].isalnum():
            prev_boundary = max(
                normalized.rfind(" ", 0, start),
                normalized.rfind(",", 0, start),
                normalized.rfind(";", 0, start),
                normalized.rfind(":", 0, start),
                normalized.rfind("(", 0, start),
                normalized.rfind(")", 0, start),
                normalized.rfind("-", 0, start),
            )
            if prev_boundary >= max(0, start - 24):
                start = prev_boundary + 1
        return normalized[start:].lstrip(" ,;:-")

    with_overlap = []
    for idx, chunk in enumerate(chunks):
        if idx == 0:
            with_overlap.append(chunk)
            continue
        tail = _safe_overlap_tail(chunks[idx - 1])
        if tail:
            with_overlap.append(f"{tail}\n\n{chunk}".strip())
        else:
            with_overlap.append(chunk)
    return with_overlap


def _cross_reference_search_text(article_text: str) -> str:
    return normalize_text(strip_article_header(article_text or ""))


# =========================
# Label / code parsing
# =========================
def normalize_suffix(s: str | None):
    if not s:
        return None
    return normalize_text(s).lower().strip("(). ")


def parse_article_label_code(header_line: str):
    """
    Exemples :
        "Article unique - ..."    -> ("Article unique", "unique")
        "Article premier - ..."   -> ("Article premier", "premier")
        "Article 70 (bis)"        -> ("Article 70 bis", "70-bis")
        "Art. 6-2 : ..."          -> ("Art. 6-2", "6-2")
        "Art. 14 quater - ..."    -> ("Art. 14 quater", "14-quater")
        "[Art. 9 - ...]"          -> ("Art. 9", "9")
    """
    s = normalize_text(header_line)

    m = re.match(
        r"""(?ix)
        ^(?:\[?\s*)?
        Article\s+
        (unique|premier|1er|\d+)
        (?:\s*\(?\s*(bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies|undecies|duodecies|nouveau)\s*\)?)?
        """,
        s,
    )
    if m:
        num = m.group(1).lower()
        suf = normalize_suffix(m.group(2))
        if suf:
            return f"Article {num} {suf}", f"{num}-{suf}"
        return f"Article {num}", num

    m = re.match(
        r"""(?ix)
        ^(?:\[?\s*)?
        Art\.?\s*
        (\d+(?:[-\.]\d+)*)
        (?:[\s\.]*\(?\s*(bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies|undecies|duodecies|nouveau)\s*\)?)?
        """,
        s,
    )
    if m:
        num = m.group(1).replace(".", "-")
        suf = normalize_suffix(m.group(2))
        if suf:
            return f"Art. {num} {suf}", f"{num}-{suf}"
        return f"Art. {num}", num

    m = re.match(
        r"""(?ix)
        ^(?:\[?\s*)?
        (\d{1,3})
        (?:\s*\(?\s*(bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies|undecies|duodecies|nouveau)\s*\)?)?
        \s*[-:–—.]?
        """,
        s,
    )
    if m:
        num = m.group(1)
        suf = normalize_suffix(m.group(2))
        if suf:
            return f"Art. {num} {suf}", f"{num}-{suf}"
        return f"Art. {num}", num

    return s[:80], None


# =========================
# Events
# =========================
def safe_insert_event(cur, tenant_id: str, doc_id: str, event_type: str, payload: dict) -> bool:
    payload_json = json.dumps(payload, ensure_ascii=False)

    statements = [
        (
            """
            INSERT INTO events(tenant_id, doc_id, event_type, payload)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (tenant_id, doc_id, event_type, payload_json),
        ),
        (
            """
            INSERT INTO events(tenant_id, doc_id, event_type, payload_json)
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


# =========================
# Main
# =========================
def segment_document(doc_id: str, max_chars: int = 1200, tenant_id: str | None = None) -> dict:
    dsn = os.getenv("PG_DSN")
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")

    with connect_db(dsn, tenant_id=tenant_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, title FROM documents WHERE doc_id=%s",
                (doc_id,),
            )
            doc_row = cur.fetchone()
            if not doc_row:
                raise RuntimeError("doc_id introuvable dans documents")

            tenant_id, doc_title = doc_row

            full_text, page_offsets = build_full_text_with_page_offsets(cur, doc_id)
            headings = extract_headings(full_text)
            arts = split_articles(full_text)

            print(f"Articles détectés : {len(arts)}", flush=True)

            # Re-segmentation d'un document déjà extrait:
            # il faut d'abord purger les exigences liées à d'anciens article_id.
            cur.execute("DELETE FROM requirements WHERE doc_id=%s", (doc_id,))
            cur.execute("DELETE FROM chunks WHERE doc_id=%s", (doc_id,))
            cur.execute("DELETE FROM articles WHERE doc_id=%s", (doc_id,))

            total_chunks = 0
            inserted_labels = []
            chunk_overlap_chars = max(0, int(os.getenv("A1_CHUNK_OVERLAP_CHARS", "180")))

            for idx, (article_ref, start_char, end_char, article_text) in enumerate(arts, start=1):
                article_label, article_code = parse_article_label_code(article_ref)

                if not article_label or not article_label.strip():
                    article_label, article_code = parse_article_label_code(article_text[:200])

                if not article_label or not article_label.strip():
                    m = ARTICLE_RE.search(article_text)
                    if m:
                        article_label, article_code = parse_article_label_code(m.group(0))

                if not article_label or not article_label.strip():
                    article_label = f"UNLABELED_ARTICLE_{idx}"

                start_page = char_to_page(start_char, page_offsets)
                end_page = char_to_page(max(end_char - 1, 0), page_offsets)

                livre = last_heading_before(headings["livre"], start_char)
                titre = last_heading_before(headings["titre"], start_char)
                chapitre = last_heading_before(headings["chapitre"], start_char)
                section = last_heading_before(headings["section"], start_char)

                cur.execute(
                    """
                    INSERT INTO articles(
                        doc_id,
                        article_ref,
                        article_label,
                        article_code,
                        livre,
                        titre,
                        chapitre,
                        section,
                        start_page,
                        end_page,
                        start_char,
                        end_char,
                        article_text
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING article_id
                    """,
                    (
                        doc_id,
                        article_ref,
                        article_label,
                        article_code,
                        livre,
                        titre,
                        chapitre,
                        section,
                        start_page,
                        end_page,
                        start_char,
                        end_char,
                        article_text,
                    ),
                )
                article_id = cur.fetchone()[0]
                inserted_labels.append(article_label)

                # Signalement renvois inter-articles (P2) — détection par regex
                # Pas de résolution automatique (risque hallucination), juste un event d'observation
                _CROSS_REF_RE = re.compile(
                    r"(?i)\b(article\s+\d+|conform[eé]ment\s+aux\s+dispositions\s+de|"
                    r"sous\s+r[eé]serve\s+de\s+l['’]?article|en\s+application\s+de\s+l['’]?article|"
                    r"pr[eé]vu\s+[àa]\s+l['’]?article|vis[eé]\s+[àa]\s+l['’]?article)\b"
                )
                if _CROSS_REF_RE.search(_cross_reference_search_text(article_text)):
                    safe_insert_event(cur, tenant_id, doc_id, "ARTICLE_HAS_CROSS_REFERENCE", {
                        "article_id": str(article_id),
                        "article_label": article_label,
                        "note": "Cet article contient un renvoi à un autre article — exigences à valider manuellement.",
                    })

                chunks = chunk_text_with_overlap(
                    article_text,
                    max_chars=max_chars,
                    overlap_chars=chunk_overlap_chars,
                )
                if not chunks:
                    chunks = [normalize_text(article_text)]

                for cno, ctext in enumerate(chunks, start=1):
                    cur.execute(
                        """
                        INSERT INTO chunks(doc_id, article_id, chunk_no, chunk_text)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (doc_id, article_id, cno, ctext),
                    )

                total_chunks += len(chunks)

            payload = {
                "stage": "segmentation_pro_jort_requirements_safe_v3",
                "doc_title": doc_title,
                "articles_detected": len(arts),
                "chunks_created": total_chunks,
                "first_labels_preview": inserted_labels[:15],
            }

            event_inserted = safe_insert_event(
                cur=cur,
                tenant_id=tenant_id,
                doc_id=doc_id,
                event_type="ARTICLES_CHUNKS_CREATED",
                payload=payload,
            )

        conn.commit()

    return {
        "doc_id": doc_id,
        "doc_title": doc_title,
        "articles_inserted": len(arts),
        "chunks_created": total_chunks,
        "event_inserted": bool(event_inserted),
        "first_labels_preview": inserted_labels[:15],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc_id", required=True, help="UUID du document")
    ap.add_argument("--max_chars", type=int, default=1200)
    ap.add_argument("--tenant", default="", help="tenant_id pour activer le contexte RLS")
    args = ap.parse_args()

    summary = segment_document(
        doc_id=args.doc_id,
        max_chars=args.max_chars,
        tenant_id=str(args.tenant or "").strip() or None,
    )

    print("Segmentation terminee.", flush=True)
    print(f"Document         : {summary['doc_title']}", flush=True)
    print(f"Articles inseres : {summary['articles_inserted']}", flush=True)
    print(f"Chunks crees     : {summary['chunks_created']}", flush=True)
    print(f"Event insere     : {summary['event_inserted']}", flush=True)
    print(f"Premiers labels  : {summary['first_labels_preview']}", flush=True)


if __name__ == "__main__":
    main()
