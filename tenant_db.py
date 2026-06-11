from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any, Callable

import psycopg

TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_REQUEST_TENANT: ContextVar[str] = ContextVar("qalitas_request_tenant", default="")
_RAW_PSYCOPG_CONNECT = psycopg.connect


def normalize_tenant_id(value: Any) -> str:
    return str(value or "").strip().lower()


def set_request_tenant(tenant_id: str | None) -> None:
    _REQUEST_TENANT.set(normalize_tenant_id(tenant_id))


def clear_request_tenant() -> None:
    _REQUEST_TENANT.set("")


def get_request_tenant() -> str:
    return _REQUEST_TENANT.get("")


def resolve_tenant_id(tenant_id: str | None = None) -> str:
    explicit = normalize_tenant_id(tenant_id)
    return explicit or get_request_tenant()


def validate_tenant_id(tenant_id: str | None) -> str:
    value = resolve_tenant_id(tenant_id)
    if not value:
        return ""
    if not TENANT_ID_RE.match(value):
        raise ValueError(f"tenant_id invalide: {tenant_id}")
    return value


def apply_tenant_context(conn: Any, tenant_id: str | None = None) -> Any:
    resolved = validate_tenant_id(tenant_id)
    if not resolved:
        return conn

    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (resolved,))
    return conn


def connect_db(
    dsn: str,
    *args: Any,
    tenant_id: str | None = None,
    connect_func: Callable[..., Any] | None = None,
    **kwargs: Any,
) -> Any:
    connector = connect_func or _RAW_PSYCOPG_CONNECT
    conn = connector(dsn, *args, **kwargs)
    return apply_tenant_context(conn, tenant_id=tenant_id)


def raw_connect(*args: Any, **kwargs: Any) -> Any:
    return _RAW_PSYCOPG_CONNECT(*args, **kwargs)
