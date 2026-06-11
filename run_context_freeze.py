import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from a1_prompt_contract import get_prompt_contract


RUN_CONTEXT_VERSION = "B0.2-1.7.0"
_PROMPT_CONTRACT = get_prompt_contract()

_DEFAULT_LLM_SETTINGS: dict[str, str] = {
    "PRIMARY_LLM_PROVIDER": "openai",
    "PRIMARY_MODEL": "gpt-4.1-mini-2025-04-14",
    "FALLBACK_LLM_PROVIDER": "gemini",
    "FALLBACK_MODEL": "gemini-2.5-flash",
    "LLM_TEMPERATURE": "0",
    "LLM_MAX_OUTPUT_TOKENS": "2048",
    "LLM_MAX_RETRIES": "2",
    "LLM_RETRY_POLICY_VERSION": "B2.2-1.1.0",
    "LLM_PRIMARY_MAX_RETRIES": "0",
    "LLM_FALLBACK_MAX_RETRIES": "1",
    "LLM_FAILFAST_PRIMARY_RATE_LIMIT": "1",
    "LLM_FAILFAST_FALLBACK_RATE_LIMIT": "0",
    "LLM_AVAILABILITY_POLICY_VERSION": "B2.3-1.1.0",
    "LLM_RETRY_BASE_SECONDS": "2",
    "LLM_RETRY_MAX_SECONDS": "45",
    "LLM_RETRY_LOG_EVERY_N": "25",
    "LLM_COOLDOWN_ENABLED": "1",
    "LLM_COOLDOWN_MIN_SECONDS": "15",
    "LLM_COOLDOWN_MAX_SECONDS": "1800",
    "LLM_CACHE_ENABLED": "1",
    "LLM_CACHE_PATH": ".cache/llm_cache.sqlite3",
    "LLM_CACHE_TTL_SECONDS": "0",
    "LLM_CACHE_MAX_ENTRIES": "50000",
    "LLM_CACHE_POLICY_VERSION": "B2.4-2.0.0",
    "LLM_CACHE_LOOKUP_RELAXED_ENABLED": "1",
    "LLM_CACHE_LOOKUP_RELAXED_MIN_CHARS": "120",
    "LLM_CACHE_WRITE_RELAXED_ALIAS": "1",
    "LLM_CACHE_NEGATIVE_ENABLED": "1",
    "LLM_CACHE_KEY_RELAXED_NORMALIZER": "legal_v1",
    "LLM_PROMPT_CONTRACT_VERSION": _PROMPT_CONTRACT.contract_version,
    "LLM_OUTPUT_SCHEMA_VERSION": _PROMPT_CONTRACT.output_schema_version,
    "LLM_SERVER_JSON_SCHEMA_ENABLED": "0",
}

_DEFAULT_TRACKED_FILES = [
    "a1_prompts.py",
    "a1_prompt_contract.py",
    "a1_schemas.py",
    "a1_llm_extractor.py",
    "a1_precall_nlp.py",
    "a1_postcall_quality.py",
    "a1_shared_helpers.py",
    "a1_extract_requirements_llm.py",
    "a1_segment_articles_chunks.py",
]

_CRITICAL_PACKAGES = [
    "pydantic",
    "psycopg",
    "python-dotenv",
    "google-genai",
    "openai",
]
_API_KEY_ENV_NAMES = [
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
]


def _normalize_provider_name(value: str | None, fallback: str) -> str:
    normalized = (value or fallback).strip().lower()
    aliases = {
        "google": "gemini",
        "google-genai": "gemini",
        "oai": "openai",
    }
    return aliases.get(normalized, normalized)


def _now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._-")
    return cleaned or "run"


def _normalize_model_setting(value: str | None, fallback: str) -> str:
    normalized = (value or "").strip()
    return normalized or fallback


def _parse_int_setting(name: str, value: str, errors: list[str]) -> int:
    try:
        return int(value)
    except Exception:
        errors.append(f"{name} invalide: '{value}'")
        return int(_DEFAULT_LLM_SETTINGS[name])


def _parse_float_setting(name: str, value: str, errors: list[str]) -> float:
    try:
        return float(value)
    except Exception:
        errors.append(f"{name} invalide: '{value}'")
        return float(_DEFAULT_LLM_SETTINGS[name])


def _parse_bool_setting(name: str, value: str, errors: list[str]) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    errors.append(f"{name} invalide: '{value}'")
    fallback = _DEFAULT_LLM_SETTINGS[name].strip().lower()
    return fallback in {"1", "true", "yes", "y", "on"}


def _file_descriptor(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "last_modified_utc": dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).isoformat(timespec="seconds"),
        "sha256": _sha256_file(path),
    }


def _safe_secret_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:12]


def _resolve_dependency_versions() -> tuple[dict[str, str | None], list[str]]:
    versions: dict[str, str | None] = {}
    warnings: list[str] = []
    for package in _CRITICAL_PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
            warnings.append(f"Package non installé: {package}")
        except Exception as exc:
            versions[package] = None
            warnings.append(f"Version package indisponible ({package}): {exc}")
    return versions, warnings


def _runtime_python_descriptor() -> dict[str, Any]:
    return {
        "python_version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
    }


def _api_key_fingerprints(env: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    src = env if env is not None else os.environ
    payload: dict[str, dict[str, Any]] = {}
    for key_name in _API_KEY_ENV_NAMES:
        raw = (src.get(key_name) or "").strip()
        payload[key_name] = {
            "present": bool(raw),
            "fingerprint": _safe_secret_fingerprint(raw),
            "length": len(raw) if raw else 0,
        }
    return payload


def _resolve_llm_settings(env: Mapping[str, str] | None = None) -> tuple[dict[str, Any], list[str]]:
    src = env if env is not None else os.environ
    errors: list[str] = []

    primary_provider = _normalize_provider_name(
        src.get("PRIMARY_LLM_PROVIDER"),
        _DEFAULT_LLM_SETTINGS["PRIMARY_LLM_PROVIDER"],
    )
    primary_model = _normalize_model_setting(
        src.get("PRIMARY_MODEL"),
        _DEFAULT_LLM_SETTINGS["PRIMARY_MODEL"],
    )
    fallback_provider = _normalize_provider_name(
        src.get("FALLBACK_LLM_PROVIDER"),
        _DEFAULT_LLM_SETTINGS["FALLBACK_LLM_PROVIDER"],
    )
    fallback_model = _normalize_model_setting(
        src.get("FALLBACK_MODEL"),
        _DEFAULT_LLM_SETTINGS["FALLBACK_MODEL"],
    )

    temperature = _parse_float_setting(
        "LLM_TEMPERATURE",
        _normalize_model_setting(src.get("LLM_TEMPERATURE"), _DEFAULT_LLM_SETTINGS["LLM_TEMPERATURE"]),
        errors,
    )
    max_output_tokens = _parse_int_setting(
        "LLM_MAX_OUTPUT_TOKENS",
        _normalize_model_setting(src.get("LLM_MAX_OUTPUT_TOKENS"), _DEFAULT_LLM_SETTINGS["LLM_MAX_OUTPUT_TOKENS"]),
        errors,
    )
    max_retries = _parse_int_setting(
        "LLM_MAX_RETRIES",
        _normalize_model_setting(src.get("LLM_MAX_RETRIES"), _DEFAULT_LLM_SETTINGS["LLM_MAX_RETRIES"]),
        errors,
    )
    retry_policy_version = _normalize_model_setting(
        src.get("LLM_RETRY_POLICY_VERSION"),
        _DEFAULT_LLM_SETTINGS["LLM_RETRY_POLICY_VERSION"],
    )
    retry_log_every_n = _parse_int_setting(
        "LLM_RETRY_LOG_EVERY_N",
        _normalize_model_setting(src.get("LLM_RETRY_LOG_EVERY_N"), _DEFAULT_LLM_SETTINGS["LLM_RETRY_LOG_EVERY_N"]),
        errors,
    )
    cooldown_enabled = _parse_bool_setting(
        "LLM_COOLDOWN_ENABLED",
        _normalize_model_setting(src.get("LLM_COOLDOWN_ENABLED"), _DEFAULT_LLM_SETTINGS["LLM_COOLDOWN_ENABLED"]),
        errors,
    )
    cooldown_min_seconds = _parse_int_setting(
        "LLM_COOLDOWN_MIN_SECONDS",
        _normalize_model_setting(src.get("LLM_COOLDOWN_MIN_SECONDS"), _DEFAULT_LLM_SETTINGS["LLM_COOLDOWN_MIN_SECONDS"]),
        errors,
    )
    cooldown_max_seconds = _parse_int_setting(
        "LLM_COOLDOWN_MAX_SECONDS",
        _normalize_model_setting(src.get("LLM_COOLDOWN_MAX_SECONDS"), _DEFAULT_LLM_SETTINGS["LLM_COOLDOWN_MAX_SECONDS"]),
        errors,
    )
    cache_enabled = _parse_bool_setting(
        "LLM_CACHE_ENABLED",
        _normalize_model_setting(src.get("LLM_CACHE_ENABLED"), _DEFAULT_LLM_SETTINGS["LLM_CACHE_ENABLED"]),
        errors,
    )
    cache_path = _normalize_model_setting(
        src.get("LLM_CACHE_PATH"),
        _DEFAULT_LLM_SETTINGS["LLM_CACHE_PATH"],
    )
    cache_ttl_seconds = _parse_int_setting(
        "LLM_CACHE_TTL_SECONDS",
        _normalize_model_setting(src.get("LLM_CACHE_TTL_SECONDS"), _DEFAULT_LLM_SETTINGS["LLM_CACHE_TTL_SECONDS"]),
        errors,
    )
    cache_max_entries = _parse_int_setting(
        "LLM_CACHE_MAX_ENTRIES",
        _normalize_model_setting(src.get("LLM_CACHE_MAX_ENTRIES"), _DEFAULT_LLM_SETTINGS["LLM_CACHE_MAX_ENTRIES"]),
        errors,
    )
    cache_policy_version = _normalize_model_setting(
        src.get("LLM_CACHE_POLICY_VERSION"),
        _DEFAULT_LLM_SETTINGS["LLM_CACHE_POLICY_VERSION"],
    )
    cache_lookup_relaxed_enabled = _parse_bool_setting(
        "LLM_CACHE_LOOKUP_RELAXED_ENABLED",
        _normalize_model_setting(
            src.get("LLM_CACHE_LOOKUP_RELAXED_ENABLED"),
            _DEFAULT_LLM_SETTINGS["LLM_CACHE_LOOKUP_RELAXED_ENABLED"],
        ),
        errors,
    )
    cache_lookup_relaxed_min_chars = _parse_int_setting(
        "LLM_CACHE_LOOKUP_RELAXED_MIN_CHARS",
        _normalize_model_setting(
            src.get("LLM_CACHE_LOOKUP_RELAXED_MIN_CHARS"),
            _DEFAULT_LLM_SETTINGS["LLM_CACHE_LOOKUP_RELAXED_MIN_CHARS"],
        ),
        errors,
    )
    cache_write_relaxed_alias = _parse_bool_setting(
        "LLM_CACHE_WRITE_RELAXED_ALIAS",
        _normalize_model_setting(
            src.get("LLM_CACHE_WRITE_RELAXED_ALIAS"),
            _DEFAULT_LLM_SETTINGS["LLM_CACHE_WRITE_RELAXED_ALIAS"],
        ),
        errors,
    )
    cache_negative_enabled = _parse_bool_setting(
        "LLM_CACHE_NEGATIVE_ENABLED",
        _normalize_model_setting(
            src.get("LLM_CACHE_NEGATIVE_ENABLED"),
            _DEFAULT_LLM_SETTINGS["LLM_CACHE_NEGATIVE_ENABLED"],
        ),
        errors,
    )
    cache_key_relaxed_normalizer = _normalize_model_setting(
        src.get("LLM_CACHE_KEY_RELAXED_NORMALIZER"),
        _DEFAULT_LLM_SETTINGS["LLM_CACHE_KEY_RELAXED_NORMALIZER"],
    )
    primary_max_retries = _parse_int_setting(
        "LLM_PRIMARY_MAX_RETRIES",
        _normalize_model_setting(src.get("LLM_PRIMARY_MAX_RETRIES"), _DEFAULT_LLM_SETTINGS["LLM_PRIMARY_MAX_RETRIES"]),
        errors,
    )
    fallback_max_retries = _parse_int_setting(
        "LLM_FALLBACK_MAX_RETRIES",
        _normalize_model_setting(src.get("LLM_FALLBACK_MAX_RETRIES"), _DEFAULT_LLM_SETTINGS["LLM_FALLBACK_MAX_RETRIES"]),
        errors,
    )
    failfast_primary_rate_limit = _parse_bool_setting(
        "LLM_FAILFAST_PRIMARY_RATE_LIMIT",
        _normalize_model_setting(
            src.get("LLM_FAILFAST_PRIMARY_RATE_LIMIT"),
            _DEFAULT_LLM_SETTINGS["LLM_FAILFAST_PRIMARY_RATE_LIMIT"],
        ),
        errors,
    )
    failfast_fallback_rate_limit = _parse_bool_setting(
        "LLM_FAILFAST_FALLBACK_RATE_LIMIT",
        _normalize_model_setting(
            src.get("LLM_FAILFAST_FALLBACK_RATE_LIMIT"),
            _DEFAULT_LLM_SETTINGS["LLM_FAILFAST_FALLBACK_RATE_LIMIT"],
        ),
        errors,
    )
    retry_base_seconds = _parse_float_setting(
        "LLM_RETRY_BASE_SECONDS",
        _normalize_model_setting(src.get("LLM_RETRY_BASE_SECONDS"), _DEFAULT_LLM_SETTINGS["LLM_RETRY_BASE_SECONDS"]),
        errors,
    )
    retry_max_seconds = _parse_float_setting(
        "LLM_RETRY_MAX_SECONDS",
        _normalize_model_setting(src.get("LLM_RETRY_MAX_SECONDS"), _DEFAULT_LLM_SETTINGS["LLM_RETRY_MAX_SECONDS"]),
        errors,
    )
    availability_policy_version = _normalize_model_setting(
        src.get("LLM_AVAILABILITY_POLICY_VERSION"),
        _DEFAULT_LLM_SETTINGS["LLM_AVAILABILITY_POLICY_VERSION"],
    )
    prompt_contract_version = _normalize_model_setting(
        src.get("LLM_PROMPT_CONTRACT_VERSION"),
        _DEFAULT_LLM_SETTINGS["LLM_PROMPT_CONTRACT_VERSION"],
    )
    output_schema_version = _normalize_model_setting(
        src.get("LLM_OUTPUT_SCHEMA_VERSION"),
        _DEFAULT_LLM_SETTINGS["LLM_OUTPUT_SCHEMA_VERSION"],
    )
    server_json_schema_enabled = _parse_bool_setting(
        "LLM_SERVER_JSON_SCHEMA_ENABLED",
        _normalize_model_setting(
            src.get("LLM_SERVER_JSON_SCHEMA_ENABLED"),
            _DEFAULT_LLM_SETTINGS["LLM_SERVER_JSON_SCHEMA_ENABLED"],
        ),
        errors,
    )

    return {
        "primary_provider": primary_provider,
        "primary_model": primary_model,
        "fallback_provider": fallback_provider,
        "fallback_model": fallback_model,
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
        "max_retries": max_retries,
        "retry_policy_version": retry_policy_version,
        "primary_max_retries": primary_max_retries,
        "fallback_max_retries": fallback_max_retries,
        "failfast_primary_rate_limit": failfast_primary_rate_limit,
        "failfast_fallback_rate_limit": failfast_fallback_rate_limit,
        "availability_policy_version": availability_policy_version,
        "prompt_contract_version": prompt_contract_version,
        "output_schema_version": output_schema_version,
        "server_json_schema_enabled": server_json_schema_enabled,
        "retry_base_seconds": retry_base_seconds,
        "retry_max_seconds": retry_max_seconds,
        "retry_log_every_n": max(1, retry_log_every_n),
        "cooldown_enabled": cooldown_enabled,
        "cooldown_min_seconds": max(1, cooldown_min_seconds),
        "cooldown_max_seconds": max(max(1, cooldown_min_seconds), cooldown_max_seconds),
        "cache_enabled": cache_enabled,
        "cache_path": cache_path,
        "cache_ttl_seconds": max(0, cache_ttl_seconds),
        "cache_max_entries": max(1000, cache_max_entries),
        "cache_policy_version": cache_policy_version,
        "cache_lookup_relaxed_enabled": cache_lookup_relaxed_enabled,
        "cache_lookup_relaxed_min_chars": max(0, cache_lookup_relaxed_min_chars),
        "cache_write_relaxed_alias": cache_write_relaxed_alias,
        "cache_negative_enabled": cache_negative_enabled,
        "cache_key_relaxed_normalizer": cache_key_relaxed_normalizer,
    }, errors


def build_frozen_run_context(
    *,
    tracked_files: list[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    files_to_track = tracked_files[:] if tracked_files else _DEFAULT_TRACKED_FILES[:]
    missing_files: list[str] = []
    descriptors: list[dict[str, Any]] = []

    for rel_path in files_to_track:
        resolved = Path(rel_path).expanduser().resolve()
        if not resolved.exists():
            missing_files.append(str(resolved))
            continue
        descriptors.append(_file_descriptor(resolved))

    if missing_files:
        raise ValueError(f"Fichiers de contexte introuvables: {missing_files}")

    llm_settings, parse_errors = _resolve_llm_settings(env=env)
    dependency_versions, dependency_warnings = _resolve_dependency_versions()
    api_keys = _api_key_fingerprints(env=env)
    runtime_python = _runtime_python_descriptor()

    prompt_contract = get_prompt_contract()
    prompt_text = prompt_contract.prompt_text or ""
    prompt_block = {
        "symbol": prompt_contract.prompt_symbol,
        "contract_version": prompt_contract.contract_version,
        "sha256": prompt_contract.prompt_sha256,
        "length_chars": len(prompt_text),
        "line_count": prompt_text.count("\n") + (1 if prompt_text else 0),
        "output_schema_symbol": prompt_contract.schema_symbol,
        "output_schema_version": prompt_contract.output_schema_version,
        "output_schema_name": prompt_contract.output_schema_name,
        "output_schema_sha256": prompt_contract.output_schema_sha256,
        "strict_json_mode": bool(prompt_contract.strict_json_mode),
    }

    fingerprint_payload = {
        "version": RUN_CONTEXT_VERSION,
        "prompt_sha256": prompt_block["sha256"],
        "prompt_contract_version": prompt_block["contract_version"],
        "output_schema_sha256": prompt_block["output_schema_sha256"],
        "llm_settings": llm_settings,
        "dependency_versions": dependency_versions,
        "api_keys": {
            key: {
                "present": val.get("present"),
                "fingerprint": val.get("fingerprint"),
                "length": val.get("length"),
            }
            for key, val in sorted(api_keys.items(), key=lambda kv: kv[0])
        },
        "runtime_python": runtime_python,
        "tracked_files": [
            {"path": f["path"], "sha256": f["sha256"]}
            for f in sorted(descriptors, key=lambda x: x["path"])
        ],
    }

    context = {
        "context_version": RUN_CONTEXT_VERSION,
        "captured_at_utc": _now_utc_iso(),
        "prompt": prompt_block,
        "llm_settings": llm_settings,
        "runtime_python": runtime_python,
        "dependency_versions": dependency_versions,
        "api_keys": api_keys,
        "tracked_files": descriptors,
        "parse_warnings": parse_errors + dependency_warnings,
        "context_fingerprint": _canonical_fingerprint(fingerprint_payload),
    }
    return context


def persist_frozen_run_context(
    *,
    run_context: dict[str, Any],
    outdir: str,
    timestamp: str,
    run_id: str,
) -> tuple[Path, Path]:
    root = Path(outdir).expanduser().resolve()
    context_dir = root / "context"
    context_history_dir = root / "history" / "context"

    context_dir.mkdir(parents=True, exist_ok=True)
    context_history_dir.mkdir(parents=True, exist_ok=True)

    safe_run_id = _safe_filename_component(run_id)
    latest_path = context_dir / "run_context_latest.json"
    history_path = context_history_dir / f"run_context_{timestamp}_{safe_run_id}.json"

    content = json.dumps(run_context, ensure_ascii=False, indent=2)
    latest_path.write_text(content, encoding="utf-8")
    history_path.write_text(content, encoding="utf-8")

    return latest_path, history_path
