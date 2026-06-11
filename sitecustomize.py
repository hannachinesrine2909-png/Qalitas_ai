"""
Global proxy sanitizer for this repository.

Python automatically imports `sitecustomize` at interpreter startup (if present
on sys.path). We use it to neutralize a known local broken proxy setup
(`127.0.0.1:9`) that causes intermittent API connection failures.
"""

from __future__ import annotations

import os


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


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _is_broken_local_proxy_value(value: str) -> bool:
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


def _active_proxy_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for key in PROXY_ENV_KEYS:
        value = (os.getenv(key) or "").strip()
        if value:
            out[key] = value
    return out


def _clear_proxy_env() -> None:
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)


def _apply_proxy_bootstrap_guard() -> None:
    # Opt-out for environments that really need a custom proxy.
    if _parse_bool_env("QALITAS_KEEP_SYSTEM_PROXY", False):
        return

    active = _active_proxy_env()
    if not active:
        return

    broken_keys = [k for k, v in active.items() if _is_broken_local_proxy_value(v)]
    if not broken_keys:
        return

    _clear_proxy_env()
    os.environ["QALITAS_PROXY_SANITIZED"] = "1"
    os.environ["QALITAS_PROXY_SANITIZE_REASON"] = "broken_local_proxy_detected"
    os.environ["QALITAS_PROXY_SANITIZED_KEYS"] = ",".join(sorted(broken_keys))


_apply_proxy_bootstrap_guard()

