import json
import os
import re
import sqlite3
import time
import hashlib
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

# Defensive import: some runtimes do not auto-load sitecustomize.
# Importing it explicitly keeps proxy sanitization consistent.
try:
    import sitecustomize  # noqa: F401
except Exception:
    pass

from dotenv import load_dotenv
from google import genai
from pydantic import ValidationError
try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency at runtime
    OpenAI = None

from a1_schemas import RequirementLLMResponse
from a1_prompt_contract import get_prompt_contract

load_dotenv()

AVAILABILITY_CONTROL_VERSION = "B2.3-1.1.0"
RETRY_POLICY_VERSION = "B2.2-1.1.0"
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)

_ARTICLE_CODE_RE = re.compile(
    r"(?i)\b(?:article|art\.?)\s*(?:n\s*)?(premier|1er|unique|\d+(?:[-\.]\d+)*)(?:\s*\(?\s*(bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies|undecies|duodecies)\s*\)?)?"
)
_FEEDBACK_NORMATIVE_VERB_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(est interdit de)\b", re.IGNORECASE), "est interdit de"),
    (re.compile(r"\b(sont interdits de)\b", re.IGNORECASE), "sont interdits de"),
    (re.compile(r"\b(est tenu de)\b", re.IGNORECASE), "est tenu de"),
    (re.compile(r"\b(sont tenus de)\b", re.IGNORECASE), "sont tenus de"),
    (re.compile(r"\b(doit)\b", re.IGNORECASE), "doit"),
    (re.compile(r"\b(doivent)\b", re.IGNORECASE), "doivent"),
    (re.compile(r"\b(ne peut pas)\b", re.IGNORECASE), "ne peut pas"),
    (re.compile(r"\b(ne peuvent pas)\b", re.IGNORECASE), "ne peuvent pas"),
    (re.compile(r"\b(peut)\b", re.IGNORECASE), "peut"),
    (re.compile(r"\b(peuvent)\b", re.IGNORECASE), "peuvent"),
]
_FEEDBACK_CONDITION_MARKERS = (
    " si ",
    " lorsque ",
    " lorsqu'",
    " quand ",
    " en cas de ",
    " sous reserve de ",
    " sous réserve de ",
    " pour les ",
    " pour le ",
    " pour la ",
)
_FEEDBACK_EXCEPTION_MARKERS = (
    " sauf ",
    " sauf si ",
    " a l'exception de ",
    " à l'exception de ",
    " hors ",
)


def _normalize_feedback_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _find_feedback_marker(segment: str, markers: tuple[str, ...]) -> int:
    haystack = f" {segment.lower()} "
    best_index = -1
    for marker in markers:
        idx = haystack.find(marker)
        if idx == -1:
            continue
        best_index = idx if best_index == -1 else min(best_index, idx)
    return max(best_index - 1, 0) if best_index >= 0 else -1


def _infer_feedback_requirement_structure(requirement_text: Any, source_snippet: Any = "") -> dict[str, str]:
    raw_requirement = _normalize_feedback_text(requirement_text)
    raw_source = _normalize_feedback_text(source_snippet)
    legal_subject = ""
    normative_verb = ""
    action_object = raw_requirement
    condition_text = ""
    exception_text = ""

    verb_match = None
    for pattern, label in _FEEDBACK_NORMATIVE_VERB_PATTERNS:
        candidate = pattern.search(raw_requirement)
        if candidate and (verb_match is None or candidate.start() < verb_match.start()):
            verb_match = candidate
            normative_verb = label

    if verb_match:
        legal_subject = raw_requirement[:verb_match.start()].strip(" ,:;-")
        remainder = raw_requirement[verb_match.end():].strip(" ,:;-")
        action_object = remainder or raw_requirement
        cond_idx = _find_feedback_marker(remainder, _FEEDBACK_CONDITION_MARKERS)
        exc_idx = _find_feedback_marker(remainder, _FEEDBACK_EXCEPTION_MARKERS)
        split_points = [idx for idx in (cond_idx, exc_idx) if idx >= 0]
        if split_points:
            first_idx = min(split_points)
            action_object = remainder[:first_idx].strip(" ,:;-")
            tail = remainder[first_idx:].strip(" ,:;-")
            tail_cond_idx = _find_feedback_marker(tail, _FEEDBACK_CONDITION_MARKERS)
            tail_exc_idx = _find_feedback_marker(tail, _FEEDBACK_EXCEPTION_MARKERS)
            if tail_cond_idx >= 0 and (tail_exc_idx == -1 or tail_cond_idx <= tail_exc_idx):
                condition_text = tail.strip(" ,:;-")
            elif tail_exc_idx >= 0:
                exception_text = tail.strip(" ,:;-")

    similarity = 0.0
    if raw_requirement and raw_source:
        similarity = SequenceMatcher(None, raw_requirement.lower(), raw_source.lower()).ratio()
    if not raw_source:
        source_mode = "NON_PRECISE"
    elif similarity >= 0.92:
        source_mode = "VERBATIM"
    elif similarity >= 0.68:
        source_mode = "REFORMULE_LEGERE"
    else:
        source_mode = "RECONSTRUCTION_CONTROLEE"

    return {
        "legal_subject": legal_subject,
        "normative_verb": normative_verb,
        "action_object": action_object,
        "condition_text": condition_text,
        "exception_text": exception_text,
        "source_mode": source_mode,
    }


class LLMExtractor:
    def __init__(self):
        # Provider / models
        self.primary_provider = self._normalize_provider_name(
            os.getenv("PRIMARY_LLM_PROVIDER", "openai")
        )
        self.primary_model = os.getenv("PRIMARY_MODEL", "gpt-4.1-mini-2025-04-14").strip()

        self.fallback_provider = self._normalize_provider_name(
            os.getenv("FALLBACK_LLM_PROVIDER", "gemini")
        )
        self.fallback_model = os.getenv("FALLBACK_MODEL", "gemini-2.5-flash").strip()

        # Generation config
        self.temperature = float(os.getenv("LLM_TEMPERATURE", "0"))
        self.max_output_tokens = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "2048"))
        self.max_retries = int(os.getenv("LLM_MAX_RETRIES", "2"))
        self.primary_max_retries = int(os.getenv("LLM_PRIMARY_MAX_RETRIES", "0"))
        self.fallback_max_retries = int(os.getenv("LLM_FALLBACK_MAX_RETRIES", "1"))
        self.retry_base_seconds = float(os.getenv("LLM_RETRY_BASE_SECONDS", "2"))
        self.retry_max_seconds = float(os.getenv("LLM_RETRY_MAX_SECONDS", "45"))
        self.retry_log_every_n = max(1, int(os.getenv("LLM_RETRY_LOG_EVERY_N", "25")))
        self.retry_policy_version = (
            os.getenv("LLM_RETRY_POLICY_VERSION", RETRY_POLICY_VERSION).strip()
            or RETRY_POLICY_VERSION
        )
        self.failfast_primary_rate_limit = self._parse_bool_env("LLM_FAILFAST_PRIMARY_RATE_LIMIT", True)
        self.failfast_fallback_rate_limit = self._parse_bool_env("LLM_FAILFAST_FALLBACK_RATE_LIMIT", False)
        self.cooldown_enabled = self._parse_bool_env("LLM_COOLDOWN_ENABLED", True)
        self.cooldown_min_seconds = max(1, int(os.getenv("LLM_COOLDOWN_MIN_SECONDS", "15")))
        self.cooldown_max_seconds = max(self.cooldown_min_seconds, int(os.getenv("LLM_COOLDOWN_MAX_SECONDS", "1800")))
        self.cache_enabled = self._parse_bool_env("LLM_CACHE_ENABLED", True)
        self.cache_path = os.getenv("LLM_CACHE_PATH", ".cache/llm_cache.sqlite3").strip()
        self.cache_ttl_seconds = max(0, int(os.getenv("LLM_CACHE_TTL_SECONDS", "0")))
        self.cache_max_entries = max(1000, int(os.getenv("LLM_CACHE_MAX_ENTRIES", "50000")))
        self.cache_policy_version = os.getenv("LLM_CACHE_POLICY_VERSION", "B2.4-2.0.0").strip() or "B2.4-2.0.0"
        self.cache_lookup_relaxed_enabled = self._parse_bool_env("LLM_CACHE_LOOKUP_RELAXED_ENABLED", True)
        self.cache_lookup_relaxed_min_chars = max(
            0, int(os.getenv("LLM_CACHE_LOOKUP_RELAXED_MIN_CHARS", "120"))
        )
        self.cache_write_relaxed_alias = self._parse_bool_env("LLM_CACHE_WRITE_RELAXED_ALIAS", True)
        self.cache_negative_enabled = self._parse_bool_env("LLM_CACHE_NEGATIVE_ENABLED", True)
        self.cache_read_negative_enabled = self._parse_bool_env(
            "LLM_CACHE_READ_NEGATIVE_ENABLED",
            True,
        )
        self.cache_key_relaxed_normalizer = (
            os.getenv("LLM_CACHE_KEY_RELAXED_NORMALIZER", "legal_v1").strip().lower() or "legal_v1"
        )
        self.availability_policy_version = (
            os.getenv("LLM_AVAILABILITY_POLICY_VERSION", AVAILABILITY_CONTROL_VERSION).strip()
            or AVAILABILITY_CONTROL_VERSION
        )
        self.server_json_schema_enabled = self._parse_bool_env("LLM_SERVER_JSON_SCHEMA_ENABLED", False)
        self.default_input_cost_per_1k = float(os.getenv("LLM_DEFAULT_INPUT_PER_1K_USD", "0"))
        self.default_output_cost_per_1k = float(os.getenv("LLM_DEFAULT_OUTPUT_PER_1K_USD", "0"))
        self.ratecard = self._load_ratecard(os.getenv("LLM_RATECARD_JSON", ""))
        self.prompt_contract = get_prompt_contract()
        self.prompt_contract_version = self.prompt_contract.contract_version
        self.output_schema_version = self.prompt_contract.output_schema_version
        self.output_schema_sha256 = self.prompt_contract.output_schema_sha256
        self.response_schema_name = self.prompt_contract.output_schema_name
        self.strict_json_mode = bool(self.prompt_contract.strict_json_mode)
        self._response_schema = self.prompt_contract.output_schema
        self._response_schema_json = self.prompt_contract.output_schema_json

        # API keys
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.openai_base_url = (os.getenv("OPENAI_BASE_URL") or "").strip()
        self.disable_system_proxy = self._parse_bool_env("LLM_DISABLE_SYSTEM_PROXY", False)
        self.auto_bypass_broken_local_proxy = self._parse_bool_env(
            "LLM_AUTO_BYPASS_BROKEN_LOCAL_PROXY",
            True,
        )
        self.proxy_policy = self._apply_proxy_policy()

        # Clients
        self.gemini_client = genai.Client(api_key=self.gemini_api_key) if self.gemini_api_key else None
        if self.openai_api_key and OpenAI is not None:
            openai_client_kwargs = {"api_key": self.openai_api_key}
            if self.openai_base_url:
                openai_client_kwargs["base_url"] = self.openai_base_url
            self.openai_client = OpenAI(**openai_client_kwargs)
        else:
            self.openai_client = None

        # Runtime metadata
        self.last_provider_used: Optional[str] = None
        self.last_model_used: Optional[str] = None
        self.last_fallback_used: bool = False
        self.last_usage: dict[str, Any] = {}
        self.usage_totals: dict[str, Any] = {
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
        }
        self.retry_warn_total = 0
        self.provider_cooldown_until: dict[str, float] = {}
        self.last_cache_hit: bool = False
        self.last_cache_hit_source: str | None = None
        self.cache_hits_total: int = 0
        self.cache_hits_strict_total: int = 0
        self.cache_hits_relaxed_total: int = 0
        self.cache_negative_hits_total: int = 0
        self.cache_lookup_total: int = 0
        self.cache_misses_total: int = 0
        self.cache_put_total: int = 0
        self.cache_put_negative_total: int = 0
        self._cache_put_count: int = 0
        self._cache_conn: sqlite3.Connection | None = None
        self._prompt_sha256 = self.prompt_contract.prompt_sha256
        self.provider_rate_limit_streak: dict[str, int] = {}
        self.provider_rate_limit_streak_peak: dict[str, int] = {}
        self.last_availability: dict[str, Any] = {}
        self.availability_totals: dict[str, Any] = {
            "extract_calls_total": 0,
            "both_providers_cooldown_blocked_total": 0,
            "primary_skipped_cooldown_total": 0,
            "fallback_skipped_cooldown_total": 0,
            "failfast_rate_limit_total": 0,
            "cooldown_events_total": 0,
            "cooldown_seconds_total": 0.0,
            "retry_attempts_total": 0,
            "retry_wait_seconds_total": 0.0,
            "retry_exhausted_total": 0,
            "fallback_invoked_total": 0,
            "fallback_success_total": 0,
        }
        self._init_cache()
    
    
    def extract(self, article_label: str, chunk_text: str) -> RequirementLLMResponse:
        return self.extract_requirements(article_label=article_label, chunk_text=chunk_text)

    def extract_requirements(self, article_label: str, chunk_text: str) -> RequirementLLMResponse:
        """
        Appel principal :
        1) provider principal
        2) fallback provider si erreur / JSON invalide / validation invalide
        """
        self.last_provider_used = None
        self.last_model_used = None
        self.last_fallback_used = False
        self.last_usage = {}
        self.last_cache_hit = False
        self.last_cache_hit_source = None
        self._init_last_availability()
        self.availability_totals["extract_calls_total"] = int(
            self.availability_totals.get("extract_calls_total") or 0
        ) + 1

        errors = []
        primary_in_cooldown = self._is_provider_in_cooldown(self.primary_provider)
        fallback_in_cooldown = self._is_provider_in_cooldown(self.fallback_provider)

        if primary_in_cooldown and fallback_in_cooldown:
            self.last_availability["both_providers_cooldown_blocked"] = True
            self.last_availability["primary_skipped_cooldown"] = True
            self.last_availability["fallback_skipped_cooldown"] = True
            self.last_availability["primary_error_category"] = "cooldown_skip"
            self.last_availability["fallback_error_category"] = "cooldown_skip"
            self.availability_totals["both_providers_cooldown_blocked_total"] = int(
                self.availability_totals.get("both_providers_cooldown_blocked_total") or 0
            ) + 1
            self.availability_totals["primary_skipped_cooldown_total"] = int(
                self.availability_totals.get("primary_skipped_cooldown_total") or 0
            ) + 1
            self.availability_totals["fallback_skipped_cooldown_total"] = int(
                self.availability_totals.get("fallback_skipped_cooldown_total") or 0
            ) + 1
            p_left = self._provider_cooldown_remaining_seconds(self.primary_provider)
            f_left = self._provider_cooldown_remaining_seconds(self.fallback_provider)
            raise RuntimeError(
                "Providers temporairement indisponibles (cooldown quota/rate-limit). "
                f"{self.primary_provider}: {p_left:.1f}s, {self.fallback_provider}: {f_left:.1f}s."
            )

        # Cache check: primary first, then fallback.
        primary_cache = self._cache_get(
            provider=self.primary_provider,
            model=self.primary_model,
            article_label=article_label,
            chunk_text=chunk_text,
        )
        if primary_cache is not None:
            self.last_provider_used = self.primary_provider
            self.last_model_used = self.primary_model
            self.last_cache_hit = True
            self.last_availability["provider_rate_limit_streak"] = dict(self.provider_rate_limit_streak)
            self.last_usage = {
                "provider": self.primary_provider,
                "model": self.primary_model,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "cache_hit": True,
                "cache_hit_source": self.last_cache_hit_source or "strict",
                "prompt_contract_version": self.prompt_contract_version,
                "output_schema_version": self.output_schema_version,
                "output_schema_sha256": self.output_schema_sha256,
                "retry_policy_version": self.retry_policy_version,
                "retry_attempts_on_call": int(self.last_availability.get("retry_attempts_total") or 0),
                "retry_wait_seconds_on_call": round(
                    float(self.last_availability.get("retry_wait_seconds_total") or 0.0), 4
                ),
                "fallback_invoked_on_call": bool(self.last_availability.get("fallback_invoked")),
                "fallback_success_on_call": bool(self.last_availability.get("fallback_success")),
            }
            return primary_cache

        fallback_cache = self._cache_get(
            provider=self.fallback_provider,
            model=self.fallback_model,
            article_label=article_label,
            chunk_text=chunk_text,
        )
        if fallback_cache is not None:
            self.last_provider_used = self.fallback_provider
            self.last_model_used = self.fallback_model
            self.last_fallback_used = True
            self.last_cache_hit = True
            self.last_availability["provider_rate_limit_streak"] = dict(self.provider_rate_limit_streak)
            self.last_usage = {
                "provider": self.fallback_provider,
                "model": self.fallback_model,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "cache_hit": True,
                "cache_hit_source": self.last_cache_hit_source or "strict",
                "prompt_contract_version": self.prompt_contract_version,
                "output_schema_version": self.output_schema_version,
                "output_schema_sha256": self.output_schema_sha256,
                "retry_policy_version": self.retry_policy_version,
                "retry_attempts_on_call": int(self.last_availability.get("retry_attempts_total") or 0),
                "retry_wait_seconds_on_call": round(
                    float(self.last_availability.get("retry_wait_seconds_total") or 0.0), 4
                ),
                "fallback_invoked_on_call": bool(self.last_availability.get("fallback_invoked")),
                "fallback_success_on_call": bool(self.last_availability.get("fallback_success")),
            }
            return fallback_cache

        # Primary
        if primary_in_cooldown:
            self.last_availability["primary_skipped_cooldown"] = True
            self.last_availability["primary_error_category"] = "cooldown_skip"
            self.availability_totals["primary_skipped_cooldown_total"] = int(
                self.availability_totals.get("primary_skipped_cooldown_total") or 0
            ) + 1
            left = self._provider_cooldown_remaining_seconds(self.primary_provider)
            errors.append(
                f"Primary provider skipped ({self.primary_provider}/{self.primary_model}) cooldown {left:.1f}s"
            )
        else:
            try:
                self.last_availability["primary_attempted"] = True
                result = self._extract_with_provider(
                    provider=self.primary_provider,
                    model=self.primary_model,
                    article_label=article_label,
                    chunk_text=chunk_text,
                    max_retries=self.primary_max_retries,
                    fail_fast_rate_limit=self.failfast_primary_rate_limit,
                )

                self.last_provider_used = self.primary_provider
                self.last_model_used = self.primary_model
                self._cache_put(
                    provider=self.primary_provider,
                    model=self.primary_model,
                    article_label=article_label,
                    chunk_text=chunk_text,
                    response=result,
                )
                self._register_provider_success(self.primary_provider)
                return result

            except Exception as e:
                self._set_provider_cooldown_from_error(self.primary_provider, e)
                self.last_availability["primary_error_category"] = self._classify_error_category(e)
                errors.append(f"Primary provider failed ({self.primary_provider}/{self.primary_model}): {e}")

        # Fallback
        self.last_availability["fallback_invoked"] = True
        self.availability_totals["fallback_invoked_total"] = int(
            self.availability_totals.get("fallback_invoked_total") or 0
        ) + 1
        fallback_in_cooldown = self._is_provider_in_cooldown(self.fallback_provider)
        if fallback_in_cooldown:
            self.last_availability["fallback_skipped_cooldown"] = True
            self.last_availability["fallback_error_category"] = "cooldown_skip"
            self.availability_totals["fallback_skipped_cooldown_total"] = int(
                self.availability_totals.get("fallback_skipped_cooldown_total") or 0
            ) + 1
            left = self._provider_cooldown_remaining_seconds(self.fallback_provider)
            errors.append(
                f"Fallback provider skipped ({self.fallback_provider}/{self.fallback_model}) cooldown {left:.1f}s"
            )
        else:
            try:
                self.last_fallback_used = True
                self.last_availability["fallback_attempted"] = True

                result = self._extract_with_provider(
                    provider=self.fallback_provider,
                    model=self.fallback_model,
                    article_label=article_label,
                    chunk_text=chunk_text,
                    max_retries=self.fallback_max_retries,
                    fail_fast_rate_limit=self.failfast_fallback_rate_limit,
                )

                self.last_provider_used = self.fallback_provider
                self.last_model_used = self.fallback_model
                self._cache_put(
                    provider=self.fallback_provider,
                    model=self.fallback_model,
                    article_label=article_label,
                    chunk_text=chunk_text,
                    response=result,
                )
                self._register_provider_success(self.fallback_provider)
                self.last_availability["fallback_success"] = True
                self.availability_totals["fallback_success_total"] = int(
                    self.availability_totals.get("fallback_success_total") or 0
                ) + 1
                return result

            except Exception as e:
                self._set_provider_cooldown_from_error(self.fallback_provider, e)
                self.last_availability["fallback_error_category"] = self._classify_error_category(e)
                errors.append(f"Fallback provider failed ({self.fallback_provider}/{self.fallback_model}): {e}")

        raise RuntimeError(" | ".join(errors))

    def _extract_with_provider(
        self,
        *,
        provider: str,
        model: str,
        article_label: str,
        chunk_text: str,
        max_retries: int | None,
        fail_fast_rate_limit: bool,
    ) -> RequirementLLMResponse:
        provider_norm = self._normalize_provider_name(provider)
        if provider_norm == "gemini":
            return self._extract_with_gemini(
                model=model,
                article_label=article_label,
                chunk_text=chunk_text,
                max_retries=max_retries,
                fail_fast_rate_limit=fail_fast_rate_limit,
            )
        if provider_norm == "openai":
            return self._extract_with_openai(
                model=model,
                article_label=article_label,
                chunk_text=chunk_text,
                max_retries=max_retries,
                fail_fast_rate_limit=fail_fast_rate_limit,
            )
        raise RuntimeError(f"LLM provider inconnu: {provider_norm}")

    def _extract_with_gemini(
        self,
        model: str,
        article_label: str,
        chunk_text: str,
        *,
        max_retries: int | None = None,
        fail_fast_rate_limit: bool = False,
    ) -> RequirementLLMResponse:
        if not self.gemini_client:
            raise RuntimeError("GEMINI_API_KEY manquant")

        prompt = self._build_user_prompt(
            article_label=article_label,
            chunk_text=chunk_text,
            include_system_prompt=True,
        )

        response = self._call_with_retry(
            provider="gemini",
            model=model,
            max_retries=max_retries,
            fail_fast_rate_limit=fail_fast_rate_limit,
            call=lambda: self.gemini_client.models.generate_content(
                model=model,
                contents=prompt,
                config={
                    "temperature": self.temperature,
                    "max_output_tokens": self.max_output_tokens,
                    "response_mime_type": "application/json",
                    "response_json_schema": self._response_schema,
                },
            ),
        )

        raw_text = getattr(response, "text", None)
        if not raw_text:
            raise RuntimeError("Gemini n'a pas retourné de texte exploitable")

        usage = self._extract_gemini_usage(response)
        self._record_usage(provider="gemini", model=model, usage=usage)

        return self._validate_response_json(raw_text)

    def _extract_with_openai(
        self,
        model: str,
        article_label: str,
        chunk_text: str,
        *,
        max_retries: int | None = None,
        fail_fast_rate_limit: bool = False,
    ) -> RequirementLLMResponse:
        if not self.openai_client:
            if not self.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY manquant")
            raise RuntimeError("Package openai non installe (pip install openai)")

        user_prompt = self._build_user_prompt(
            article_label=article_label,
            chunk_text=chunk_text,
            include_system_prompt=False,
        )
        response_format: dict[str, Any] = {"type": "json_object"}
        if self.server_json_schema_enabled:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": self.response_schema_name,
                    "schema": self._response_schema,
                    "strict": True,
                },
            }

        completion = self._call_with_retry(
            provider="openai",
            model=model,
            max_retries=max_retries,
            fail_fast_rate_limit=fail_fast_rate_limit,
            call=lambda: self.openai_client.chat.completions.create(
                model=model,
                temperature=self.temperature,
                max_completion_tokens=self.max_output_tokens,
                messages=[
                    {"role": "system", "content": self.prompt_contract.prompt_text},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=response_format,
            ),
        )

        raw_text = None
        if getattr(completion, "choices", None):
            first_choice = completion.choices[0]
            message = getattr(first_choice, "message", None)
            if message is not None:
                content = getattr(message, "content", None)
                if isinstance(content, str):
                    raw_text = content
                elif isinstance(content, list):
                    parts = []
                    for part in content:
                        if isinstance(part, dict):
                            text_part = str(part.get("text") or "").strip()
                            if text_part:
                                parts.append(text_part)
                    raw_text = "\n".join(parts).strip() if parts else None

        if not raw_text:
            raise RuntimeError("OpenAI n'a pas retourne de texte exploitable")

        usage = self._extract_openai_usage(completion)
        self._record_usage(provider="openai", model=model, usage=usage)
        return self._validate_response_json(raw_text)

    def load_fewshot_examples(
        self,
        dsn: str,
        limit: int = 8,
        min_confidence: float = 0.82,
        tenant_id: str | None = None,
    ) -> None:
        """
        Charge les exemples few-shot depuis les validations en base :
        - APPROVE / EDIT  → exemples positifs (bonne extraction)
        - REJECT          → exemples négatifs (erreurs à éviter)
        - EDIT before/after → corrections humaines
        """
        try:
            from a1_prompts import (
                build_dynamic_fewshot_suffix,
                build_rejection_fewshot_suffix,
                build_edited_fewshot_suffix,
            )
            import psycopg as _psycopg

            def _fetch_approved(cur, tenant: str | None):
                sql = """
                    SELECT
                        COALESCE(r.citation_snippet, r.requirement_text) AS citation_snippet,
                        r.req_type,
                        r.requirement_text,
                        COALESCE(r.normative_strength, 'IMPERATIF') AS normative_strength,
                        COALESCE(v.comment, '') AS validator_comment
                    FROM requirement_validations v
                    JOIN requirements r ON r.requirement_id = v.requirement_id
                    JOIN documents d ON d.doc_id = r.doc_id
                    WHERE v.decision IN ('APPROVE', 'EDIT')
                      AND COALESCE(r.confidence, 0) >= %s
                      AND LENGTH(COALESCE(r.citation_snippet, r.requirement_text, '')) >= 40
                """
                params: list[Any] = [min_confidence]
                if tenant:
                    sql += " AND d.tenant_id = %s"
                    params.append(tenant)
                sql += """
                    ORDER BY COALESCE(r.quality_score, r.confidence, 0) DESC,
                             v.created_at DESC
                    LIMIT %s
                """
                params.append(limit * 3)
                cur.execute(sql, tuple(params))
                return cur.fetchall()

            def _fetch_rejected(cur, tenant: str | None):
                sql = """
                    SELECT
                        COALESCE(r.citation_snippet, '') AS citation_snippet,
                        r.requirement_text,
                        COALESCE(v.rejection_reason, 'AUTRE') AS rejection_reason,
                        COALESCE(v.comment, '') AS validator_comment
                    FROM requirement_validations v
                    JOIN requirements r ON r.requirement_id = v.requirement_id
                    JOIN documents d ON d.doc_id = r.doc_id
                    WHERE v.decision = 'REJECT'
                      AND LENGTH(COALESCE(r.citation_snippet, '')) >= 20
                      AND v.rejection_reason IS NOT NULL
                """
                params: list[Any] = []
                if tenant:
                    sql += " AND d.tenant_id = %s"
                    params.append(tenant)
                sql += " ORDER BY v.created_at DESC LIMIT %s"
                params.append(limit)
                cur.execute(sql, tuple(params))
                return cur.fetchall()

            def _fetch_edited(cur, tenant: str | None):
                sql = """
                    SELECT
                        COALESCE(r.citation_snippet, '') AS citation_snippet,
                        v.original_text,
                        r.requirement_text                              AS corrected_text,
                        r.req_type,
                        COALESCE(r.normative_strength, 'IMPERATIF')    AS normative_strength,
                        COALESCE(v.comment, '')                        AS validator_comment
                    FROM requirement_validations v
                    JOIN requirements r ON r.requirement_id = v.requirement_id
                    JOIN documents d ON d.doc_id = r.doc_id
                    WHERE v.decision = 'EDIT'
                      AND v.original_text IS NOT NULL
                      AND LENGTH(COALESCE(r.requirement_text, '')) >= 10
                """
                params: list[Any] = []
                if tenant:
                    sql += " AND d.tenant_id = %s"
                    params.append(tenant)
                sql += " ORDER BY v.created_at DESC LIMIT %s"
                params.append(5)
                cur.execute(sql, tuple(params))
                return cur.fetchall()

            with _psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    approved_rows = _fetch_approved(cur, tenant_id)
                    if not approved_rows and tenant_id:
                        approved_rows = _fetch_approved(cur, None)
                    rejected_rows = _fetch_rejected(cur, tenant_id)
                    edited_rows   = _fetch_edited(cur, tenant_id)

            # --- Exemples positifs (APPROVE + EDIT) ---
            groups: dict[str, dict] = {}
            for snippet, req_type, req_text, norm_str in approved_rows:
                key = str(snippet or "")[:120]
                if key not in groups:
                    groups[key] = {"citation_snippet": snippet, "requirements": []}
                groups[key]["requirements"].append({
                    "req_type": req_type,
                    "requirement_text": req_text,
                    "normative_strength": norm_str,
                    "legal_subject": "",
                    "normative_verb": "",
                    "action_object": "",
                    "condition_text": "",
                    "exception_text": "",
                    "source_mode": "NON_PRECISE",
                })
            good_examples = list(groups.values())[:limit]

            # --- Exemples négatifs (REJECT) ---
            bad_examples = [
                {
                    "citation_snippet": r[0],
                    "requirement_text": r[1],
                    "rejection_reason": r[2],
                }
                for r in rejected_rows
            ]

            # --- Corrections humaines (EDIT before/after) ---
            edit_examples = [
                {
                    "citation_snippet":  r[0],
                    "original_text":     r[1],
                    "corrected_text":    r[2],
                    "req_type":          r[3],
                    "normative_strength": r[4],
                    "legal_subject": "",
                    "normative_verb": "",
                    "action_object": "",
                    "condition_text": "",
                    "exception_text": "",
                    "source_mode": "NON_PRECISE",
                }
                for r in edited_rows
            ]

            suffix = build_dynamic_fewshot_suffix(good_examples)
            suffix += build_rejection_fewshot_suffix(bad_examples)
            suffix += build_edited_fewshot_suffix(edit_examples)

            self._fewshot_suffix = suffix
            print(
                f"  [Few-shot] +{len(good_examples)} approuvé(s), "
                f"+{len(bad_examples)} rejeté(s), "
                f"+{len(edit_examples)} corrigé(s).",
                flush=True,
            )
        except Exception as e:
            self._fewshot_suffix = ""
            print(f"  [Few-shot] Chargement ignoré (non-bloquant): {e}", flush=True)

    def load_fewshot_examples(
        self,
        dsn: str,
        limit: int = 8,
        min_confidence: float = 0.82,
        tenant_id: str | None = None,
    ) -> None:
        """
        Variante enrichie :
        - few-shots positifs structurés
        - rejets avec commentaire validateur
        - corrections humaines before/after
        """
        try:
            from a1_prompts import (
                build_dynamic_fewshot_suffix,
                build_rejection_fewshot_suffix,
                build_edited_fewshot_suffix,
            )
            import psycopg as _psycopg

            def _fetch_approved(cur, tenant: str | None):
                sql = """
                    SELECT
                        COALESCE(r.citation_snippet, r.requirement_text) AS citation_snippet,
                        r.req_type,
                        r.requirement_text,
                        COALESCE(r.normative_strength, 'IMPERATIF') AS normative_strength,
                        COALESCE(v.comment, '') AS validator_comment
                    FROM requirement_validations v
                    JOIN requirements r ON r.requirement_id = v.requirement_id
                    JOIN documents d ON d.doc_id = r.doc_id
                    WHERE v.decision IN ('APPROVE', 'EDIT')
                      AND COALESCE(r.confidence, 0) >= %s
                      AND LENGTH(COALESCE(r.citation_snippet, r.requirement_text, '')) >= 40
                """
                params: list[Any] = [min_confidence]
                if tenant:
                    sql += " AND d.tenant_id = %s"
                    params.append(tenant)
                sql += """
                    ORDER BY COALESCE(r.quality_score, r.confidence, 0) DESC,
                             v.created_at DESC
                    LIMIT %s
                """
                params.append(limit * 3)
                cur.execute(sql, tuple(params))
                return cur.fetchall()

            def _fetch_rejected(cur, tenant: str | None):
                sql = """
                    SELECT
                        COALESCE(r.citation_snippet, '') AS citation_snippet,
                        r.requirement_text,
                        COALESCE(v.rejection_reason, 'AUTRE') AS rejection_reason,
                        COALESCE(v.comment, '') AS validator_comment
                    FROM requirement_validations v
                    JOIN requirements r ON r.requirement_id = v.requirement_id
                    JOIN documents d ON d.doc_id = r.doc_id
                    WHERE v.decision = 'REJECT'
                      AND LENGTH(COALESCE(r.citation_snippet, '')) >= 20
                      AND v.rejection_reason IS NOT NULL
                """
                params: list[Any] = []
                if tenant:
                    sql += " AND d.tenant_id = %s"
                    params.append(tenant)
                sql += " ORDER BY v.created_at DESC LIMIT %s"
                params.append(limit)
                cur.execute(sql, tuple(params))
                return cur.fetchall()

            def _fetch_edited(cur, tenant: str | None):
                sql = """
                    SELECT
                        COALESCE(r.citation_snippet, '') AS citation_snippet,
                        v.original_text,
                        r.requirement_text                              AS corrected_text,
                        r.req_type,
                        COALESCE(r.normative_strength, 'IMPERATIF')    AS normative_strength,
                        COALESCE(v.comment, '')                        AS validator_comment
                    FROM requirement_validations v
                    JOIN requirements r ON r.requirement_id = v.requirement_id
                    JOIN documents d ON d.doc_id = r.doc_id
                    WHERE v.decision = 'EDIT'
                      AND v.original_text IS NOT NULL
                      AND LENGTH(COALESCE(r.requirement_text, '')) >= 10
                """
                params: list[Any] = []
                if tenant:
                    sql += " AND d.tenant_id = %s"
                    params.append(tenant)
                sql += " ORDER BY v.created_at DESC LIMIT %s"
                params.append(5)
                cur.execute(sql, tuple(params))
                return cur.fetchall()

            with _psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    approved_rows = _fetch_approved(cur, tenant_id)
                    if not approved_rows and tenant_id:
                        approved_rows = _fetch_approved(cur, None)
                    rejected_rows = _fetch_rejected(cur, tenant_id)
                    edited_rows = _fetch_edited(cur, tenant_id)

            groups: dict[str, dict[str, Any]] = {}
            seen_good_keys: set[str] = set()
            for snippet, req_type, req_text, norm_str, validator_comment in approved_rows:
                dedup_key = _normalize_feedback_text(req_text).lower()
                if not dedup_key or dedup_key in seen_good_keys:
                    continue
                seen_good_keys.add(dedup_key)
                key = str(snippet or "")[:120]
                if key not in groups:
                    groups[key] = {
                        "citation_snippet": snippet,
                        "requirements": [],
                        "validator_note": str(validator_comment or "").strip(),
                    }
                structure = _infer_feedback_requirement_structure(req_text, snippet)
                groups[key]["requirements"].append(
                    {
                        "req_type": req_type,
                        "requirement_text": req_text,
                        "normative_strength": norm_str,
                        "legal_subject": structure["legal_subject"],
                        "normative_verb": structure["normative_verb"],
                        "action_object": structure["action_object"],
                        "condition_text": structure["condition_text"],
                        "exception_text": structure["exception_text"],
                        "source_mode": structure["source_mode"],
                    }
                )
            good_examples = list(groups.values())[:limit]

            bad_examples: list[dict[str, Any]] = []
            seen_bad_keys: set[str] = set()
            for snippet, req_text, rejection_reason, validator_comment in rejected_rows:
                dedup_key = _normalize_feedback_text(req_text).lower()
                if not dedup_key or dedup_key in seen_bad_keys:
                    continue
                seen_bad_keys.add(dedup_key)
                bad_examples.append(
                    {
                        "citation_snippet": snippet,
                        "requirement_text": req_text,
                        "rejection_reason": rejection_reason,
                        "validator_comment": validator_comment,
                    }
                )

            edit_examples: list[dict[str, Any]] = []
            seen_edit_keys: set[str] = set()
            for snippet, original_text, corrected_text, req_type, normative_strength, validator_comment in edited_rows:
                dedup_key = _normalize_feedback_text(corrected_text).lower()
                if not dedup_key or dedup_key in seen_edit_keys:
                    continue
                seen_edit_keys.add(dedup_key)
                edit_examples.append(
                    {
                        "citation_snippet": snippet,
                        "original_text": original_text,
                        "corrected_text": corrected_text,
                        "req_type": req_type,
                        "normative_strength": normative_strength,
                        **_infer_feedback_requirement_structure(corrected_text, snippet),
                        "validator_comment": validator_comment,
                    }
                )

            suffix = build_dynamic_fewshot_suffix(good_examples)
            suffix += build_rejection_fewshot_suffix(bad_examples)
            suffix += build_edited_fewshot_suffix(edit_examples)

            self._fewshot_suffix = suffix
            print(
                f"  [Few-shot] +{len(good_examples)} approuve(s), "
                f"+{len(bad_examples)} rejete(s), "
                f"+{len(edit_examples)} corrige(s).",
                flush=True,
            )
        except Exception as e:
            self._fewshot_suffix = ""
            print(f"  [Few-shot] Chargement ignore (non-bloquant): {e}", flush=True)

    def _build_user_prompt(self, article_label: str, chunk_text: str, include_system_prompt: bool) -> str:
        fewshot = getattr(self, "_fewshot_suffix", "")
        return self.prompt_contract.build_user_prompt(
            article_label=article_label,
            chunk_text=chunk_text,
            include_system_prompt=include_system_prompt,
            fewshot_suffix=fewshot,
        )

    def _call_with_retry(
        self,
        provider: str,
        model: str,
        call,
        *,
        max_retries: int | None = None,
        fail_fast_rate_limit: bool = False,
    ):
        last_error = None
        retries = self.max_retries if max_retries is None else max(0, int(max_retries))

        for attempt in range(retries + 1):
            try:
                return call()
            except Exception as exc:
                last_error = exc
                if fail_fast_rate_limit and self._is_rate_limit_error(exc):
                    self._mark_failfast_rate_limit(provider)
                    raise
                retryable = self._is_retryable_error(exc)
                if (not retryable) or attempt >= retries:
                    if retryable:
                        self._mark_retry_exhausted(provider)
                    raise

                wait_seconds = self._retry_delay_seconds(exc, attempt)
                self._record_retry_attempt(
                    provider=provider,
                    model=model,
                    attempt_index=attempt + 1,
                    max_retries=retries,
                    wait_seconds=wait_seconds,
                    rate_limited=self._is_rate_limit_error(exc),
                )
                if self._should_log_retry_warning():
                    print(
                        f"WARN {provider}/{model} rate-limited, retry {attempt + 1}/{retries} in {wait_seconds:.1f}s",
                        flush=True,
                    )
                time.sleep(wait_seconds)

        raise RuntimeError(f"{provider}/{model} failed after retries: {last_error}")

    def _is_retryable_error(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        retry_markers = [
            "429",
            "rate limit",
            "rate_limit",
            "rate_limit_exceeded",
            "too many requests",
            "quota",
            "insufficient_quota",
            "resource_exhausted",
            "retry in",
            "tokens per min",
            "requests per min",
            "connection error",
            "connection reset",
            "temporarily unavailable",
        ]
        return any(marker in msg for marker in retry_markers)

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        markers = [
            "429",
            "rate limit",
            "rate_limit",
            "too many requests",
            "resource_exhausted",
            "quota",
            "insufficient_quota",
            "retry in",
            "tokens per min",
            "requests per min",
        ]
        return any(marker in msg for marker in markers)

    def _is_network_error(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        markers = [
            "connection error",
            "connection reset",
            "connection refused",
            "winerror 10061",
            "aucune connexion",
            "connexion n’a pu être établie",
            "connexion n'a pu être établie",
            "ordinateur cible l’a expressément refusée",
            "ordinateur cible l'a expressément refusée",
            "name or service not known",
            "temporary failure in name resolution",
            "network is unreachable",
            "host unreachable",
            "ssl",
            "tls",
        ]
        return any(marker in msg for marker in markers)

    def _classify_error_category(self, exc: Exception) -> str:
        msg = str(exc).lower()
        if self._is_rate_limit_error(exc):
            return "rate_limit"
        if self._is_network_error(exc):
            return "network"
        if "json/validation invalide" in msg or "validationerror" in msg or "json" in msg:
            return "json_validation"
        if (
            "invalid_request_error" in msg
            or "unsupported parameter" in msg
            or "badrequesterror" in msg
            or "context_length_exceeded" in msg
        ):
            return "invalid_request"
        if "timeout" in msg or "timed out" in msg:
            return "timeout"
        if "unauthorized" in msg or "invalid api key" in msg or "forbidden" in msg:
            return "auth"
        if "service unavailable" in msg or "internal server error" in msg:
            return "provider_unavailable"
        return "unknown"

    def _should_log_retry_warning(self) -> bool:
        self.retry_warn_total += 1
        n = self.retry_warn_total
        return n <= 3 or (n % self.retry_log_every_n == 0)

    def _normalize_provider_name(self, provider_name: str | None) -> str:
        normalized = (provider_name or "").strip().lower()
        if not normalized:
            return "gemini"

        aliases = {
            "google": "gemini",
            "google-genai": "gemini",
            "oai": "openai",
        }
        return aliases.get(normalized, normalized)

    def _parse_bool_env(self, name: str, default: bool) -> bool:
        raw = (os.getenv(name) or "").strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "y", "on"}

    def _active_proxy_env(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for key in PROXY_ENV_KEYS:
            value = (os.getenv(key) or "").strip()
            if value:
                out[key] = value
        return out

    def _is_broken_local_proxy_value(self, value: str) -> bool:
        raw = (value or "").strip().lower()
        if not raw:
            return False
        normalized = raw
        if "://" in normalized:
            normalized = normalized.split("://", 1)[1]
        if "@" in normalized:
            normalized = normalized.rsplit("@", 1)[1]
        normalized = normalized.split("/", 1)[0]
        return normalized in {"127.0.0.1:9", "localhost:9", "[::1]:9"}

    def _clear_proxy_env(self) -> None:
        for key in PROXY_ENV_KEYS:
            if key in os.environ:
                os.environ.pop(key, None)

    def _apply_proxy_policy(self) -> dict[str, Any]:
        before = self._active_proxy_env()
        if not before:
            return {
                "mode": "none",
                "bypass_applied": False,
                "reason": "no_proxy_env",
                "before": {},
                "after": {},
            }

        if self.disable_system_proxy:
            self._clear_proxy_env()
            return {
                "mode": "disabled",
                "bypass_applied": True,
                "reason": "disable_system_proxy_flag",
                "before": before,
                "after": self._active_proxy_env(),
            }

        broken_keys = [
            key for key, value in before.items() if self._is_broken_local_proxy_value(value)
        ]
        if self.auto_bypass_broken_local_proxy and broken_keys:
            self._clear_proxy_env()
            return {
                "mode": "auto_bypass_broken_local_proxy",
                "bypass_applied": True,
                "reason": "broken_local_proxy_detected",
                "broken_keys": broken_keys,
                "before": before,
                "after": self._active_proxy_env(),
            }

        return {
            "mode": "respect_env_proxy",
            "bypass_applied": False,
            "reason": "proxy_env_kept",
            "before": before,
            "after": before,
        }

    def _init_last_availability(self) -> None:
        self.last_availability = {
            "policy_version": self.availability_policy_version,
            "retry_policy_version": self.retry_policy_version,
            "primary_provider": self.primary_provider,
            "fallback_provider": self.fallback_provider,
            "proxy_policy_mode": str(self.proxy_policy.get("mode") or ""),
            "proxy_bypass_applied": bool(self.proxy_policy.get("bypass_applied")),
            "primary_skipped_cooldown": False,
            "fallback_skipped_cooldown": False,
            "both_providers_cooldown_blocked": False,
            "primary_attempted": False,
            "fallback_invoked": False,
            "fallback_attempted": False,
            "fallback_success": False,
            "primary_error_category": None,
            "fallback_error_category": None,
            "failfast_rate_limit_events": 0,
            "failfast_providers": [],
            "retry_attempts_total": 0,
            "retry_wait_seconds_total": 0.0,
            "retry_exhausted_total": 0,
            "retry_events": [],
            "cooldown_events": [],
            "cooldown_events_total": 0,
            "cooldown_seconds_total": 0.0,
            "provider_rate_limit_streak": dict(self.provider_rate_limit_streak),
        }

    def _mark_failfast_rate_limit(self, provider: str) -> None:
        if (not isinstance(self.last_availability, dict)) or ("policy_version" not in self.last_availability):
            self._init_last_availability()
        self.last_availability["failfast_rate_limit_events"] = int(
            self.last_availability.get("failfast_rate_limit_events") or 0
        ) + 1
        providers = self.last_availability.get("failfast_providers") or []
        if provider not in providers:
            providers.append(provider)
        self.last_availability["failfast_providers"] = providers
        self.availability_totals["failfast_rate_limit_total"] = int(
            self.availability_totals.get("failfast_rate_limit_total") or 0
        ) + 1

    def _record_retry_attempt(
        self,
        *,
        provider: str,
        model: str,
        attempt_index: int,
        max_retries: int,
        wait_seconds: float,
        rate_limited: bool,
    ) -> None:
        if (not isinstance(self.last_availability, dict)) or ("policy_version" not in self.last_availability):
            self._init_last_availability()

        event = {
            "provider": provider,
            "model": model,
            "attempt_index": int(attempt_index),
            "max_retries": int(max_retries),
            "wait_seconds": round(float(wait_seconds), 4),
            "rate_limited": bool(rate_limited),
        }
        retry_events = self.last_availability.get("retry_events") or []
        retry_events.append(event)
        self.last_availability["retry_events"] = retry_events
        self.last_availability["retry_attempts_total"] = int(
            self.last_availability.get("retry_attempts_total") or 0
        ) + 1
        self.last_availability["retry_wait_seconds_total"] = round(
            float(self.last_availability.get("retry_wait_seconds_total") or 0.0) + float(wait_seconds),
            4,
        )
        self.availability_totals["retry_attempts_total"] = int(
            self.availability_totals.get("retry_attempts_total") or 0
        ) + 1
        self.availability_totals["retry_wait_seconds_total"] = round(
            float(self.availability_totals.get("retry_wait_seconds_total") or 0.0) + float(wait_seconds),
            4,
        )

    def _mark_retry_exhausted(self, provider: str) -> None:
        if (not isinstance(self.last_availability, dict)) or ("policy_version" not in self.last_availability):
            self._init_last_availability()
        exhausted_providers = self.last_availability.get("retry_exhausted_providers") or []
        if provider not in exhausted_providers:
            exhausted_providers.append(provider)
        self.last_availability["retry_exhausted_providers"] = exhausted_providers
        self.last_availability["retry_exhausted_total"] = int(
            self.last_availability.get("retry_exhausted_total") or 0
        ) + 1
        self.availability_totals["retry_exhausted_total"] = int(
            self.availability_totals.get("retry_exhausted_total") or 0
        ) + 1

    def _record_cooldown_event(self, provider: str, cooldown_seconds: float, streak: int) -> None:
        if (not isinstance(self.last_availability, dict)) or ("policy_version" not in self.last_availability):
            self._init_last_availability()
        event = {
            "provider": provider,
            "seconds": round(float(cooldown_seconds), 4),
            "streak": int(streak),
        }
        events = self.last_availability.get("cooldown_events") or []
        events.append(event)
        self.last_availability["cooldown_events"] = events
        self.last_availability["cooldown_events_total"] = int(
            self.last_availability.get("cooldown_events_total") or 0
        ) + 1
        self.last_availability["cooldown_seconds_total"] = round(
            float(self.last_availability.get("cooldown_seconds_total") or 0.0) + float(cooldown_seconds),
            4,
        )
        self.availability_totals["cooldown_events_total"] = int(
            self.availability_totals.get("cooldown_events_total") or 0
        ) + 1
        self.availability_totals["cooldown_seconds_total"] = round(
            float(self.availability_totals.get("cooldown_seconds_total") or 0.0) + float(cooldown_seconds),
            4,
        )

    def _register_provider_success(self, provider: str) -> None:
        self.provider_rate_limit_streak[provider] = 0
        if isinstance(self.last_availability, dict):
            self.last_availability["provider_rate_limit_streak"] = dict(self.provider_rate_limit_streak)

    def _retry_delay_seconds(self, exc: Exception, attempt: int) -> float:
        msg = str(exc).lower()

        # Formats possibles :
        # - "retry in 59.3s"
        # - "retry in 1h38m9.888s"
        hms = re.search(r"retry in\s+(\d+)h(\d+)m(\d+(?:\.\d+)?)s", msg)
        if hms:
            hours = int(hms.group(1))
            minutes = int(hms.group(2))
            seconds = float(hms.group(3))
            return min(hours * 3600 + minutes * 60 + seconds, self.retry_max_seconds)

        sec = re.search(r"retry in\s+(\d+(?:\.\d+)?)s", msg)
        if sec:
            return min(float(sec.group(1)), self.retry_max_seconds)

        sec_alt = re.search(r"'retrydelay':\s*'(\d+)s'", msg)
        if sec_alt:
            return min(float(sec_alt.group(1)), self.retry_max_seconds)

        backoff = self.retry_base_seconds * (2 ** attempt)
        return min(backoff, self.retry_max_seconds)

    def _extract_retry_delay_seconds_uncapped(self, exc: Exception) -> float | None:
        msg = str(exc).lower()

        # Ex: "retry in 1h38m9.888s"
        hms = re.search(r"retry in\s+(\d+)h(\d+)m(\d+(?:\.\d+)?)s", msg)
        if hms:
            hours = int(hms.group(1))
            minutes = int(hms.group(2))
            seconds = float(hms.group(3))
            return hours * 3600 + minutes * 60 + seconds

        # Ex: "retry in 29.5s"
        sec = re.search(r"retry in\s+(\d+(?:\.\d+)?)s", msg)
        if sec:
            return float(sec.group(1))

        # Ex: "'retryDelay': '29s'"
        sec_alt = re.search(r"'retrydelay':\s*'(\d+)s'", msg)
        if sec_alt:
            return float(sec_alt.group(1))

        # Ex: "try again in 11m18.24s"
        minsec = re.search(r"try again in\s+(\d+)m(\d+(?:\.\d+)?)s", msg)
        if minsec:
            return int(minsec.group(1)) * 60 + float(minsec.group(2))

        # Ex: "try again in 54m0s"
        minsec_int = re.search(r"try again in\s+(\d+)m(\d+)s", msg)
        if minsec_int:
            return int(minsec_int.group(1)) * 60 + float(minsec_int.group(2))

        return None

    def _set_provider_cooldown_from_error(self, provider: str, exc: Exception) -> None:
        if not self.cooldown_enabled:
            return
        if not self._is_rate_limit_error(exc):
            return

        suggested = self._extract_retry_delay_seconds_uncapped(exc)
        current_streak = int(self.provider_rate_limit_streak.get(provider) or 0) + 1
        self.provider_rate_limit_streak[provider] = current_streak
        self.provider_rate_limit_streak_peak[provider] = max(
            int(self.provider_rate_limit_streak_peak.get(provider) or 0),
            current_streak,
        )
        if suggested is None:
            suggested = float(self.cooldown_min_seconds) * float(2 ** (current_streak - 1))

        cooldown_seconds = min(
            float(self.cooldown_max_seconds),
            max(float(self.cooldown_min_seconds), float(suggested)),
        )
        until = time.time() + cooldown_seconds
        previous = float(self.provider_cooldown_until.get(provider) or 0.0)
        self.provider_cooldown_until[provider] = max(previous, until)
        self._record_cooldown_event(provider, cooldown_seconds, current_streak)
        self.last_availability["provider_rate_limit_streak"] = dict(self.provider_rate_limit_streak)

    def _provider_cooldown_remaining_seconds(self, provider: str) -> float:
        until = float(self.provider_cooldown_until.get(provider) or 0.0)
        return max(0.0, until - time.time())

    def _is_provider_in_cooldown(self, provider: str) -> bool:
        return self._provider_cooldown_remaining_seconds(provider) > 0

    def _init_cache(self) -> None:
        if not self.cache_enabled:
            return
        try:
            if self._cache_conn is not None:
                try:
                    self._cache_conn.close()
                except Exception:
                    pass
                self._cache_conn = None
            cache_file = Path(self.cache_path).expanduser().resolve()
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(cache_file), timeout=30.0)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_cache (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    response_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_cache_expires_at ON llm_cache(expires_at)"
            )
            conn.commit()
            self._cache_conn = conn
        except Exception:
            # Le cache est optionnel: en cas d'erreur locale, on désactive proprement.
            self.cache_enabled = False
            self._cache_conn = None

    def _normalize_cache_text(self, text: str) -> str:
        normalized = unicodedata.normalize("NFKC", (text or ""))
        normalized = normalized.replace("’", "'").replace("`", "'")
        return re.sub(r"\s+", " ", normalized.strip())

    def _strip_accents(self, text: str) -> str:
        text = unicodedata.normalize("NFKD", text)
        return "".join(ch for ch in text if not unicodedata.combining(ch))

    def _normalize_cache_text_relaxed(self, text: str) -> str:
        normalized = self._normalize_cache_text(text).lower()
        normalized = self._strip_accents(normalized)
        if self.cache_key_relaxed_normalizer == "none":
            return normalized
        normalized = re.sub(r"[^\w\s]", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    def _canonical_article_label(self, article_label: str) -> str:
        label = self._normalize_cache_text(article_label).lower()
        if not label:
            return ""
        m = _ARTICLE_CODE_RE.search(label)
        if m:
            main = (m.group(1) or "").lower().replace(".", "-")
            if main == "1er":
                main = "premier"
            suffix = (m.group(2) or "").lower()
            code = f"{main}-{suffix}" if suffix else main
            return f"article:{code}"
        return f"label:{label}"

    def _cache_key(
        self,
        *,
        provider: str,
        model: str,
        article_label: str,
        chunk_text: str,
        strategy: str = "strict",
    ) -> str:
        article_norm = self._normalize_cache_text(article_label)
        chunk_norm = self._normalize_cache_text(chunk_text)

        if strategy == "relaxed":
            article_norm = self._canonical_article_label(article_label)
            chunk_norm = self._normalize_cache_text_relaxed(chunk_text)

        payload = {
            "v": self.cache_policy_version,
            "strategy": strategy,
            "provider": (provider or "").strip().lower(),
            "model": (model or "").strip(),
            "prompt_sha256": self._prompt_sha256,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "article_label": article_norm,
            "chunk_text": chunk_norm,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_keys_for_lookup(self, *, provider: str, model: str, article_label: str, chunk_text: str) -> list[tuple[str, str]]:
        keys: list[tuple[str, str]] = [
            (
                self._cache_key(
                    provider=provider,
                    model=model,
                    article_label=article_label,
                    chunk_text=chunk_text,
                    strategy="strict",
                ),
                "strict",
            )
        ]

        if self.cache_lookup_relaxed_enabled and len(self._normalize_cache_text(chunk_text)) >= self.cache_lookup_relaxed_min_chars:
            relaxed_key = self._cache_key(
                provider=provider,
                model=model,
                article_label=article_label,
                chunk_text=chunk_text,
                strategy="relaxed",
            )
            if relaxed_key != keys[0][0]:
                keys.append((relaxed_key, "relaxed"))
        return keys

    def _cache_get(self, *, provider: str, model: str, article_label: str, chunk_text: str) -> RequirementLLMResponse | None:
        if not self.cache_enabled or not self._cache_conn:
            return None
        try:
            self.cache_lookup_total += 1
            for key, strategy in self._cache_keys_for_lookup(
                provider=provider,
                model=model,
                article_label=article_label,
                chunk_text=chunk_text,
            ):
                row = self._cache_conn.execute(
                    "SELECT response_json, expires_at FROM llm_cache WHERE cache_key=?",
                    (key,),
                ).fetchone()
                if not row:
                    continue

                response_json, expires_at = row
                if expires_at is not None and float(expires_at) <= time.time():
                    self._cache_conn.execute("DELETE FROM llm_cache WHERE cache_key=?", (key,))
                    self._cache_conn.commit()
                    continue

                parsed = RequirementLLMResponse.model_validate_json(response_json)
                if (not parsed.requirements) and (not self.cache_read_negative_enabled):
                    continue
                self.last_cache_hit_source = strategy
                self.cache_hits_total += 1
                if strategy == "relaxed":
                    self.cache_hits_relaxed_total += 1
                else:
                    self.cache_hits_strict_total += 1
                if not parsed.requirements:
                    self.cache_negative_hits_total += 1
                return parsed

            self.cache_misses_total += 1
            return None
        except Exception:
            self.cache_misses_total += 1
            return None

    def _cache_put(
        self,
        *,
        provider: str,
        model: str,
        article_label: str,
        chunk_text: str,
        response: RequirementLLMResponse,
    ) -> None:
        if not self.cache_enabled or not self._cache_conn:
            return
        try:
            if (not response.requirements) and (not self.cache_negative_enabled):
                return
            now = time.time()
            expires_at = (now + self.cache_ttl_seconds) if self.cache_ttl_seconds > 0 else None
            response_json = json.dumps(
                response.model_dump(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            strict_key = self._cache_key(
                provider=provider,
                model=model,
                article_label=article_label,
                chunk_text=chunk_text,
                strategy="strict",
            )
            keys_to_upsert = [(strict_key, "strict")]

            if (
                self.cache_write_relaxed_alias
                and self.cache_lookup_relaxed_enabled
                and len(self._normalize_cache_text(chunk_text)) >= self.cache_lookup_relaxed_min_chars
            ):
                relaxed_key = self._cache_key(
                    provider=provider,
                    model=model,
                    article_label=article_label,
                    chunk_text=chunk_text,
                    strategy="relaxed",
                )
                if relaxed_key != strict_key:
                    keys_to_upsert.append((relaxed_key, "relaxed"))

            for key, _strategy in keys_to_upsert:
                self._cache_conn.execute(
                    """
                    INSERT INTO llm_cache(cache_key, provider, model, created_at, expires_at, response_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        provider=excluded.provider,
                        model=excluded.model,
                        created_at=excluded.created_at,
                        expires_at=excluded.expires_at,
                        response_json=excluded.response_json
                    """,
                    (key, provider, model, now, expires_at, response_json),
                )
            self._cache_conn.commit()
            self.cache_put_total += len(keys_to_upsert)
            if not response.requirements:
                self.cache_put_negative_total += len(keys_to_upsert)
            self._cache_put_count += 1
            if self._cache_put_count % 100 == 0:
                self._cache_prune()
        except Exception:
            return

    def close(self) -> None:
        if self._cache_conn is not None:
            try:
                self._cache_conn.close()
            except Exception:
                pass
            self._cache_conn = None

    def _cache_prune(self) -> None:
        if not self._cache_conn:
            return
        try:
            self._cache_conn.execute(
                "DELETE FROM llm_cache WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (time.time(),),
            )
            total = self._cache_conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
            overflow = int(total) - int(self.cache_max_entries)
            if overflow > 0:
                self._cache_conn.execute(
                    """
                    DELETE FROM llm_cache
                    WHERE cache_key IN (
                        SELECT cache_key
                        FROM llm_cache
                        ORDER BY created_at ASC
                        LIMIT ?
                    )
                    """,
                    (overflow,),
                )
            self._cache_conn.commit()
        except Exception:
            return

    def _load_ratecard(self, raw_json: str) -> dict[str, dict[str, float]]:
        if not raw_json.strip():
            return {}
        try:
            parsed = json.loads(raw_json)
        except Exception:
            return {}

        if not isinstance(parsed, dict):
            return {}

        cleaned: dict[str, dict[str, float]] = {}
        for model_name, model_rates in parsed.items():
            if not isinstance(model_name, str) or not isinstance(model_rates, dict):
                continue

            in_key = model_rates.get("input_per_1k")
            out_key = model_rates.get("output_per_1k")
            try:
                input_rate = float(in_key)
                output_rate = float(out_key)
            except Exception:
                continue

            cleaned[model_name.strip().lower()] = {
                "input_per_1k": input_rate,
                "output_per_1k": output_rate,
            }

        return cleaned

    def _estimate_cost_usd(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        rates = self.ratecard.get((model or "").strip().lower(), {})
        input_per_1k = float(rates.get("input_per_1k", self.default_input_cost_per_1k))
        output_per_1k = float(rates.get("output_per_1k", self.default_output_cost_per_1k))

        in_cost = (prompt_tokens / 1000.0) * input_per_1k
        out_cost = (completion_tokens / 1000.0) * output_per_1k
        return round(in_cost + out_cost, 8)

    def _record_usage(self, provider: str, model: str, usage: dict[str, int]) -> None:
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
        estimated_cost_usd = self._estimate_cost_usd(model, prompt_tokens, completion_tokens)

        usage_entry = {
            "provider": provider,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": estimated_cost_usd,
            "cache_hit": False,
            "cache_hit_source": None,
            "prompt_contract_version": self.prompt_contract_version,
            "output_schema_version": self.output_schema_version,
            "output_schema_sha256": self.output_schema_sha256,
            "retry_policy_version": self.retry_policy_version,
            "retry_attempts_on_call": int(self.last_availability.get("retry_attempts_total") or 0),
            "retry_wait_seconds_on_call": round(
                float(self.last_availability.get("retry_wait_seconds_total") or 0.0), 4
            ),
            "fallback_invoked_on_call": bool(self.last_availability.get("fallback_invoked")),
            "fallback_success_on_call": bool(self.last_availability.get("fallback_success")),
        }
        self.last_usage = usage_entry

        self.usage_totals["llm_calls"] += 1
        self.usage_totals["prompt_tokens"] += prompt_tokens
        self.usage_totals["completion_tokens"] += completion_tokens
        self.usage_totals["total_tokens"] += total_tokens
        self.usage_totals["estimated_cost_usd"] = round(
            float(self.usage_totals["estimated_cost_usd"]) + estimated_cost_usd,
            8,
        )

    def _extract_gemini_usage(self, response: Any) -> dict[str, int]:
        usage_meta = getattr(response, "usage_metadata", None)
        if not usage_meta:
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        prompt_tokens = int(getattr(usage_meta, "prompt_token_count", 0) or 0)
        completion_tokens = int(getattr(usage_meta, "candidates_token_count", 0) or 0)
        total_tokens = int(getattr(usage_meta, "total_token_count", prompt_tokens + completion_tokens) or 0)

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    def _extract_openai_usage(self, completion: Any) -> dict[str, int]:
        usage = getattr(completion, "usage", None)
        if not usage:
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", prompt_tokens + completion_tokens) or 0)

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    def _validate_response_json(self, raw_text: str) -> RequirementLLMResponse:
        cleaned = self._clean_json_text(raw_text)

        try:
            return RequirementLLMResponse.model_validate_json(cleaned)
        except ValidationError as first_error:
            try:
                obj = json.loads(cleaned)
            except Exception as e:
                raise RuntimeError(f"JSON/Validation invalide: {e}") from e

            if not isinstance(obj, dict):
                raise RuntimeError("JSON/Validation invalide: la racine JSON doit etre un objet.")

            try:
                return RequirementLLMResponse.model_validate(obj)
            except Exception as e:
                raise RuntimeError(f"JSON/Validation invalide: {e}") from first_error

    def _clean_json_text(self, raw_text: str) -> str:
        text = raw_text.strip()

        text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"^```\s*", "", text).strip()
        text = re.sub(r"\s*```$", "", text).strip()

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]

        return text
