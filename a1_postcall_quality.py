import datetime as dt
import difflib
import json
import math
import re
from pathlib import Path
from typing import Any

try:
    import spacy
except Exception:
    spacy = None

try:
    from sentence_transformers import SentenceTransformer
except Exception:
    SentenceTransformer = None

from a1_shared_helpers import (
    build_empty_llm_fallback_requirements,
    has_heavy_ocr_artifact_signals,
    has_ocr_artifact_signals,
    normalize_subject_from_context,
    ocr_artifact_score,
    repair_common_ocr_artifacts,
)


POSTCALL_QUALITY_VERSION = "B4.4-1.2.0"

_WORD_RE = re.compile(r"[a-zàâäéèêëîïôöùûüç0-9']+", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?:[.;:]\s+|\n+)")
_GROUNDING_EXCERPT_MAX_CHARS = 180
_DANGLING_TAIL_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:est|sont)\s+(?:"
    r"fix(?:é|e|ées|ees|és|es)"
    r"|détermin(?:é|e|ées|ees|és|es)"
    r"|determin(?:é|e|ées|ees|és|es)"
    r"|prévu(?:e|es|s)?"
    r"|prevu(?:e|es|s)?"
    r"|arrêt(?:é|e|ées|ees|és|es)"
    r"|arrete(?:e|es|s)?"
    r"|défini(?:e|es|s)?"
    r"|defini(?:e|es|s)?"
    r")\s+par\b"
    r"|"
    r"\b(?:contre|par|pour|de|du|des|à|a|au|aux|avec|sur|sous)\b"
    r")[\s\.,;:!?-]*$"
)
_INCOMPLETE_TRANSITIVE_ACTION_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:doit(?:vent)?|est\s+tenu(?:e|s|es)?\s+d(?:e|')|sont\s+tenu(?:s|es)?\s+d(?:e|')|"
    r"peut(?:vent)?)\s+"
    r"(?:informer|notifier|communiquer|transmettre|adresser|declarer|d[ée]clarer|"
    r"indiquer|presenter|pr[ée]senter|soumettre|remettre|tenir|assurer)\b"
    r"|"
    r"\b(?:informe|notifie|communique|transmet|adresse|declare|d[ée]clare|"
    r"indique|presente|pr[ée]sente|soumet|remet|assure)\b"
    r")[\s\.,;:!?-]*$"
)
_FUSED_AUXILIARY_CHAIN_RE = re.compile(
    r"(?i)^.+?\s+(?:est|sont)\s+.+?\bet\s+(?:est|sont)\s+.+$"
)
_FUSED_MODAL_CHAIN_RE = re.compile(
    r"(?i)^.+?(?:"
    r"\bdoit(?:vent)?\b|"
    r"\best\s+tenu(?:e|s|es)?\s+d(?:e|')|"
    r"\bsont\s+tenu(?:s|es)?\s+d(?:e|')|"
    r"\best\s+h(?:abilit|abilit)[ée]?\s+[àa]\b|"
    r"\bsont\s+h(?:abilit|abilit)[ée]s?\s+[àa]\b|"
    r"\best\s+charg[ée]?\s+de\b|"
    r"\bsont\s+charg[ée]s?\s+de\b"
    r").+\bet\s+(?:[àa]\s+|de\s+)?[a-zàâäéèêëîïôöùûüç'-]{3,}(?:er|ir|re|oir)\b"
)
_BROKEN_HYPHEN_SPLIT_RE = re.compile(r"(?i)\b[a-z0-9']{4,}\s*-\s+[a-z0-9']{4,}\b")
_LEADING_FRAGMENT_BLEND_RE = re.compile(
    r"(?i)^\s*(?:d'|de\s+)[a-z0-9'-]{4,}\s*-\s+(?:dans|si|lorsque|pour|en\s+cas)\b"
)
_DASH_MODAL_CLAUSE_BLEND_RE = re.compile(
    r"(?is)\b(?:"
    r"doit(?:vent)?|"
    r"est\s+tenu(?:e|s|es)?\s+d(?:e|')|"
    r"sont\s+tenu(?:s|es)?\s+d(?:e|')"
    r")\b.{0,160}\s-\s+(?:dans|si|lorsque|pour|en\s+cas)\b.{0,260}\b(?:doit(?:vent)?|est\b|sont\b)"
)
_ARTICLE_HEADER_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:art\.?|article)\s*(?:n[°o]?\s*)?"
    r"(?:premier|1er|unique|\d+(?:[-.]\d+)*)\s*[-:–—]\s*"
)
_PAGE_PREFIX_RE = re.compile(r"(?i)^\s*page\s+\d+\s*[-:–—]?\s*")
_JORT_META_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:n[°o]\s*\d+\s*)?"
    r"journal\s+officiel\s+de\s+la\s+r[ée]publique\s+tunisienne\s*[-:–—]?\s*"
)
_FRENCH_STOPWORDS = {
    "a",
    "au",
    "aux",
    "avec",
    "ce",
    "ces",
    "dans",
    "de",
    "des",
    "du",
    "elle",
    "elles",
    "en",
    "et",
    "il",
    "ils",
    "je",
    "la",
    "le",
    "les",
    "leur",
    "leurs",
    "lui",
    "mais",
    "me",
    "meme",
    "même",
    "ne",
    "nos",
    "notre",
    "nous",
    "on",
    "ou",
    "où",
    "par",
    "pas",
    "pour",
    "qu",
    "que",
    "qui",
    "sa",
    "se",
    "ses",
    "son",
    "sur",
    "ta",
    "te",
    "tes",
    "toi",
    "ton",
    "tu",
    "un",
    "une",
    "vos",
    "votre",
    "vous",
    "y",
}

_NORMATIVE_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("OBLIGATION_DOIT", re.compile(r"(?i)\bdoit(?:vent)?\b")),
    ("OBLIGATION_TENU", re.compile(r"(?i)\best\s+tenu(?:e|s|es)?\s+d(?:e|')")),
    (
        "OBLIGATION_COMMUNICATION",
        re.compile(r"(?i)\bcommuniqu\w+\b|\btransmett\w+\b|\badress\w+\b|\bfourn\w+\b"),
    ),
    (
        "INTERDICTION",
        re.compile(
            r"(?i)\binterdit(?:e|es|s)?\b|"
            r"\bne\s+doit(?:vent)?\s+pas\b|"
            r"\bne\s+peu(?:t|vent)\s+pas\b|"
            r"\bne\s+peu(?:t|vent)\s+"
            r"(?![^.!?;:]{0,280}\bque\b)"
            r"(?:participer|concourir|b[ée]n[ée]ficier|pr[eé]tendre|acc[eé]der|exercer|d[eé]poser|soumettre)\b"
        ),
    ),
    (
        "CONDITION_RESTRICTIVE_NE_QUE",
        re.compile(r"(?i)\bne\s+peu(?:t|vent)\s+[^.!?;:]{0,280}\bque\b"),
    ),
    ("EXCEPTION", re.compile(r"(?i)\bsauf\b|\bexception\b|\bcependant\b|\btoutefois\b")),
    ("CONDITION", re.compile(r"(?i)\bsi\b|\blorsque\b|\ben\s+cas\s+de\b|\bà\s+condition\s+de\b|\ba\s+condition\s+de\b")),
    ("RESPONSABILITE", re.compile(r"(?i)\bresponsable\b|\brépond\s+de\b|\brepond\s+de\b")),
    (
        "DECLARATION",
        re.compile(
            r"(?i)\bdéclar(?:er|ation)\b|\bdeclar(?:er|ation)\b|\bnotifier\b|"
            r"\bcommuniqu\w+\b|\btransmett\w+\b|\badress\w+\b"
        ),
    ),
    ("REGISTRE", re.compile(r"(?i)\bregistre\b|\bconsigner\b|\btenir\s+un\s+registre\b")),
]

_DESCRIPTIVE_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("DEFINITION", re.compile(r"(?i)\bon\s+entend\s+par\b|\bdésigne\b|\bdesigne\b|\best\s+défini(?:e)?\b")),
    ("NOMINATION", re.compile(r"(?i)\best\s+nomm[ée]e?\b|\best\s+désign[ée]e?\b|\best\s+charg[ée]e?\b")),
    ("SOMMAIRE", re.compile(r"(?i)\blivre\b|\btitre\b|\bchapitre\b|\bsection\b|\bsommaire\b")),
    ("REFERENCE_META", re.compile(r"(?i)\bjournal\s+officiel\b|\bla\s+présente\s+loi\b|\bla\s+presente\s+loi\b")),
    # Phase 1: organisational creation / competition opening / conformity boilerplate
    ("CREATION_STRUCTURE", re.compile(r"(?i)\bil\s+est\s+(?:cr[ée][ée]e?|institu[ée]e?|[ée]tabli[ée]?)\b")),
    ("OUVERTURE_CONCOURS", re.compile(r"(?i)\bconcours\b.{0,80}\best\s+ouvert\b|\best\s+ouvert\b.{0,80}\bconcours\b", re.DOTALL)),
    ("CONFORMITE_PRESENT_ARRETE", re.compile(r"(?i)\best\s+organis[ée]e?\s+conform[ée]ment\s+aux\s+dispositions\s+du\s+pr[ée]sent\b")),
]

_TYPE_MARKERS: dict[str, list[re.Pattern[str]]] = {
    "OBLIGATION": [
        re.compile(r"(?i)\bdoit(?:vent)?\b"),
        re.compile(r"(?i)\best\s+tenu(?:e|s|es)?\s+d(?:e|')"),
        re.compile(r"(?i)\bprend\s+en\s+charge\b"),
        re.compile(r"(?i)\bb[ée]n[ée]fici\w*\b"),
        re.compile(r"(?i)\bcommuniqu\w+\b|\btransmet\w+\b|\badress\w+\b"),
        re.compile(r"(?i)\bsont?\s+fix\w+\b|\bfix\w+\s+conform[ée]ment\b"),
        re.compile(r"(?i)\bsont?\s+recouvr\w+\b|\bsont?\s+affect\w+\b"),
    ],
    "INTERDICTION": [
        re.compile(r"(?i)\binterdit(?:e|es|s)?\b"),
        re.compile(r"(?i)\bne\s+doit(?:vent)?\s+pas\b"),
        re.compile(r"(?i)\bne\s+peu(?:t|vent)\s+pas\b"),
        re.compile(
            r"(?i)\bne\s+peu(?:t|vent)\s+"
            r"(?![^.!?;:]{0,280}\bque\b)"
            r"(?:participer|concourir|b[ée]n[ée]ficier|pr[eé]tendre|acc[eé]der|exercer|d[eé]poser|soumettre)\b"
        ),
    ],
    "RESPONSABILITE": [re.compile(r"(?i)\bresponsable\b"), re.compile(r"(?i)\brépond\s+de\b|\brepond\s+de\b")],
    "EXCEPTION": [re.compile(r"(?i)\bsauf\b|\bexception\b|\bcependant\b|\btoutefois\b")],
    "CONDITION": [
        re.compile(r"(?i)\bsi\b|\blorsque\b|\ben\s+cas\s+de\b|\bà\s+condition\s+de\b|\ba\s+condition\s+de\b"),
        re.compile(r"(?i)\bne\s+peu(?:t|vent)\s+[^.!?;:]{0,280}\bque\b"),
    ],
    "DECLARATION": [
        re.compile(
            r"(?i)\bdéclar(?:er|ation)\b|\bdeclar(?:er|ation)\b|\bnotifier\b|"
            r"\bcommuniqu\w+\b|\btransmett?\w+\b|\badress\w+\b|\bfourn\w+\b|\binform\w+\b"
        )
    ],
    "REGISTRE": [re.compile(r"(?i)\bregistre\b|\bconsigner\b|\btenir\s+un\s+registre\b")],
    "CONTROLE": [re.compile(r"(?i)\bcontr[oô]le\b|\binspection\b")],
}

_TYPE_PRIORITY: dict[str, int] = {
    "INTERDICTION": 90,
    "REGISTRE": 86,
    "DECLARATION": 82,
    "OBLIGATION": 80,
    "EXCEPTION": 70,
    "CONDITION": 65,
    "RESPONSABILITE": 60,
    "CONTROLE": 45,
    "AUTRE": 10,
}

_TYPE_COMPATIBLE_GROUPS: list[set[str]] = [
    {"OBLIGATION", "DECLARATION"},
    {"RESPONSABILITE", "OBLIGATION"},
]

_CONDITION_CUES: list[re.Pattern[str]] = [
    re.compile(r"(?i)\bsi\b"),
    re.compile(r"(?i)\blorsque\b"),
    re.compile(r"(?i)\ben\s+cas\s+de\b"),
    re.compile(r"(?i)\bà\s+condition\s+de\b|\ba\s+condition\s+de\b"),
    re.compile(r"(?i)\bsous\s+r[ée]serve\s+de\b"),
    re.compile(r"(?i)\bne\s+peu(?:t|vent)\s+[^.!?;:]{0,280}\bque\b"),
]

_EXCEPTION_CUES: list[re.Pattern[str]] = [
    re.compile(r"(?i)\bsauf\b"),
    re.compile(r"(?i)\bexcept(?:ion|[ée]?)\b"),
    re.compile(r"(?i)\btoutefois\b|\bcependant\b"),
    re.compile(r"(?i)\bd[ée]rogation\b|\bpar\s+d[ée]rogation\b"),
]

_SCOPE_CUES: list[re.Pattern[str]] = [
    re.compile(r"(?i)\bà\s+l['’]égard\s+de\b|\ba\s+l['’]egard\s+de\b"),
    re.compile(r"(?i)\bau\s+titre\s+de\b"),
    re.compile(r"(?i)\bdans\s+le\s+cadre\s+de\b"),
    re.compile(r"(?i)\bconcernant\b|\brelatif(?:ve)?s?\s+à\b"),
    re.compile(r"(?i)\buniquement\b|\bseulement\b|\blimité?\s+à\b"),
]

_QUALITY_WEIGHTS: dict[str, float] = {
    "llm_confidence": 0.22,       # Phase 2: +0.02 (LLM confidence is a reliable pre-filter)
    "grounding_score": 0.30,       # Phase 2: +0.02 (grounding is the core validity check)
    "type_consistency": 0.18,
    "condition_completeness": 0.18, # Phase 2: -0.04 (was over-penalising partial conditions)
    "subject_consistency": 0.12,
}

_QUALITY_PENALTIES: dict[str, float] = {
    "DUPLICATE_GROUP": 0.04,
    "TYPE_CONFLICT_GROUP": 0.08,
    "OUT_OF_SCOPE_AUTRE_DESCRIPTIVE": 0.15,
    "OUT_OF_SCOPE_INDIVIDUAL_ACT": 0.22,
    "OUT_OF_SCOPE_PUBLICATION": 0.22,
    "NORMATIVE_STRENGTH_INCOHERENT": 0.18,
    # LLM met IMPERATIF alors que la source contient des marqueurs FACULTATIF/CONDITIONNEL
    "NORMATIVE_STRENGTH_OVERQUALIFIED": 0.15,
}

# Marqueurs qui signalent une force FACULTATIVE ou CONDITIONNELLE dans le texte source
_FACULTATIF_MARKERS_RE = re.compile(
    r"\bpeut\s+(?:bénéficier|demander|obtenir|solliciter|déposer|recourir|choisir)\b"
    r"|\bdevrait\b"
    r"|\bil\s+est\s+recommandé\b"
    r"|\ba\s+la\s+possibilité\s+de\b"
    r"|\best\s+susceptible\s+de\b"
    r"|\bpeut\s+être\s+accordé\b"
    r"|\bpeut\s+être\s+exonéré\b",
    re.IGNORECASE,
)

# Marqueurs qui signalent clairement une obligation dans le texte source
_IMPERATIF_MARKERS_RE = re.compile(
    r"\bdoit\b"
    r"|\best\s+tenu\s+de\b"
    r"|\best\s+interdit\s+de?\b"
    r"|\bne\s+peut\s+pas\b"
    r"|\bne\s+peut\s+en\s+aucun\s+cas\b"
    r"|\bobligatoirement\b"
    r"|\bentraîne\s+(?:l'exclusion|la\s+nullité|le\s+rejet)\b"
    r"|\bsera\s+rejeté\b"
    r"|\brépond\s+de\b"
    r"|\best\s+passible\b",
    re.IGNORECASE,
)

# Types incompatibles avec FACULTATIF : une OBLIGATION ou INTERDICTION ne peut pas
# avoir une force normative facultative — c'est une contradiction qui indique
# soit une mauvaise classification du type, soit une mauvaise extraction.
_IMPERATIF_ONLY_TYPES: frozenset[str] = frozenset({"OBLIGATION", "INTERDICTION", "RESPONSABILITE"})

_PUBLICATION_BOILERPLATE_RE = re.compile(
    r"(?i)\b(?:l['’]|le|la)\s+pr[ée]sent(?:e)?\s+(?:arr[êe]t[ée]?|d[ée]cret(?:-loi)?|loi)\s+sera\s+"
    r"(?:publi[ée]e?|ex[ée]cut[ée]e?)\b"
)
_OCR_MIXED_SOURCE_RE = re.compile(
    r"(?i)\b(?:journal\s+officiel|page\s+\d+|n[??o]\s*\d+|chapitre|section|titre|livre)\b"
)
_INDIVIDUAL_ACT_NAME_RE = re.compile(
    r"(?i)\b(?:monsieur|madame|m\.|mme\.?)\s+[a-zàâäéèêëîïôöùûüç][a-zàâäéèêëîïôöùûüç'\-]{1,}"
)
_INDIVIDUAL_ACT_MARKERS = (
    "est nommé",
    "est nommée",
    "sont nommés",
    "sont nommées",
    "est désigné",
    "sont désignés",
    "est nomme",
    "est nommee",
    "sont nommes",
    "sont nommees",
    "est designe",
    "sont designes",
    "chargé des fonctions de",
    "chargée des fonctions de",
    "charge des fonctions de",
    "chargee des fonctions de",
    "acceptation de démission",
    "acceptation de demission",
    "délégation de signature accordée à monsieur",
    "délégation de signature accordée à madame",
    "délégation de signature",
    "delegation de signature",
    "par délégation",
    "par delegation",
    "est habilité à signer",
    "est habilite a signer",
    "delegation de signature accordee a monsieur",
    "delegation de signature accordee a madame",
)

_SPACY_NLP: Any | None = None
_SPACY_BACKEND = "UNINITIALIZED"
_ST_MODEL: Any | None = None
_ST_BACKEND = "UNINITIALIZED"
_ST_EMBED_CACHE: dict[str, list[float]] = {}
_ST_EMBED_CACHE_MAX = 2048
_SPACY_INIT_DONE = False
_ST_INIT_DONE = False


def _now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _safe_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._-")
    return cleaned or "run"


def _normalize_text(value: str) -> str:
    text = (value or "").replace("\u00a0", " ").strip().lower()
    text = text.replace("’", "'")
    text = re.sub(r"\s+", " ", text)
    return text


def _tokenize(value: str) -> set[str]:
    text = _normalize_text(value)
    return {m.group(0) for m in _WORD_RE.finditer(text)}


def _content_tokens(value: str) -> set[str]:
    tokens = _tokenize(value)
    return {
        tok
        for tok in tokens
        if len(tok) >= 3 and tok not in _FRENCH_STOPWORDS and not tok.isdigit()
    }


def _lexical_overlap(a: str, b: str) -> float:
    ta = _tokenize(a)
    tb = _tokenize(b)
    if not ta or not tb:
        return 0.0
    return round(len(ta & tb) / len(ta | tb), 4)


def _content_overlap(a: str, b: str) -> float:
    ta = _content_tokens(a)
    tb = _content_tokens(b)
    if not ta or not tb:
        return 0.0
    return round(len(ta & tb) / len(ta | tb), 4)


def _char_similarity(a: str, b: str) -> float:
    na = _normalize_text(a)
    nb = _normalize_text(b)
    if not na or not nb:
        return 0.0
    return round(difflib.SequenceMatcher(None, na, nb).ratio(), 4)


def _sentence_units(text: str) -> list[str]:
    normalized = (text or "").replace("\r\n", "\n")
    pieces = [p.strip() for p in _SENTENCE_SPLIT_RE.split(normalized)]
    units = [p for p in pieces if len(p) >= 15]
    if units:
        return units
    fallback = normalize_spaces(text or "")
    return [fallback] if fallback else []


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _quality_decision_from_score(score: float) -> str:
    s = _clip01(score)
    if s >= 0.78:   # Phase 2: raised from 0.72 — only genuinely high-quality reqs reach DRAFT
        return "DRAFT"
    if s >= 0.50:
        return "TO_VALIDATE"
    return "REJECT"


def _get_spacy_nlp() -> tuple[Any | None, str]:
    global _SPACY_NLP, _SPACY_BACKEND, _SPACY_INIT_DONE
    if _SPACY_INIT_DONE:
        return _SPACY_NLP, _SPACY_BACKEND
    _SPACY_INIT_DONE = True
    if spacy is None:
        _SPACY_BACKEND = "NOT_INSTALLED"
        return None, _SPACY_BACKEND
    for model_name in ("fr_core_news_md", "fr_core_news_sm"):
        try:
            _SPACY_NLP = spacy.load(model_name)
            _SPACY_BACKEND = model_name
            return _SPACY_NLP, _SPACY_BACKEND
        except Exception:
            continue
    try:
        _SPACY_NLP = spacy.blank("fr")
        if "sentencizer" not in _SPACY_NLP.pipe_names:
            _SPACY_NLP.add_pipe("sentencizer")
        _SPACY_BACKEND = "fr_blank"
        return _SPACY_NLP, _SPACY_BACKEND
    except Exception:
        _SPACY_BACKEND = "LOAD_FAILED"
        return None, _SPACY_BACKEND


def _get_sentence_transformer() -> tuple[Any | None, str]:
    global _ST_MODEL, _ST_BACKEND, _ST_INIT_DONE
    if _ST_INIT_DONE:
        return _ST_MODEL, _ST_BACKEND
    _ST_INIT_DONE = True
    if SentenceTransformer is None:
        _ST_BACKEND = "NOT_INSTALLED"
        return None, _ST_BACKEND
    try:
        _ST_MODEL = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        _ST_BACKEND = "paraphrase-multilingual-MiniLM-L12-v2"
        return _ST_MODEL, _ST_BACKEND
    except Exception:
        _ST_BACKEND = "LOAD_FAILED"
        return None, _ST_BACKEND


def _vector_to_list(vec: Any) -> list[float]:
    if hasattr(vec, "tolist"):
        vec = vec.tolist()
    if isinstance(vec, list) and vec and isinstance(vec[0], list):
        vec = vec[0]
    if not isinstance(vec, list):
        return []
    out: list[float] = []
    for item in vec:
        try:
            out.append(float(item))
        except Exception:
            return []
    return out


def _embed_with_cache(text: str) -> tuple[list[float] | None, str]:
    normalized = _normalize_text(text)
    if not normalized:
        return None, "EMPTY_TEXT"
    if normalized in _ST_EMBED_CACHE:
        return _ST_EMBED_CACHE[normalized], "CACHE_HIT"
    model, backend = _get_sentence_transformer()
    if model is None:
        return None, backend
    try:
        emb = model.encode(normalized, normalize_embeddings=True)
    except Exception:
        return None, f"{backend}_ENCODE_FAILED"
    emb_list = _vector_to_list(emb)
    if not emb_list:
        return None, f"{backend}_EMPTY_EMBED"
    if len(_ST_EMBED_CACHE) >= _ST_EMBED_CACHE_MAX:
        oldest = next(iter(_ST_EMBED_CACHE.keys()))
        _ST_EMBED_CACHE.pop(oldest, None)
    _ST_EMBED_CACHE[normalized] = emb_list
    return emb_list, backend


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b:
        return 0.0
    n = min(len(vec_a), len(vec_b))
    if n <= 0:
        return 0.0
    dot = sum(vec_a[i] * vec_b[i] for i in range(n))
    norm_a = math.sqrt(sum(vec_a[i] * vec_a[i] for i in range(n)))
    norm_b = math.sqrt(sum(vec_b[i] * vec_b[i] for i in range(n)))
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def _semantic_similarity_transformer(a: str, b: str) -> tuple[float | None, str]:
    emb_a, backend_a = _embed_with_cache(a)
    emb_b, backend_b = _embed_with_cache(b)
    if emb_a is None or emb_b is None:
        return None, backend_a if emb_a is None else backend_b
    return round(_cosine_similarity(emb_a, emb_b), 4), backend_a


def _spacy_doc(text: str) -> Any | None:
    nlp, _ = _get_spacy_nlp()
    if nlp is None or not text:
        return None
    try:
        return nlp(text)
    except Exception:
        return None


def _spacy_has_condition_cue(text: str) -> bool:
    lowered = _normalize_text(text)
    if any(fragment in lowered for fragment in ("en cas de", "a condition de", "à condition de", "sous reserve de", "sous réserve de")):
        return True
    doc = _spacy_doc(text)
    if doc is None:
        return False
    return any(tok.lower_ in {"si", "lorsque", "quand", "lorsqu"} for tok in doc)


def _spacy_has_exception_cue(text: str) -> bool:
    lowered = _normalize_text(text)
    if any(fragment in lowered for fragment in ("par derogation", "par dérogation", "sauf", "toutefois", "cependant", "except")):
        return True
    doc = _spacy_doc(text)
    if doc is None:
        return False
    return any(tok.lower_ in {"sauf", "toutefois", "cependant", "excepté", "excepte", "derogation", "dérogation"} for tok in doc)


def _spacy_has_scope_cue(text: str) -> bool:
    lowered = _normalize_text(text)
    if any(fragment in lowered for fragment in ("a l'egard de", "à l'égard de", "au titre de", "dans le cadre de", "relatif", "concernant")):
        return True
    doc = _spacy_doc(text)
    if doc is None:
        return False
    return any(tok.lower_ in {"pour", "concernant", "relatif", "relative", "relatives", "relatifs"} for tok in doc)


def _infer_req_type_spacy(requirement_text: str) -> str | None:
    text = requirement_text or ""
    if not text.strip():
        return None
    lowered = _normalize_text(text)
    doc = _spacy_doc(text)
    has_neg = False
    has_modal = False
    if doc is not None:
        has_neg = any(tok.dep_ == "neg" or tok.lower_ in {"ne", "pas", "jamais", "aucun"} for tok in doc)
        has_modal = any(tok.lemma_ in {"devoir", "falloir", "tenir"} for tok in doc)
    has_neg = has_neg or bool(
        re.search(r"(?i)\bne\b[^.!?]{0,30}\bpas\b|\bn['’][a-z]{1,12}\b[^.!?]{0,30}\bpas\b", lowered)
    )
    has_modal = has_modal or (" doit " in f" {lowered} " or " est tenu" in lowered)
    has_reporting_verb = bool(
        re.search(
            r"(?i)\bcommuniqu\w+\b|\btransmett?\w+\b|\badress\w+\b|\bfourn\w+\b|\binform\w+\b",
            lowered,
        )
    )
    has_reporting_context = bool(
        re.search(
            r"(?i)\b(?:minist[èe]re|autorit[ée]s?|inspection|administration|service\s+comp[ée]tent|"
            r"organisme|office|agence|informations?|donn[ée]es|renseignements?|rapport)\b",
            lowered,
        )
    ) or "chaque année" in lowered or "annuellement" in lowered
    has_obligation_effect = bool(
        re.search(
            r"(?i)\bprend\s+en\s+charge\b|"
            r"\bb[ée]n[ée]fici\w*\b|"
            r"\bcommuniqu\w+\b|\btransmet\w+\b|\badress\w+\b|"
            r"\bsont?\s+fix\w+\b|\bfix\w+\s+conform[ée]ment\b|"
            r"\bsont?\s+recouvr\w+\b|\bsont?\s+affect\w+\b",
            lowered,
        )
    )
    restrictive_condition = bool(
        re.search(r"(?i)\bne\s+peu(?:t|vent)\s+[^.!?;:]{0,280}\bque\b", lowered)
    )
    explicit_interdiction = bool(
        re.search(
            r"(?i)\binterdit(?:e|es|s)?\b|"
            r"\bne\s+(?:doit(?:vent)?|peu(?:t|vent))\s+pas\b|"
            r"\bne\s+peu(?:t|vent)\s+"
            r"(?![^.!?;:]{0,280}\bque\b)"
            r"(?:participer|concourir|b[ée]n[ée]ficier|pr[eé]tendre|acc[eé]der|exercer|d[eé]poser|soumettre)\b",
            lowered,
        )
    )

    if restrictive_condition:
        return "CONDITION"
    if explicit_interdiction:
        return "INTERDICTION"
    if _spacy_has_exception_cue(text):
        return "EXCEPTION"
    if _spacy_has_condition_cue(text):
        return "CONDITION"
    if "responsable" in lowered or "repond de" in lowered or "répond de" in lowered:
        return "RESPONSABILITE"
    if "registre" in lowered or "consigner" in lowered:
        return "REGISTRE"
    if "notifier" in lowered or "declar" in lowered or "déclar" in lowered:
        return "DECLARATION"
    if has_reporting_verb and has_reporting_context:
        return "DECLARATION"
    if has_modal or has_obligation_effect:
        return "OBLIGATION"
    return None


def _truncate_excerpt(value: str, max_chars: int = _GROUNDING_EXCERPT_MAX_CHARS) -> str:
    text = normalize_spaces(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def assess_grounding(
    *,
    requirement_text: str,
    snippet: str,
    chunk_text: str,
) -> dict[str, Any]:
    req = (requirement_text or "").strip()
    src = (snippet or "").strip()
    chunk = (chunk_text or "").strip()

    req_to_snippet_overlap = _content_overlap(req, src) if src else 0.0
    req_to_snippet_char = _char_similarity(req, src) if src else 0.0
    snippet_in_chunk = bool(src) and (_normalize_text(src) in _normalize_text(chunk))
    semantic_similarity_snippet, semantic_backend = (
        _semantic_similarity_transformer(req, src) if src else (None, "NO_SNIPPET")
    )

    best_unit = ""
    best_unit_overlap = 0.0
    best_unit_char = 0.0
    for unit in _sentence_units(chunk):
        overlap = _content_overlap(req, unit)
        char_sim = _char_similarity(req, unit)
        if (overlap, char_sim) > (best_unit_overlap, best_unit_char):
            best_unit = unit
            best_unit_overlap = overlap
            best_unit_char = char_sim

    semantic_similarity_best_unit, _ = (
        _semantic_similarity_transformer(req, best_unit) if best_unit else (None, semantic_backend)
    )

    weighted_parts: list[tuple[float, float]] = []
    if src:
        weighted_parts.extend(
            [
                (0.25, req_to_snippet_overlap),   # Phase 2: 0.45→0.25 (LLM paraphrases ≠ bad grounding)
                (0.15, req_to_snippet_char),       # Phase 2: 0.20→0.15
                (0.20, 1.0 if snippet_in_chunk else 0.0),
                (0.10, best_unit_overlap),
                (0.05, best_unit_char),
            ]
        )
        if semantic_similarity_snippet is not None:
            weighted_parts.append((0.35, semantic_similarity_snippet))   # Phase 2: 0.20→0.35
        if semantic_similarity_best_unit is not None:
            weighted_parts.append((0.12, semantic_similarity_best_unit)) # Phase 2: 0.08→0.12
    else:
        weighted_parts.extend([(0.75, best_unit_overlap), (0.25, best_unit_char)])
        if semantic_similarity_best_unit is not None:
            weighted_parts.append((0.2, semantic_similarity_best_unit))

    weight_sum = sum(w for w, _ in weighted_parts) or 1.0
    score = sum(w * v for w, v in weighted_parts) / weight_sum

    score = round(min(1.0, max(0.0, score)), 4)

    reasons: list[str] = []
    if not src:
        reasons.append("GROUNDING_SNIPPET_EMPTY")
    if src and not snippet_in_chunk:
        reasons.append("GROUNDING_SNIPPET_NOT_IN_CHUNK")
    if score < 0.28:
        reasons.append("GROUNDING_SCORE_HARD_FAIL")
        verdict = "HARD_FAIL"
    elif score < 0.48:
        reasons.append("GROUNDING_SCORE_SOFT_FAIL")
        verdict = "SOFT_FAIL"
    else:
        verdict = "PASS"

    return {
        "score": score,
        "verdict": verdict,
        "req_to_snippet_overlap": req_to_snippet_overlap,
        "req_to_snippet_char_ratio": req_to_snippet_char,
        "best_chunk_unit_overlap": best_unit_overlap,
        "best_chunk_unit_char_ratio": best_unit_char,
        "snippet_in_chunk": snippet_in_chunk,
        "semantic_similarity_snippet": semantic_similarity_snippet,
        "semantic_similarity_best_unit": semantic_similarity_best_unit,
        "semantic_backend": semantic_backend,
        "best_chunk_unit_excerpt": _truncate_excerpt(best_unit),
        "reasons": reasons,
    }


def _find_hits(text: str, rules: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    lowered = _normalize_text(text)
    hits: list[str] = []
    for name, pattern in rules:
        if pattern.search(lowered):
            hits.append(name)
    return hits


def _matches_type_markers(req_type: str, text: str) -> bool:
    req_type_up = (req_type or "").strip().upper()
    markers = _TYPE_MARKERS.get((req_type or "").strip().upper(), [])
    if not markers:
        inferred = _infer_req_type_spacy(text)
        return True if inferred is None else inferred == req_type_up
    lowered = _normalize_text(text)
    if any(pattern.search(lowered) for pattern in markers):
        return True
    inferred = _infer_req_type_spacy(text)
    return True if inferred is None else inferred == req_type_up


def _has_any_cue(text: str, cues: list[re.Pattern[str]]) -> bool:
    lowered = _normalize_text(text)
    return any(pattern.search(lowered) for pattern in cues)


def _scope_present(requirement_text: str, source_text: str) -> bool:
    if _has_any_cue(requirement_text, _SCOPE_CUES):
        return True

    lowered_source = _normalize_text(source_text)
    lowered_req = _normalize_text(requirement_text)
    if not lowered_source or not lowered_req:
        return False

    for cue in _SCOPE_CUES:
        for match in cue.finditer(lowered_source):
            fragment = lowered_source[match.start() : match.start() + 120]
            if _content_overlap(fragment, lowered_req) >= 0.25:
                return True
    return False


def _looks_like_fused_action_chain(text: str) -> bool:
    normalized = normalize_spaces(text)
    if not normalized:
        return False

    if ";" in normalized and re.search(
        r"(?i)\b(?:doit(?:vent)?|est\s+tenu(?:e|s|es)?\s+d(?:e|')|sont\s+tenu(?:s|es)?\s+d(?:e|')|"
        r"est\s+h(?:abilit|abilit)[ée]?\s+[àa]|sont\s+h(?:abilit|abilit)[ée]s?\s+[àa]|"
        r"est\s+charg[ée]?\s+de|sont\s+charg[ée]s?\s+de)\b",
        normalized,
    ):
        return True
    if _FUSED_AUXILIARY_CHAIN_RE.match(normalized):
        return True
    if _FUSED_MODAL_CHAIN_RE.match(normalized):
        return True
    return False


def _has_incomplete_transitive_clause(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    if _INCOMPLETE_TRANSITIVE_ACTION_RE.search(normalized):
        return True
    sentence_parts = [
        part.strip()
        for part in re.split(r"(?<=[\.;:!?])\s+", normalized)
        if part and part.strip()
    ]
    for part in sentence_parts:
        if _INCOMPLETE_TRANSITIVE_ACTION_RE.search(part):
            return True
    return False


def assess_completeness(
    *,
    requirement_text: str,
    snippet: str,
    chunk_text: str,
) -> dict[str, Any]:
    req = (requirement_text or "").strip()
    src = (snippet or "").strip()
    chunk = (chunk_text or "").strip()
    source = src if src else chunk

    condition_required = _has_any_cue(source, _CONDITION_CUES) or _spacy_has_condition_cue(source)
    exception_required = _has_any_cue(source, _EXCEPTION_CUES) or _spacy_has_exception_cue(source)
    scope_required = len(_normalize_text(source)) >= 35 and (
        _has_any_cue(source, _SCOPE_CUES) or _spacy_has_scope_cue(source)
    )

    condition_present = _has_any_cue(req, _CONDITION_CUES) or _spacy_has_condition_cue(req)
    exception_present = _has_any_cue(req, _EXCEPTION_CUES) or _spacy_has_exception_cue(req)
    scope_present = _scope_present(req, source) or _spacy_has_scope_cue(req)
    dangling_tail = bool(_DANGLING_TAIL_RE.search(_normalize_text(req)))
    missing_object = _has_incomplete_transitive_clause(req)
    fused_action_chain = _looks_like_fused_action_chain(req)

    missing_condition = condition_required and not condition_present
    missing_exception = exception_required and not exception_present
    missing_scope = scope_required and not scope_present

    missing_components: list[str] = []
    reasons: list[str] = []
    if missing_condition:
        missing_components.append("CONDITION")
        reasons.append("COMPLETENESS_MISSING_CONDITION")
    if missing_exception:
        missing_components.append("EXCEPTION")
        reasons.append("COMPLETENESS_MISSING_EXCEPTION")
    if missing_scope:
        missing_components.append("SCOPE")
        reasons.append("COMPLETENESS_MISSING_SCOPE")
    if missing_object:
        missing_components.append("OBJECT")
        reasons.append("COMPLETENESS_MISSING_OBJECT")
    if dangling_tail:
        missing_components.append("TAIL")
        reasons.append("COMPLETENESS_DANGLING_TAIL")
    if fused_action_chain:
        missing_components.append("GRANULARITY")
        reasons.append("COMPLETENESS_FUSED_ACTION_CHAIN")

    score = 1.0
    if missing_condition:
        score -= 0.35
    if missing_exception:
        score -= 0.35
    if missing_scope:
        score -= 0.3
    if missing_object:
        score -= 0.42
    if dangling_tail:
        score -= 0.45
    if fused_action_chain:
        score -= 0.22
    score = round(max(0.0, min(1.0, score)), 4)

    missing_count = len(missing_components)
    if missing_count == 0:
        verdict = "PASS"
    elif missing_count == 1:
        verdict = "SOFT_FAIL"
    else:
        verdict = "HARD_FAIL"

    return {
        "score": score,
        "verdict": verdict,
        "condition_required": condition_required,
        "condition_present": condition_present,
        "missing_condition": missing_condition,
        "exception_required": exception_required,
        "exception_present": exception_present,
        "missing_exception": missing_exception,
        "scope_required": scope_required,
        "scope_present": scope_present,
        "missing_scope": missing_scope,
        "missing_object": missing_object,
        "dangling_tail": dangling_tail,
        "fused_action_chain": fused_action_chain,
        "missing_components": missing_components,
        "reasons": reasons,
    }


def assess_postcall_requirement(
    *,
    requirement_text: str,
    req_type: str,
    snippet: str,
    chunk_text: str,
    confidence: float,
    status: str,
    normative_strength: str | None = None,
    overlap_hard_drop_threshold: float = 0.05,
    overlap_soft_validate_threshold: float = 0.12,
) -> dict[str, Any]:
    raw_req = (requirement_text or "").strip()
    raw_src = (snippet or "").strip()
    autocorr = apply_postcall_autocorrection(
        requirement_text=requirement_text,
        req_type=req_type,
        snippet=snippet,
        chunk_text=chunk_text,
    )
    req = (autocorr.get("requirement_text") or requirement_text or "").strip()
    req_type_up = (autocorr.get("req_type") or req_type or "AUTRE").strip().upper() or "AUTRE"
    src = (snippet or "").strip()
    chunk = (chunk_text or "").strip()

    reasons: list[str] = []
    reasons.extend(list(autocorr.get("changes") or []))
    normative_hits = _find_hits(req, _NORMATIVE_MARKERS)
    descriptive_hits = _find_hits(req, _DESCRIPTIVE_MARKERS)
    overlap = _lexical_overlap(req, src) if src else 0.0
    type_match = _matches_type_markers(req_type_up, req)
    grounding = assess_grounding(
        requirement_text=req,
        snippet=src,
        chunk_text=chunk,
    )
    completeness = assess_completeness(
        requirement_text=req,
        snippet=src,
        chunk_text=chunk,
    )
    subject_consistency = _subject_consistency_score(
        requirement_text=req,
        snippet=src,
        chunk_text=chunk,
    )

    decision = "KEEP"
    adjusted_status = status

    if len(req) < 15 or len(req.split()) < 3:
        reasons.append("TOO_SHORT")
        decision = "DROP"

    if descriptive_hits and not normative_hits:
        reasons.append("DESCRIPTIVE_WITHOUT_NORMATIVE_MARKER")
        decision = "DROP"

    if has_ocr_artifact_signals(req) and (has_ocr_artifact_signals(src) or overlap < 0.58):
        reasons.append("OCR_ARTIFACT_IN_REQUIREMENT")
        decision = "DROP"
    heavy_req_ocr = has_heavy_ocr_artifact_signals(raw_req)
    heavy_src_ocr = has_heavy_ocr_artifact_signals(raw_src)
    req_ocr_score = ocr_artifact_score(raw_req)
    src_ocr_score = ocr_artifact_score(raw_src)
    if heavy_req_ocr and (heavy_src_ocr or overlap < 0.82 or req_ocr_score >= 0.16):
        reasons.append("OCR_HEAVY_DEGRADATION_DROP")
        decision = "DROP"
    elif (req_ocr_score >= 0.12 or src_ocr_score >= 0.12) and adjusted_status != "TO_VALIDATE":
        reasons.append("OCR_DEGRADATION_VALIDATE")
        adjusted_status = "TO_VALIDATE"

    out_of_scope_reason = _detect_out_of_scope_reason(
        requirement_text=req,
        snippet=src,
    )
    if out_of_scope_reason:
        reasons.append(out_of_scope_reason)
        decision = "DROP"

    ocr_source_reason = _detect_ocr_source_pollution(
        requirement_text=req,
        snippet=src,
        lexical_overlap=overlap,
    )
    if ocr_source_reason:
        reasons.append(ocr_source_reason)
        decision = "DROP"

    corrupted_clause_reason = _detect_corrupted_clause_blend(
        requirement_text=req,
        snippet=src,
    )
    if corrupted_clause_reason:
        reasons.append(corrupted_clause_reason)
        decision = "DROP"

    if src and overlap < overlap_hard_drop_threshold and not normative_hits:
        reasons.append("LOW_SNIPPET_OVERLAP_HARD")
        decision = "DROP"
    elif src and overlap < overlap_soft_validate_threshold:
        reasons.append("LOW_SNIPPET_OVERLAP_SOFT")
        if adjusted_status != "TO_VALIDATE":
            adjusted_status = "TO_VALIDATE"

    if not type_match:
        reasons.append("TYPE_TEXT_MISMATCH")
        if req_type_up in {"INTERDICTION", "EXCEPTION", "CONDITION", "RESPONSABILITE"} and not normative_hits:
            decision = "DROP"
            reasons.append("TYPE_MISMATCH_HARD_DROP")
        elif adjusted_status != "TO_VALIDATE":
            adjusted_status = "TO_VALIDATE"

    if confidence < 0.35 and not normative_hits:
        reasons.append("LOW_CONFIDENCE_NON_NORMATIVE")
        decision = "DROP"
    elif confidence < 0.55 and adjusted_status != "TO_VALIDATE":
        reasons.append("LOW_CONFIDENCE_SOFT")
        adjusted_status = "TO_VALIDATE"

    if src and _normalize_text(src) not in _normalize_text(chunk):
        reasons.append("SNIPPET_NOT_IN_CHUNK")
        if adjusted_status != "TO_VALIDATE":
            adjusted_status = "TO_VALIDATE"

    grounding_verdict = str(grounding.get("verdict") or "SOFT_FAIL")
    grounding_score = float(grounding.get("score") or 0.0)
    reasons.extend(grounding.get("reasons", []))
    if grounding_verdict == "HARD_FAIL":
        if normative_hits and confidence >= 0.75:
            reasons.append("GROUNDING_HARD_FAIL_ESCALATE_VALIDATE")
            if adjusted_status != "TO_VALIDATE":
                adjusted_status = "TO_VALIDATE"
        else:
            reasons.append("GROUNDING_HARD_FAIL_DROP")
            decision = "DROP"
    elif grounding_verdict == "SOFT_FAIL" and adjusted_status != "TO_VALIDATE":
        reasons.append("GROUNDING_SOFT_FAIL_VALIDATE")
        adjusted_status = "TO_VALIDATE"

    completeness_verdict = str(completeness.get("verdict") or "SOFT_FAIL")
    completeness_score = float(completeness.get("score") or 0.0)
    reasons.extend(completeness.get("reasons", []))
    dangling_tail = bool(completeness.get("dangling_tail"))
    fused_action_chain = bool(completeness.get("fused_action_chain"))
    missing_object = bool(completeness.get("missing_object"))
    if missing_object:
        reasons.append("COMPLETENESS_MISSING_OBJECT_DROP")
        decision = "DROP"
    if dangling_tail:
        reasons.append("COMPLETENESS_DANGLING_TAIL_DROP")
        decision = "DROP"
    if completeness_verdict == "HARD_FAIL":
        if confidence >= 0.8 and normative_hits:
            reasons.append("COMPLETENESS_HARD_FAIL_ESCALATE_VALIDATE")
            if adjusted_status != "TO_VALIDATE":
                adjusted_status = "TO_VALIDATE"
        else:
            reasons.append("COMPLETENESS_HARD_FAIL_DROP")
            decision = "DROP"
    elif completeness_verdict == "SOFT_FAIL" and adjusted_status != "TO_VALIDATE":
        reasons.append("COMPLETENESS_SOFT_FAIL_VALIDATE")
        adjusted_status = "TO_VALIDATE"

    controlled_reject = False
    if decision == "DROP":
        controlled_reject = _is_controlled_reject(reasons)

    component_scores = {
        "llm_confidence": _clip01(confidence),
        "grounding_score": _clip01(grounding_score),
        "type_consistency": 1.0 if type_match else 0.0,
        "condition_completeness": _clip01(completeness_score),
        "subject_consistency": _clip01(float(subject_consistency.get("score") or 0.0)),
    }
    quality_score_raw = round(
        sum(_QUALITY_WEIGHTS[key] * component_scores[key] for key in _QUALITY_WEIGHTS.keys()),
        4,
    )
    quality_penalties_local: list[dict[str, Any]] = []

    # Pénalité incohérence force normative / type d'exigence
    # Ex: req_type=OBLIGATION + normative_strength=FACULTATIF → contradiction
    _ns = str(normative_strength or "").strip().upper()
    if _ns == "FACULTATIF" and req_type_up in _IMPERATIF_ONLY_TYPES:
        quality_penalties_local.append({
            "code": "NORMATIVE_STRENGTH_INCOHERENT",
            "value": _QUALITY_PENALTIES["NORMATIVE_STRENGTH_INCOHERENT"],
            "detail": f"req_type={req_type_up} incompatible avec normative_strength=FACULTATIF",
        })

    # Détection sur-qualification : LLM met IMPERATIF alors que le snippet
    # contient des marqueurs FACULTATIF sans aucun marqueur IMPERATIF.
    # Cela indique que le LLM n'a pas analysé la force normative et a mis la valeur
    # par défaut. On pénalise et on force TO_VALIDATE pour revue humaine.
    if _ns in ("IMPERATIF", ""):
        _src_for_ns = (src or chunk or "")
        _has_facultatif = bool(_FACULTATIF_MARKERS_RE.search(_src_for_ns))
        _has_imperatif  = bool(_IMPERATIF_MARKERS_RE.search(_src_for_ns))
        if _has_facultatif and not _has_imperatif:
            quality_penalties_local.append({
                "code": "NORMATIVE_STRENGTH_OVERQUALIFIED",
                "value": _QUALITY_PENALTIES["NORMATIVE_STRENGTH_OVERQUALIFIED"],
                "detail": (
                    "snippet contient marqueurs FACULTATIF/CONDITIONNEL "
                    "sans marqueur IMPERATIF, mais normative_strength=IMPERATIF"
                ),
            })
            if adjusted_status not in {"REJECT", "DROP"}:
                adjusted_status = "TO_VALIDATE"
                reasons.append("NORMATIVE_STRENGTH_SUSPECTED_OVERQUALIFIED")

    quality_penalty_total_local = round(
        sum(float(p.get("value") or 0.0) for p in quality_penalties_local),
        4,
    )
    quality_score = round(_clip01(quality_score_raw - quality_penalty_total_local), 4)
    quality_decision = _quality_decision_from_score(quality_score)
    draft_blocked_by_missing_exception = bool(completeness.get("missing_exception"))
    draft_blocked_by_grounding_weak = grounding_verdict == "HARD_FAIL" or grounding_score < 0.28

    if decision != "DROP":
        if quality_decision == "REJECT":
            decision = "DROP"
            adjusted_status = "REJECT"
            reasons.append("QUALITY_THRESHOLD_REJECT")
        elif quality_decision == "TO_VALIDATE":
            if adjusted_status != "TO_VALIDATE":
                adjusted_status = "TO_VALIDATE"
            reasons.append("QUALITY_THRESHOLD_VALIDATE")
        else:
            if adjusted_status not in {"TO_VALIDATE", "REJECT"}:
                adjusted_status = "DRAFT"
        if quality_decision == "DRAFT" and (
            draft_blocked_by_missing_exception or draft_blocked_by_grounding_weak
        ):
            quality_decision = "TO_VALIDATE"
            reasons.append("QUALITY_DRAFT_BLOCKED_RISK_SIGNAL")
            if draft_blocked_by_missing_exception:
                reasons.append("QUALITY_DRAFT_BLOCKED_MISSING_EXCEPTION")
            if draft_blocked_by_grounding_weak:
                reasons.append("QUALITY_DRAFT_BLOCKED_GROUNDING_WEAK")
            if adjusted_status not in {"TO_VALIDATE", "REJECT"}:
                adjusted_status = "TO_VALIDATE"
    elif adjusted_status != "REJECT":
        adjusted_status = "REJECT"

    risk_score = 0.0
    risk_score += 0.5 if decision == "DROP" else 0.0
    risk_score += 0.25 if adjusted_status == "TO_VALIDATE" else 0.0
    risk_score += 0.15 if not type_match else 0.0
    risk_score += 0.1 if overlap < overlap_soft_validate_threshold else 0.0
    risk_score += 0.15 if grounding_verdict == "HARD_FAIL" else 0.0
    risk_score += 0.08 if grounding_verdict == "SOFT_FAIL" else 0.0
    risk_score += 0.12 if completeness_verdict == "HARD_FAIL" else 0.0
    risk_score += 0.06 if completeness_verdict == "SOFT_FAIL" else 0.0
    risk_score += 0.06 if fused_action_chain else 0.0
    risk_score = round(min(1.0, risk_score), 4)

    return {
        "decision": decision,
        "adjusted_status": adjusted_status,
        "adjusted_req_type": req_type_up,
        "adjusted_requirement_text": req,
        "risk_score": risk_score,
        "lexical_overlap": overlap,
        "type_match": type_match,
        "grounding_score": grounding_score,
        "grounding_verdict": grounding_verdict,
        "grounding_pass": grounding_verdict == "PASS",
        "grounding_details": grounding,
        "completeness_score": completeness_score,
        "completeness_verdict": completeness_verdict,
        "completeness_pass": completeness_verdict == "PASS",
        "completeness_details": completeness,
        "missing_condition": bool(completeness.get("missing_condition")),
        "missing_exception": bool(completeness.get("missing_exception")),
        "missing_scope": bool(completeness.get("missing_scope")),
        "missing_object": missing_object,
        "dangling_tail": dangling_tail,
        "fused_action_chain": fused_action_chain,
        "auto_corrected": bool(autocorr.get("auto_corrected")),
        "auto_corrections": list(autocorr.get("changes") or []),
        "controlled_reject": controlled_reject,
        "normative_hits": normative_hits,
        "descriptive_hits": descriptive_hits,
        "quality_score_raw": quality_score_raw,
        "quality_score": quality_score,
        "quality_decision": quality_decision if decision != "DROP" else "REJECT",
        "quality_components": component_scores,
        "quality_weights": dict(_QUALITY_WEIGHTS),
        "quality_penalties": quality_penalties_local,
        "quality_penalty_total": quality_penalty_total_local,
        "draft_blocked_by_missing_exception": draft_blocked_by_missing_exception,
        "draft_blocked_by_grounding_weak": draft_blocked_by_grounding_weak,
        "quality_explainability": {
            "components": component_scores,
            "weights": dict(_QUALITY_WEIGHTS),
            "penalties": quality_penalties_local,
            "penalty_total": quality_penalty_total_local,
            "thresholds": {
                "draft_min": 0.72,
                "to_validate_min": 0.50,
            },
            "decision": quality_decision if decision != "DROP" else "REJECT",
        },
        "subject_consistency_score": float(subject_consistency.get("score") or 0.0),
        "subject_consistent": bool(subject_consistency.get("consistent", True)),
        "subject_consistency_details": subject_consistency,
        "reasons": reasons,
    }


def _canonical_requirement_text(value: str) -> str:
    cleaned, _ = _strip_requirement_leading_header(value)
    normalized = _normalize_text(cleaned)
    normalized = re.sub(r"[^\w\sàâäéèêëîïôöùûüç'-]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _strip_requirement_leading_header(text: str) -> tuple[str, bool]:
    value = normalize_spaces(text or "")
    changed = False
    ocr_art_prefix_re = re.compile(
        r"(?i)^\s*(?:art\.?|article)\s*(?:n[Â°o]?\s*)?"
        r"(?:premier|1er|unique|\d+(?:[-.]\d+)*)?\s*[-:'â€™\"â€“â€”]\s*"
    )
    for _ in range(3):
        page_stripped = _PAGE_PREFIX_RE.sub("", value)
        if page_stripped != value:
            value = normalize_spaces(page_stripped)
            changed = True
        jort_stripped = _JORT_META_PREFIX_RE.sub("", value)
        if jort_stripped != value:
            value = normalize_spaces(jort_stripped)
            changed = True
        art_stripped = _ARTICLE_HEADER_PREFIX_RE.sub("", value)
        if art_stripped != value:
            value = normalize_spaces(art_stripped)
            changed = True
            continue
        ocr_art_stripped = ocr_art_prefix_re.sub("", value)
        if ocr_art_stripped != value:
            value = normalize_spaces(ocr_art_stripped)
            changed = True
            continue
        break
    return value, changed


def _is_prefix_extended_duplicate(a: str, b: str, *, min_prefix_chars: int = 78) -> bool:
    na = _canonical_requirement_text(a)
    nb = _canonical_requirement_text(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) < min_prefix_chars:
        return False
    if not long.startswith(short + " "):
        return False
    overlap = _content_overlap(short, long)
    if overlap < 0.56:
        return False
    if _char_similarity(short, long) < 0.7:
        return False
    short_subject = _extract_legal_subject(short)
    long_subject = _extract_legal_subject(long)
    if short_subject and long_subject and _content_overlap(short_subject, long_subject) < 0.8:
        return False
    return True


def _is_noise_contaminated_requirement(text: str) -> bool:
    normalized = _normalize_text(text)
    return (
        has_ocr_artifact_signals(text)
        or has_heavy_ocr_artifact_signals(text)
        or bool(_BROKEN_HYPHEN_SPLIT_RE.search(normalized))
        or bool(_LEADING_FRAGMENT_BLEND_RE.search(normalized))
        or bool(_DASH_MODAL_CLAUSE_BLEND_RE.search(normalized))
    )


def _is_high_overlap_duplicate(a: str, b: str) -> bool:
    na = _canonical_requirement_text(a)
    nb = _canonical_requirement_text(b)
    if not na or not nb:
        return False

    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    short_tokens = _content_tokens(short)
    long_tokens = _content_tokens(long)
    if len(short_tokens) < 7 or not long_tokens:
        return False

    shared_tokens = short_tokens & long_tokens
    containment_ratio = len(shared_tokens) / max(1, len(short_tokens))
    lexical_overlap = _content_overlap(short, long)
    char_similarity = _char_similarity(short, long)
    if containment_ratio < 0.74:
        return False
    if max(lexical_overlap, char_similarity) < 0.3:
        return False
    if not (_is_noise_contaminated_requirement(a) or _is_noise_contaminated_requirement(b)):
        return False

    short_subject = _extract_legal_subject(short)
    long_subject = _extract_legal_subject(long)
    if short_subject and long_subject and _content_overlap(short_subject, long_subject) < 0.7:
        return False
    return True


def _semantic_requirement_similarity(a: str, b: str) -> float:
    lexical = max(_content_overlap(a, b), _char_similarity(a, b))
    semantic, _ = _semantic_similarity_transformer(a, b)
    if semantic is None:
        return lexical
    return max(lexical, semantic)


def _types_compatible(a: str, b: str) -> bool:
    a_up = (a or "AUTRE").strip().upper()
    b_up = (b or "AUTRE").strip().upper()
    if a_up == b_up:
        return True
    for group in _TYPE_COMPATIBLE_GROUPS:
        if a_up in group and b_up in group:
            return True
    return False


def _detect_out_of_scope_reason(
    *,
    requirement_text: str,
    snippet: str,
) -> str | None:
    req_norm = _normalize_text(requirement_text)
    src_norm = _normalize_text(snippet)
    combined = f"{req_norm} || {src_norm}".strip()

    if _PUBLICATION_BOILERPLATE_RE.search(combined):
        return "OUT_OF_SCOPE_PUBLICATION"
    if (
        "tableau annex" in combined
        and ("parcelle" in combined or "parcelles" in combined)
        and ("modifie" in combined or "modifiée" in combined or "modifiées" in combined)
    ):
        return "OUT_OF_SCOPE_AUTRE_DESCRIPTIVE"

    has_named_person = bool(_INDIVIDUAL_ACT_NAME_RE.search(combined))
    has_individual_act = any(marker in combined for marker in _INDIVIDUAL_ACT_MARKERS)
    has_general_normative = any(
        marker in combined
        for marker in (
            "doit",
            "doivent",
            "est tenu de",
            "sont tenus de",
            "interdit",
            "en cas de",
            "lorsque",
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
        )
    )
    has_institutional_subject = any(
        marker in combined
        for marker in (
            "direction generale",
            "direction générale",
            "ministere",
            "ministère",
            "administration",
            "etablissement",
            "établissement",
            "agriculteurs",
            "fonds national",
        )
    )
    has_jort_institutional_cue = any(
        marker in combined
        for marker in (
            "jury",
            "concours",
            "candidat",
            "commission",
            "deliberation",
            "délibération",
            "notation",
            "classement",
            "postes a pourvoir",
            "postes à pourvoir",
            "anciennete dans le grade",
            "ancienneté dans le grade",
        )
    )
    if has_jort_institutional_cue or has_general_normative:
        return None

    if has_named_person and has_individual_act and not has_general_normative and not has_institutional_subject:
        return "OUT_OF_SCOPE_INDIVIDUAL_ACT"
    if (
        has_individual_act
        and ("par delegation" in combined or "par délégation" in combined)
        and ("habilite a signer" in combined or "habilitée à signer" in combined or "habilité à signer" in combined)
        and not has_jort_institutional_cue
    ):
        return "OUT_OF_SCOPE_INDIVIDUAL_ACT"
    return None


def _detect_ocr_source_pollution(
    *,
    requirement_text: str,
    snippet: str,
    lexical_overlap: float,
) -> str | None:
    src = snippet or ""
    if not src.strip():
        return None
    pollution_hits = len(_OCR_MIXED_SOURCE_RE.findall(src))
    foreign_article_refs = len(re.findall(r"(?i)\b(?:article|art\.?)\s*\d{1,3}(?:[-.]\d+)*\b", src))
    if "journal officiel" in _normalize_text(src):
        pollution_hits += 2
    if foreign_article_refs >= 2:
        pollution_hits += 1
    if pollution_hits < 2:
        return None
    if lexical_overlap >= 0.62 and not has_ocr_artifact_signals(src):
        return None
    if len(_find_hits(requirement_text, _NORMATIVE_MARKERS)) >= 2 and lexical_overlap >= 0.42:
        return None
    return "OCR_SOURCE_MIXED_ARTICLE"


def _detect_corrupted_clause_blend(
    *,
    requirement_text: str,
    snippet: str,
) -> str | None:
    text = normalize_spaces(requirement_text or "")
    if not text:
        return None

    normalized = _normalize_text(text)
    modal_count = len(_find_hits(text, _NORMATIVE_MARKERS))
    broken_hyphen_count = len(_BROKEN_HYPHEN_SPLIT_RE.findall(normalized))
    ocr_score = max(
        ocr_artifact_score(text),
        ocr_artifact_score(snippet or ""),
    )

    if _LEADING_FRAGMENT_BLEND_RE.search(normalized):
        return "OCR_CORRUPTED_CLAUSE_BLEND"
    if _DASH_MODAL_CLAUSE_BLEND_RE.search(normalized):
        return "OCR_CORRUPTED_CLAUSE_BLEND"
    has_dash_scope_restart = any(
        token in normalized
        for token in (" - dans ", " - si ", " - lorsque ", " - pour ", " - en cas ")
    )
    dash_tail = normalized.split(" - ", 1)[1] if " - " in normalized else ""
    has_initial_modal = any(
        token in normalized
        for token in (
            " est tenu d'",
            " est tenue d'",
            " sont tenus d'",
            " sont tenues d'",
            " est tenu de ",
            " est tenue de ",
            " sont tenus de ",
            " sont tenues de ",
            " doit ",
            " doivent ",
        )
    )
    has_second_modal = any(
        token in dash_tail
        for token in (" doit ", " doivent ", " est ", " sont ", " etre ", " etres ")
    )
    if has_dash_scope_restart and has_initial_modal and has_second_modal:
        return "OCR_CORRUPTED_CLAUSE_BLEND"
    if broken_hyphen_count >= 2 and modal_count >= 1 and ocr_score >= 0.08:
        return "OCR_CORRUPTED_CLAUSE_BLEND"
    return None


def _is_out_of_scope_autre_descriptive(req_type: str, requirement_text: str) -> bool:
    req_type_up = (req_type or "AUTRE").strip().upper() or "AUTRE"
    if req_type_up != "AUTRE":
        return False
    req_norm = _normalize_text(requirement_text)
    normative_hits = _find_hits(requirement_text, _NORMATIVE_MARKERS)
    descriptive_hits = _find_hits(requirement_text, _DESCRIPTIVE_MARKERS)
    if "fait foi" in req_norm and not bool(normative_hits):
        return True
    return bool(descriptive_hits) and not bool(normative_hits)


def _infer_req_type_from_text(requirement_text: str, fallback_type: str) -> str:
    text = requirement_text or ""
    lowered = _normalize_text(text)
    for candidate_type in (
        "INTERDICTION",
        "EXCEPTION",
        "RESPONSABILITE",
        "REGISTRE",
        "DECLARATION",
        "OBLIGATION",
        "CONDITION",
    ):
        markers = _TYPE_MARKERS.get(candidate_type, [])
        if any(pattern.search(lowered) for pattern in markers):
            return candidate_type
    inferred = _infer_req_type_spacy(text)
    if inferred:
        return inferred
    return (fallback_type or "AUTRE").strip().upper() or "AUTRE"


def _status_rank(status: str) -> int:
    s = (status or "").strip().upper()
    if s == "DRAFT":
        return 3
    if s == "TO_VALIDATE":
        return 2
    if s == "REJECT":
        return 1
    return 0


def _ensure_sentence_style(text: str) -> tuple[str, list[str]]:
    value = normalize_spaces(text)
    changes: list[str] = []
    if not value:
        return value, changes
    if value != text:
        changes.append("AUTO_SPACE_NORMALIZATION")
    if value and value[0].isalpha() and value[0].islower():
        value = value[0].upper() + value[1:]
        changes.append("AUTO_CAPITALIZE")
    if value and value[-1] not in {".", ";", ":"}:
        value = value + "."
        changes.append("AUTO_TRAILING_PUNCT")
    return value, changes


def _extract_legal_subject(source_text: str) -> str:
    text = normalize_spaces(source_text)
    if not text:
        return ""
    pattern = re.compile(
        r"(?i)\b((?:l'|le|la|les|tout(?:e)?|chaque)\s+[a-zàâäéèêëîïôöùûüç0-9'-]{2,}(?:\s+[a-zàâäéèêëîïôöùûüç0-9'-]{2,}){0,4})\s+"
        r"(?:doit|est\s+tenu|ne\s+doit\s+pas|ne\s+peu(?:t|vent)\s+(?:pas\s+)?[a-zàâäéèêëîïôöùûüç]{3,}|est\s+interdit)"
    )
    m = pattern.search(text)
    if not m:
        return ""
    return normalize_spaces(m.group(1))


def _subject_consistency_score(
    *,
    requirement_text: str,
    snippet: str,
    chunk_text: str,
) -> dict[str, Any]:
    req = requirement_text or ""
    source = snippet or chunk_text or ""
    source_subject = _extract_legal_subject(source)
    req_subject = _extract_legal_subject(req)

    if not source_subject:
        return {
            "score": 0.85,
            "consistent": True,
            "source_subject": "",
            "requirement_subject": req_subject,
            "reason": "SUBJECT_SOURCE_UNAVAILABLE",
        }

    if not req_subject:
        starts_with_modal = _normalize_text(req).startswith(
            ("doit ", "est tenu", "ne doit pas", "ne peut ", "ne peuvent ")
        )
        return {
            "score": 0.35 if starts_with_modal else 0.65,
            "consistent": not starts_with_modal,
            "source_subject": source_subject,
            "requirement_subject": "",
            "reason": "SUBJECT_MISSING_IN_REQUIREMENT" if starts_with_modal else "SUBJECT_IMPLICIT",
        }

    overlap = _content_overlap(req_subject, source_subject)
    char_sim = _char_similarity(req_subject, source_subject)
    score = round((0.65 * overlap) + (0.35 * char_sim), 4)
    consistent = score >= 0.6
    return {
        "score": _clip01(score),
        "consistent": consistent,
        "source_subject": source_subject,
        "requirement_subject": req_subject,
        "reason": "SUBJECT_MATCH" if consistent else "SUBJECT_MISMATCH",
    }


def apply_postcall_autocorrection(
    *,
    requirement_text: str,
    req_type: str,
    snippet: str,
    chunk_text: str,
) -> dict[str, Any]:
    corrected_text = repair_common_ocr_artifacts(requirement_text or "")
    corrected_text, changes = _ensure_sentence_style(corrected_text)
    corrected_text, removed_header = _strip_requirement_leading_header(corrected_text)
    if removed_header:
        corrected_text = repair_common_ocr_artifacts(corrected_text)
        corrected_text, style_changes = _ensure_sentence_style(corrected_text)
        changes.extend(style_changes)
        changes.append("AUTO_STRIP_ARTICLE_PREFIX")
    corrected_type = (req_type or "AUTRE").strip().upper() or "AUTRE"

    inferred_type = _infer_req_type_from_text(corrected_text, corrected_type)
    if inferred_type != corrected_type and inferred_type != "AUTRE":
        corrected_type = inferred_type
        changes.append("AUTO_REQ_TYPE_FROM_TEXT")

    lowered = _normalize_text(corrected_text)
    subject = _extract_legal_subject(snippet or chunk_text)
    starts_with_modal = lowered.startswith("doit ") or lowered.startswith("est tenu") or lowered.startswith(
        "ne doit pas"
    )
    if starts_with_modal and subject:
        corrected_text = f"{subject} {corrected_text[0].lower() + corrected_text[1:]}"
        corrected_text, style_changes = _ensure_sentence_style(corrected_text)
        changes.extend(style_changes)
        changes.append("AUTO_SUBJECT_PREFIXED")

    normalized_subject_text = normalize_subject_from_context(
        corrected_text,
        snippet or chunk_text,
        chunk_text,
    )
    normalized_subject_text = repair_common_ocr_artifacts(normalized_subject_text)
    if normalized_subject_text != corrected_text:
        corrected_text = normalized_subject_text
        corrected_text, style_changes = _ensure_sentence_style(corrected_text)
        changes.extend(style_changes)
        changes.append("AUTO_SUBJECT_NORMALIZED")

    rebuilt_from_source = False
    if not _find_hits(corrected_text, _NORMATIVE_MARKERS):
        source_for_rebuild = snippet or chunk_text
        if has_ocr_artifact_signals(source_for_rebuild):
            fallback_candidates = build_empty_llm_fallback_requirements(source_for_rebuild)
            if fallback_candidates:
                best_candidate = fallback_candidates[0]
                rebuilt_text = normalize_spaces(str(best_candidate.get("requirement_text") or ""))
                rebuilt_type = str(best_candidate.get("req_type") or corrected_type).strip().upper() or corrected_type
                if rebuilt_text and rebuilt_text != corrected_text:
                    corrected_text = rebuilt_text
                    corrected_text, style_changes = _ensure_sentence_style(corrected_text)
                    changes.extend(style_changes)
                    corrected_type = rebuilt_type
                    rebuilt_from_source = True

    if rebuilt_from_source:
        changes.append("AUTO_OCR_REBUILD_FROM_SOURCE")

    # Deduplicate changes while preserving order.
    dedup_changes: list[str] = []
    for c in changes:
        if c not in dedup_changes:
            dedup_changes.append(c)

    return {
        "requirement_text": corrected_text,
        "req_type": corrected_type,
        "changes": dedup_changes,
        "auto_corrected": bool(dedup_changes),
    }


def _is_controlled_reject(reasons: list[str]) -> bool:
    hard_reject_markers = {
        "DESCRIPTIVE_WITHOUT_NORMATIVE_MARKER",
        "GROUNDING_HARD_FAIL_DROP",
        "COMPLETENESS_HARD_FAIL_DROP",
        "TYPE_MISMATCH_HARD_DROP",
        "LOW_CONFIDENCE_NON_NORMATIVE",
        "TOO_SHORT",
        "OUT_OF_SCOPE_INDIVIDUAL_ACT",
        "OUT_OF_SCOPE_PUBLICATION",
    }
    return any(reason in hard_reject_markers for reason in reasons)


def _apply_group_quality_penalties(
    *,
    candidate: dict[str, Any],
    penalty_codes: list[str],
) -> dict[str, Any]:
    quality_components = dict(candidate.get("quality_components") or {})
    quality_weights = dict(candidate.get("quality_weights") or _QUALITY_WEIGHTS)
    quality_penalties = list(candidate.get("quality_penalties") or [])
    penalty_total = float(candidate.get("quality_penalty_total") or 0.0)
    score_raw = float(candidate.get("quality_score_raw") or candidate.get("quality_score") or 0.0)

    for code in penalty_codes:
        value = float(_QUALITY_PENALTIES.get(code) or 0.0)
        if value <= 0:
            continue
        quality_penalties.append({"code": code, "value": round(value, 4)})
        penalty_total += value

    penalty_total = round(penalty_total, 4)
    final_score = round(_clip01(score_raw - penalty_total), 4)
    final_decision = _quality_decision_from_score(final_score)

    candidate["quality_score_raw"] = round(score_raw, 4)
    candidate["quality_score"] = final_score
    candidate["quality_decision"] = final_decision
    candidate["quality_components"] = quality_components
    candidate["quality_weights"] = quality_weights
    candidate["quality_penalties"] = quality_penalties
    candidate["quality_penalty_total"] = penalty_total
    candidate["quality_explainability"] = {
        "components": quality_components,
        "weights": quality_weights,
        "penalties": quality_penalties,
        "penalty_total": penalty_total,
        "thresholds": {"draft_min": 0.72, "to_validate_min": 0.50},
        "decision": final_decision,
    }

    if final_decision == "REJECT":
        candidate["_postcall_quality_force_drop"] = True
    elif final_decision == "TO_VALIDATE":
        if _status_rank(str(candidate.get("status") or "")) >= 3:
            candidate["status"] = "TO_VALIDATE"
    else:
        if str(candidate.get("status") or "").strip().upper() not in {"TO_VALIDATE", "REJECT"}:
            candidate["status"] = "DRAFT"

    return candidate


def resolve_postcall_candidates(
    *,
    predictions: list[dict[str, Any]],
    semantic_threshold: float = 0.9,
) -> dict[str, Any]:
    if not predictions:
        return {
            "predictions": [],
            "stats": {
                "input_predictions_total": 0,
                "kept_predictions_total": 0,
                "duplicates_removed_total": 0,
                "duplicate_rate": 0.0,
                "type_conflicts_total": 0,
                "type_conflicts_resolved_total": 0,
                "type_arbitration_updates_total": 0,
                "type_conflict_rate": 0.0,
                "out_of_scope_dropped_total": 0,
                "out_of_scope_fp_rate": 0.0,
                "status_downgraded_total": 0,
                "quality_score_avg": 0.0,
                "quality_decision_draft_total": 0,
                "quality_decision_to_validate_total": 0,
                "quality_decision_reject_total": 0,
                "reason_counts": {},
            },
        }

    groups: list[dict[str, Any]] = []
    internal_error_placeholders_total = 0
    for idx, pred in enumerate(predictions):
        if pred.get("_internal_error"):
            # Les placeholders d'erreurs provider ne sont pas des candidats juridiques:
            # on les exclut du scoring qualité post-call pour éviter un biais artificiel.
            internal_error_placeholders_total += 1
            continue
        req_text = str(pred.get("requirement_text") or "").strip()
        req_type = str(pred.get("req_type") or "AUTRE").strip().upper() or "AUTRE"
        canonical = _canonical_requirement_text(req_text)
        assigned = False
        for group in groups:
            if canonical and canonical == group["canonical"]:
                group["members"].append((idx, pred))
                assigned = True
                break
            group_ref_type = str(group.get("reference_type") or "AUTRE").strip().upper() or "AUTRE"
            if req_type == group_ref_type or _types_compatible(req_type, group_ref_type):
                if _is_prefix_extended_duplicate(req_text, group["reference_text"]) or _is_high_overlap_duplicate(
                    req_text,
                    group["reference_text"],
                ):
                    group["members"].append((idx, pred))
                    assigned = True
                    break
            sim = _semantic_requirement_similarity(req_text, group["reference_text"])
            if sim >= semantic_threshold:
                group["members"].append((idx, pred))
                assigned = True
                break
        if not assigned:
            groups.append(
                {
                    "canonical": canonical,
                    "reference_text": req_text,
                    "reference_type": req_type,
                    "members": [(idx, pred)],
                }
            )

    kept_predictions: list[dict[str, Any]] = []
    duplicates_removed_total = 0
    type_conflicts_total = 0
    type_conflicts_resolved_total = 0
    type_arbitration_updates_total = 0
    out_of_scope_dropped_total = 0
    status_downgraded_total = 0
    quality_score_sum = 0.0
    quality_decision_draft_total = 0
    quality_decision_to_validate_total = 0
    quality_decision_reject_total = 0
    reason_counts: dict[str, int] = {}

    for group in groups:
        members = group["members"]
        if not members:
            continue
        duplicates_removed_total += max(0, len(members) - 1)
        duplicate_group = len(members) > 1

        ranked = sorted(
            members,
            key=lambda item: (
                _status_rank(str(item[1].get("status") or "")),
                float(item[1].get("confidence") or 0.0),
                float(item[1].get("_postcall_grounding_score") or 0.0),
                float(item[1].get("_postcall_completeness_score") or 0.0),
            ),
            reverse=True,
        )
        _, base = ranked[0]
        candidate = dict(base)
        original_status_rank = _status_rank(str(candidate.get("status") or ""))

        observed_types = [
            str(m.get("req_type") or "AUTRE").strip().upper() or "AUTRE"
            for _, m in members
        ]
        distinct_types = sorted({t for t in observed_types if t})
        incompatible_pairs = 0
        for i, t1 in enumerate(distinct_types):
            for t2 in distinct_types[i + 1 :]:
                if not _types_compatible(t1, t2):
                    incompatible_pairs += 1
        has_conflict = incompatible_pairs > 0
        if has_conflict:
            type_conflicts_total += 1
            reason_counts["TYPE_CONFLICT_GROUP"] = int(reason_counts.get("TYPE_CONFLICT_GROUP") or 0) + 1

        text_type = _infer_req_type_from_text(
            requirement_text=str(candidate.get("requirement_text") or ""),
            fallback_type=str(candidate.get("req_type") or "AUTRE"),
        )

        if has_conflict:
            candidate_type = str(candidate.get("req_type") or "AUTRE").strip().upper() or "AUTRE"
            if text_type != candidate_type:
                type_arbitration_updates_total += 1
                candidate["req_type"] = text_type
                reason_counts["TYPE_ARBITRATION_TEXT_MATCH"] = int(
                    reason_counts.get("TYPE_ARBITRATION_TEXT_MATCH") or 0
                ) + 1
            else:
                freq: dict[str, int] = {}
                for t in observed_types:
                    freq[t] = int(freq.get(t) or 0) + 1
                best = sorted(
                    freq.items(),
                    key=lambda kv: (kv[1], _TYPE_PRIORITY.get(kv[0], 0)),
                    reverse=True,
                )[0][0]
                if best != candidate_type:
                    type_arbitration_updates_total += 1
                    candidate["req_type"] = best
                    reason_counts["TYPE_ARBITRATION_FREQUENCY"] = int(
                        reason_counts.get("TYPE_ARBITRATION_FREQUENCY") or 0
                    ) + 1
            type_conflicts_resolved_total += 1
            if _status_rank(str(candidate.get("status") or "")) >= 3:
                candidate["status"] = "TO_VALIDATE"
                status_downgraded_total += 1
                reason_counts["TYPE_CONFLICT_STATUS_DOWNGRADED"] = int(
                    reason_counts.get("TYPE_CONFLICT_STATUS_DOWNGRADED") or 0
                ) + 1

        penalty_codes: list[str] = []
        if duplicate_group:
            penalty_codes.append("DUPLICATE_GROUP")
        if has_conflict:
            penalty_codes.append("TYPE_CONFLICT_GROUP")

        out_of_scope_reason = _detect_out_of_scope_reason(
            requirement_text=str(candidate.get("requirement_text") or ""),
            snippet=str(candidate.get("citation_snippet") or ""),
        )
        if out_of_scope_reason:
            penalty_codes.append(out_of_scope_reason)
            candidate = _apply_group_quality_penalties(candidate=candidate, penalty_codes=penalty_codes)
            quality_score_sum += float(candidate.get("quality_score") or 0.0)
            quality_decision_reject_total += 1
            out_of_scope_dropped_total += 1
            reason_counts[out_of_scope_reason] = int(reason_counts.get(out_of_scope_reason) or 0) + 1
            continue

        if _is_out_of_scope_autre_descriptive(
            req_type=str(candidate.get("req_type") or "AUTRE"),
            requirement_text=str(candidate.get("requirement_text") or ""),
        ):
            penalty_codes.append("OUT_OF_SCOPE_AUTRE_DESCRIPTIVE")
            candidate = _apply_group_quality_penalties(candidate=candidate, penalty_codes=penalty_codes)
            quality_score_sum += float(candidate.get("quality_score") or 0.0)
            quality_decision_reject_total += 1
            out_of_scope_dropped_total += 1
            reason_counts["OUT_OF_SCOPE_AUTRE_DESCRIPTIVE"] = int(
                reason_counts.get("OUT_OF_SCOPE_AUTRE_DESCRIPTIVE") or 0
            ) + 1
            continue

        candidate = _apply_group_quality_penalties(candidate=candidate, penalty_codes=penalty_codes)
        quality_score_sum += float(candidate.get("quality_score") or 0.0)
        q_decision = str(candidate.get("quality_decision") or "TO_VALIDATE").upper()
        draft_blockers: list[str] = []
        if has_conflict:
            draft_blockers.append("TYPE_CONFLICT")
        if bool(candidate.get("missing_exception")):
            draft_blockers.append("MISSING_EXCEPTION")
        candidate_grounding_verdict = str(candidate.get("grounding_verdict") or "SOFT_FAIL").upper()
        candidate_grounding_score = float(candidate.get("grounding_score") or 0.0)
        if candidate_grounding_verdict == "HARD_FAIL" or candidate_grounding_score < 0.28:
            draft_blockers.append("GROUNDING_WEAK")
        if q_decision == "DRAFT" and draft_blockers:
            q_decision = "TO_VALIDATE"
            candidate["quality_decision"] = "TO_VALIDATE"
            if str(candidate.get("status") or "").strip().upper() not in {"REJECT", "TO_VALIDATE"}:
                candidate["status"] = "TO_VALIDATE"
            reason_counts["QUALITY_DRAFT_BLOCKED_RISK"] = int(
                reason_counts.get("QUALITY_DRAFT_BLOCKED_RISK") or 0
            ) + 1
            reasons_list = candidate.get("reasons")
            if isinstance(reasons_list, list):
                reasons_list.append("QUALITY_DRAFT_BLOCKED_RISK")
                for blocker in draft_blockers:
                    reasons_list.append(f"QUALITY_DRAFT_BLOCKED_{blocker}")
            for blocker in draft_blockers:
                reason_key = f"QUALITY_DRAFT_BLOCKED_{blocker}"
                reason_counts[reason_key] = int(reason_counts.get(reason_key) or 0) + 1
        if q_decision == "TO_VALIDATE":
            # Conservative promotion path: only promote when all high-trust checks pass.
            completeness_verdict = str(candidate.get("completeness_verdict") or "SOFT_FAIL").upper()
            completeness_score = float(candidate.get("completeness_score") or 0.0)
            type_match_flag = bool(candidate.get("type_match"))
            req_type_up = str(candidate.get("req_type") or "AUTRE").strip().upper()
            overlap_score = float(candidate.get("lexical_overlap") or 0.0)
            quality_score_candidate = float(candidate.get("quality_score") or 0.0)
            promote_to_draft = (
                not draft_blockers
                and candidate_grounding_verdict == "PASS"
                and candidate_grounding_score >= 0.72
                and completeness_verdict == "PASS"
                and completeness_score >= 0.85
                and type_match_flag
                and req_type_up != "AUTRE"
                and quality_score_candidate >= 0.84
                and overlap_score >= 0.16
            )
            if promote_to_draft:
                q_decision = "DRAFT"
                candidate["quality_decision"] = "DRAFT"
                if str(candidate.get("status") or "").strip().upper() != "REJECT":
                    candidate["status"] = "DRAFT"
                reason_counts["QUALITY_PROMOTED_TO_DRAFT_STRONG"] = int(
                    reason_counts.get("QUALITY_PROMOTED_TO_DRAFT_STRONG") or 0
                ) + 1
                reasons_list = candidate.get("reasons")
                if isinstance(reasons_list, list):
                    reasons_list.append("QUALITY_PROMOTED_TO_DRAFT_STRONG")
        if q_decision == "DRAFT":
            quality_decision_draft_total += 1
        elif q_decision == "TO_VALIDATE":
            quality_decision_to_validate_total += 1
        else:
            quality_decision_reject_total += 1

        if candidate.pop("_postcall_quality_force_drop", False):
            reason_counts["QUALITY_SCORE_REJECT"] = int(reason_counts.get("QUALITY_SCORE_REJECT") or 0) + 1
            continue

        if original_status_rank >= 3 and str(candidate.get("status") or "").strip().upper() == "TO_VALIDATE":
            status_downgraded_total += 1
            reason_counts["QUALITY_STATUS_DOWNGRADED"] = int(
                reason_counts.get("QUALITY_STATUS_DOWNGRADED") or 0
            ) + 1

        kept_predictions.append(candidate)

    input_total = max(0, len(predictions) - internal_error_placeholders_total)
    kept_total = len(kept_predictions)
    duplicate_rate = round(duplicates_removed_total / input_total, 4) if input_total else 0.0
    type_conflict_rate = round(type_conflicts_total / input_total, 4) if input_total else 0.0
    out_of_scope_fp_rate = round(out_of_scope_dropped_total / input_total, 4) if input_total else 0.0
    quality_score_avg = round(quality_score_sum / input_total, 4) if input_total else 0.0

    return {
        "predictions": kept_predictions,
        "stats": {
            "input_predictions_total": input_total,
            "kept_predictions_total": kept_total,
            "internal_error_placeholders_total": internal_error_placeholders_total,
            "duplicates_removed_total": duplicates_removed_total,
            "duplicate_rate": duplicate_rate,
            "type_conflicts_total": type_conflicts_total,
            "type_conflicts_resolved_total": type_conflicts_resolved_total,
            "type_arbitration_updates_total": type_arbitration_updates_total,
            "type_conflict_rate": type_conflict_rate,
            "out_of_scope_dropped_total": out_of_scope_dropped_total,
            "out_of_scope_fp_rate": out_of_scope_fp_rate,
            "status_downgraded_total": status_downgraded_total,
            "quality_score_avg": quality_score_avg,
            "quality_decision_draft_total": quality_decision_draft_total,
            "quality_decision_to_validate_total": quality_decision_to_validate_total,
            "quality_decision_reject_total": quality_decision_reject_total,
            "reason_counts": dict(sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        },
    }


def build_postcall_report(*, results: list[dict[str, Any]]) -> dict[str, Any]:
    total_cases = len(results)
    total_candidates = 0
    total_kept = 0
    total_dropped = 0
    total_status_downgraded = 0
    total_type_mismatch = 0
    total_grounding_pass = 0
    total_grounding_soft_fail = 0
    total_grounding_hard_fail = 0
    grounding_score_sum = 0.0
    total_completeness_pass = 0
    total_completeness_soft_fail = 0
    total_completeness_hard_fail = 0
    completeness_score_sum = 0.0
    total_missing_condition = 0
    total_missing_exception = 0
    total_missing_scope = 0
    total_auto_corrected = 0
    total_auto_corrections = 0
    total_controlled_reject = 0
    total_duplicates_removed = 0
    total_type_conflicts = 0
    total_type_conflicts_resolved = 0
    total_type_arbitration_updates = 0
    total_out_of_scope_dropped = 0
    total_quality_score_sum = 0.0
    total_quality_score_count = 0
    total_quality_decision_draft = 0
    total_quality_decision_to_validate = 0
    total_quality_decision_reject = 0
    reason_counts: dict[str, int] = {}
    case_rows: list[dict[str, Any]] = []

    for result in results:
        postcall = result.get("postcall", {})
        candidates = int(postcall.get("candidates_total") or 0)
        kept = int(postcall.get("kept_total") or 0)
        dropped = int(postcall.get("dropped_total") or 0)
        downgraded = int(postcall.get("status_downgraded_total") or 0)
        mismatches = int(postcall.get("type_mismatch_total") or 0)
        grounding_pass = int(postcall.get("grounding_pass_total") or 0)
        grounding_soft_fail = int(postcall.get("grounding_soft_fail_total") or 0)
        grounding_hard_fail = int(postcall.get("grounding_hard_fail_total") or 0)
        case_grounding_avg = float(postcall.get("grounding_score_avg") or 0.0)
        completeness_pass = int(postcall.get("completeness_pass_total") or 0)
        completeness_soft_fail = int(postcall.get("completeness_soft_fail_total") or 0)
        completeness_hard_fail = int(postcall.get("completeness_hard_fail_total") or 0)
        case_completeness_avg = float(postcall.get("completeness_score_avg") or 0.0)
        missing_condition = int(postcall.get("missing_condition_total") or 0)
        missing_exception = int(postcall.get("missing_exception_total") or 0)
        missing_scope = int(postcall.get("missing_scope_total") or 0)
        auto_corrected = int(postcall.get("auto_corrected_total") or 0)
        auto_corrections = int(postcall.get("auto_corrections_total") or 0)
        controlled_reject = int(postcall.get("controlled_reject_total") or 0)
        duplicates_removed = int(postcall.get("duplicates_removed_total") or 0)
        type_conflicts = int(postcall.get("type_conflicts_total") or 0)
        type_conflicts_resolved = int(postcall.get("type_conflicts_resolved_total") or 0)
        type_arbitration_updates = int(postcall.get("type_arbitration_updates_total") or 0)
        out_of_scope_dropped = int(postcall.get("out_of_scope_dropped_total") or 0)
        quality_score_avg = float(postcall.get("quality_score_avg") or 0.0)
        quality_decision_draft_total = int(postcall.get("quality_decision_draft_total") or 0)
        quality_decision_to_validate_total = int(postcall.get("quality_decision_to_validate_total") or 0)
        quality_decision_reject_total = int(postcall.get("quality_decision_reject_total") or 0)

        total_candidates += candidates
        total_kept += kept
        total_dropped += dropped
        total_status_downgraded += downgraded
        total_type_mismatch += mismatches
        total_grounding_pass += grounding_pass
        total_grounding_soft_fail += grounding_soft_fail
        total_grounding_hard_fail += grounding_hard_fail
        grounding_score_sum += case_grounding_avg * max(0, candidates)
        total_completeness_pass += completeness_pass
        total_completeness_soft_fail += completeness_soft_fail
        total_completeness_hard_fail += completeness_hard_fail
        completeness_score_sum += case_completeness_avg * max(0, candidates)
        total_missing_condition += missing_condition
        total_missing_exception += missing_exception
        total_missing_scope += missing_scope
        total_auto_corrected += auto_corrected
        total_auto_corrections += auto_corrections
        total_controlled_reject += controlled_reject
        total_duplicates_removed += duplicates_removed
        total_type_conflicts += type_conflicts
        total_type_conflicts_resolved += type_conflicts_resolved
        total_type_arbitration_updates += type_arbitration_updates
        total_out_of_scope_dropped += out_of_scope_dropped
        total_quality_score_sum += quality_score_avg * max(0, candidates)
        total_quality_score_count += max(0, candidates)
        total_quality_decision_draft += quality_decision_draft_total
        total_quality_decision_to_validate += quality_decision_to_validate_total
        total_quality_decision_reject += quality_decision_reject_total

        rc = postcall.get("reason_counts", {})
        if isinstance(rc, dict):
            for reason, count in rc.items():
                reason_counts[reason] = int(reason_counts.get(reason, 0)) + int(count or 0)

        case_rows.append(
            {
                "case_id": result.get("case_id"),
                "status": result.get("status"),
                "postcall_candidates_total": candidates,
                "postcall_kept_total": kept,
                "postcall_dropped_total": dropped,
                "postcall_drop_rate": round(dropped / candidates, 4) if candidates else 0.0,
                "postcall_type_mismatch_total": mismatches,
                "postcall_type_consistency_rate": (
                    round((candidates - mismatches) / candidates, 4) if candidates else 0.0
                ),
                "postcall_status_downgraded_total": downgraded,
                "postcall_grounding_pass_total": grounding_pass,
                "postcall_grounding_soft_fail_total": grounding_soft_fail,
                "postcall_grounding_hard_fail_total": grounding_hard_fail,
                "postcall_grounding_score_avg": round(case_grounding_avg, 4),
                "postcall_completeness_pass_total": completeness_pass,
                "postcall_completeness_soft_fail_total": completeness_soft_fail,
                "postcall_completeness_hard_fail_total": completeness_hard_fail,
                "postcall_completeness_score_avg": round(case_completeness_avg, 4),
                "postcall_missing_condition_total": missing_condition,
                "postcall_missing_exception_total": missing_exception,
                "postcall_missing_scope_total": missing_scope,
                "postcall_auto_corrected_total": auto_corrected,
                "postcall_auto_corrections_total": auto_corrections,
                "postcall_controlled_reject_total": controlled_reject,
                "postcall_duplicates_removed_total": duplicates_removed,
                "postcall_type_conflicts_total": type_conflicts,
                "postcall_type_conflicts_resolved_total": type_conflicts_resolved,
                "postcall_type_arbitration_updates_total": type_arbitration_updates,
                "postcall_out_of_scope_dropped_total": out_of_scope_dropped,
                "postcall_duplicate_rate": round(duplicates_removed / candidates, 4) if candidates else 0.0,
                "postcall_type_conflict_rate": round(type_conflicts / candidates, 4) if candidates else 0.0,
                "postcall_out_of_scope_fp_rate": round(out_of_scope_dropped / candidates, 4) if candidates else 0.0,
                "postcall_quality_score_avg": round(quality_score_avg, 4),
                "postcall_quality_decision_draft_total": quality_decision_draft_total,
                "postcall_quality_decision_to_validate_total": quality_decision_to_validate_total,
                "postcall_quality_decision_reject_total": quality_decision_reject_total,
            }
        )

    drop_rate = round(total_dropped / total_candidates, 4) if total_candidates else 0.0
    keep_rate = round(total_kept / total_candidates, 4) if total_candidates else 0.0
    grounding_pass_rate = (
        round(total_grounding_pass / total_candidates, 4) if total_candidates else 0.0
    )
    grounding_score_avg = (
        round(grounding_score_sum / total_candidates, 4) if total_candidates else 0.0
    )
    completeness_pass_rate = (
        round(total_completeness_pass / total_candidates, 4) if total_candidates else 0.0
    )
    completeness_score_avg = (
        round(completeness_score_sum / total_candidates, 4) if total_candidates else 0.0
    )
    auto_corrected_rate = round(total_auto_corrected / total_candidates, 4) if total_candidates else 0.0
    controlled_reject_rate = (
        round(total_controlled_reject / total_candidates, 4) if total_candidates else 0.0
    )
    duplicate_rate = round(total_duplicates_removed / total_candidates, 4) if total_candidates else 0.0
    type_conflict_rate = round(total_type_conflicts / total_candidates, 4) if total_candidates else 0.0
    out_of_scope_fp_rate = (
        round(total_out_of_scope_dropped / total_candidates, 4) if total_candidates else 0.0
    )
    type_consistency_rate = (
        round((total_candidates - total_type_mismatch) / total_candidates, 4)
        if total_candidates
        else 0.0
    )
    quality_score_avg = (
        round(total_quality_score_sum / total_quality_score_count, 4)
        if total_quality_score_count
        else 0.0
    )

    return {
        "postcall_version": POSTCALL_QUALITY_VERSION,
        "generated_at_utc": _now_utc_iso(),
        "summary": {
            "cases_total": total_cases,
            "candidates_total": total_candidates,
            "kept_total": total_kept,
            "dropped_total": total_dropped,
            "keep_rate": keep_rate,
            "drop_rate": drop_rate,
            "status_downgraded_total": total_status_downgraded,
            "type_mismatch_total": total_type_mismatch,
            "type_consistency_rate": type_consistency_rate,
            "grounding_pass_total": total_grounding_pass,
            "grounding_soft_fail_total": total_grounding_soft_fail,
            "grounding_hard_fail_total": total_grounding_hard_fail,
            "grounding_pass_rate": grounding_pass_rate,
            "grounding_score_avg": grounding_score_avg,
            "completeness_pass_total": total_completeness_pass,
            "completeness_soft_fail_total": total_completeness_soft_fail,
            "completeness_hard_fail_total": total_completeness_hard_fail,
            "completeness_pass_rate": completeness_pass_rate,
            "completeness_score_avg": completeness_score_avg,
            "missing_condition_total": total_missing_condition,
            "missing_exception_total": total_missing_exception,
            "missing_scope_total": total_missing_scope,
            "auto_corrected_total": total_auto_corrected,
            "auto_corrections_total": total_auto_corrections,
            "auto_corrected_rate": auto_corrected_rate,
            "controlled_reject_total": total_controlled_reject,
            "controlled_reject_rate": controlled_reject_rate,
            "duplicates_removed_total": total_duplicates_removed,
            "duplicate_rate": duplicate_rate,
            "type_conflicts_total": total_type_conflicts,
            "type_conflicts_resolved_total": total_type_conflicts_resolved,
            "type_arbitration_updates_total": total_type_arbitration_updates,
            "type_conflict_rate": type_conflict_rate,
            "out_of_scope_dropped_total": total_out_of_scope_dropped,
            "out_of_scope_fp_rate": out_of_scope_fp_rate,
            "quality_score_avg": quality_score_avg,
            "quality_decision_draft_total": total_quality_decision_draft,
            "quality_decision_to_validate_total": total_quality_decision_to_validate,
            "quality_decision_reject_total": total_quality_decision_reject,
            "reason_counts": dict(sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        },
        "cases": case_rows,
    }


def persist_postcall_report(
    *,
    postcall_report: dict[str, Any],
    outdir: str,
    timestamp: str,
    run_id: str,
) -> tuple[Path, Path]:
    root = Path(outdir).expanduser().resolve()
    postcall_dir = root / "postcall"
    postcall_history_dir = root / "history" / "postcall"

    postcall_dir.mkdir(parents=True, exist_ok=True)
    postcall_history_dir.mkdir(parents=True, exist_ok=True)

    safe_run_id = _safe_filename_component(run_id)
    latest_path = postcall_dir / "postcall_latest.json"
    history_path = postcall_history_dir / f"postcall_{timestamp}_{safe_run_id}.json"

    content = json.dumps(postcall_report, ensure_ascii=False, indent=2)
    latest_path.write_text(content, encoding="utf-8")
    history_path.write_text(content, encoding="utf-8")

    return latest_path, history_path
