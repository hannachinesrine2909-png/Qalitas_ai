import argparse
import hashlib
import hmac
import inspect
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal

import psycopg
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from a1_error_memory import (
    build_human_validation_feedback_signal,
    error_memory_table_exists,
    persist_error_memory_signal,
)
from bulk_company_import import SUPPORTED_DATASET_TYPES, import_company_dataset, normalize_dataset_type
from a1_document_qualification_gate import classify_document_policy
from tenant_db import clear_request_tenant, connect_db, set_request_tenant


DOC_ID_RE = re.compile(r"doc_id[:=]\s*([0-9a-fA-F-]{36})")
STEP_FAILURE_RE = re.compile(r"Echec\s+([^\n\r]+)", re.IGNORECASE)
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@+-]{3,160}$")
ACTIVE_RUN_STALE_HOURS = 12
JOBS_LOCK = threading.Lock()
JOBS: dict[str, dict[str, Any]] = {}
JOBS_PATH = Path("reports/api/jobs_latest.json")
RUN_LOGS_DIR = Path("reports/api/run_logs")
UPLOADS_DIR = Path("storage/uploads_api")
PROOFS_DIR = Path("storage/proofs")
DEFAULT_A2_DELAY_SECONDS = 0.15
_RAW_PSYCOPG_CONNECT = psycopg.connect


def _connect_db(dsn: str, *args: Any, tenant_id: str | None = None, **kwargs: Any) -> psycopg.Connection:
    return connect_db(
        dsn,
        *args,
        tenant_id=tenant_id,
        connect_func=_RAW_PSYCOPG_CONNECT,
        **kwargs,
    )


psycopg.connect = _connect_db


def _configure_stdio_utf8() -> None:
    """Best effort: prevent Windows cp1252 crashes when logging special chars."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_stdio_utf8()


class NoCacheStaticFiles(StaticFiles):
    """Force un rafraîchissement UI pour éviter les JS/CSS obsolètes en dev."""

    async def get_response(self, path: str, scope: dict[str, Any]) -> Any:
        response = await super().get_response(path, scope)
        if getattr(response, "status_code", 0) == 200:
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


class RunResponse(BaseModel):
    job_id: str
    status: str
    message: str
    doc_id: str | None = None
    error_category: str | None = None
    failed_step: str | None = None
    current_stage: str | None = None
    stage_message: str | None = None
    pipeline_plan: dict[str, bool] | None = None
    pipeline_steps: dict[str, str] | None = None


class LoginRequest(BaseModel):
    username: str
    password: str
    tenant_id: str | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    display_name: str
    company_name: str = ""
    tenant_id: str
    home_tenant_id: str
    active_tenant_id: str
    is_super_admin: bool


class SwitchTenantRequest(BaseModel):
    tenant_id: str


class SwitchTenantResponse(BaseModel):
    username: str
    role: str
    display_name: str
    company_name: str = ""
    tenant_id: str
    home_tenant_id: str
    active_tenant_id: str
    is_super_admin: bool


class ValidationRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT", "EDIT", "FLAG"]
    comment: str = ""
    rejection_reason: Literal[
        "TEXTE_INCORRECT",
        "HORS_PERIMETRE",
        "DOUBLON",
        "FORCE_NORMATIVE_FAUSSE",
        "INCOMPLET",
        "AUTRE",
    ] | None = None
    corrected_text: str | None = None  # obligatoire si decision == "EDIT"


AUTH_LOCK = threading.Lock()
AUTH_TOKENS: dict[str, dict[str, Any]] = {}
TOKEN_TTL_HOURS = 8  # Token valide 8h

# RBAC : operations par role
# SUPER_ADMIN = admin plateforme, ADMIN_QHSE/ANALYSTE/AUDITEUR = roles tenant
# AUDITEUR = lecture seule (pas dans WRITE_ROLES)
PLATFORM_ROLES = {"SUPER_ADMIN"}
WRITE_ROLES  = {"SUPER_ADMIN", "ADMIN_QHSE", "ANALYSTE_CONFORMITE"}   # peuvent lancer agents + valider
ADMIN_ROLES  = {"SUPER_ADMIN", "ADMIN_QHSE"}                           # config, index, pipeline full
AUTH_ROLES = WRITE_ROLES | {"AUDITEUR"}
TENANT_USER_ROLES = {"ADMIN_QHSE", "ANALYSTE_CONFORMITE", "AUDITEUR"}
PASSWORD_HASH_SCHEME = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 390000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _job_tenant_norm(item: dict[str, Any]) -> str:
    return str(item.get("tenant") or item.get("tenant_id") or "").strip().lower()


def _normalize_tenant_id(value: Any) -> str:
    return str(value or "").strip().lower()


def _parse_tenant_id(value: Any, *, field_name: str = "tenant_id", allow_empty: bool = False) -> str:
    if value is not None and not isinstance(value, str) and hasattr(value, "default"):
        value = getattr(value, "default")
    tenant_id = _normalize_tenant_id(value)
    if not tenant_id:
        if allow_empty:
            return ""
        raise HTTPException(status_code=400, detail=f"{field_name} est obligatoire")
    if not TENANT_ID_RE.match(tenant_id):
        raise HTTPException(status_code=400, detail=f"{field_name} invalide (format attendu: [a-z0-9_-], 2-64 chars)")
    return tenant_id


def _require_tenant_access(
    session: dict[str, Any],
    requested_tenant: Any,
    *,
    field_name: str = "tenant_id",
) -> str:
    session_tenant = _session_active_tenant(session)
    tenant_id = _parse_tenant_id(requested_tenant, field_name=field_name, allow_empty=True)
    resolved_tenant = tenant_id or session_tenant
    if not resolved_tenant:
        raise HTTPException(status_code=400, detail=f"{field_name} est obligatoire")
    if session_tenant and resolved_tenant != session_tenant:
        raise HTTPException(
            status_code=403,
            detail=f"Acces refuse: session liee au tenant '{session_tenant}', pas '{resolved_tenant}'",
        )
    set_request_tenant(resolved_tenant)
    return resolved_tenant


def _session_home_tenant(session: dict[str, Any]) -> str:
    return _parse_tenant_id(
        session.get("home_tenant_id") or session.get("tenant_id"),
        field_name="session.home_tenant_id",
        allow_empty=True,
    )


def _session_active_tenant(session: dict[str, Any]) -> str:
    return _parse_tenant_id(
        session.get("active_tenant_id") or session.get("tenant_id"),
        field_name="session.active_tenant_id",
        allow_empty=True,
    )


def _is_super_admin(session: dict[str, Any]) -> bool:
    return str(session.get("role") or "").strip().upper() == "SUPER_ADMIN"


def _sanitize_filename(filename: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in (filename or ""))
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    cleaned = cleaned.strip("._")
    return cleaned or "document.pdf"


def _clean_pdf_text(value: Any, *, max_len: int | None = None) -> str:
    """Normalize strings for PDF rendering and fix common mojibake patterns."""
    text = str(value or "")
    replacements = {
        "Ã©": "e",
        "Ã¨": "e",
        "Ãª": "e",
        "Ã«": "e",
        "Ã ": "a",
        "Ã¢": "a",
        "Ã´": "o",
        "Ã¶": "o",
        "Ã®": "i",
        "Ã¯": "i",
        "Ã¹": "u",
        "Ã»": "u",
        "Ã§": "c",
        "â€“": "-",
        "â€”": "-",
        "â€˜": "'",
        "â€™": "'",
        "â€œ": "\"",
        "â€": "\"",
        "Â": "",
        "Ã": "",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    text = re.sub(r"\s+", " ", text).strip()
    if max_len and max_len > 0 and len(text) > max_len:
        return text[: max_len - 1] + "..."
    return text


def _load_env_dsn() -> str:
    load_dotenv()
    dsn = os.getenv("PG_DSN", "").strip()
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")
    return dsn


def _hash_password(password: str) -> str:
    secret = str(password or "")
    if not secret:
        raise ValueError("Mot de passe vide")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8", errors="ignore"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return f"{PASSWORD_HASH_SCHEME}${PASSWORD_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored_hash: str) -> bool:
    raw_password = str(password or "")
    raw_hash = str(stored_hash or "").strip()
    if not raw_password or not raw_hash:
        return False
    try:
        scheme, iterations_raw, salt_hex, digest_hex = raw_hash.split("$", 3)
        if scheme != PASSWORD_HASH_SCHEME:
            return False
        iterations = int(iterations_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        raw_password.encode("utf-8", errors="ignore"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def _auth_bootstrap_tenant() -> str:
    load_dotenv()
    candidate = (
        os.getenv("AUTH_BOOTSTRAP_TENANT")
        or os.getenv("AUTH_DEFAULT_TENANT")
        or os.getenv("QALITAS_TENANT")
        or "tenant_demo"
    )
    return _parse_tenant_id(candidate, field_name="AUTH_BOOTSTRAP_TENANT")


def _auth_bootstrap_specs() -> tuple[str, list[dict[str, str]]]:
    load_dotenv()
    enabled = str(os.getenv("AUTH_BOOTSTRAP_ENABLED", "1")).strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return "", []
    tenant_id = _auth_bootstrap_tenant()
    specs: list[dict[str, str]] = []
    definitions = [
        ("SUPER_ADMIN", "SUPER_ADMIN", "Super Admin Plateforme"),
        ("ADMIN", "ADMIN_QHSE", "Admin QALITAS"),
        ("ANALYSTE", "ANALYSTE_CONFORMITE", "Analyste Conformite"),
        ("AUDITEUR", "AUDITEUR", "Auditeur Interne"),
    ]
    for prefix, role, default_display in definitions:
        username = str(os.getenv(f"AUTH_BOOTSTRAP_{prefix}_USERNAME", "")).strip().lower()
        password = str(os.getenv(f"AUTH_BOOTSTRAP_{prefix}_PASSWORD", "")).strip()
        display_name = str(os.getenv(f"AUTH_BOOTSTRAP_{prefix}_DISPLAY_NAME", default_display)).strip() or default_display
        if not username or not password:
            continue
        specs.append(
            {
                "username": username,
                "password": password,
                "role": role,
                "display_name": display_name,
            }
        )
    return tenant_id, specs


def _bootstrap_local_auth_users() -> None:
    tenant_id, specs = _auth_bootstrap_specs()
    if not tenant_id or not specs:
        return
    dsn = _load_env_dsn()
    try:
        with psycopg.connect(dsn, tenant_id=tenant_id) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name = 'app_users'
                    )
                    """
                )
                row = cur.fetchone()
                if not row or not bool(row[0]):
                    return
                reset_passwords = str(os.getenv("AUTH_BOOTSTRAP_RESET_PASSWORDS", "0")).strip().lower() in {
                    "1", "true", "yes", "on"
                }
                for spec in specs:
                    cur.execute(
                        """
                        SELECT user_id::text
                        FROM app_users
                        WHERE tenant_id = %s
                          AND LOWER(username) = LOWER(%s)
                        LIMIT 1
                        """,
                        (tenant_id, spec["username"]),
                    )
                    existing = cur.fetchone()
                    if existing:
                        if reset_passwords:
                            cur.execute(
                                """
                                UPDATE app_users
                                   SET password_hash = %s,
                                       role = %s,
                                       display_name = %s,
                                       is_active = TRUE,
                                       updated_at = now()
                                 WHERE tenant_id = %s
                                   AND LOWER(username) = LOWER(%s)
                                """,
                                (
                                    _hash_password(spec["password"]),
                                    spec["role"],
                                    spec["display_name"],
                                    tenant_id,
                                    spec["username"],
                                ),
                            )
                        continue
                    cur.execute(
                        """
                        INSERT INTO app_users
                            (tenant_id, username, password_hash, role, display_name, is_active)
                        VALUES (%s, %s, %s, %s, %s, TRUE)
                        """,
                        (
                            tenant_id,
                            spec["username"],
                            _hash_password(spec["password"]),
                            spec["role"],
                            spec["display_name"],
                        ),
                    )
            conn.commit()
    except Exception:
        pass


def _rows_to_dicts(cur: Any) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _call_with_supported_kwargs(func: Any, **kwargs: Any) -> Any:
    try:
        sig = inspect.signature(func)
    except Exception:
        return func(**kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return func(**kwargs)
    supported = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return func(**supported)


def _normalize_text_list(values: list[str], *, limit: int = 200) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values or []:
        item = str(raw or "").strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= int(limit):
            break
    return out


PIPELINE_STAGE_ORDER = ("A1", "A2", "A3", "EMB")
PIPELINE_STAGE_LABELS = {
    "A1": "Extraction",
    "A2": "Applicabilité",
    "A3": "Conformité",
    "EMB": "Indexation",
}
ACTIVE_JOB_STATUSES = {"QUEUED", "PENDING", "RUNNING"}
TERMINAL_JOB_STATUSES = {"DONE", "FAILED", "ERROR", "PAUSED", "CANCELLED"}
TERMINAL_STEP_STATUSES = {"DONE", "SKIPPED", "ERROR", "FAILED", "PAUSED"}
FINALIZED_STEP_STATUSES = {"DONE", "SKIPPED"}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on", "oui"}


def _build_pipeline_plan(
    *,
    run_applicability: Any = False,
    run_compliance: Any = False,
    run_embedding: Any = False,
) -> dict[str, bool]:
    plan = {
        "A1": True,
        "A2": _coerce_bool(run_applicability),
        "A3": _coerce_bool(run_compliance),
        "EMB": _coerce_bool(run_embedding),
    }
    if plan["A3"] and not plan["A2"]:
        plan["A2"] = True
    return plan


def _build_pipeline_steps(plan: dict[str, bool], *, active_stage: str | None = None) -> dict[str, str]:
    active_norm = str(active_stage or "").strip().upper() or None
    steps: dict[str, str] = {}
    for stage in PIPELINE_STAGE_ORDER:
        if not bool(plan.get(stage)):
            steps[stage] = "SKIPPED"
        elif active_norm == stage:
            steps[stage] = "RUNNING"
        else:
            steps[stage] = "PENDING"
    return steps


def _build_followup_pipeline_context(stage: str) -> tuple[dict[str, bool], dict[str, str]]:
    stage_norm = str(stage or "").strip().upper()
    if stage_norm == "A3":
        plan = _build_pipeline_plan(run_applicability=True, run_compliance=True, run_embedding=False)
        steps = _build_pipeline_steps(plan)
        steps["A1"] = "DONE"
        steps["A2"] = "DONE"
        return plan, steps
    plan = _build_pipeline_plan(run_applicability=True, run_compliance=False, run_embedding=False)
    steps = _build_pipeline_steps(plan)
    steps["A1"] = "DONE"
    return plan, steps


def _a2_followup_outcome(result: dict[str, Any]) -> tuple[str, str, str | None]:
    payload = dict(result or {})
    stats = dict(payload.get("engine_stats") or {})
    counts = dict(payload.get("counts") or {})
    loaded = int(stats.get("requirements_loaded") or 0)
    total = int(payload.get("total") or 0)
    error_count = int(counts.get("ERROR") or 0)
    if loaded <= 0:
        return "SKIPPED", "Applicabilité ignorée (aucune exigence A1 exploitable)", None
    if total <= 0 and error_count > 0:
        return "ERROR", f"Applicabilité échouée ({error_count} erreur(s) moteur)", "applicability_engine_error"
    if total <= 0:
        return "DONE", "Applicabilité terminée (0 décision)", None
    if error_count > 0:
        return "DONE", f"Applicabilité terminée ({total} décision(s), {error_count} erreur(s))", None
    return "DONE", f"Applicabilité terminée ({total} décision(s))", None


def _a3_followup_outcome(result: dict[str, Any]) -> tuple[str, str, str | None]:
    payload = dict(result or {})
    counts = dict(payload.get("counts") or {})
    total = int(payload.get("total") or 0)
    error_count = int(counts.get("ERROR") or 0)
    error_samples = [str(x).strip() for x in list(payload.get("error_samples") or []) if str(x).strip()]
    error_hint = f" : {error_samples[0]}" if error_samples else ""
    if total <= 0 and error_count > 0:
        return "ERROR", f"Conformité échouée ({error_count} évaluation(s) en erreur){error_hint}", "compliance_engine_error"
    if total <= 0:
        return "SKIPPED", "Conformité ignorée (aucune exigence applicable A2)", None
    if error_count > 0:
        return "DONE", f"Conformité terminée ({total} évaluation(s), {error_count} erreur(s))", None
    return "DONE", f"Conformité terminée ({total} évaluation(s))", None


def _mark_pipeline_stage(
    job_id: str,
    stage: str,
    status: str,
    *,
    stage_message: str | None = None,
    extra_updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stage_norm = str(stage or "").strip().upper()
    status_norm = str(status or "").strip().upper() or "PENDING"
    with JOBS_LOCK:
        if job_id not in JOBS:
            raise KeyError(f"job_id inconnu: {job_id}")
        item = JOBS[job_id]
        plan = dict(item.get("pipeline_plan") or {})
        steps = dict(item.get("pipeline_steps") or _build_pipeline_steps(plan))
        if stage_norm:
            steps[stage_norm] = status_norm
        item["pipeline_steps"] = steps
        item["current_stage"] = stage_norm if status_norm in {"RUNNING", "PAUSED", "ERROR", "FAILED"} else item.get("current_stage")
        if stage_message is not None:
            item["stage_message"] = str(stage_message)
        if extra_updates:
            item.update(extra_updates)
        item["updated_at"] = _now_iso()
        _persist_jobs()
        return dict(item)


def _normalize_pipeline_step_status(value: Any, *, default: str = "PENDING") -> str:
    text = str(value or "").strip().upper()
    return text or default


def _selected_pipeline_stages(plan: dict[str, bool]) -> list[str]:
    return [stage for stage in PIPELINE_STAGE_ORDER if bool(plan.get(stage))]


def _derive_job_stage(
    steps: dict[str, str],
    *,
    preferred: str | None = None,
    states: tuple[str, ...] = ("RUNNING", "PAUSED", "ERROR", "FAILED"),
) -> str | None:
    preferred_norm = str(preferred or "").strip().upper()
    if preferred_norm and steps.get(preferred_norm) in states:
        return preferred_norm
    for state in states:
        for stage in PIPELINE_STAGE_ORDER:
            if steps.get(stage) == state:
                return stage
    return preferred_norm or None


def _normalized_job_snapshot(item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    snapshot = dict(item or {})
    type_norm = _job_type_norm(snapshot)
    plan = dict(snapshot.get("pipeline_plan") or {})
    if not plan:
        if type_norm == "applicability":
            plan, _ = _build_followup_pipeline_context("A2")
        elif type_norm == "compliance":
            plan, _ = _build_followup_pipeline_context("A3")
        elif type_norm == "embedding_index":
            plan = _build_pipeline_plan(run_embedding=True)
        else:
            plan = _build_pipeline_plan()
        snapshot["pipeline_plan"] = plan

    if snapshot.get("pipeline_steps"):
        raw_steps = dict(snapshot.get("pipeline_steps") or {})
        if type_norm == "applicability":
            _, fallback_steps = _build_followup_pipeline_context("A2")
            if all(str(raw_steps.get(stage) or "").strip().upper() == "SKIPPED" for stage in ("A1", "A2")):
                raw_steps = fallback_steps
            elif str(raw_steps.get("A1") or "").strip().upper() == "SKIPPED":
                raw_steps["A1"] = fallback_steps["A1"]
        elif type_norm == "compliance":
            _, fallback_steps = _build_followup_pipeline_context("A3")
            if all(str(raw_steps.get(stage) or "").strip().upper() == "SKIPPED" for stage in ("A1", "A2", "A3")):
                raw_steps = fallback_steps
            else:
                if str(raw_steps.get("A1") or "").strip().upper() == "SKIPPED":
                    raw_steps["A1"] = fallback_steps["A1"]
                if str(raw_steps.get("A2") or "").strip().upper() == "SKIPPED":
                    raw_steps["A2"] = fallback_steps["A2"]
    elif type_norm == "applicability":
        _, raw_steps = _build_followup_pipeline_context("A2")
    elif type_norm == "compliance":
        _, raw_steps = _build_followup_pipeline_context("A3")
    else:
        raw_steps = _build_pipeline_steps(plan)
    status = str(snapshot.get("status") or "UNKNOWN").strip().upper() or "UNKNOWN"
    stage = str(snapshot.get("current_stage") or "").strip().upper() or None
    stop_requested = bool(snapshot.get("stop_requested"))
    has_finished = bool(snapshot.get("finished_at"))
    has_stopped = bool(snapshot.get("stopped_at"))

    steps: dict[str, str] = {}
    for pipeline_stage in PIPELINE_STAGE_ORDER:
        if not bool(plan.get(pipeline_stage)):
            steps[pipeline_stage] = "SKIPPED"
            continue
        default = "RUNNING" if stage == pipeline_stage and status in ACTIVE_JOB_STATUSES else "PENDING"
        steps[pipeline_stage] = _normalize_pipeline_step_status(raw_steps.get(pipeline_stage), default=default)

    if type_norm == "applicability":
        a2_payload = dict(snapshot.get("a2_result") or snapshot.get("result") or {})
        stage_status, stage_message, error_category = _a2_followup_outcome(a2_payload)
        if a2_payload:
            steps["A2"] = stage_status
            if status == "DONE":
                current_msg = str(snapshot.get("stage_message") or "").strip()
                if not current_msg or current_msg == "Pipeline terminé":
                    snapshot["stage_message"] = stage_message
                if stage_status == "ERROR":
                    status = "FAILED"
                    stage = "A2"
                    snapshot["error_category"] = str(snapshot.get("error_category") or error_category or "applicability_engine_error")
                    snapshot["failed_step"] = str(snapshot.get("failed_step") or "A2_PIPELINE")
                    snapshot["error"] = str(snapshot.get("error") or stage_message)

    if type_norm == "compliance":
        a3_payload = dict(snapshot.get("a3_result") or snapshot.get("result") or {})
        stage_status, stage_message, error_category = _a3_followup_outcome(a3_payload)
        if a3_payload:
            steps["A3"] = stage_status
            if status == "DONE":
                current_msg = str(snapshot.get("stage_message") or "").strip()
                if not current_msg or current_msg == "Pipeline terminé":
                    snapshot["stage_message"] = stage_message
                if stage_status == "ERROR":
                    status = "FAILED"
                    stage = "A3"
                    snapshot["error_category"] = str(snapshot.get("error_category") or error_category or "compliance_engine_error")
                    snapshot["failed_step"] = str(snapshot.get("failed_step") or "A3_PIPELINE")
                    snapshot["error"] = str(snapshot.get("error") or stage_message)

    selected = _selected_pipeline_stages(plan)
    running_stage = _derive_job_stage(steps, preferred=stage, states=("RUNNING",))
    paused_stage = _derive_job_stage(steps, preferred=stage, states=("PAUSED",))
    failed_stage = _derive_job_stage(steps, preferred=stage, states=("ERROR", "FAILED"))
    all_finalized = bool(selected) and all(steps.get(s) in FINALIZED_STEP_STATUSES for s in selected)

    if status in ACTIVE_JOB_STATUSES and stop_requested and (has_stopped or has_finished):
        status = "PAUSED"
        stage = paused_stage or running_stage or stage
        if stage and steps.get(stage) == "RUNNING":
            steps[stage] = "PAUSED"
        if not snapshot.get("stopped_at") and snapshot.get("finished_at"):
            snapshot["stopped_at"] = snapshot.get("finished_at")
        current_msg = str(snapshot.get("stage_message") or "").strip().lower()
        if not current_msg or "en cours" in current_msg or "running" in current_msg:
            label = PIPELINE_STAGE_LABELS.get(stage or "", "Pipeline")
            snapshot["stage_message"] = f"{label} en pause"
    elif status in ACTIVE_JOB_STATUSES and failed_stage and not running_stage:
        status = "FAILED"
        stage = failed_stage
        snapshot["stage_message"] = str(snapshot.get("stage_message") or f"{PIPELINE_STAGE_LABELS.get(stage or '', 'Pipeline')} échoué")
    elif status in ACTIVE_JOB_STATUSES and paused_stage and not running_stage:
        status = "PAUSED"
        stage = paused_stage
        if not str(snapshot.get("stage_message") or "").strip():
            snapshot["stage_message"] = f"{PIPELINE_STAGE_LABELS.get(stage or '', 'Pipeline')} en pause"
    elif status in ACTIVE_JOB_STATUSES and all_finalized:
        status = "DONE"
        stage = None
        snapshot["stage_message"] = "Pipeline terminé"
        snapshot["stop_requested"] = False
    elif status == "DONE":
        if failed_stage:
            status = "FAILED"
            stage = failed_stage
        elif paused_stage:
            status = "PAUSED"
            stage = paused_stage
            if not str(snapshot.get("stage_message") or "").strip():
                snapshot["stage_message"] = f"{PIPELINE_STAGE_LABELS.get(stage or '', 'Pipeline')} en pause"
        elif running_stage:
            status = "RUNNING"
            stage = running_stage
        else:
            stage = None
            snapshot["stage_message"] = str(snapshot.get("stage_message") or "Pipeline terminé")
            snapshot["stop_requested"] = False
    elif status == "PAUSED":
        stage = paused_stage or running_stage or stage
        if stage and steps.get(stage) == "RUNNING":
            steps[stage] = "PAUSED"
        if not str(snapshot.get("stage_message") or "").strip():
            snapshot["stage_message"] = f"{PIPELINE_STAGE_LABELS.get(stage or '', 'Pipeline')} en pause"
    elif status in {"FAILED", "ERROR"}:
        stage = failed_stage or stage

    snapshot["status"] = status
    snapshot["current_stage"] = stage
    snapshot["pipeline_steps"] = steps
    if status == "DONE":
        snapshot["current_stage"] = None
        snapshot["stage_message"] = str(snapshot.get("stage_message") or "Pipeline terminé")

    changed = snapshot != item
    return snapshot, changed


def _find_profile_id(conn: psycopg.Connection, tenant_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT profile_id::text FROM company_profiles WHERE LOWER(COALESCE(tenant_id, '')) = LOWER(%s)",
            (tenant_id,),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def _load_auth_user(username: str, tenant_id: str) -> dict[str, Any] | None:
    dsn = _load_env_dsn()
    with psycopg.connect(dsn, tenant_id=tenant_id) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id::text, tenant_id, username, password_hash, role, display_name, is_active
                FROM app_users
                WHERE tenant_id = %s
                  AND LOWER(username) = LOWER(%s)
                LIMIT 1
                """,
                (tenant_id, username),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "user_id": str(row[0]),
        "tenant_id": str(row[1]),
        "username": str(row[2]),
        "password_hash": str(row[3]),
        "role": str(row[4]),
        "display_name": str(row[5]),
        "is_active": bool(row[6]),
    }


def _load_auth_user_without_tenant(username: str) -> dict[str, Any] | None:
    dsn = _load_env_dsn()
    with _RAW_PSYCOPG_CONNECT(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id
                FROM tenant_directory
                WHERE COALESCE(tenant_id, '') <> ''
                ORDER BY tenant_id
                """,
            )
            tenant_rows = cur.fetchall() or []

    matches: list[dict[str, Any]] = []
    for row in tenant_rows:
        tenant_id = str(row[0] or "").strip()
        if not tenant_id:
            continue
        user = _load_auth_user(username, tenant_id)
        if user:
            matches.append(user)

    if not matches:
        return None

    if len(matches) > 1:
        super_admin_rows = [row for row in matches if str(row.get("role") or "").strip().upper() == "SUPER_ADMIN"]
        if len(super_admin_rows) == 1:
            matches = super_admin_rows
        else:
            raise HTTPException(
                status_code=400,
                detail="tenant_id requis pour ce compte entreprise",
            )

    return matches[0]


def _tenant_exists(tenant_id: str) -> bool:
    tenant_norm = _parse_tenant_id(tenant_id, field_name="tenant_id")
    dsn = _load_env_dsn()
    with psycopg.connect(dsn, tenant_id=tenant_norm) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM company_profiles WHERE LOWER(COALESCE(tenant_id, '')) = LOWER(%s)
                )
                OR EXISTS (
                    SELECT 1 FROM documents WHERE LOWER(COALESCE(tenant_id, '')) = LOWER(%s)
                )
                OR EXISTS (
                    SELECT 1 FROM app_users WHERE LOWER(COALESCE(tenant_id, '')) = LOWER(%s)
                )
                """,
                (tenant_norm, tenant_norm, tenant_norm),
            )
            row = cur.fetchone()
            return bool(row[0]) if row else False


def _tenant_directory_row(tenant_id: str) -> dict[str, Any] | None:
    tenant_norm = _parse_tenant_id(tenant_id, field_name="tenant_id", allow_empty=True)
    if not tenant_norm:
        return None
    dsn = _load_env_dsn()
    try:
        with _RAW_PSYCOPG_CONNECT(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        tenant_id,
                        COALESCE(company_name, '') AS company_name,
                        COALESCE(documents_count, 0)::int AS documents_count,
                        COALESCE(has_company_profile, FALSE) AS has_company_profile
                    FROM tenant_directory
                    WHERE tenant_id = %s
                    LIMIT 1
                    """,
                    (tenant_norm,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "tenant_id": str(row[0]),
                    "company_name": str(row[1] or ""),
                    "documents_count": int(row[2] or 0),
                    "has_company_profile": bool(row[3]),
                }
    except Exception:
        return None


def _tenant_directory_company_name(tenant_id: str) -> str:
    row = _tenant_directory_row(tenant_id)
    return str((row or {}).get("company_name") or "")


def _scoped_company_name(tenant_id: str) -> str:
    tenant_norm = _parse_tenant_id(tenant_id, field_name="tenant_id", allow_empty=True)
    if not tenant_norm:
        return ""
    dsn = _load_env_dsn()
    try:
        with psycopg.connect(dsn, tenant_id=tenant_norm) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(company_name, '')
                    FROM company_profiles
                    WHERE LOWER(COALESCE(tenant_id, '')) = LOWER(%s)
                    LIMIT 1
                    """,
                    (tenant_norm,),
                )
                row = cur.fetchone()
                return str(row[0] or "") if row else ""
    except Exception:
        return ""


def _tenant_label_for_session(tenant_id: str) -> str:
    return _tenant_directory_company_name(tenant_id) or _scoped_company_name(tenant_id)


def _similarity_text(text: Any) -> str:
    raw = str(text or "").strip().lower()
    return re.sub(r"\s+", " ", raw)


def _text_similarity(a: Any, b: Any) -> float:
    aa = _similarity_text(a)
    bb = _similarity_text(b)
    if not aa or not bb:
        return 0.0
    return round(float(SequenceMatcher(None, aa, bb).ratio()), 2)


_REVIEW_NORMATIVE_VERB_PATTERNS: list[tuple[re.Pattern[str], str]] = [
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
    (re.compile(r"\b(repond(?:ent)? de)\b", re.IGNORECASE), "répond de"),
    (re.compile(r"\b(est passible de)\b", re.IGNORECASE), "est passible de"),
]
_REVIEW_CONDITION_MARKERS = (
    " si ",
    " lorsque ",
    " lorsqu'",
    " quand ",
    " en cas de ",
    " pour les ",
    " pour le ",
    " pour la ",
    " pour tout ",
    " pour toute ",
    " des lors que ",
    " sous reserve de ",
    " sous réserve de ",
)
_REVIEW_EXCEPTION_MARKERS = (
    " sauf ",
    " sauf si ",
    " a l'exception de ",
    " à l'exception de ",
    " hors ",
    " excepté ",
    " excepte ",
)


def _normalize_review_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _humanize_review_blocked_reason(value: Any) -> str:
    norm = str(value or "").strip().upper()
    labels = {
        "QUALITY_AND_GROUNDING_TOO_LOW": "qualité et ancrage source insuffisants",
        "QUALITY_TOO_LOW": "qualité de formulation insuffisante",
        "GROUNDING_TOO_WEAK": "ancrage source trop faible",
        "LOW_CONFIDENCE": "confiance trop faible",
        "POLICY_OR_TYPE": "règle non promue automatiquement",
        "TEXT_TOO_SHORT": "texte trop court",
        "TEXT_TOO_LONG": "texte trop long",
        "NO_LEGAL_SUBJECT": "sujet juridique absent",
        "NO_NORMATIVE_VERB": "verbe normatif absent",
    }
    return labels.get(norm, str(value or "").strip())


def _find_review_marker(segment: str, markers: tuple[str, ...]) -> tuple[int, str]:
    segment_norm = f" {segment.lower()} "
    best_index = -1
    best_marker = ""
    for marker in markers:
        idx = segment_norm.find(marker)
        if idx == -1:
            continue
        if best_index == -1 or idx < best_index:
            best_index = idx
            best_marker = marker.strip()
    if best_index == -1:
        return -1, ""
    return max(best_index - 1, 0), best_marker


def _infer_requirement_review_structure(requirement_text: Any, source_snippet: Any = "") -> dict[str, Any]:
    raw_requirement = _normalize_review_text(requirement_text)
    raw_source = _normalize_review_text(source_snippet)
    if not raw_requirement:
        return {
            "legal_subject": "",
            "normative_verb": "",
            "action_object": "",
            "condition_text": "",
            "exception_text": "",
            "source_mode": "NON_PRECISE",
            "requirement_word_count": 0,
            "source_word_count": len(raw_source.split()),
            "source_requirement_similarity": _text_similarity(raw_requirement, raw_source),
        }

    legal_subject = ""
    normative_verb = ""
    action_object = raw_requirement
    condition_text = ""
    exception_text = ""

    verb_match = None
    for pattern, label in _REVIEW_NORMATIVE_VERB_PATTERNS:
        candidate = pattern.search(raw_requirement)
        if candidate and (verb_match is None or candidate.start() < verb_match.start()):
            verb_match = candidate
            normative_verb = label

    if verb_match:
        legal_subject = raw_requirement[:verb_match.start()].strip(" ,:;-")
        remainder = raw_requirement[verb_match.end():].strip(" ,:;-")
        action_object = remainder or raw_requirement
        cond_idx, _ = _find_review_marker(remainder, _REVIEW_CONDITION_MARKERS)
        exc_idx, _ = _find_review_marker(remainder, _REVIEW_EXCEPTION_MARKERS)
        split_points = [idx for idx in (cond_idx, exc_idx) if idx >= 0]
        if split_points:
            first_idx = min(split_points)
            action_object = remainder[:first_idx].strip(" ,:;-")
            tail = remainder[first_idx:].strip()
            tail_cond_idx, _ = _find_review_marker(tail, _REVIEW_CONDITION_MARKERS)
            tail_exc_idx, _ = _find_review_marker(tail, _REVIEW_EXCEPTION_MARKERS)
            if tail_cond_idx >= 0 and (tail_exc_idx == -1 or tail_cond_idx <= tail_exc_idx):
                next_exc_idx, _ = _find_review_marker(tail[tail_cond_idx + 1 :], _REVIEW_EXCEPTION_MARKERS)
                if next_exc_idx >= 0:
                    exception_text = tail[tail_cond_idx + 1 + next_exc_idx :].strip(" ,:;-")
                    condition_text = tail[: tail_cond_idx + 1 + next_exc_idx].strip(" ,:;-")
                else:
                    condition_text = tail.strip(" ,:;-")
            elif tail_exc_idx >= 0:
                next_cond_idx, _ = _find_review_marker(tail[tail_exc_idx + 1 :], _REVIEW_CONDITION_MARKERS)
                if next_cond_idx >= 0:
                    condition_text = tail[tail_exc_idx + 1 + next_cond_idx :].strip(" ,:;-")
                    exception_text = tail[: tail_exc_idx + 1 + next_cond_idx].strip(" ,:;-")
                else:
                    exception_text = tail.strip(" ,:;-")

    similarity = _text_similarity(raw_requirement, raw_source)
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
        "requirement_word_count": len(raw_requirement.split()),
        "source_word_count": len(raw_source.split()),
        "source_requirement_similarity": similarity,
    }


def _build_review_guidance(
    item: dict[str, Any],
    review_structure: dict[str, Any],
    similar_existing: list[dict[str, Any]],
) -> list[dict[str, str]]:
    guidance: list[dict[str, str]] = []
    blocked_reason = str(item.get("promotion_blocked_reason") or "").strip()
    if blocked_reason:
        guidance.append(
            {
                "level": "warning",
                "title": "Blocage automatique",
                "message": f"Vérifier ce point avant promotion: {_humanize_review_blocked_reason(blocked_reason)}.",
            }
        )
    if not str(review_structure.get("legal_subject") or "").strip():
        guidance.append(
            {
                "level": "warning",
                "title": "Sujet juridique",
                "message": "Le sujet juridique n'est pas clairement identifié. Une correction humaine est recommandée.",
            }
        )
    if not str(review_structure.get("normative_verb") or "").strip():
        guidance.append(
            {
                "level": "warning",
                "title": "Verbe normatif",
                "message": "Le verbe normatif n'est pas explicite. Vérifier qu'il s'agit bien d'une exigence exploitable.",
            }
        )
    if int(review_structure.get("requirement_word_count") or 0) > 45:
        guidance.append(
            {
                "level": "warning",
                "title": "Texte long",
                "message": "Le texte dépasse la longueur idéale. Envisager de le raccourcir ou de le découper avant validation.",
            }
        )
    if similar_existing:
        best_similarity = max(float(item.get("similarity") or 0) for item in similar_existing)
        guidance.append(
            {
                "level": "info" if best_similarity < 0.9 else "warning",
                "title": "Doublon potentiel",
                "message": f"Un contenu très proche existe déjà dans le registre ({round(best_similarity * 100)}%). Vérifier avant promotion.",
            }
        )
    if not guidance:
        guidance.append(
            {
                "level": "success",
                "title": "Revue rapide",
                "message": "Le texte semble lisible et suffisamment structuré pour une validation rapide.",
            }
        )
    return guidance


def _recommend_review_decision(
    item: dict[str, Any],
    review_structure: dict[str, Any],
    similar_existing: list[dict[str, Any]],
) -> tuple[str, str]:
    blocked_reason = str(item.get("promotion_blocked_reason") or "").strip().upper()
    best_similarity = max((float(other.get("similarity") or 0) for other in similar_existing), default=0.0)
    if best_similarity >= 0.95:
        return ("FLAG", "Doublon presque certain avec une exigence déjà validée.")
    if blocked_reason in {"TEXT_TOO_SHORT", "TEXT_TOO_LONG", "NO_LEGAL_SUBJECT", "NO_NORMATIVE_VERB"}:
        return ("EDIT", "Le texte doit être corrigé avant promotion.")
    if not str(review_structure.get("legal_subject") or "").strip():
        return ("EDIT", "Le sujet juridique est implicite ou mal formulé.")
    if not str(review_structure.get("normative_verb") or "").strip():
        return ("EDIT", "Le verbe normatif n'est pas assez clair pour le registre final.")
    if int(review_structure.get("requirement_word_count") or 0) > 45:
        return ("EDIT", "La formulation est trop longue pour le registre final.")
    return ("APPROVE", "Le texte semble prêt pour promotion dans le registre final.")


def _list_visible_tenants_for_session(session: dict[str, Any]) -> list[dict[str, Any]]:
    active_tenant = _session_active_tenant(session)
    items: list[dict[str, Any]] = []
    dsn = _load_env_dsn()
    if _is_super_admin(session):
        try:
            with _RAW_PSYCOPG_CONNECT(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            tenant_id,
                            COALESCE(company_name, '') AS company_name,
                            COALESCE(documents_count, 0)::int AS documents_count,
                            COALESCE(has_company_profile, FALSE) AS has_company_profile
                        FROM tenant_directory
                        ORDER BY
                            CASE WHEN tenant_id = %s THEN 0 ELSE 1 END,
                            LOWER(COALESCE(company_name, '')),
                            tenant_id
                        """,
                        (active_tenant,),
                    )
                    cols = [d[0] for d in cur.description]
                    items = [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            items = []
    else:
        row = _tenant_directory_row(active_tenant)
        if row:
            items = [row]

    if active_tenant and not any(str(item.get("tenant_id") or "") == active_tenant for item in items):
        items.insert(
            0,
            {
                "tenant_id": active_tenant,
                "company_name": _tenant_label_for_session(active_tenant),
                "documents_count": 0,
                "has_company_profile": bool(_scoped_company_name(active_tenant)),
            },
        )

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        tenant_norm = _parse_tenant_id(item.get("tenant_id"), field_name="tenant_id", allow_empty=True)
        if not tenant_norm or tenant_norm in seen:
            continue
        company_name = str(item.get("company_name") or "")
        has_company_profile = bool(item.get("has_company_profile")) or bool(company_name)
        if not has_company_profile and tenant_norm != active_tenant:
            continue
        seen.add(tenant_norm)
        deduped.append(
            {
                "tenant_id": tenant_norm,
                "company_name": company_name,
                "documents_count": int(item.get("documents_count") or 0),
                "has_company_profile": has_company_profile,
                "is_active_context": tenant_norm == active_tenant,
            }
        )
    return deduped


def _require_profile_id(conn: psycopg.Connection, tenant_id: str) -> str:
    profile_id = _find_profile_id(conn, tenant_id)
    if not profile_id:
        raise HTTPException(status_code=404, detail=f"Tenant inconnu: {tenant_id}. Creez d'abord le profil entreprise.")
    return profile_id


def _persist_jobs() -> None:
    JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JOBS_PATH.open("w", encoding="utf-8") as f:
        json.dump({"updated_at": _now_iso(), "jobs": JOBS}, f, ensure_ascii=False, indent=2)


def _delete_run_log_artifacts(item: dict[str, Any]) -> int:
    deleted = 0
    candidates = [
        item.get("stdout_path"),
        item.get("stderr_path"),
        str((RUN_LOGS_DIR / f"{str(item.get('job_id') or '').strip()}.stdout.log").resolve()),
        str((RUN_LOGS_DIR / f"{str(item.get('job_id') or '').strip()}.stderr.log").resolve()),
    ]
    seen: set[str] = set()
    for raw_path in candidates:
        path_str = str(raw_path or "").strip()
        if not path_str or path_str in seen:
            continue
        seen.add(path_str)
        try:
            path = Path(path_str)
            if path.exists() and path.is_file():
                path.unlink()
                deleted += 1
        except Exception:
            continue
    return deleted


def _restore_jobs_from_disk() -> None:
    if not JOBS_PATH.exists():
        return
    try:
        payload = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
        jobs = payload.get("jobs", {})
        if isinstance(jobs, dict):
            for k, v in jobs.items():
                if not isinstance(v, dict):
                    continue
                JOBS[str(k)] = dict(v)
    except Exception:
        return


def _reconcile_orphan_jobs_on_boot() -> int:
    """
    Après redémarrage serveur, les background tasks précédentes sont perdues.
    Tout job resté actif est donc marqué FAILED pour éviter les runs fantômes.
    """
    active_states = {"QUEUED", "PENDING", "RUNNING"}
    now_iso = _now_iso()
    changed = 0
    with JOBS_LOCK:
        for item in JOBS.values():
            status = str(item.get("status") or "").strip().upper()
            if status not in active_states:
                continue
            item["status"] = "FAILED"
            item["finished_at"] = now_iso
            item["updated_at"] = now_iso
            item["return_code"] = int(item.get("return_code") or -1)
            item["error_category"] = str(item.get("error_category") or "interrupted_run")
            item["failed_step"] = str(item.get("failed_step") or "SERVER_RESTART")
            item["error"] = str(item.get("error") or "run interrompu suite redemarrage serveur")
            changed += 1
        if changed:
            _persist_jobs()
    return changed


def _update_job(job_id: str, **updates: Any) -> dict[str, Any]:
    with JOBS_LOCK:
        if job_id not in JOBS:
            raise KeyError(f"job_id inconnu: {job_id}")
        JOBS[job_id].update(updates)
        JOBS[job_id]["updated_at"] = _now_iso()
        _persist_jobs()
        return dict(JOBS[job_id])


def _job_stop_requested(job_id: str) -> bool:
    with JOBS_LOCK:
        item = JOBS.get(job_id) or {}
        return bool(item.get("stop_requested"))


def _normalized_jobs_for_tenant(tenant_norm: str) -> list[dict[str, Any]]:
    changed = False
    values: list[dict[str, Any]] = []
    with JOBS_LOCK:
        for job_id, item in JOBS.items():
            if _job_tenant_norm(item) != tenant_norm:
                continue
            snapshot, is_changed = _normalized_job_snapshot(item)
            if is_changed:
                JOBS[job_id] = dict(snapshot)
                changed = True
            values.append(snapshot)
        if changed:
            _persist_jobs()
    return values


def _normalized_job_for_tenant(job_id: str, tenant_norm: str) -> dict[str, Any] | None:
    with JOBS_LOCK:
        item = JOBS.get(job_id)
        if not item or _job_tenant_norm(item) != tenant_norm:
            return None
        snapshot, changed = _normalized_job_snapshot(item)
        if changed:
            JOBS[job_id] = dict(snapshot)
            _persist_jobs()
        return snapshot


def _job_type_norm(item: dict[str, Any]) -> str:
    return str(item.get("type") or "extraction").strip().lower() or "extraction"


def _safe_read_text(path: str, *, max_chars: int = 2_000_000) -> str:
    raw_path = str(path or "").strip()
    if not raw_path:
        return ""
    try:
        text = Path(raw_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if max_chars > 0 and len(text) > max_chars:
        return text[-max_chars:]
    return text


def _tail_lines(text: str, *, limit: int = 80) -> str:
    lines = str(text or "").splitlines()
    return "\n".join(lines[-max(1, int(limit)):])


def _parse_doc_id(stdout_text: str, stderr_text: str) -> str | None:
    joined = f"{stdout_text or ''}\n{stderr_text or ''}"
    match = DOC_ID_RE.search(joined)
    if not match:
        return None
    return str(match.group(1))


def _classify_error(text: str) -> str:
    low = str(text or "").lower()
    if not low.strip():
        return "unknown_error"
    if "texte pdf illisible" in low or "ocr indisponible" in low or "tesseract is not installed" in low:
        return "unreadable_pdf_text"
    if "unicodeencodeerror" in low or "charmap" in low or "can't encode character" in low:
        return "encoding_error"
    if "timeoutexpired" in low or "timed out" in low:
        return "timeout_error"
    if "rate-limit" in low or "rate limited" in low or "rate_limited" in low:
        return "provider_rate_limit"
    if "openai" in low or "gemini" in low or "groq" in low or "provider_error" in low:
        return "provider_error"
    if "psycopg" in low or "pg_dsn" in low or "postgres" in low or "database" in low:
        return "database_error"
    if "file not found" in low or "filenotfounderror" in low:
        return "file_error"
    if "runtimeerror: echec" in low:
        return "pipeline_step_error"
    return "pipeline_error"


def _extract_failed_step(text: str) -> str | None:
    raw = str(text or "")
    m = STEP_FAILURE_RE.search(raw)
    if not m:
        low = raw.lower()
        timeout_seen = "timeoutexpired" in low or "timed out" in low
        if timeout_seen and "a1_segment_articles_chunks.py" in low:
            return "SEGMENTATION_TIMEOUT"
        if timeout_seen and "a1_extract_requirements_llm.py" in low:
            return "EXTRACTION_TIMEOUT"
        if timeout_seen and "a1_ingest_pdf_min.py" in low:
            return "INGESTION_TIMEOUT"
        if timeout_seen and "a1_backfill_qse_fields.py" in low:
            return "QSE_BACKFILL_TIMEOUT"
        if timeout_seen and "a1_export_registry.py" in low:
            return "REGISTRY_EXPORT_TIMEOUT"
        return None
    return str(m.group(1)).strip().replace(" ", "_").upper()


def _parse_iso_utc(text: str | None) -> datetime | None:
    value = str(text or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _is_active_job_stale(item: dict[str, Any]) -> bool:
    stamp = _parse_iso_utc(item.get("updated_at")) or _parse_iso_utc(item.get("created_at"))
    if not stamp:
        return False
    try:
        return (datetime.now(UTC) - stamp) > timedelta(hours=ACTIVE_RUN_STALE_HOURS)
    except Exception:
        return False


def _normalize_title_key(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _find_active_a1_run(tenant: str, title: str, file_name: str) -> dict[str, Any] | None:
    tenant_key = _normalize_tenant_id(tenant)
    title_key = _normalize_title_key(title)
    file_key = str(file_name or "").strip().lower()
    active_states = {"QUEUED", "PENDING", "RUNNING"}
    with JOBS_LOCK:
        changed = False
        for job_id, item in JOBS.items():
            snapshot, is_changed = _normalized_job_snapshot(item)
            if is_changed:
                JOBS[job_id] = dict(snapshot)
                changed = True
            if _job_tenant_norm(snapshot) != tenant_key:
                continue
            if str(snapshot.get("type") or "").strip().lower() not in {"", "extraction"}:
                continue
            if str(snapshot.get("status") or "").strip().upper() not in active_states:
                continue
            if _is_active_job_stale(snapshot):
                continue

            item_title = _normalize_title_key(str(snapshot.get("title") or ""))
            item_file = str(snapshot.get("file_name") or "").strip().lower()
            same_title = bool(title_key and item_title and item_title == title_key)
            same_file = bool(file_key and item_file and item_file == file_key)
            if same_title or same_file:
                if changed:
                    _persist_jobs()
                return dict(snapshot)
        if changed:
            _persist_jobs()
    return None


def _issue_token(username: str) -> str:
    seed = f"{username}|{uuid.uuid4()}|{_now_iso()}"
    return hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()


def _auth_from_header(authorization: str | None) -> dict[str, Any]:
    raw = str(authorization or "").strip()
    if not raw.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authorization bearer requis")
    token = raw.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token manquant")
    with AUTH_LOCK:
        session = AUTH_TOKENS.get(token)
    if not session:
        raise HTTPException(status_code=401, detail="Token invalide ou expire")
    # Verification expiration
    expires_at = session.get("expires_at")
    if expires_at and datetime.fromisoformat(expires_at) < datetime.now(UTC):
        with AUTH_LOCK:
            AUTH_TOKENS.pop(token, None)
        raise HTTPException(status_code=401, detail="Session expiree - reconnectez-vous")
    return dict(session)


def _require_role(session: dict[str, Any], allowed: set[str], action: str = "cette action") -> None:
    """Leve HTTP 403 si le role de la session n'est pas dans `allowed`."""
    role = str(session.get("role") or "")
    if role not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Role '{role}' insuffisant pour {action}. Requis : {', '.join(sorted(allowed))}",
        )


def _purge_expired_tokens() -> None:
    """Supprime les tokens expires de AUTH_TOKENS (appele a chaque login)."""
    now = datetime.now(UTC)
    with AUTH_LOCK:
        expired = [
            t for t, s in AUTH_TOKENS.items()
            if s.get("expires_at") and datetime.fromisoformat(s["expires_at"]) < now
        ]
        for t in expired:
            AUTH_TOKENS.pop(t, None)


def _run_pipeline_job(
    *,
    job_id: str,
    pdf_path: str,
    tenant: str,
    title: str,
    source: str,
    jurisdiction: str,
    document_family: str,
    extract_limit: int,
    sleep_ms: int,
    on_duplicate: str,
    timeout_ingest_sec: int,
    timeout_segment_sec: int,
    timeout_extract_sec: int,
    timeout_backfill_sec: int,
    timeout_export_sec: int,
    max_chars: int,
    enable_qse_backfill: str,
    registry_outdir: str,
    run_applicability: bool = False,
    run_compliance: bool = False,
    run_embedding: bool = False,
) -> None:
    py = sys.executable
    cmd: list[str] = [
        py,
        "a1_ingest_extract_registry.py",
        "--pdf",
        pdf_path,
        "--tenant",
        tenant,
        "--source",
        source,
        "--jurisdiction",
        jurisdiction,
        "--document_family",
        document_family,
        "--sleep_ms",
        str(int(sleep_ms)),
        "--on_duplicate",
        on_duplicate,
        "--timeout_ingest_sec",
        str(int(timeout_ingest_sec)),
        "--timeout_segment_sec",
        str(int(timeout_segment_sec)),
        "--timeout_extract_sec",
        str(int(timeout_extract_sec)),
        "--timeout_backfill_sec",
        str(int(timeout_backfill_sec)),
        "--timeout_export_sec",
        str(int(timeout_export_sec)),
        "--max_chars",
        str(int(max_chars)),
        "--enable_qse_backfill",
        enable_qse_backfill,
        "--registry_outdir",
        registry_outdir,
    ]
    if title.strip():
        cmd.extend(["--title", title.strip()])
    if int(extract_limit) > 0:
        cmd.extend(["--extract_limit", str(int(extract_limit))])

    RUN_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stdout_path = str((RUN_LOGS_DIR / f"{job_id}.stdout.log").resolve())
    stderr_path = str((RUN_LOGS_DIR / f"{job_id}.stderr.log").resolve())

    pipeline_plan = _build_pipeline_plan(
        run_applicability=run_applicability,
        run_compliance=run_compliance,
        run_embedding=run_embedding,
    )
    _update_job(
        job_id,
        status="RUNNING",
        started_at=_now_iso(),
        command=" ".join(cmd),
        error_category=None,
        failed_step=None,
        error=None,
        stop_requested=False,
        pid=None,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        pipeline_plan=pipeline_plan,
        pipeline_steps=_build_pipeline_steps(pipeline_plan, active_stage="A1"),
        current_stage="A1",
        stage_message="Extraction en cours",
    )

    total_timeout = int(timeout_ingest_sec) + int(timeout_segment_sec) + int(timeout_extract_sec)
    total_timeout += int(timeout_backfill_sec) + int(timeout_export_sec) + 120

    try:
        with open(stdout_path, "w", encoding="utf-8", errors="replace") as out_f, open(
            stderr_path, "w", encoding="utf-8", errors="replace"
        ) as err_f:
            proc = subprocess.Popen(
                cmd,
                stdout=out_f,
                stderr=err_f,
                text=True,
            )
            _update_job(job_id, pid=int(proc.pid))

            deadline = time.monotonic() + max(1, int(total_timeout))
            stop_requested = False
            timeout_hit = False
            while True:
                return_code = proc.poll()
                if return_code is not None:
                    break
                if _job_stop_requested(job_id):
                    stop_requested = True
                    try:
                        proc.terminate()
                        proc.wait(timeout=10)
                    except Exception:
                        try:
                            proc.kill()
                            proc.wait(timeout=5)
                        except Exception:
                            pass
                    break
                if time.monotonic() >= deadline:
                    timeout_hit = True
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                    break
                time.sleep(1.0)

            return_code = int(proc.poll() if proc.poll() is not None else -1)
            stdout_text = _safe_read_text(stdout_path)
            stderr_text = _safe_read_text(stderr_path)
            doc_id = _parse_doc_id(stdout_text, stderr_text)

            if stop_requested:
                _update_job(
                    job_id,
                    status="PAUSED",
                    finished_at=_now_iso(),
                    return_code=return_code,
                    doc_id=doc_id,
                    stdout_tail=_tail_lines(stdout_text, limit=80),
                    stderr_tail=_tail_lines(stderr_text, limit=80),
                    error="run interrompu par utilisateur",
                    error_category="user_stop",
                    failed_step="USER_STOP",
                    stop_requested=False,
                    stopped_at=_now_iso(),
                    pid=None,
                    pipeline_steps=_mark_pipeline_stage(
                        job_id,
                        "A1",
                        "PAUSED",
                        stage_message="Extraction en pause",
                    ).get("pipeline_steps"),
                    current_stage="A1",
                    stage_message="Extraction en pause",
                )
                return

            if timeout_hit:
                _update_job(
                    job_id,
                    status="FAILED",
                    finished_at=_now_iso(),
                    return_code=-1,
                    doc_id=doc_id,
                    stdout_tail=_tail_lines(stdout_text, limit=80),
                    stderr_tail=_tail_lines(stderr_text, limit=80),
                    error=f"pipeline timeout ({int(total_timeout)}s)",
                    error_category="timeout_error",
                    failed_step="PIPELINE_TIMEOUT",
                    pid=None,
                    pipeline_steps=_mark_pipeline_stage(
                        job_id,
                        "A1",
                        "ERROR",
                        stage_message="Extraction échouée (timeout)",
                    ).get("pipeline_steps"),
                    current_stage="A1",
                    stage_message="Extraction échouée (timeout)",
                )
                return

    except Exception as exc:
        err_text = str(exc)
        _update_job(
            job_id,
            status="FAILED",
            finished_at=_now_iso(),
            error=err_text,
            error_category=_classify_error(err_text),
            failed_step="PIPELINE_EXCEPTION",
            return_code=-1,
            pid=None,
            pipeline_steps=_mark_pipeline_stage(
                job_id,
                "A1",
                "ERROR",
                stage_message="Extraction échouée",
            ).get("pipeline_steps"),
            current_stage="A1",
            stage_message="Extraction échouée",
        )
        return

    stdout_text = _safe_read_text(stdout_path)
    stderr_text = _safe_read_text(stderr_path)
    doc_id = _parse_doc_id(stdout_text, stderr_text)
    if int(return_code) == 0:
        current_stage = "A1"
        current_message = "Extraction terminée"
        marked = _mark_pipeline_stage(
            job_id,
            "A1",
            "DONE",
            stage_message=current_message,
            extra_updates={
                "doc_id": doc_id,
                "return_code": int(return_code),
                "stdout_tail": _tail_lines(stdout_text, limit=60),
                "stderr_tail": _tail_lines(stderr_text, limit=60),
                "error_category": None,
                "failed_step": None,
                "stop_requested": False,
                "pid": None,
            },
        )

        def _fail_stage(stage: str, label: str, exc: Exception) -> None:
            err_text = str(exc)
            _mark_pipeline_stage(job_id, stage, "ERROR", stage_message=f"{label} échoué")
            _update_job(
                job_id,
                status="FAILED",
                finished_at=_now_iso(),
                error=err_text,
                error_category=_classify_error(err_text),
                failed_step=f"{stage}_PIPELINE",
                stop_requested=False,
                pid=None,
                current_stage=stage,
                stage_message=f"{label} échoué",
            )

        def _pause_stage(stage: str, label: str) -> None:
            _mark_pipeline_stage(job_id, stage, "PAUSED", stage_message=f"{label} en pause")
            _update_job(
                job_id,
                status="PAUSED",
                finished_at=_now_iso(),
                stop_requested=False,
                stopped_at=_now_iso(),
                current_stage=stage,
                stage_message=f"{label} en pause",
                pid=None,
            )

        def _a2_stage_outcome(result: dict[str, Any]) -> tuple[str, str]:
            stats = dict((result or {}).get("engine_stats") or {})
            loaded = int(stats.get("requirements_loaded") or 0)
            total = int((result or {}).get("total") or 0)
            if loaded <= 0:
                return "SKIPPED", "Applicabilité ignorée (aucune exigence A1 exploitable)"
            if total <= 0:
                return "DONE", "Applicabilité terminée (0 décision)"
            return "DONE", f"Applicabilité terminée ({total} décision(s))"

        def _a3_stage_outcome(result: dict[str, Any]) -> tuple[str, str]:
            total = int((result or {}).get("total") or 0)
            if total <= 0:
                return "SKIPPED", "Conformité ignorée (aucune exigence applicable A2)"
            return "DONE", f"Conformité terminée ({total} évaluation(s))"

        def _emb_stage_outcome(indexed: int) -> tuple[str, str]:
            if int(indexed or 0) <= 0:
                return "SKIPPED", "Indexation ignorée (aucune exigence à indexer)"
            return "DONE", f"Indexation terminée ({int(indexed)} embedding(s))"

        if _job_stop_requested(job_id):
            _pause_stage("A1", "Extraction")
            return

        followups: list[tuple[str, str]] = []
        if bool(pipeline_plan.get("A2")):
            followups.append(("A2", "Applicabilité"))
        if bool(pipeline_plan.get("A3")):
            followups.append(("A3", "Conformité"))
        if bool(pipeline_plan.get("EMB")):
            followups.append(("EMB", "Indexation"))

        for stage, label in followups:
            if _job_stop_requested(job_id):
                _pause_stage(stage, label)
                return
            _mark_pipeline_stage(job_id, stage, "RUNNING", stage_message=f"{label} en cours")
            current_stage = stage
            current_message = f"{label} en cours"
            try:
                if stage == "A2":
                    from a2_applicability_engine import run_applicability as _engine
                    result = _call_with_supported_kwargs(
                        _engine,
                        tenant_id=tenant,
                        doc_id=doc_id,
                        mode="full",
                        force=True,
                        force_recompute=True,
                        stop_requested=lambda: _job_stop_requested(job_id),
                    ) or {}
                    if bool(result.get("stopped")):
                        _pause_stage(stage, label)
                        return
                    stage_status, stage_message = _a2_stage_outcome(result)
                    _mark_pipeline_stage(job_id, stage, stage_status, stage_message=stage_message)
                    _update_job(job_id, a2_result=result, current_stage=stage, stage_message=stage_message)
                    continue

                if stage == "A3":
                    from a3_compliance_engine import run_compliance as _engine
                    result = _call_with_supported_kwargs(
                        _engine,
                        tenant_id=tenant,
                        doc_id=doc_id,
                        mode="full",
                        force=True,
                        force_recompute=True,
                        stop_requested=lambda: _job_stop_requested(job_id),
                    ) or {}
                    if bool(result.get("stopped")):
                        _pause_stage(stage, label)
                        return
                    stage_status, stage_message = _a3_stage_outcome(result)
                    _mark_pipeline_stage(job_id, stage, stage_status, stage_message=stage_message)
                    _update_job(job_id, a3_result=result, current_stage=stage, stage_message=stage_message)
                    continue

                if stage == "EMB":
                    from a4_chat_engine import index_requirements
                    indexed = int(index_requirements(tenant, force=False) or 0)
                    stage_status, stage_message = _emb_stage_outcome(indexed)
                    _mark_pipeline_stage(job_id, stage, stage_status, stage_message=stage_message)
                    _update_job(job_id, indexed=indexed, current_stage=stage, stage_message=stage_message)
                    continue
            except Exception as exc:
                _fail_stage(stage, label, exc)
                return

        final_steps = dict((marked.get("pipeline_steps") or {}))
        with JOBS_LOCK:
            final_item = dict(JOBS.get(job_id) or {})
            final_steps = dict(final_item.get("pipeline_steps") or final_steps)
        _update_job(
            job_id,
            status="DONE",
            finished_at=_now_iso(),
            return_code=int(return_code),
            doc_id=doc_id,
            error_category=None,
            failed_step=None,
            stdout_tail=_tail_lines(stdout_text, limit=60),
            stderr_tail=_tail_lines(stderr_text, limit=60),
            stop_requested=False,
            pid=None,
            current_stage=None,
            stage_message="Pipeline terminé",
            pipeline_steps=final_steps,
        )
        return

    full_err_text = f"{stdout_text}\n{stderr_text}"
    _mark_pipeline_stage(job_id, "A1", "ERROR", stage_message="Extraction échouée")
    _update_job(
        job_id,
        status="FAILED",
        finished_at=_now_iso(),
        return_code=int(return_code),
        doc_id=doc_id,
        stdout_tail=_tail_lines(stdout_text, limit=80),
        stderr_tail=_tail_lines(stderr_text, limit=80),
        error="pipeline_error",
        error_category=_classify_error(full_err_text),
        failed_step=_extract_failed_step(full_err_text),
        stop_requested=False,
        pid=None,
        current_stage="A1",
        stage_message="Extraction échouée",
    )


def _fetch_requirements(doc_id: str, limit: int, offset: int, tenant_id: str) -> tuple[list[dict[str, Any]], int]:
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM requirements r
                JOIN documents d ON d.doc_id = r.doc_id
                WHERE r.doc_id = %s::uuid
                  AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                """,
                (doc_id, tenant_id),
            )
            total = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT
                    r.requirement_id::text,
                    r.requirement_no,
                    r.req_type,
                    r.requirement_text,
                    COALESCE(r.status, '') AS status,
                    COALESCE(r.confidence, 0)::float AS confidence,
                    COALESCE(r.qse_domain, '') AS qse_domain,
                    COALESCE(r.qse_sub_domain, '') AS qse_sub_domain,
                    COALESCE(r.citation_ref, '') AS citation_ref,
                    COALESCE(r.citation_snippet, '') AS citation_snippet,
                    COALESCE(a.article_label, a.article_code, '(no_label)') AS article_label
                FROM requirements r
                LEFT JOIN articles a ON a.article_id = r.article_id
                JOIN documents d ON d.doc_id = r.doc_id
                WHERE r.doc_id = %s::uuid
                  AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                ORDER BY COALESCE(r.requirement_no, 999999), r.requirement_id
                LIMIT %s OFFSET %s
                """,
                (doc_id, tenant_id, int(limit), int(offset)),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return rows, total


def _fetch_document_summary(doc_id: str, tenant_id: str) -> dict[str, Any]:
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    d.doc_id::text,
                    COALESCE(d.title, '') AS title,
                    COALESCE(d.source, '') AS source,
                    COALESCE(d.document_family, '') AS document_family
                FROM documents d
                WHERE d.doc_id = %s::uuid
                  AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                """,
                (doc_id, tenant_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Document introuvable")

            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE COALESCE(status, '') = 'PROMOTED') AS promoted_count,
                    COUNT(*) FILTER (WHERE COALESCE(status, '') = 'DRAFT') AS draft_count,
                    COUNT(*) FILTER (WHERE COALESCE(status, '') = 'TO_VALIDATE') AS to_validate_count,
                    COUNT(*) FILTER (WHERE COALESCE(status, '') = 'REJECT') AS reject_count,
                    AVG(COALESCE(confidence, 0))::float AS avg_conf
                FROM requirements
                WHERE doc_id = %s::uuid
                """,
                (doc_id,),
            )
            metrics = cur.fetchone()
            latest_extraction_payload = _fetch_latest_event_payload(
                cur,
                tenant_id=tenant_id,
                doc_id=doc_id,
                event_type="REQUIREMENTS_EXTRACTED",
            )
    total = int(metrics[0] or 0)
    return {
        "doc_id": row[0],
        "title": row[1],
        "source": row[2],
        "document_family": row[3],
        "requirements_total": total,
        "promoted_count": int(metrics[1] or 0),
        "draft_count": int(metrics[2] or 0),
        "to_validate_count": int(metrics[3] or 0),
        "reject_count": int(metrics[4] or 0),
        "avg_confidence": float(metrics[5] or 0.0),
        "latest_extraction": _build_latest_extraction_summary(latest_extraction_payload),
    }


def _load_recall_global() -> float | None:
    path = Path("reports/eval_latest.json")
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = payload.get("summary", {}).get("recall_global")
        if value is None:
            value = payload.get("recall_global")
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _fetch_dashboard_overview(tenant_id: str) -> dict[str, Any]:
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM documents WHERE LOWER(COALESCE(tenant_id, '')) = LOWER(%s)",
                (tenant_id,),
            )
            documents_total = int(cur.fetchone()[0] or 0)

            cur.execute(
                """
                SELECT
                    COUNT(*) AS requirements_total,
                    COUNT(*) FILTER (WHERE COALESCE(status, '') = 'PROMOTED') AS promoted_total,
                    COUNT(*) FILTER (WHERE COALESCE(status, '') = 'DRAFT') AS draft_total,
                    COUNT(*) FILTER (WHERE COALESCE(status, '') = 'TO_VALIDATE') AS to_validate_total,
                    COUNT(*) FILTER (WHERE COALESCE(status, '') = 'REJECT') AS reject_total
                FROM requirements r
                JOIN documents d ON d.doc_id = r.doc_id
                WHERE LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                """
                ,
                (tenant_id,),
            )
            req = cur.fetchone()
            requirements_total = int(req[0] or 0)
            promoted_total = int(req[1] or 0)
            draft_total = int(req[2] or 0)
            to_validate_total = int(req[3] or 0)
            reject_total = int(req[4] or 0)

            cur.execute(
                """
                SELECT COALESCE(req_type, 'UNKNOWN') AS req_type, COUNT(*)::int AS c
                FROM requirements r
                JOIN documents d ON d.doc_id = r.doc_id
                WHERE LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                GROUP BY COALESCE(req_type, 'UNKNOWN')
                ORDER BY c DESC, req_type ASC
                LIMIT 15
                """,
                (tenant_id,),
            )
            type_distribution = {str(t): int(c) for (t, c) in cur.fetchall()}

    with JOBS_LOCK:
        runs = []
        for item in JOBS.values():
            job_tenant = str(item.get("tenant") or item.get("tenant_id") or "").strip()
            if job_tenant and job_tenant.lower() == str(tenant_id or "").strip().lower():
                runs.append(item)
    runs.sort(key=lambda it: str(it.get("updated_at", "")), reverse=True)

    recent_runs: list[dict[str, Any]] = []
    for item in runs[:8]:
        recent_runs.append(
            {
                "job_id": item.get("job_id"),
                "status": item.get("status"),
                "file_name": item.get("file_name"),
                "updated_at": item.get("updated_at"),
                "error_category": item.get("error_category"),
                "doc_id": item.get("doc_id"),
            }
        )

    return {
        "documents_total": documents_total,
        "requirements_total": requirements_total,
        "promoted_total": promoted_total,
        "draft_total": draft_total,
        "to_validate_total": to_validate_total,
        "reject_total": reject_total,
        "type_distribution": type_distribution,
        "recent_runs": recent_runs,
        "recall_global": _load_recall_global(),
    }


def _fetch_documents_page(
    *,
    limit: int,
    offset: int,
    family: str | None,
    search: str | None,
    tenant_id: str,
) -> dict[str, Any]:
    dsn = _load_env_dsn()
    where_clauses: list[str] = ["LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)"]
    where_params: list[Any] = [tenant_id]

    family_norm = str(family or "").strip()
    if family_norm:
        where_clauses.append("COALESCE(d.document_family, '') = %s")
        where_params.append(family_norm)

    search_norm = str(search or "").strip()
    if search_norm:
        token = f"%{search_norm}%"
        where_clauses.append("(COALESCE(d.title, '') ILIKE %s OR COALESCE(d.source, '') ILIKE %s)")
        where_params.extend([token, token])

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM documents d
                {where_sql}
                """,
                tuple(where_params),
            )
            total = int(cur.fetchone()[0] or 0)

            params_rows = list(where_params) + [int(limit), int(offset)]
            cur.execute(
                f"""
                SELECT
                    d.doc_id::text AS doc_id,
                    COALESCE(d.title, '') AS title,
                    COALESCE(d.source, '') AS source,
                    COALESCE(d.document_family, '') AS document_family,
                    COALESCE(d.created_at::text, '') AS created_at,
                    COUNT(r.requirement_id)::int AS requirements_total,
                    COUNT(*) FILTER (WHERE COALESCE(r.status, '')='PROMOTED')::int AS promoted_count,
                    COUNT(*) FILTER (WHERE COALESCE(r.status, '')='DRAFT')::int AS draft_count,
                    COUNT(*) FILTER (WHERE COALESCE(r.status, '')='TO_VALIDATE')::int AS to_validate_count,
                    COUNT(*) FILTER (WHERE COALESCE(r.status, '')='REJECT')::int AS reject_count
                FROM documents d
                LEFT JOIN requirements r ON r.doc_id = d.doc_id
                {where_sql}
                GROUP BY d.doc_id, d.title, d.source, d.document_family, d.created_at
                ORDER BY d.created_at DESC NULLS LAST
                LIMIT %s OFFSET %s
                """,
                tuple(params_rows),
            )
            cols = [x[0] for x in cur.description]
            items = [dict(zip(cols, row)) for row in cur.fetchall()]
    return {"total": total, "limit": int(limit), "offset": int(offset), "items": items}


def _fetch_document_detail(doc_id: str, tenant_id: str) -> dict[str, Any]:
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    d.doc_id::text,
                    COALESCE(d.title, '') AS title,
                    COALESCE(d.source, '') AS source,
                    COALESCE(d.document_family, '') AS document_family,
                    COALESCE(d.jurisdiction, '') AS jurisdiction,
                    COALESCE(d.file_path, '') AS file_path,
                    COALESCE(d.created_at::text, '') AS created_at
                FROM documents d
                WHERE d.doc_id = %s::uuid
                  AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                """,
                (doc_id, tenant_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Document introuvable")

            cur.execute(
                """
                SELECT COUNT(*)::int
                FROM articles
                WHERE doc_id = %s::uuid
                """,
                (doc_id,),
            )
            articles_count = int(cur.fetchone()[0] or 0)

            cur.execute(
                """
                SELECT COUNT(*)::int
                FROM document_pages
                WHERE doc_id = %s::uuid
                """,
                (doc_id,),
            )
            pages_count = int(cur.fetchone()[0] or 0)

            cur.execute(
                """
                SELECT
                    COUNT(*)::int AS requirements_total,
                    COUNT(*) FILTER (WHERE COALESCE(status, '')='PROMOTED')::int AS promoted_count,
                    COUNT(*) FILTER (WHERE COALESCE(status, '')='DRAFT')::int AS draft_count,
                    COUNT(*) FILTER (WHERE COALESCE(status, '')='TO_VALIDATE')::int AS to_validate_count,
                    COUNT(*) FILTER (WHERE COALESCE(status, '')='REJECT')::int AS reject_count,
                    AVG(COALESCE(confidence, 0))::float AS avg_confidence
                FROM requirements
                WHERE doc_id = %s::uuid
                """,
                (doc_id,),
            )
            req = cur.fetchone()

            cur.execute(
                """
                SELECT COALESCE(page_text, '')
                FROM document_pages
                WHERE doc_id = %s::uuid
                ORDER BY page_no ASC
                LIMIT 1
                """,
                (doc_id,),
            )
            preview_row = cur.fetchone()
            preview = str(preview_row[0] or "")[:2500] if preview_row else ""

    gate = classify_document_policy(
        doc_title=str(row[1] or ""),
        doc_text_preview=preview,
        article_count=int(articles_count),
    ).to_dict()

    timeline = [
        {"step": "INGESTION", "status": "DONE"},
        {"step": "SEGMENTATION", "status": "DONE"},
        {"step": "DOC_GATE", "status": "DONE", "policy": gate.get("policy")},
        {"step": "EXTRACTION", "status": "DONE"},
        {"step": "POSTCALL", "status": "DONE"},
        {"step": "QSE_ENRICHMENT", "status": "DONE"},
    ]

    return {
        "doc_id": row[0],
        "title": row[1],
        "source": row[2],
        "document_family": row[3],
        "jurisdiction": row[4],
        "file_path": row[5],
        "created_at": row[6],
        "pages_count": pages_count,
        "articles_count": articles_count,
        "requirements_total": int(req[0] or 0),
        "promoted_count": int(req[1] or 0),
        "draft_count": int(req[2] or 0),
        "to_validate_count": int(req[3] or 0),
        "reject_count": int(req[4] or 0),
        "avg_confidence": float(req[5] or 0.0),
        "doc_gate": gate,
        "timeline": timeline,
    }


def _fetch_requirements_page(
    *,
    limit: int,
    offset: int,
    tenant_id: str,
    req_type: str,
    status: str,
    qse_domain: str,
    qse_sub_domain: str,
    doc_id: str,
    search: str,
    min_conf: float | None,
    max_conf: float | None,
) -> dict[str, Any]:
    clauses: list[str] = ["LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)"]
    params: list[Any] = [tenant_id]

    if str(req_type or "").strip():
        clauses.append("COALESCE(r.req_type, '') = %s")
        params.append(str(req_type).strip())
    if str(status or "").strip():
        clauses.append("COALESCE(r.status, '') = %s")
        params.append(str(status).strip())
    if str(qse_domain or "").strip():
        clauses.append("COALESCE(r.qse_domain, '') = %s")
        params.append(str(qse_domain).strip())
    if str(qse_sub_domain or "").strip():
        clauses.append("COALESCE(r.qse_sub_domain, '') = %s")
        params.append(str(qse_sub_domain).strip())
    if str(doc_id or "").strip():
        clauses.append("r.doc_id = %s::uuid")
        params.append(str(doc_id).strip())
    if str(search or "").strip():
        tok = f"%{str(search).strip()}%"
        clauses.append(
            "("
            "COALESCE(r.requirement_text, '') ILIKE %s OR "
            "COALESCE(d.title, '') ILIKE %s OR "
            "COALESCE(a.article_label, '') ILIKE %s"
            ")"
        )
        params.extend([tok, tok, tok])
    if min_conf is not None:
        clauses.append("COALESCE(r.confidence, 0) >= %s")
        params.append(float(min_conf))
    if max_conf is not None:
        clauses.append("COALESCE(r.confidence, 0) <= %s")
        params.append(float(max_conf))

    where_sql = ""
    if clauses:
        where_sql = "WHERE " + " AND ".join(clauses)

    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM requirements r
                LEFT JOIN documents d ON d.doc_id = r.doc_id
                LEFT JOIN articles a ON a.article_id = r.article_id
                {where_sql}
                """,
                tuple(params),
            )
            total = int(cur.fetchone()[0] or 0)

            params_rows = list(params) + [int(limit), int(offset)]
            cur.execute(
                f"""
                SELECT
                    r.requirement_id::text AS requirement_id,
                    r.requirement_no,
                    COALESCE(r.req_type, '') AS req_type,
                    COALESCE(r.requirement_text, '') AS requirement_text,
                    COALESCE(r.status, '') AS status,
                    COALESCE(r.confidence, 0)::float AS confidence,
                    r.grounding_score,
                    r.quality_score,
                    COALESCE(r.normative_strength, 'IMPERATIF') AS normative_strength,
                    COALESCE(r.extraction_source, '') AS extraction_source,
                    CASE
                        WHEN COALESCE(r.extraction_source, '') = 'human_edit'
                             OR EXISTS (
                                 SELECT 1 FROM requirement_validations v_edit
                                 WHERE v_edit.requirement_id = r.requirement_id
                                   AND v_edit.decision = 'EDIT'
                             )
                            THEN 'HUMAN_EDITED'
                        ELSE ''
                    END AS human_validation_flag,
                    COALESCE(r.qse_domain, '') AS qse_domain,
                    COALESCE(r.qse_sub_domain, '') AS qse_sub_domain,
                    COALESCE(r.citation_ref, '') AS citation_ref,
                    COALESCE(r.citation_snippet, '') AS citation_snippet,
                    COALESCE(d.doc_id::text, '') AS doc_id,
                    COALESCE(d.title, '') AS document_title,
                    COALESCE(d.source, '') AS source,
                    COALESCE(d.document_family, '') AS document_family,
                    COALESCE(r.extracted_at::text, d.created_at::text, '') AS extracted_at,
                    COALESCE(a.article_label, a.article_code, '(no_label)') AS article_label
                FROM requirements r
                LEFT JOIN documents d ON d.doc_id = r.doc_id
                LEFT JOIN articles a ON a.article_id = r.article_id
                {where_sql}
                ORDER BY d.created_at DESC NULLS LAST, r.requirement_id DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params_rows),
            )
            cols = [x[0] for x in cur.description]
            items = [dict(zip(cols, row)) for row in cur.fetchall()]
    return {"total": total, "limit": int(limit), "offset": int(offset), "items": items}


def _fetch_requirement_validation_context(req_id: str, tenant_id: str) -> dict[str, Any] | None:
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    r.requirement_id::text,
                    COALESCE(r.req_type, '')                        AS req_type,
                    COALESCE(r.requirement_text, '')                AS requirement_text,
                    COALESCE(r.status, '')                          AS status,
                    COALESCE(r.confidence, 0)::float                AS confidence,
                    COALESCE(r.normative_strength, 'IMPERATIF')     AS normative_strength,
                    COALESCE(r.citation_snippet, '')                AS source_snippet,
                    COALESCE(r.citation_ref, '')                    AS source_article,
                    COALESCE(r.qse_domain, '')                      AS qse_domain,
                    COALESCE(r.qse_sub_domain, '')                  AS qse_sub_domain,
                    COALESCE(r.quality_score, 0)::float             AS quality_score,
                    COALESCE(r.grounding_score, 0)::float           AS grounding_score,
                    COALESCE(r.extraction_source, '')               AS extraction_source,
                    CASE
                        WHEN COALESCE(r.extraction_source, '') = 'human_edit'
                             OR EXISTS (
                                 SELECT 1 FROM requirement_validations v_edit
                                 WHERE v_edit.requirement_id = r.requirement_id
                                   AND v_edit.decision = 'EDIT'
                             )
                            THEN 'HUMAN_EDITED'
                        ELSE ''
                    END                                             AS human_validation_flag,
                    COALESCE(d.title, '')                           AS document_title,
                    COALESCE(a.article_label, a.article_code, '')   AS article_label,
                    CASE
                        WHEN COALESCE(r.status, '') NOT IN ('TO_VALIDATE', 'DRAFT')
                            THEN ''
                        WHEN COALESCE(r.status, '') = 'DRAFT'
                            THEN 'PRE_VALIDATED_AUTO'
                        WHEN COALESCE(r.quality_score, 0) < 0.84
                             AND COALESCE(r.grounding_score, 0) < 0.72
                            THEN 'QUALITY_AND_GROUNDING_TOO_LOW'
                        WHEN COALESCE(r.quality_score, 0) < 0.84
                            THEN 'QUALITY_TOO_LOW'
                        WHEN COALESCE(r.grounding_score, 0) < 0.72
                            THEN 'GROUNDING_TOO_WEAK'
                        WHEN COALESCE(r.confidence, 0) < 0.65
                            THEN 'LOW_CONFIDENCE'
                        ELSE 'POLICY_OR_TYPE'
                    END                                             AS promotion_blocked_reason
                FROM requirements r
                LEFT JOIN documents d ON d.doc_id = r.doc_id
                LEFT JOIN articles a ON a.article_id = r.article_id
                WHERE r.requirement_id = %s::uuid
                  AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                LIMIT 1
                """,
                (req_id, tenant_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            item = dict(zip(cols, row))
            cur.execute(
                """
                SELECT r2.requirement_id::text, COALESCE(r2.requirement_text, '')
                FROM requirements r2
                JOIN documents d2 ON d2.doc_id = r2.doc_id
                WHERE r2.requirement_id != %s::uuid
                  AND LOWER(COALESCE(d2.tenant_id,'')) = LOWER(%s)
                  AND COALESCE(r2.status,'') IN ('DRAFT','PROMOTED')
                  AND LEFT(LOWER(COALESCE(r2.requirement_text,'')), 80)
                      = LEFT(LOWER(%s), 80)
                LIMIT 3
                """,
                (req_id, tenant_id, item.get("requirement_text") or ""),
            )
            rows = cur.fetchall()
            item["similar_existing"] = [
                {
                    "req_id": r[0],
                    "text": str(r[1] or "")[:120],
                    "similarity": _text_similarity(item.get("requirement_text"), r[1]),
                }
                for r in rows
            ]
            review_structure = _infer_requirement_review_structure(
                item.get("requirement_text"),
                item.get("source_snippet"),
            )
            item["review_structure"] = review_structure
            item["review_guidance"] = _build_review_guidance(
                item,
                review_structure,
                item["similar_existing"],
            )
            recommended_decision, recommended_reason = _recommend_review_decision(
                item,
                review_structure,
                item["similar_existing"],
            )
            item["review_recommended_decision"] = recommended_decision
            item["review_recommended_reason"] = recommended_reason
            return item


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return {}


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


def _get_events_column_info(cur: Any) -> dict[str, str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public'
          AND table_name='events'
        """
    )
    columns = {str(row[0]) for row in cur.fetchall() if row and row[0]}
    payload_column = next(
        (name for name in ("payload_json", "payload", "event_payload") if name in columns),
        "",
    )
    created_at_column = "created_at" if "created_at" in columns else ""
    event_id_column = next((name for name in ("event_id", "id") if name in columns), "")
    return {
        "payload_column": payload_column,
        "created_at_column": created_at_column,
        "event_id_column": event_id_column,
    }


def _fetch_latest_event_payload(
    cur: Any,
    *,
    tenant_id: str,
    doc_id: str,
    event_type: str,
) -> dict[str, Any]:
    column_info = _get_events_column_info(cur)
    payload_column = column_info["payload_column"]
    if not payload_column:
        return {}

    created_at_expr = column_info["created_at_column"] or "NULL"
    order_expr = column_info["created_at_column"] or column_info["event_id_column"] or "doc_id"
    cur.execute(
        f"""
        SELECT {payload_column} AS payload_value, {created_at_expr} AS created_at
        FROM events
        WHERE LOWER(COALESCE(tenant_id, '')) = LOWER(%s)
          AND doc_id = %s::uuid
          AND event_type = %s
          AND {payload_column} IS NOT NULL
        ORDER BY {order_expr} DESC
        LIMIT 1
        """,
        (tenant_id, doc_id, event_type),
    )
    row = cur.fetchone()
    if not row:
        return {}
    payload = _parse_event_json_value(row[0])
    if not isinstance(payload, dict):
        return {}
    created_at = row[1]
    if hasattr(created_at, "isoformat"):
        payload["_event_created_at"] = created_at.isoformat()
    elif created_at:
        payload["_event_created_at"] = str(created_at)
    return payload


def _build_latest_extraction_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    if not source:
        return {}

    llm_usage = dict(source.get("llm_usage_totals") or {})
    llm_cache = dict(source.get("llm_cache_stats") or {})
    llm_availability = dict(source.get("llm_availability_totals") or {})
    return {
        "generated_at": str(source.get("_event_created_at") or ""),
        "requirements_inserted": int(source.get("requirements_inserted") or 0),
        "raw_llm_requirements": int(source.get("raw_llm_requirements") or 0),
        "chunks_ok": int(source.get("chunks_ok") or 0),
        "chunks_empty": int(source.get("chunks_empty") or 0),
        "chunks_error": int(source.get("chunks_error") or 0),
        "precall_runtime_mode": str(source.get("precall_runtime_mode") or ""),
        "precall_runtime_chunks_total": int(source.get("precall_runtime_chunks_total") or 0),
        "precall_runtime_units_high_total": int(source.get("precall_runtime_units_high_total") or 0),
        "precall_runtime_units_low_total": int(source.get("precall_runtime_units_low_total") or 0),
        "precall_runtime_units_drop_total": int(source.get("precall_runtime_units_drop_total") or 0),
        "runtime_unit_deduped_total": int(source.get("runtime_unit_deduped_total") or 0),
        "runtime_unit_cache_hits_total": int(source.get("runtime_unit_cache_hits_total") or 0),
        "runtime_unit_cache_store_total": int(source.get("runtime_unit_cache_store_total") or 0),
        "runtime_budget_blocked_units_total": int(source.get("runtime_budget_blocked_units_total") or 0),
        "runtime_budget_fallback_units_total": int(source.get("runtime_budget_fallback_units_total") or 0),
        "error_memory_storage_source": str(source.get("error_memory_storage_source") or ""),
        "error_memory_loaded_total": int(source.get("error_memory_loaded_total") or 0),
        "error_memory_signals_total": int(source.get("error_memory_signals_total") or 0),
        "error_memory_hits_total": int(source.get("error_memory_hits_total") or 0),
        "error_memory_fix_applied_total": int(source.get("error_memory_fix_applied_total") or 0),
        "promoted_to_draft_total": int(source.get("promoted_to_draft_total") or 0),
        "promotion_reviewed_to_validate_total": int(source.get("promotion_reviewed_to_validate_total") or 0),
        "policy_forced_to_validate_total": int(source.get("policy_forced_to_validate_total") or 0),
        "llm_usage_totals": {
            "llm_calls": int(llm_usage.get("llm_calls") or 0),
            "prompt_tokens": int(llm_usage.get("prompt_tokens") or 0),
            "completion_tokens": int(llm_usage.get("completion_tokens") or 0),
            "total_tokens": int(llm_usage.get("total_tokens") or 0),
            "estimated_cost_usd": float(llm_usage.get("estimated_cost_usd") or 0.0),
        },
        "llm_cache_stats": {
            "cache_enabled": bool(llm_cache.get("cache_enabled")),
            "cache_hits_total": int(llm_cache.get("cache_hits_total") or 0),
            "cache_hits_strict_total": int(llm_cache.get("cache_hits_strict_total") or 0),
            "cache_hits_relaxed_total": int(llm_cache.get("cache_hits_relaxed_total") or 0),
            "cache_negative_hits_total": int(llm_cache.get("cache_negative_hits_total") or 0),
            "cache_lookup_total": int(llm_cache.get("cache_lookup_total") or 0),
            "cache_misses_total": int(llm_cache.get("cache_misses_total") or 0),
        },
        "llm_availability_totals": llm_availability,
    }


def _fetch_analytics_overview(tenant_id: str) -> dict[str, Any]:
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(req_type, 'UNKNOWN') AS k, COUNT(*)::int AS c
                FROM requirements r
                JOIN documents d ON d.doc_id = r.doc_id
                WHERE LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                GROUP BY COALESCE(req_type, 'UNKNOWN')
                ORDER BY c DESC, k ASC
                """,
                (tenant_id,),
            )
            by_type = {str(k): int(v) for (k, v) in cur.fetchall()}

            cur.execute(
                """
                SELECT COALESCE(status, 'UNKNOWN') AS k, COUNT(*)::int AS c
                FROM requirements r
                JOIN documents d ON d.doc_id = r.doc_id
                WHERE LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                GROUP BY COALESCE(status, 'UNKNOWN')
                ORDER BY c DESC, k ASC
                """,
                (tenant_id,),
            )
            by_status = {str(k): int(v) for (k, v) in cur.fetchall()}

            cur.execute(
                """
                SELECT COALESCE(qse_domain, 'UNKNOWN') AS k, COUNT(*)::int AS c
                FROM requirements r
                JOIN documents d ON d.doc_id = r.doc_id
                WHERE LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                GROUP BY COALESCE(qse_domain, 'UNKNOWN')
                ORDER BY c DESC, k ASC
                """,
                (tenant_id,),
            )
            by_domain = {str(k): int(v) for (k, v) in cur.fetchall()}

            cur.execute(
                """
                SELECT COALESCE(qse_sub_domain, 'UNKNOWN') AS k, COUNT(*)::int AS c
                FROM requirements r
                JOIN documents d ON d.doc_id = r.doc_id
                WHERE LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                GROUP BY COALESCE(qse_sub_domain, 'UNKNOWN')
                ORDER BY c DESC, k ASC
                LIMIT 20
                """,
                (tenant_id,),
            )
            by_sub_domain = {str(k): int(v) for (k, v) in cur.fetchall()}

            cur.execute(
                """
                SELECT
                    COALESCE(d.title, '(sans titre)') AS title,
                    COUNT(r.requirement_id)::int AS requirements_count
                FROM documents d
                LEFT JOIN requirements r ON r.doc_id = d.doc_id
                WHERE LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                GROUP BY d.doc_id, d.title
                ORDER BY requirements_count DESC, title ASC
                LIMIT 12
                """,
                (tenant_id,),
            )
            top_documents = [{"title": str(t), "requirements_count": int(c)} for (t, c) in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    to_char(date_trunc('day', d.created_at), 'YYYY-MM-DD') AS day,
                    COUNT(DISTINCT d.doc_id)::int AS docs_count,
                    COUNT(r.requirement_id)::int AS requirements_count
                FROM documents d
                LEFT JOIN requirements r ON r.doc_id = d.doc_id
                WHERE LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                GROUP BY date_trunc('day', d.created_at)
                ORDER BY date_trunc('day', d.created_at) DESC
                LIMIT 30
                """,
                (tenant_id,),
            )
            trend_rows = [{"day": str(day), "docs_count": int(dc), "requirements_count": int(rc)} for (day, dc, rc) in cur.fetchall()]
    trend_rows.reverse()

    quality = _load_json(Path("reports/preflight/a1_real_extraction_benchmark_latest.json"))
    return {
        "by_type": by_type,
        "by_status": by_status,
        "by_domain": by_domain,
        "by_sub_domain": by_sub_domain,
        "top_documents": top_documents,
        "trend": trend_rows,
        "quality_snapshot": {
            "docs_success": quality.get("docs_success"),
            "to_validate_share_avg": quality.get("to_validate_share_avg"),
            "current_draft_share": quality.get("current_draft_share"),
            "raw_to_final_conversion_rate": quality.get("raw_to_final_conversion_rate"),
            "doc_gate_policy_counts": quality.get("doc_gate_policy_counts") or {},
            "drop_share_by_reason_code": quality.get("drop_share_by_reason_code") or {},
        },
    }


def _list_reports(category: str, limit: int) -> list[dict[str, Any]]:
    roots: list[tuple[str, Path]] = [
        ("preflight", Path("reports/preflight")),
        ("registry", Path("reports/registry")),
        ("history", Path("reports/history")),
    ]
    cat = str(category or "").strip().lower()
    if cat:
        roots = [item for item in roots if item[0] == cat]

    items: list[dict[str, Any]] = []
    for bucket, root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            suffix = p.suffix.lower()
            if suffix not in {".json", ".md", ".csv"}:
                continue
            st = p.stat()
            rel = p.as_posix()
            items.append(
                {
                    "category": bucket,
                    "name": p.name,
                    "path": rel,
                    "size_bytes": int(st.st_size),
                    "updated_at": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
                    "ext": suffix,
                }
            )
    items.sort(key=lambda it: str(it.get("updated_at", "")), reverse=True)
    return items[: int(limit)]


def _fetch_family_analytics(tenant_id: str) -> list[dict[str, Any]]:
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(d.document_family, 'UNKNOWN') AS family,
                    COUNT(DISTINCT d.doc_id)::int AS documents_count,
                    COUNT(r.requirement_id)::int AS requirements_count,
                    COUNT(*) FILTER (WHERE COALESCE(r.status, '')='DRAFT')::int AS draft_count,
                    COUNT(*) FILTER (WHERE COALESCE(r.status, '')='TO_VALIDATE')::int AS to_validate_count,
                    COUNT(*) FILTER (WHERE COALESCE(r.status, '')='REJECT')::int AS reject_count,
                    AVG(COALESCE(r.confidence, 0))::float AS avg_confidence
                FROM documents d
                LEFT JOIN requirements r ON r.doc_id = d.doc_id
                WHERE LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                GROUP BY COALESCE(d.document_family, 'UNKNOWN')
                ORDER BY documents_count DESC, family ASC
                """,
                (tenant_id,),
            )
            cols = [x[0] for x in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _load_gate_overview() -> dict[str, Any]:
    path = Path("reports/preflight/a1_real_extraction_benchmark_latest.json")
    if not path.exists():
        return {"doc_gate_policy_counts": {}, "drop_share_by_reason_code": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            "doc_gate_policy_counts": payload.get("doc_gate_policy_counts") or {},
            "drop_share_by_reason_code": payload.get("drop_share_by_reason_code") or {},
        }
    except Exception:
        return {"doc_gate_policy_counts": {}, "drop_share_by_reason_code": {}}


def _ensure_validation_table() -> None:
    """Create requirement_validations table if it doesn't exist (idempotent)."""
    try:
        dsn = _load_env_dsn()
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS requirement_validations (
                        validation_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                        requirement_id     UUID        NOT NULL
                            REFERENCES requirements(requirement_id) ON DELETE CASCADE,
                        validator_username TEXT        NOT NULL,
                        validator_role     TEXT        NOT NULL,
                        decision           TEXT        NOT NULL CHECK (decision IN ('APPROVE','REJECT','EDIT','FLAG')),
                        comment            TEXT,
                        rejection_reason   TEXT,
                        original_text      TEXT,
                        created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_req_validations_req_id
                        ON requirement_validations(requirement_id)
                    """
                )
                # Migration: ajoute la FK si la table existait deja sans elle
                cur.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.table_constraints
                            WHERE constraint_name = 'requirement_validations_requirement_id_fkey'
                              AND table_name = 'requirement_validations'
                        ) THEN
                            ALTER TABLE requirement_validations
                                ADD CONSTRAINT requirement_validations_requirement_id_fkey
                                FOREIGN KEY (requirement_id)
                                REFERENCES requirements(requirement_id)
                                ON DELETE CASCADE;
                        END IF;
                    END
                    $$;
                """)
                # Backfill idempotent: corrige l'historique DRAFT -> PROMOTED
                # quand la derniere validation humaine est APPROVE.
                cur.execute("""
                    WITH latest_validation AS (
                        SELECT DISTINCT ON (v.requirement_id)
                               v.requirement_id,
                               v.decision
                          FROM requirement_validations v
                      ORDER BY v.requirement_id, v.created_at DESC, v.validation_id DESC
                    )
                    UPDATE requirements r
                       SET status = 'PROMOTED'
                      FROM latest_validation lv
                     WHERE r.requirement_id = lv.requirement_id
                       AND r.status = 'DRAFT'
                       AND lv.decision = 'APPROVE'
                """)
            conn.commit()
    except Exception:
        pass  # Non-fatal at startup - DB may not be reachable yet


_restore_jobs_from_disk()
_reconciled_jobs = _reconcile_orphan_jobs_on_boot()
if _reconciled_jobs:
    print(f"[A1 API] { _reconciled_jobs } run(s) actifs reconcilies en FAILED apres redemarrage.", flush=True)
_ensure_validation_table()
_bootstrap_local_auth_users()


app = FastAPI(title="QALITAS AI API", version="1.0.0")
_ALLOWED_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    # Ajoutez ici le domaine de production si deploye (ex: https://qalitas.gds.tn)
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)


@app.middleware("http")
async def _reset_tenant_db_context(request: Request, call_next: Any) -> Any:
    clear_request_tenant()
    try:
        response = await call_next(request)
        return response
    finally:
        clear_request_tenant()

if Path("frontend").exists():
    app.mount("/ui", NoCacheStaticFiles(directory="frontend", html=True), name="ui")


# ---------------------------------------------------------------------------
# Routes publiques et authentification
# - /health et / redirigent vers l'etat API et l'interface.
# - /auth/* gere login, logout, utilisateur courant et changement de tenant.
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "qalitas-api"}


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui/")


@app.post("/api/v1/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    _purge_expired_tokens()  # Nettoyage passif a chaque login
    load_dotenv()
    username = str(payload.username or "").strip().lower()
    password = str(payload.password or "")
    default_tenant = os.getenv("AUTH_DEFAULT_TENANT") or os.getenv("QALITAS_TENANT") or "tenant_demo"
    explicit_tenant = _parse_tenant_id(payload.tenant_id or "", field_name="tenant_id", allow_empty=True)
    try:
        if explicit_tenant:
            user = _load_auth_user(username, explicit_tenant)
        else:
            user = _load_auth_user_without_tenant(username)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=500, detail="Authentification indisponible") from exc
    if not user or not bool(user.get("is_active")) or not _verify_password(password, str(user.get("password_hash") or "")):
        raise HTTPException(status_code=401, detail="Identifiants invalides")
    token = _issue_token(username)
    expires_at = (datetime.now(UTC) + timedelta(hours=TOKEN_TTL_HOURS)).isoformat()
    resolved_tenant = str(user.get("tenant_id") or explicit_tenant or default_tenant)
    home_tenant_id = resolved_tenant
    active_tenant_id = home_tenant_id
    is_super_admin = str(user.get("role") or "").strip().upper() == "SUPER_ADMIN"
    company_name = _tenant_label_for_session(active_tenant_id)
    session: dict[str, Any] = {
        "user_id": str(user.get("user_id") or ""),
        "username": username,
        "role": str(user.get("role") or ""),
        "display_name": str(user.get("display_name") or username),
        "company_name": company_name,
        "tenant_id": active_tenant_id,
        "home_tenant_id": home_tenant_id,
        "active_tenant_id": active_tenant_id,
        "is_super_admin": is_super_admin,
        "created_at": _now_iso(),
        "expires_at": expires_at,
    }
    with AUTH_LOCK:
        AUTH_TOKENS[token] = session
    return LoginResponse(
        access_token=token,
        username=username,
        role=session["role"],
        display_name=session["display_name"],
        company_name=company_name,
        tenant_id=active_tenant_id,
        home_tenant_id=home_tenant_id,
        active_tenant_id=active_tenant_id,
        is_super_admin=is_super_admin,
    )


@app.post("/api/v1/auth/logout")
def logout(authorization: str | None = Header(default=None)) -> dict[str, str]:
    """Invalide le token cote serveur (revocation immediate)."""
    raw = str(authorization or "").strip()
    if raw.lower().startswith("bearer "):
        token = raw.split(" ", 1)[1].strip()
        with AUTH_LOCK:
            AUTH_TOKENS.pop(token, None)
    return {"status": "ok", "message": "Session terminee"}


@app.get("/api/v1/auth/me")
def auth_me(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _auth_from_header(authorization)


@app.post("/api/v1/auth/switch-tenant", response_model=SwitchTenantResponse)
def switch_active_tenant(
    payload: SwitchTenantRequest,
    authorization: str | None = Header(default=None),
) -> SwitchTenantResponse:
    raw = str(authorization or "").strip()
    if not raw.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authorization bearer requis")
    token = raw.split(" ", 1)[1].strip()
    session = _auth_from_header(authorization)
    if not _is_super_admin(session):
        raise HTTPException(status_code=403, detail="Seul SUPER_ADMIN peut changer de tenant actif")

    target_tenant = _parse_tenant_id(payload.tenant_id, field_name="tenant_id")
    if not _tenant_exists(target_tenant):
        raise HTTPException(status_code=404, detail=f"Tenant introuvable: {target_tenant}")

    with AUTH_LOCK:
        current = AUTH_TOKENS.get(token)
        if not current:
            raise HTTPException(status_code=401, detail="Token invalide ou expiré")
        company_name = _tenant_label_for_session(target_tenant)
        current["active_tenant_id"] = target_tenant
        current["tenant_id"] = target_tenant
        current["company_name"] = company_name
        current["is_super_admin"] = True
        AUTH_TOKENS[token] = current
        updated = dict(current)

    return SwitchTenantResponse(
        username=str(updated.get("username") or ""),
        role=str(updated.get("role") or ""),
        display_name=str(updated.get("display_name") or ""),
        company_name=str(updated.get("company_name") or ""),
        tenant_id=target_tenant,
        home_tenant_id=_session_home_tenant(updated),
        active_tenant_id=target_tenant,
        is_super_admin=True,
    )


# ---------------------------------------------------------------------------
# Routes pipeline / jobs
# Ces endpoints lancent, suivent, arretent ou reprennent les traitements longs:
# ingestion/extraction A1, applicabilite A2, conformite A3 et indexation A4.
# ---------------------------------------------------------------------------

@app.post("/api/v1/runs", response_model=RunResponse)
async def create_run(
    background_tasks: BackgroundTasks,
    pdf: UploadFile = File(...),
    tenant: str = Form(""),
    title: str = Form(""),
    source: str = Form("jort_manual"),
    jurisdiction: str = Form("TN"),
    document_family: str = Form("REGLEMENTAIRE"),
    extract_limit: int = Form(0),
    sleep_ms: int = Form(0),
    on_duplicate: str = Form("reuse"),
    timeout_ingest_sec: int = Form(900),
    timeout_segment_sec: int = Form(7200),
    timeout_extract_sec: int = Form(3600),
    timeout_backfill_sec: int = Form(900),
    timeout_export_sec: int = Form(900),
    max_chars: int = Form(1200),
    enable_qse_backfill: str = Form("on"),
    registry_outdir: str = Form("reports/registry"),
    run_applicability: bool = Form(False),
    run_compliance: bool = Form(False),
    run_embedding: bool = Form(False),
    authorization: str | None = Header(default=None),
) -> RunResponse:
    session = _auth_from_header(authorization)
    _require_role(session, WRITE_ROLES, "lancer un pipeline d'extraction")
    tenant = _require_tenant_access(session, tenant, field_name="tenant")
    if not pdf.filename:
        raise HTTPException(status_code=400, detail="PDF manquant")
    if not str(pdf.filename).lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Le fichier doit etre un PDF")

    job_id = str(uuid.uuid4())
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = _sanitize_filename(pdf.filename)
    requested_title = str(title or "").strip()
    inferred_title = Path(safe_name).stem
    effective_title = requested_title or inferred_title
    timeout_segment_sec = max(300, int(timeout_segment_sec))
    timeout_extract_sec = max(600, int(timeout_extract_sec))
    pipeline_plan = _build_pipeline_plan(
        run_applicability=run_applicability,
        run_compliance=run_compliance,
        run_embedding=run_embedding,
    )

    active_conflict = _find_active_a1_run(
        tenant=tenant,
        title=effective_title,
        file_name=safe_name,
    )
    if active_conflict:
        conflict_id = str(active_conflict.get("job_id") or "")
        conflict_status = str(active_conflict.get("status") or "RUNNING")
        conflict_updated = str(active_conflict.get("updated_at") or active_conflict.get("created_at") or "")
        raise HTTPException(
            status_code=409,
            detail=(
                "Un run A1 est deja actif pour ce document "
                f"(job_id={conflict_id}, status={conflict_status}, maj={conflict_updated}). "
                "Attendre sa fin ou annuler le run precedent."
            ),
        )

    saved_pdf = UPLOADS_DIR / f"{job_id}_{safe_name}"
    raw_bytes = await pdf.read()
    saved_pdf.write_bytes(raw_bytes)

    job_payload = {
        "job_id": job_id,
        "type": "extraction",
        "status": "QUEUED",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "tenant": tenant,
        "tenant_id": tenant,
        "file_name": safe_name,
        "title": effective_title,
        "pdf_path": str(saved_pdf.resolve()),
        "doc_id": None,
        "error_category": None,
        "failed_step": None,
        "stop_requested": False,
        "pipeline_plan": pipeline_plan,
        "pipeline_steps": _build_pipeline_steps(pipeline_plan),
        "current_stage": "A1",
        "stage_message": "Extraction en attente",
        "run_params": {
            "tenant": tenant,
            "title": effective_title,
            "source": source,
            "jurisdiction": jurisdiction,
            "document_family": document_family,
            "extract_limit": int(extract_limit),
            "sleep_ms": int(sleep_ms),
            "on_duplicate": on_duplicate,
            "timeout_ingest_sec": int(timeout_ingest_sec),
            "timeout_segment_sec": int(timeout_segment_sec),
            "timeout_extract_sec": int(timeout_extract_sec),
            "timeout_backfill_sec": int(timeout_backfill_sec),
            "timeout_export_sec": int(timeout_export_sec),
            "max_chars": int(max_chars),
            "enable_qse_backfill": enable_qse_backfill,
            "registry_outdir": registry_outdir,
            "pdf_path": str(saved_pdf.resolve()),
            "run_applicability": bool(pipeline_plan.get("A2")),
            "run_compliance": bool(pipeline_plan.get("A3")),
            "run_embedding": bool(pipeline_plan.get("EMB")),
        },
    }
    with JOBS_LOCK:
        JOBS[job_id] = job_payload
        _persist_jobs()

    background_tasks.add_task(
        _run_pipeline_job,
        job_id=job_id,
        pdf_path=str(saved_pdf.resolve()),
        tenant=tenant,
        title=effective_title,
        source=source,
        jurisdiction=jurisdiction,
        document_family=document_family,
        extract_limit=int(extract_limit),
        sleep_ms=int(sleep_ms),
        on_duplicate=on_duplicate,
        timeout_ingest_sec=int(timeout_ingest_sec),
        timeout_segment_sec=timeout_segment_sec,
        timeout_extract_sec=timeout_extract_sec,
        timeout_backfill_sec=int(timeout_backfill_sec),
        timeout_export_sec=int(timeout_export_sec),
        max_chars=int(max_chars),
        enable_qse_backfill=enable_qse_backfill,
        registry_outdir=registry_outdir,
        run_applicability=bool(pipeline_plan.get("A2")),
        run_compliance=bool(pipeline_plan.get("A3")),
        run_embedding=bool(pipeline_plan.get("EMB")),
    )

    return RunResponse(
        job_id=job_id,
        status="QUEUED",
        message="Extraction lancee",
        error_category=None,
        failed_step=None,
        current_stage="A1",
        stage_message="Extraction en attente",
        pipeline_plan=pipeline_plan,
        pipeline_steps=_build_pipeline_steps(pipeline_plan),
    )


@app.get("/api/v1/runs")
def list_runs(
    limit: int = Query(default=25, ge=1, le=500),
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_norm = _require_tenant_access(session, tenant_id)
    values = _normalized_jobs_for_tenant(tenant_norm)
    values.sort(key=lambda it: str(it.get("updated_at") or it.get("created_at") or ""), reverse=True)
    return {"count": len(values), "items": values[: int(limit)]}


@app.delete("/api/v1/runs/history")
def delete_runs_history(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_norm = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "supprimer l'historique des runs")

    active_statuses = {"QUEUED", "PENDING", "RUNNING"}
    protected_statuses = {"PAUSED"}
    deleted_runs = 0
    deleted_log_files = 0
    kept_active_runs = 0
    kept_paused_runs = 0
    changed = False

    with JOBS_LOCK:
        to_delete: list[str] = []
        for job_id, item in list(JOBS.items()):
            if _job_tenant_norm(item) != tenant_norm:
                continue
            snapshot, is_changed = _normalized_job_snapshot(item)
            if is_changed:
                JOBS[job_id] = dict(snapshot)
                changed = True
            status = str(snapshot.get("status") or "").strip().upper()
            if status in active_statuses:
                kept_active_runs += 1
                continue
            if status in protected_statuses:
                kept_paused_runs += 1
                continue
            to_delete.append(str(job_id))

        for job_id in to_delete:
            item = dict(JOBS.pop(job_id, {}) or {})
            deleted_log_files += _delete_run_log_artifacts(item)
            deleted_runs += 1

        if to_delete or changed:
            _persist_jobs()

    return {
        "tenant_id": tenant_norm,
        "deleted_runs": deleted_runs,
        "deleted_log_files": deleted_log_files,
        "kept_active_runs": kept_active_runs,
        "kept_paused_runs": kept_paused_runs,
    }


@app.get("/api/v1/runs/{job_id}", response_model=RunResponse)
def get_run(
    job_id: str,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> RunResponse:
    session = _auth_from_header(authorization)
    tenant_norm = _require_tenant_access(session, tenant_id)
    item = _normalized_job_for_tenant(job_id, tenant_norm)
    if not item:
        raise HTTPException(status_code=404, detail="job_id introuvable")
    status = str(item.get("status") or "UNKNOWN")
    stage = str(item.get("current_stage") or "").strip().upper()
    stage_message = str(item.get("stage_message") or "").strip()
    label = PIPELINE_STAGE_LABELS.get(stage, "Pipeline")
    msg = stage_message or (f"{label} terminée" if status == "DONE" else f"{label} en cours")
    if status == "FAILED":
        msg = stage_message or f"{label} échouée"
    if status == "PAUSED":
        msg = stage_message or f"{label} en pause"
    return RunResponse(
        job_id=job_id,
        status=status,
        message=msg,
        doc_id=item.get("doc_id"),
        error_category=item.get("error_category"),
        failed_step=item.get("failed_step"),
        current_stage=item.get("current_stage"),
        stage_message=item.get("stage_message"),
        pipeline_plan=item.get("pipeline_plan"),
        pipeline_steps=item.get("pipeline_steps"),
    )


@app.get("/api/v1/runs/{job_id}/details")
def get_run_details(
    job_id: str,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_norm = _require_tenant_access(session, tenant_id)
    item = _normalized_job_for_tenant(job_id, tenant_norm)
    if not item:
        raise HTTPException(status_code=404, detail="job_id introuvable")
    out = dict(item)
    doc_id = str(out.get("doc_id") or "").strip()
    if doc_id:
        try:
            out["document_summary"] = _fetch_document_summary(doc_id, tenant_id=tenant_norm)
        except Exception:
            pass
    return out


@app.post("/api/v1/runs/{job_id}/stop")
def stop_run(
    job_id: str,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Demande l'arrêt gracieux d'un run (A1/A2/A3/Embeddings)."""
    session = _auth_from_header(authorization)
    tenant_norm = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "arrêter un run")

    with JOBS_LOCK:
        item = JOBS.get(job_id)
        if not item or _job_tenant_norm(item) != tenant_norm:
            raise HTTPException(status_code=404, detail="job_id introuvable")

        status = str(item.get("status") or "").strip().upper()
        terminal = {"DONE", "FAILED", "ERROR", "PAUSED", "CANCELLED"}
        if status in terminal:
            return {
                "job_id": job_id,
                "status": status,
                "message": "Run déjà terminé/arrêté.",
            }

        now_iso = _now_iso()
        item["stop_requested"] = True
        item["stop_requested_at"] = now_iso
        item["stop_requested_by"] = str(session.get("username") or "unknown")
        item["updated_at"] = now_iso

        if status in {"QUEUED", "PENDING"}:
            item["status"] = "PAUSED"
            item["finished_at"] = now_iso
            item["error"] = "run mis en pause avant démarrage"
            item["error_category"] = "user_stop"
            item["failed_step"] = "USER_STOP"

        _persist_jobs()
        snapshot = dict(item)

    return {
        "job_id": job_id,
        "status": str(snapshot.get("status") or "UNKNOWN"),
        "stop_requested": bool(snapshot.get("stop_requested")),
        "message": "Demande d'arrêt envoyée.",
    }


@app.post("/api/v1/runs/{job_id}/resume")
def resume_run(
    job_id: str,
    background_tasks: BackgroundTasks,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Relance un run en pause avec les mêmes paramètres."""
    session = _auth_from_header(authorization)
    tenant_norm = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "reprendre un run")
    extraction_task_kwargs: dict[str, Any] | None = None

    with JOBS_LOCK:
        item = JOBS.get(job_id)
        if not item or _job_tenant_norm(item) != tenant_norm:
            raise HTTPException(status_code=404, detail="job_id introuvable")
        status = str(item.get("status") or "").strip().upper()
        if status != "PAUSED":
            raise HTTPException(status_code=409, detail="Seuls les runs en pause peuvent être repris.")

        run_type = _job_type_norm(item)
        if run_type not in {"extraction", "applicability", "compliance", "embedding_index"}:
            raise HTTPException(status_code=400, detail="Reprise non supportée pour ce type de run.")

        params = dict(item.get("run_params") or {})
        params["tenant_id"] = tenant_norm
        params["tenant"] = tenant_norm
        new_job_id = str(uuid.uuid4())

        if run_type == "extraction":
            pdf_path = str(params.get("pdf_path") or item.get("pdf_path") or "").strip()
            if not pdf_path or not Path(pdf_path).exists():
                raise HTTPException(status_code=400, detail="PDF source introuvable pour reprendre ce run A1.")
            title = str(params.get("title") or item.get("title") or Path(pdf_path).stem).strip() or Path(pdf_path).stem
            file_name = str(item.get("file_name") or Path(pdf_path).name).strip()
            pipeline_plan = _build_pipeline_plan(
                run_applicability=_coerce_bool(params.get("run_applicability")),
                run_compliance=_coerce_bool(params.get("run_compliance")),
                run_embedding=_coerce_bool(params.get("run_embedding")),
            )
            JOBS[new_job_id] = {
                "job_id": new_job_id,
                "type": "extraction",
                "status": "QUEUED",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "tenant": tenant_norm,
                "tenant_id": tenant_norm,
                "file_name": file_name,
                "title": title,
                "pdf_path": pdf_path,
                "doc_id": item.get("doc_id"),
                "error_category": None,
                "failed_step": None,
                "stop_requested": False,
                "resumed_from": job_id,
                "pipeline_plan": pipeline_plan,
                "pipeline_steps": _build_pipeline_steps(pipeline_plan),
                "current_stage": "A1",
                "stage_message": "Extraction en attente",
                "run_params": params,
            }
            extraction_task_kwargs = {
                "job_id": new_job_id,
                "pdf_path": pdf_path,
                "tenant": tenant_norm,
                "title": title,
                "source": str(params.get("source") or "jort_manual"),
                "jurisdiction": str(params.get("jurisdiction") or "TN"),
                "document_family": str(params.get("document_family") or "REGLEMENTAIRE"),
                "extract_limit": int(params.get("extract_limit") or 0),
                "sleep_ms": int(params.get("sleep_ms") or 0),
                "on_duplicate": str(params.get("on_duplicate") or "reuse"),
                "timeout_ingest_sec": int(params.get("timeout_ingest_sec") or 900),
                "timeout_segment_sec": max(300, int(params.get("timeout_segment_sec") or 7200)),
                "timeout_extract_sec": max(600, int(params.get("timeout_extract_sec") or 3600)),
                "timeout_backfill_sec": int(params.get("timeout_backfill_sec") or 900),
                "timeout_export_sec": int(params.get("timeout_export_sec") or 900),
                "max_chars": int(params.get("max_chars") or 1200),
                "enable_qse_backfill": str(params.get("enable_qse_backfill") or "on"),
                "registry_outdir": str(params.get("registry_outdir") or "reports/registry"),
                "run_applicability": bool(pipeline_plan.get("A2")),
                "run_compliance": bool(pipeline_plan.get("A3")),
                "run_embedding": bool(pipeline_plan.get("EMB")),
            }
        else:
            JOBS[new_job_id] = {
                "job_id": new_job_id,
                "type": run_type,
                "tenant_id": tenant_norm,
                "doc_id": params.get("doc_id"),
                "status": "PENDING",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "stop_requested": False,
                "resumed_from": job_id,
                "run_params": params,
            }
        item["resumed_by"] = new_job_id
        item["resumed_at"] = _now_iso()
        _persist_jobs()

    if extraction_task_kwargs is not None:
        background_tasks.add_task(_run_pipeline_job, **extraction_task_kwargs)
        return {
            "job_id": new_job_id,
            "status": "QUEUED",
            "tenant_id": tenant_norm,
            "resumed_from": job_id,
            "type": run_type,
        }

    def _run() -> None:
        def _opt_int(value: Any) -> int | None:
            if value is None or str(value).strip() == "":
                return None
            try:
                return int(value)
            except Exception:
                return None

        with JOBS_LOCK:
            current_item = JOBS.get(new_job_id) or {}
            current_status = str(current_item.get("status") or "").upper()
            stop_flag = bool(current_item.get("stop_requested"))
            if current_status == "PAUSED" or stop_flag:
                JOBS[new_job_id].update({"status": "PAUSED", "updated_at": _now_iso(), "stopped_at": _now_iso()})
                _persist_jobs()
                return
            JOBS[new_job_id]["status"] = "RUNNING"
            _persist_jobs()

        try:
            if run_type == "applicability":
                from a2_applicability_engine import run_applicability as _engine
                result = _engine(
                    tenant_id=tenant_norm,
                    doc_id=params.get("doc_id"),
                    limit=_opt_int(params.get("limit")),
                    min_quality=float(params.get("min_quality") or 0.70),
                    delay_between=float(params.get("delay_between") or DEFAULT_A2_DELAY_SECONDS),
                    stop_requested=lambda: _job_stop_requested(new_job_id),
                )
                next_status = "PAUSED" if bool((result or {}).get("stopped")) else "DONE"
                with JOBS_LOCK:
                    JOBS[new_job_id].update({
                        "status": next_status,
                        "result": result,
                        "updated_at": _now_iso(),
                        "finished_at": _now_iso(),
                        "stop_requested": False,
                    })
                    _persist_jobs()
                return

            if run_type == "compliance":
                from a3_compliance_engine import run_compliance as _engine
                result = _engine(
                    tenant_id=tenant_norm,
                    doc_id=params.get("doc_id"),
                    limit=_opt_int(params.get("limit")),
                    delay_between=float(params.get("delay_between") or 2.0),
                    stop_requested=lambda: _job_stop_requested(new_job_id),
                )
                next_status = "PAUSED" if bool((result or {}).get("stopped")) else "DONE"
                with JOBS_LOCK:
                    JOBS[new_job_id].update({
                        "status": next_status,
                        "result": result,
                        "updated_at": _now_iso(),
                        "finished_at": _now_iso(),
                        "stop_requested": False,
                    })
                    _persist_jobs()
                return

            from a4_chat_engine import index_requirements
            count = index_requirements(tenant_norm, force=bool(params.get("force")))
            with JOBS_LOCK:
                JOBS[new_job_id].update({
                    "status": "DONE",
                    "indexed": count,
                    "updated_at": _now_iso(),
                    "finished_at": _now_iso(),
                    "stop_requested": False,
                })
                _persist_jobs()
        except Exception as e:
            with JOBS_LOCK:
                JOBS[new_job_id].update({
                    "status": "ERROR",
                    "error": str(e),
                    "updated_at": _now_iso(),
                    "finished_at": _now_iso(),
                    "stop_requested": False,
                })
                _persist_jobs()

    background_tasks.add_task(_run)
    return {
        "job_id": new_job_id,
        "status": "PENDING",
        "tenant_id": tenant_norm,
        "resumed_from": job_id,
        "type": run_type,
    }


# ---------------------------------------------------------------------------
# Routes dashboard, documents et registre A1
# Elles exposent les documents extraits, les exigences du registre, les
# statistiques d'extraction et les donnees affichees dans le tableau de bord.
# ---------------------------------------------------------------------------

@app.get("/api/v1/dashboard/overview")
def get_dashboard_overview(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    return _fetch_dashboard_overview(tenant_id=tenant_id)


@app.get("/api/v1/documents")
def list_documents(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    family: str = Query(default=""),
    search: str = Query(default=""),
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    return _fetch_documents_page(
        limit=int(limit),
        offset=int(offset),
        family=family,
        search=search,
        tenant_id=tenant_id,
    )


@app.get("/api/v1/documents/{doc_id}/detail")
def get_document_detail(
    doc_id: str,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    return _fetch_document_detail(doc_id, tenant_id=tenant_id)


@app.get("/api/v1/analytics/families")
def analytics_families(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    return {"items": _fetch_family_analytics(tenant_id=tenant_id)}


@app.get("/api/v1/analytics/gate")
def analytics_gate(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth_from_header(authorization)
    # Gate analytics = benchmark global (fichier JSON), pas de donnees client - OK multi-tenant
    return _load_gate_overview()


@app.get("/api/v1/analytics/overview")
def analytics_overview(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    return _fetch_analytics_overview(tenant_id=tenant_id)


@app.get("/api/v1/requirements")
def list_requirements(
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    tenant_id: str = Query(default=""),
    req_type: str = Query(default=""),
    status: str = Query(default=""),
    qse_domain: str = Query(default=""),
    qse_sub_domain: str = Query(default=""),
    doc_id: str = Query(default=""),
    search: str = Query(default=""),
    min_conf: float | None = Query(default=None),
    max_conf: float | None = Query(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    return _fetch_requirements_page(
        limit=int(limit),
        offset=int(offset),
        tenant_id=tenant_id,
        req_type=req_type,
        status=status,
        qse_domain=qse_domain,
        qse_sub_domain=qse_sub_domain,
        doc_id=doc_id,
        search=search,
        min_conf=min_conf,
        max_conf=max_conf,
    )


@app.get("/api/v1/reports")
def list_reports(
    category: str = Query(default="", description="preflight | registry | history"),
    limit: int = Query(default=200, ge=1, le=2000),
    authorization: str = Header(default=""),
) -> dict[str, Any]:
    _auth_from_header(authorization)
    return {"items": _list_reports(category=category, limit=int(limit))}


@app.get("/api/v1/system/status")
def system_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _auth_from_header(authorization)
    load_dotenv()
    ratecard_json = str(os.getenv("LLM_RATECARD_JSON", "") or "").strip()
    default_input_per_1k = float(os.getenv("LLM_DEFAULT_INPUT_PER_1K_USD", "0") or 0.0)
    default_output_per_1k = float(os.getenv("LLM_DEFAULT_OUTPUT_PER_1K_USD", "0") or 0.0)
    return {
        "service": "qalitas-api",
        "status": "ok",
        "primary_provider": os.getenv("PRIMARY_LLM_PROVIDER", ""),
        "primary_model": os.getenv("PRIMARY_MODEL", ""),
        "fallback_provider": os.getenv("FALLBACK_LLM_PROVIDER", ""),
        "fallback_model": os.getenv("FALLBACK_MODEL", ""),
        "a1_runtime": {
            "promotion_min_confidence": float(
                os.getenv(
                    "A1_PROMOTION_TO_DRAFT_MIN_CONFIDENCE",
                    os.getenv("A1_PROMOTION_LIMIT_CONF", "0.76"),
                )
            ),
            "promotion_min_quality": float(os.getenv("A1_PROMOTION_TO_DRAFT_MIN_QUALITY", "0.84")),
            "promotion_min_grounding": float(os.getenv("A1_PROMOTION_TO_DRAFT_MIN_GROUNDING", "0.72")),
            "promotion_min_completeness": float(os.getenv("A1_PROMOTION_TO_DRAFT_MIN_COMPLETENESS", "0.85")),
            "promotion_min_subject_consistency": float(
                os.getenv("A1_PROMOTION_TO_DRAFT_MIN_SUBJECT_CONSISTENCY", "0.60")
            ),
            "precall_runtime_mode": str(os.getenv("A1_PRECALL_RUNTIME_MODE", "conservative")).strip().lower(),
            "error_memory_enabled": str(os.getenv("A1_ERROR_MEMORY_ENABLED", "1")).strip().lower() not in {"0", "false", "off"},
            "error_memory_load_limit": int(os.getenv("A1_ERROR_MEMORY_LOAD_LIMIT", "400") or 400),
            "runtime_unit_cache_enabled": str(os.getenv("A1_RUNTIME_UNIT_CACHE_ENABLED", "1")).strip().lower() not in {"0", "false", "off"},
            "runtime_unit_dedup_enabled": str(os.getenv("A1_RUNTIME_UNIT_DEDUP_ENABLED", "1")).strip().lower() not in {"0", "false", "off"},
            "runtime_budget_fallback_enabled": str(os.getenv("A1_RUNTIME_BUDGET_FALLBACK_ENABLED", "1")).strip().lower() not in {"0", "false", "off"},
            "runtime_max_llm_calls_per_doc": int(os.getenv("A1_RUNTIME_MAX_LLM_CALLS_PER_DOC", "0") or 0),
            "runtime_max_total_tokens_per_doc": int(os.getenv("A1_RUNTIME_MAX_TOTAL_TOKENS_PER_DOC", "0") or 0),
            "runtime_max_estimated_cost_usd": float(os.getenv("A1_RUNTIME_MAX_ESTIMATED_COST_USD", "0") or 0.0),
        },
        "llm_cost_tracking": {
            "ratecard_configured": bool(ratecard_json),
            "default_input_per_1k_usd": default_input_per_1k,
            "default_output_per_1k_usd": default_output_per_1k,
        },
    }


@app.get("/api/v1/documents/{doc_id}/summary")
def get_document_summary(
    doc_id: str,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    return _fetch_document_summary(doc_id, tenant_id=tenant_id)


@app.get("/api/v1/documents/{doc_id}/requirements")
def get_document_requirements(
    doc_id: str,
    limit: int = Query(default=300, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    rows, total = _fetch_requirements(
        doc_id=doc_id,
        limit=int(limit),
        offset=int(offset),
        tenant_id=tenant_id,
    )
    return {"doc_id": doc_id, "total": int(total), "limit": int(limit), "offset": int(offset), "items": rows}


# ---------------------------------------------------------------------------
# Routes validation humaine A1
# Elles permettent de valider, corriger et consulter les exigences extraites.
# ---------------------------------------------------------------------------

@app.post("/api/v1/requirements/{req_id}/validate")
def validate_requirement(
    req_id: str,
    payload: ValidationRequest,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "valider une exigence")

    if payload.decision == "EDIT" and not str(payload.corrected_text or "").strip():
        raise HTTPException(status_code=422, detail="corrected_text est obligatoire pour EDIT")

    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    r.requirement_id,
                    r.status,
                    r.requirement_text,
                    COALESCE(r.req_type, 'AUTRE') AS req_type,
                    COALESCE(r.citation_snippet, '') AS citation_snippet,
                    COALESCE(r.doc_id::text, '') AS doc_id,
                    COALESCE(a.article_label, a.article_code, '') AS article_label
                FROM requirements r
                JOIN documents d ON d.doc_id = r.doc_id
                LEFT JOIN articles a ON a.article_id = r.article_id
                WHERE r.requirement_id = %s::uuid
                  AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                """,
                (req_id, tenant_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Exigence introuvable")

            original_text = str(row[2] or "")
            req_type = str(row[3] or "AUTRE")
            citation_snippet = str(row[4] or "")
            source_doc_id = str(row[5] or "")
            article_label = str(row[6] or "")

            cur.execute(
                """
                INSERT INTO requirement_validations
                    (requirement_id, validator_username, validator_role,
                     decision, comment, rejection_reason, original_text)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s)
                RETURNING validation_id::text, created_at::text
                """,
                (
                    req_id,
                    session["username"],
                    session["role"],
                    payload.decision,
                    str(payload.comment or "").strip()[:500],
                    str(payload.rejection_reason or "") or None,
                    original_text if payload.decision in ("EDIT", "REJECT") else None,
                ),
            )
            val_row = cur.fetchone()

            new_status = str(row[1] or "TO_VALIDATE")
            if payload.decision == "APPROVE":
                new_status = "PROMOTED"
            elif payload.decision == "REJECT":
                new_status = "REJECT"
            elif payload.decision == "EDIT":
                # Correction humaine : on met à jour le texte et on promeut directement
                corrected = str(payload.corrected_text or "").strip()
                cur.execute(
                    """
                    UPDATE requirements
                       SET requirement_text = %s,
                           status           = 'PROMOTED',
                           extraction_source = 'human_edit'
                     WHERE requirement_id = %s::uuid
                    """,
                    (corrected, req_id),
                )
                new_status = "PROMOTED"

            if payload.decision != "EDIT":
                cur.execute(
                    "UPDATE requirements SET status = %s WHERE requirement_id = %s::uuid",
                    (new_status, req_id),
                )

            feedback_signal = build_human_validation_feedback_signal(
                decision=payload.decision,
                requirement_text=original_text,
                req_type=req_type,
                snippet=citation_snippet,
                rejection_reason=str(payload.rejection_reason or ""),
                comment=str(payload.comment or ""),
                corrected_text=str(payload.corrected_text or ""),
                article_label=article_label,
                status=new_status,
            )
            if feedback_signal:
                try:
                    if error_memory_table_exists(cur):
                        persist_error_memory_signal(
                            cur,
                            tenant_id=tenant_id,
                            doc_id=source_doc_id,
                            signal=feedback_signal,
                            source_event_type="A1_HUMAN_VALIDATION_FEEDBACK",
                            source_event_key=str(val_row[0] or ""),
                        )
                except Exception:
                    pass
        conn.commit()

    return {
        "validation_id": val_row[0],
        "requirement_id": req_id,
        "decision": payload.decision,
        "new_status": new_status,
        "human_validation_flag": "HUMAN_EDITED" if payload.decision == "EDIT" else "",
        "validator": session["username"],
        "created_at": str(val_row[1]),
    }


@app.get("/api/v1/requirements/{req_id}/validations")
def get_requirement_validations(
    req_id: str,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM requirements r
                JOIN documents d ON d.doc_id = r.doc_id
                WHERE r.requirement_id = %s::uuid
                  AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                """,
                (req_id, tenant_id),
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Exigence introuvable")
            cur.execute(
                """
                SELECT
                    validation_id::text,
                    validator_username,
                    validator_role,
                    decision,
                    COALESCE(comment, '') AS comment,
                    COALESCE(rejection_reason, '') AS rejection_reason,
                    created_at::text
                FROM requirement_validations
                WHERE requirement_id = %s::uuid
                ORDER BY created_at DESC
                LIMIT 50
                """,
                (req_id,),
            )
            cols = [d[0] for d in cur.description]
            items = [dict(zip(cols, row)) for row in cur.fetchall()]
    return {"requirement_id": req_id, "total": len(items), "items": items}


@app.get("/api/v1/validation/queue")
def get_validation_queue(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM requirements r
                JOIN documents d ON d.doc_id = r.doc_id
                WHERE COALESCE(r.status, '') IN ('TO_VALIDATE', 'DRAFT')
                  AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                  AND NOT EXISTS (
                      SELECT 1 FROM requirement_validations v
                      WHERE v.requirement_id = r.requirement_id
                  )
                """,
                (tenant_id,),
            )
            total = int(cur.fetchone()[0] or 0)
            cur.execute(
                """
                SELECT
                    r.requirement_id::text,
                    COALESCE(r.req_type, '')                        AS req_type,
                    COALESCE(r.requirement_text, '')                AS requirement_text,
                    COALESCE(r.status, '')                          AS status,
                    COALESCE(r.confidence, 0)::float                AS confidence,
                    COALESCE(r.normative_strength, 'IMPERATIF')     AS normative_strength,
                    COALESCE(r.citation_snippet, '')                AS source_snippet,
                    COALESCE(r.citation_ref, '')                    AS source_article,
                    COALESCE(r.qse_domain, '')                      AS qse_domain,
                    COALESCE(r.quality_score, 0)::float             AS quality_score,
                    COALESCE(r.grounding_score, 0)::float           AS grounding_score,
                    COALESCE(d.title, '')                           AS document_title,
                    COALESCE(a.article_label, a.article_code, '')   AS article_label,
                    CASE
                        WHEN COALESCE(r.status, '') = 'DRAFT'
                            THEN 'PRE_VALIDATED_AUTO'
                        WHEN COALESCE(r.quality_score, 0) < 0.84
                             AND COALESCE(r.grounding_score, 0) < 0.72
                            THEN 'QUALITY_AND_GROUNDING_TOO_LOW'
                        WHEN COALESCE(r.quality_score, 0) < 0.84
                            THEN 'QUALITY_TOO_LOW'
                        WHEN COALESCE(r.grounding_score, 0) < 0.72
                            THEN 'GROUNDING_TOO_WEAK'
                        WHEN COALESCE(r.confidence, 0) < 0.65
                            THEN 'LOW_CONFIDENCE'
                        ELSE 'POLICY_OR_TYPE'
                    END                                             AS promotion_blocked_reason
                FROM requirements r
                LEFT JOIN documents d ON d.doc_id = r.doc_id
                LEFT JOIN articles a ON a.article_id = r.article_id
                WHERE COALESCE(r.status, '') IN ('TO_VALIDATE', 'DRAFT')
                  AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                  AND NOT EXISTS (
                      SELECT 1 FROM requirement_validations v
                      WHERE v.requirement_id = r.requirement_id
                  )
                ORDER BY
                    CASE WHEN COALESCE(r.status, '') = 'DRAFT' THEN 0 ELSE 1 END,
                    COALESCE(r.confidence, 0) DESC
                LIMIT %s OFFSET %s
                """,
                (tenant_id, int(limit), int(offset)),
            )
            cols = [d[0] for d in cur.description]
            items = [dict(zip(cols, row)) for row in cur.fetchall()]

    # Enrichir chaque item avec les doublons potentiels (même début de texte, même tenant)
    if items:
        req_ids = [i["requirement_id"] for i in items]
        with psycopg.connect(dsn) as conn2:
            with conn2.cursor() as cur2:
                for item in items:
                    cur2.execute(
                        """
                        SELECT r2.requirement_id::text, r2.requirement_text
                        FROM requirements r2
                        JOIN documents d2 ON d2.doc_id = r2.doc_id
                        WHERE r2.requirement_id != %s::uuid
                          AND LOWER(COALESCE(d2.tenant_id,'')) = LOWER(%s)
                          AND COALESCE(r2.status,'') IN ('DRAFT','PROMOTED')
                          AND LEFT(LOWER(COALESCE(r2.requirement_text,'')), 80)
                              = LEFT(LOWER(%s), 80)
                        LIMIT 3
                        """,
                        (item["requirement_id"], tenant_id,
                         item["requirement_text"]),
                    )
                    rows = cur2.fetchall()
                    item["similar_existing"] = [
                        {
                            "req_id": r[0],
                            "text": str(r[1] or "")[:120],
                            "similarity": _text_similarity(item.get("requirement_text"), r[1]),
                        }
                        for r in rows
                    ]

    return {"total": total, "limit": int(limit), "offset": int(offset), "items": items}


@app.get("/api/v1/requirements/{req_id}/validation-context")
def get_requirement_validation_context(
    req_id: str,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    item = _fetch_requirement_validation_context(req_id, tenant_id)
    if not item:
        raise HTTPException(status_code=404, detail="Exigence introuvable")
    return item


@app.get("/api/v1/validations")
def get_all_validations(
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    decision: str | None = Query(default=None),
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Retourne toutes les decisions de validation (APPROVE/REJECT/FLAG) - utilise par dashboard et analytics."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            where = """
                WHERE LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
            """
            params: list[Any] = [tenant_id]
            if decision:
                where += " AND UPPER(v.decision) = UPPER(%s)"
                params.append(decision)
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM requirement_validations v
                JOIN requirements r ON r.requirement_id = v.requirement_id
                JOIN documents d ON d.doc_id = r.doc_id
                {where}
                """,
                params,
            )
            total = int(cur.fetchone()[0] or 0)
            cur.execute(
                f"""
                SELECT
                    v.validation_id::text,
                    v.requirement_id::text,
                    v.validator_username,
                    v.validator_role,
                    v.decision,
                    COALESCE(v.comment, '') AS comment,
                    v.created_at::text
                FROM requirement_validations v
                JOIN requirements r ON r.requirement_id = v.requirement_id
                JOIN documents d ON d.doc_id = r.doc_id
                {where}
                ORDER BY v.created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [int(limit), int(offset)],
            )
            cols = [d[0] for d in cur.description]
            validations = [dict(zip(cols, row)) for row in cur.fetchall()]
    return {"total": total, "limit": int(limit), "offset": int(offset), "validations": validations}


@app.get("/api/v1/validation/fewshot")
def get_fewshot_examples(
    limit: int = Query(default=5, ge=1, le=20),
    min_confidence: float = Query(default=0.82),
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    Retourne les N meilleures exigences approuvees par validation humaine,
    formatees pour injection dynamique dans le prompt LLM (Phase 6).
    Triees par confiance DESC - les exemples les plus fiables en premier.
    """
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (r.requirement_id)
                    r.requirement_id::text                              AS requirement_id,
                    COALESCE(r.req_type, 'OBLIGATION')                  AS req_type,
                    COALESCE(r.requirement_text, '')                     AS requirement_text,
                    COALESCE(r.confidence, 0)::float                    AS confidence,
                    COALESCE(r.citation_snippet, '')                     AS citation_snippet,
                    COALESCE(a.article_label, a.article_code, '')        AS article_label,
                    COALESCE(d.title, '')                                AS doc_title
                FROM requirement_validations v
                JOIN requirements r ON r.requirement_id = v.requirement_id
                LEFT JOIN articles a ON a.article_id = r.article_id
                LEFT JOIN documents d ON d.doc_id = r.doc_id
                WHERE v.decision = 'APPROVE'
                  AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                  AND COALESCE(r.confidence, 0) >= %s
                  AND LENGTH(COALESCE(r.citation_snippet, '')) >= 40
                ORDER BY r.requirement_id, v.created_at DESC
                LIMIT %s
                """,
                (tenant_id, float(min_confidence), int(limit)),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    # Grouper par citation_snippet pour construire les blocs few-shot
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["citation_snippet"][:120]
        if key not in groups:
            groups[key] = {
                "citation_snippet": row["citation_snippet"],
                "article_label": row["article_label"],
                "doc_title": row["doc_title"],
                "requirements": [],
            }
        groups[key]["requirements"].append({
            "req_type": row["req_type"],
            "requirement_text": row["requirement_text"],
        })

    examples = list(groups.values())[:int(limit)]
    return {"total": len(examples), "min_confidence": min_confidence, "examples": examples}


# ---------------------------------------------------------------------------
# AGENTS 2 / 3 / 4 - Routes API
# ---------------------------------------------------------------------------

# --- Agent 2 - Applicabilite ------------------------------------------------
# Resume A2, rapport PDF A2, revue humaine des decisions et lancement du
# moteur qui croise exigences A1 promues avec les scopes entreprise.

class ApplicabilityRunRequest(BaseModel):
    tenant_id: str = ""
    doc_id: str | None = None
    limit: int | None = None
    min_quality: float = 0.70
    delay_between: float = DEFAULT_A2_DELAY_SECONDS
    mode: Literal["full", "delta"] = "delta"
    force: bool = False
    site_ids: list[str] = Field(default_factory=list)
    process_ids: list[str] = Field(default_factory=list)
    activity_ids: list[str] = Field(default_factory=list)


class ApplicabilityReviewRequest(BaseModel):
    tenant_id: str = ""
    status: Literal["APPLICABLE", "APPLICABLE_FUTUR", "NON_APPLICABLE", "APPLICABLE_SOUS_CONDITIONS", "INCERTAIN"]
    comment: str = Field(default="", max_length=1000)
    scope_key: str | None = None


@app.get("/api/v2/applicability/summary")
def get_applicability_summary(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Resume des decisions d'applicabilite pour un tenant."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    from a2_applicability_engine import get_applicability_summary
    return get_applicability_summary(tenant_id)


@app.get("/api/v2/applicability/report.pdf")
def get_applicability_report_pdf(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> Any:
    """Genere un rapport PDF A2 lisible et exploitable."""
    from fastapi.responses import StreamingResponse
    import io
    from datetime import date

    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    from a2_applicability_engine import get_applicability_summary

    data = get_applicability_summary(tenant_id)

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        )

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=1.8 * cm,
            rightMargin=1.8 * cm,
            topMargin=1.6 * cm,
            bottomMargin=1.6 * cm,
        )
        styles = getSampleStyleSheet()
        story = []

        title_style = ParagraphStyle(
            "a2Title",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            textColor=colors.HexColor("#1f3ea6"),
            spaceAfter=6,
        )
        h2_style = ParagraphStyle(
            "a2H2",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            textColor=colors.HexColor("#0f172a"),
            spaceBefore=10,
            spaceAfter=4,
        )
        body_style = ParagraphStyle(
            "a2Body",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.3,
            leading=11,
        )
        caption_style = ParagraphStyle(
            "a2Caption",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8,
            textColor=colors.HexColor("#475569"),
        )

        today_str = date.today().strftime("%d/%m/%Y")
        story.append(Paragraph("QALITAS - Rapport Applicabilite Reglementaire (A2)", title_style))
        story.append(
            Paragraph(
                f"Tenant: <b>{_clean_pdf_text(tenant_id, max_len=80)}</b> | Date: <b>{today_str}</b> | Agent A2",
                caption_style,
            )
        )
        story.append(HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#2563eb")))
        story.append(Spacer(1, 8))

        counts = data.get("counts", {})
        total = int(data.get("total", 0) or 0)
        kpi_data = [
            ["Indicateur", "Valeur"],
            ["Total evalue", str(total)],
            ["Applicable", str(int(counts.get("APPLICABLE", 0) or 0))],
            ["Applicable futur", str(int(counts.get("APPLICABLE_FUTUR", 0) or 0))],
            ["Sous conditions", str(int(counts.get("APPLICABLE_SOUS_CONDITIONS", 0) or 0))],
            ["Non applicable", str(int(counts.get("NON_APPLICABLE", 0) or 0))],
            ["Incertain", str(int(counts.get("INCERTAIN", 0) or 0))],
        ]
        kpi_table = Table(kpi_data, colWidths=[9 * cm, 7 * cm], repeatRows=1)
        kpi_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3ea6")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(Paragraph("Synthese des decisions", h2_style))
        story.append(kpi_table)
        story.append(Spacer(1, 10))

        decisions = list(data.get("decisions", []) or [])
        decisions.sort(key=lambda row: float(row.get("confidence") or 0), reverse=True)
        if decisions:
            rows = [[
                "Reference",
                "Perimetre",
                "Domaine",
                "Type",
                "Decision",
                "Confiance",
                "Justification",
            ]]
            for item in decisions[:120]:
                conf = float(item.get("confidence") or 0)
                rows.append(
                    [
                        Paragraph(_clean_pdf_text(item.get("article_ref") or item.get("citation_ref") or "-", max_len=70), body_style),
                        Paragraph(_clean_pdf_text(item.get("scope_label") or item.get("scope_level") or "-", max_len=42), body_style),
                        Paragraph(_clean_pdf_text(item.get("qse_domain") or "-", max_len=40), body_style),
                        Paragraph(_clean_pdf_text(item.get("req_type") or "-", max_len=24), body_style),
                        Paragraph(_clean_pdf_text(item.get("status") or "-", max_len=28), body_style),
                        f"{max(0, min(100, round(conf * 100)))}%",
                        Paragraph(_clean_pdf_text(item.get("justification") or "-", max_len=320), body_style),
                    ]
                )

            dec_table = Table(
                rows,
                colWidths=[2.0 * cm, 2.5 * cm, 1.8 * cm, 1.5 * cm, 2.0 * cm, 1.2 * cm, 5.5 * cm],
                repeatRows=1,
            )
            dec_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 7.6),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0fdfa")]),
                        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ]
                )
            )
            story.append(Paragraph(f"Decisions detaillees ({len(decisions)})", h2_style))
            story.append(dec_table)
        else:
            story.append(Paragraph("Aucune decision A2 disponible.", body_style))

        story.append(Spacer(1, 16))
        story.append(HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#e2e8f0")))
        story.append(
            Paragraph(
                "Rapport genere automatiquement par QALITAS - Agent A2",
                caption_style,
            )
        )

        doc.build(story)
        buffer.seek(0)
        filename = f"rapport_applicabilite_{tenant_id}_{date.today().isoformat()}.pdf"
        return StreamingResponse(
            buffer,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=\"{filename}\""},
        )
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"reportlab non disponible: {e}")


@app.post("/api/v2/applicability/decisions/{requirement_id}/review")
def review_applicability_decision(
    requirement_id: str,
    body: ApplicabilityReviewRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Validation humaine d'une décision A2 (override du statut)."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, body.tenant_id)
    _require_role(session, WRITE_ROLES, "valider une décision d'applicabilité")

    try:
        from a2_applicability_engine import review_applicability_decision as _review
        return _review(
            tenant_id=tenant_id,
            requirement_id=requirement_id,
            status=body.status,
            reviewer_username=str(session.get("username") or "unknown"),
            reviewer_role=str(session.get("role") or "unknown"),
            comment=body.comment,
            scope_key=body.scope_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v2/applicability/run")
def run_applicability(
    body: ApplicabilityRunRequest,
    background_tasks: BackgroundTasks,
    mode: Literal["full", "delta"] | None = Query(default=None),
    force: bool | None = Query(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Lance le pipeline d'applicabilité en arrière-plan."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, body.tenant_id)
    _require_role(session, WRITE_ROLES, "lancer l'agent A2")
    run_mode = str(mode or body.mode or "delta").lower()
    if run_mode not in {"full", "delta"}:
        raise HTTPException(status_code=400, detail="mode doit etre 'full' ou 'delta'")
    force_flag = body.force if force is None else bool(force)

    # Verrou pipeline : A2 ne doit partir que sur un registre final valide.
    # Toute exigence encore en revue (TO_VALIDATE ou DRAFT) bloque donc le run, sauf force=true.
    if not force_flag:
        try:
            _dsn = _load_env_dsn()
            with psycopg.connect(_dsn) as _conn:
                with _conn.cursor() as _cur:
                    _cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM requirements r
                        JOIN documents d ON d.doc_id = r.doc_id
                        WHERE COALESCE(r.status, '') IN ('TO_VALIDATE', 'DRAFT')
                          AND LOWER(COALESCE(d.tenant_id, '')) = LOWER(%s)
                        """,
                        (tenant_id,),
                    )
                    pending_count = int(_cur.fetchone()[0] or 0)
            if pending_count > 0:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "VALIDATION_PENDING",
                        "count": pending_count,
                        "message": (
                            f"{pending_count} exigence(s) restent hors registre final "
                            f"(a valider ou pre-validees auto). "
                            f"Finalisez la validation A1 avant de lancer A2."
                        ),
                        "hint": "Utilisez force=true uniquement si vous assumez un run A2 sur un registre final incomplet.",
                    },
                )
        except HTTPException:
            raise
        except Exception:
            pass  # Ne pas bloquer A2 si la vérification DB échoue

    job_id = str(uuid.uuid4())
    pipeline_plan, pipeline_steps = _build_followup_pipeline_context("A2")
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "type": "applicability",
            "tenant_id": tenant_id,
            "doc_id": body.doc_id,
            "status": "PENDING",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "stop_requested": False,
            "pipeline_plan": pipeline_plan,
            "pipeline_steps": pipeline_steps,
            "current_stage": "A2",
            "stage_message": "Applicabilité en attente",
            "error_category": None,
            "failed_step": None,
            "run_params": {
                "tenant_id": tenant_id,
                "doc_id": body.doc_id,
                "limit": body.limit,
                "min_quality": body.min_quality,
                "delay_between": body.delay_between,
                "mode": run_mode,
                "force": force_flag,
                "site_ids": list(body.site_ids or []),
                "process_ids": list(body.process_ids or []),
                "activity_ids": list(body.activity_ids or []),
            },
        }
        _persist_jobs()

    def _run() -> None:
        should_pause = False
        with JOBS_LOCK:
            current_item = JOBS.get(job_id) or {}
            current_status = str(current_item.get("status") or "").upper()
            stop_flag = bool(current_item.get("stop_requested"))
            if current_status == "PAUSED" or stop_flag:
                should_pause = True
        if should_pause:
            _mark_pipeline_stage(job_id, "A2", "PAUSED", stage_message="Applicabilité en pause")
            _update_job(job_id, status="PAUSED", stopped_at=_now_iso(), current_stage="A2", stage_message="Applicabilité en pause")
            return
        _mark_pipeline_stage(job_id, "A2", "RUNNING", stage_message="Applicabilité en cours")
        _update_job(job_id, status="RUNNING", current_stage="A2", stage_message="Applicabilité en cours")
        try:
            from a2_applicability_engine import run_applicability as _engine
            result = _call_with_supported_kwargs(
                _engine,
                tenant_id=tenant_id,
                doc_id=body.doc_id,
                limit=body.limit,
                min_quality=body.min_quality,
                delay_between=body.delay_between,
                mode=run_mode,
                force=force_flag,
                force_recompute=force_flag,
                site_ids=list(body.site_ids or []),
                process_ids=list(body.process_ids or []),
                activity_ids=list(body.activity_ids or []),
                stop_requested=lambda: _job_stop_requested(job_id),
            )
            if bool((result or {}).get("stopped")):
                _mark_pipeline_stage(job_id, "A2", "PAUSED", stage_message="Applicabilité en pause")
                _update_job(
                    job_id,
                    status="PAUSED",
                    result=result,
                    a2_result=result,
                    stopped_at=_now_iso(),
                    finished_at=_now_iso(),
                    stop_requested=False,
                    current_stage="A2",
                    stage_message="Applicabilité en pause",
                )
                return

            stage_status, stage_message, error_category = _a2_followup_outcome(result or {})
            _mark_pipeline_stage(job_id, "A2", stage_status, stage_message=stage_message)
            next_status = "FAILED" if stage_status == "ERROR" else "DONE"
            _update_job(
                job_id,
                status=next_status,
                result=result,
                a2_result=result,
                updated_at=_now_iso(),
                finished_at=_now_iso(),
                stop_requested=False,
                current_stage=None if next_status == "DONE" else "A2",
                stage_message=stage_message,
                error_category=error_category,
                failed_step="A2_PIPELINE" if next_status == "FAILED" else None,
                error=stage_message if next_status == "FAILED" else None,
            )
        except Exception as e:
            err_text = str(e)
            _mark_pipeline_stage(job_id, "A2", "ERROR", stage_message="Applicabilité échouée")
            _update_job(
                job_id,
                status="ERROR",
                error=err_text,
                updated_at=_now_iso(),
                finished_at=_now_iso(),
                stop_requested=False,
                current_stage="A2",
                stage_message="Applicabilité échouée",
                error_category=_classify_error(err_text),
                failed_step="A2_PIPELINE",
            )

    background_tasks.add_task(_run)
    return {
        "job_id": job_id,
        "status": "PENDING",
        "tenant_id": tenant_id,
        "current_stage": "A2",
        "stage_message": "Applicabilité en attente",
        "pipeline_plan": pipeline_plan,
        "pipeline_steps": pipeline_steps,
    }


# --- Agent 3 - Conformite ---------------------------------------------------
# Resume A3, rapport PDF A3 et lancement du moteur qui compare les exigences
# applicables avec les preuves operationnelles du tenant.

class ComplianceRunRequest(BaseModel):
    tenant_id: str = ""
    doc_id: str | None = None
    limit: int | None = None
    delay_between: float = 2.0
    mode: Literal["full", "delta"] = "delta"
    force: bool = False
    site_ids: list[str] = Field(default_factory=list)
    process_ids: list[str] = Field(default_factory=list)
    activity_ids: list[str] = Field(default_factory=list)


@app.get("/api/v2/compliance/summary")
def get_compliance_summary(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Resume de l'etat de conformite pour un tenant."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    from a3_compliance_engine import get_compliance_summary
    return get_compliance_summary(tenant_id)


@app.get("/api/v2/compliance/report.pdf")
def get_compliance_report_pdf(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> Any:
    """Genere et retourne un rapport PDF de conformite A3."""
    from fastapi.responses import StreamingResponse
    import io
    from datetime import date

    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    from a3_compliance_engine import get_compliance_summary

    data = get_compliance_summary(tenant_id)

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        )

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=1.8 * cm,
            rightMargin=1.8 * cm,
            topMargin=1.6 * cm,
            bottomMargin=1.6 * cm,
        )
        styles = getSampleStyleSheet()
        story = []

        title_style = ParagraphStyle(
            "a3Title",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            textColor=colors.HexColor("#1f3ea6"),
            spaceAfter=6,
        )
        h2_style = ParagraphStyle(
            "a3H2",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            textColor=colors.HexColor("#0f172a"),
            spaceBefore=10,
            spaceAfter=4,
        )
        body_style = ParagraphStyle(
            "a3Body",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.3,
            leading=11,
        )
        caption_style = ParagraphStyle(
            "a3Caption",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8,
            textColor=colors.HexColor("#475569"),
        )

        today_str = date.today().strftime("%d/%m/%Y")
        story.append(Paragraph("QALITAS - Rapport Conformite Reglementaire (A3)", title_style))
        story.append(
            Paragraph(
                f"Tenant: <b>{_clean_pdf_text(tenant_id, max_len=80)}</b> | Date: <b>{today_str}</b> | Agent A3",
                caption_style,
            )
        )
        story.append(HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#2563eb")))
        story.append(Spacer(1, 8))

        total = int(data.get("total_checks", 0) or 0)
        rate = float(data.get("compliance_rate", 0) or 0)
        breakdown = data.get("status_breakdown", {})
        conforme = int(breakdown.get("CONFORME", {}).get("count", 0) or 0)
        non_conf = int(breakdown.get("NON_CONFORME", {}).get("count", 0) or 0)
        partiel = int(
            breakdown.get("PARTIELLEMENT_CONFORME", {}).get("count", breakdown.get("PARTIEL", {}).get("count", 0))
            or 0
        )
        absence = int(breakdown.get("ABSENCE_DE_PREUVE", {}).get("count", 0) or 0)
        gaps_total = int(sum(int(g.get("count", 0) or 0) for g in data.get("gaps_breakdown", [])))
        actions_total = int(sum(int(v or 0) for v in data.get("actions_breakdown", {}).values()))
        nc_reg = data.get("nc_reglementaire", {}) or {}

        kpi_data = [
            ["Indicateur", "Valeur"],
            ["Total verifications", str(total)],
            ["Taux de conformite", f"{round(rate * 100, 1)}%"],
            ["Conformes", str(conforme)],
            ["Partiellement conformes", str(partiel)],
            ["Non conformes", str(non_conf)],
            ["Absence de preuve", str(absence)],
            ["NC reglementaires", str(int(nc_reg.get("total", 0) or 0))],
            ["Ecarts detectes (tous types)", str(gaps_total)],
            ["Actions ouvertes", str(actions_total)],
        ]
        kpi_table = Table(kpi_data, colWidths=[9 * cm, 7 * cm], repeatRows=1)
        kpi_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3ea6")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(Paragraph("Synthese des indicateurs", h2_style))
        story.append(kpi_table)
        story.append(Spacer(1, 10))

        worst = list(data.get("worst_items", []) or [])
        worst.sort(key=lambda item: float(item.get("score") or 0))
        if worst:
            rows = [["Domaine", "Statut", "Score", "Exigence", "Preuves manquantes"]]
            for item in worst[:70]:
                rows.append(
                    [
                        Paragraph(_clean_pdf_text(item.get("qse_domain") or item.get("domain") or "-", max_len=30), body_style),
                        Paragraph(_clean_pdf_text(str(item.get("status") or "-").replace("_", " "), max_len=28), body_style),
                        f"{max(0, min(100, round(float(item.get('score') or 0) * 100)))}%",
                        Paragraph(_clean_pdf_text(item.get("requirement_text") or item.get("requirement") or "-", max_len=200), body_style),
                        Paragraph(_clean_pdf_text(item.get("missing_proofs") or "-", max_len=240), body_style),
                    ]
                )
            worst_table = Table(
                rows,
                colWidths=[2.3 * cm, 2.4 * cm, 1.3 * cm, 5.2 * cm, 5.8 * cm],
                repeatRows=1,
            )
            worst_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 7.6),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fef2f2")]),
                        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ]
                )
            )
            story.append(Paragraph(f"Verifications critiques ({len(worst)})", h2_style))
            story.append(worst_table)
            story.append(Spacer(1, 8))

        gaps = list(data.get("gaps_breakdown", []) or [])
        if gaps:
            gap_rows = [["Severite", "Type d'ecart", "Nombre"]]
            for gap in gaps:
                gap_rows.append(
                    [
                        _clean_pdf_text(gap.get("severity") or "-", max_len=22),
                        _clean_pdf_text(gap.get("gap_type") or "-", max_len=32),
                        str(int(gap.get("count", 0) or 0)),
                    ]
                )
            gap_table = Table(gap_rows, colWidths=[4.2 * cm, 8.6 * cm, 3.2 * cm], repeatRows=1)
            gap_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#b91c1c")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 8.3),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fff1f2")]),
                        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.append(Paragraph("Repartition des ecarts", h2_style))
            story.append(gap_table)
            story.append(Spacer(1, 8))

        actions = data.get("actions_breakdown", {}) or {}
        if actions:
            action_rows = [["Etat action", "Nombre"]]
            for state, count in sorted(actions.items(), key=lambda kv: int(kv[1] or 0), reverse=True):
                action_rows.append(
                    [
                        _clean_pdf_text(str(state).replace("_", " "), max_len=28),
                        str(int(count or 0)),
                    ]
                )
            action_table = Table(action_rows, colWidths=[9.5 * cm, 6.5 * cm], repeatRows=1)
            action_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#166534")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0fdf4")]),
                        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.append(Paragraph("Actions correctives", h2_style))
            story.append(action_table)

        story.append(Spacer(1, 14))
        story.append(HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#e2e8f0")))
        story.append(
            Paragraph(
                "Rapport genere automatiquement par QALITAS - Agent A3",
                caption_style,
            )
        )

        doc.build(story)
        buffer.seek(0)

        filename = f"rapport_conformite_{tenant_id}_{date.today().isoformat()}.pdf"
        return StreamingResponse(
            buffer,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"reportlab non disponible: {e}")


# ---------------------------------------------------------------------------
# Routes reports transverses
# Ces PDF consolident les resultats A1/A2/A3 pour la direction et le plan
# d'action, sans relancer les moteurs metier.
# ---------------------------------------------------------------------------

@app.get("/api/v2/reports/executive.pdf")
def get_executive_report_pdf(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> Any:
    """Genere un Executive Summary PDF base sur les donnees A1/A2/A3."""
    from fastapi.responses import StreamingResponse
    from datetime import date
    import io

    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)

    overview = _fetch_dashboard_overview(tenant_id)
    from a2_applicability_engine import get_applicability_summary
    from a3_compliance_engine import get_compliance_summary

    a2 = get_applicability_summary(tenant_id)
    a3 = get_compliance_summary(tenant_id)
    if a2.get("error"):
        raise HTTPException(status_code=404, detail=str(a2.get("error")))
    if a3.get("error"):
        raise HTTPException(status_code=404, detail=str(a3.get("error")))

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate,
            Paragraph,
            Spacer,
            Table,
            TableStyle,
            HRFlowable,
        )

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=1.8 * cm,
            rightMargin=1.8 * cm,
            topMargin=1.6 * cm,
            bottomMargin=1.6 * cm,
        )
        styles = getSampleStyleSheet()
        story: list[Any] = []

        title_style = ParagraphStyle(
            "execTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            textColor=colors.HexColor("#1f3ea6"),
            spaceAfter=6,
        )
        h2_style = ParagraphStyle(
            "execH2",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            textColor=colors.HexColor("#0f172a"),
            spaceBefore=10,
            spaceAfter=4,
        )
        body_style = ParagraphStyle(
            "execBody",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.4,
            leading=11,
        )
        caption_style = ParagraphStyle(
            "execCaption",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8,
            textColor=colors.HexColor("#475569"),
        )

        counts_a2 = a2.get("counts", {}) or {}
        breakdown = a3.get("status_breakdown", {}) or {}
        nc = a3.get("nc_reglementaire", {}) or {}

        open_states = {"PLANIFIEE", "EN_COURS", "A_FAIRE", "OUVERTE"}
        actions_breakdown = a3.get("actions_breakdown", {}) or {}
        actions_open = int(
            sum(
                int(v or 0)
                for k, v in actions_breakdown.items()
                if str(k or "").upper() in open_states
            )
        )

        today_str = date.today().strftime("%d/%m/%Y")
        story.append(Paragraph("QALITAS - Executive Summary QHSE", title_style))
        story.append(
            Paragraph(
                f"Tenant: <b>{_clean_pdf_text(tenant_id, max_len=80)}</b> | Date: <b>{today_str}</b> | Sources A1/A2/A3",
                caption_style,
            )
        )
        story.append(HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#2563eb")))
        story.append(Spacer(1, 8))

        kpi_a1 = [
            ["Indicateur A1", "Valeur"],
            ["Documents suivis", str(int(overview.get("documents_total", 0) or 0))],
            ["Exigences extraites", str(int(overview.get("requirements_total", 0) or 0))],
            ["A valider", str(int(overview.get("to_validate_total", 0) or 0))],
            ["Rejetees", str(int(overview.get("reject_total", 0) or 0))],
        ]
        table_a1 = Table(kpi_a1, colWidths=[9 * cm, 7 * cm], repeatRows=1)
        table_a1.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3ea6")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(Paragraph("1. Extraction et validation humaine (A1)", h2_style))
        story.append(table_a1)
        story.append(Spacer(1, 8))

        kpi_a2 = [
            ["Indicateur A2", "Valeur"],
            ["Total decisions", str(int(a2.get("total", 0) or 0))],
            [
                "Applicables",
                str(
                    int(counts_a2.get("APPLICABLE", 0) or 0)
                    + int(counts_a2.get("APPLICABLE_SOUS_CONDITIONS", 0) or 0)
                ),
            ],
            ["Applicables futures", str(int(counts_a2.get("APPLICABLE_FUTUR", 0) or 0))],
            ["Non applicables", str(int(counts_a2.get("NON_APPLICABLE", 0) or 0))],
            ["Incertaines", str(int(counts_a2.get("INCERTAIN", 0) or 0))],
        ]
        table_a2 = Table(kpi_a2, colWidths=[9 * cm, 7 * cm], repeatRows=1)
        table_a2.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0fdfa")]),
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(Paragraph("2. Applicabilite reglementaire (A2)", h2_style))
        story.append(table_a2)
        story.append(Spacer(1, 8))

        conformes = int((breakdown.get("CONFORME", {}) or {}).get("count", 0) or 0)
        partiels = int(
            (breakdown.get("PARTIELLEMENT_CONFORME", {}) or {}).get("count", 0)
            or (breakdown.get("PARTIEL", {}) or {}).get("count", 0)
            or 0
        )
        non_conf = int((breakdown.get("NON_CONFORME", {}) or {}).get("count", 0) or 0)
        no_proof = int((breakdown.get("ABSENCE_DE_PREUVE", {}) or {}).get("count", 0) or 0)
        compliance_rate = float(a3.get("compliance_rate", 0) or 0)
        kpi_a3 = [
            ["Indicateur A3", "Valeur"],
            ["Total checks", str(int(a3.get("total_checks", 0) or 0))],
            ["Taux conformite", f"{round(compliance_rate * 100, 1)}%"],
            ["Conformes", str(conformes)],
            ["Partiels", str(partiels)],
            ["Non conformes", str(non_conf)],
            ["Absence de preuve", str(no_proof)],
            [
                "NC reglementaires",
                f"{int(nc.get('total', 0) or 0)} (critiques {int(nc.get('critical', 0) or 0)}, majeures {int(nc.get('major', 0) or 0)}, mineures {int(nc.get('minor', 0) or 0)})",
            ],
            ["Actions ouvertes", str(actions_open)],
        ]
        table_a3 = Table(kpi_a3, colWidths=[9 * cm, 7 * cm], repeatRows=1)
        table_a3.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8.6),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(Paragraph("3. Conformite operationnelle (A3)", h2_style))
        story.append(table_a3)
        story.append(Spacer(1, 8))

        domain_map: dict[str, int] = {}
        for row in list(a2.get("decisions", []) or []):
            status = str(row.get("status") or "").upper()
            if status not in {"APPLICABLE", "APPLICABLE_SOUS_CONDITIONS"}:
                continue
            domain = _clean_pdf_text(row.get("qse_domain") or "N/A", max_len=36)
            domain_map[domain] = int(domain_map.get(domain, 0)) + 1
        top_domains = sorted(domain_map.items(), key=lambda kv: kv[1], reverse=True)[:8]
        if top_domains:
            rows_domain = [["Domaine", "Exigences applicables"]]
            for domain, count in top_domains:
                rows_domain.append([domain, str(int(count))])
            domain_table = Table(rows_domain, colWidths=[12 * cm, 4 * cm], repeatRows=1)
            domain_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0fdfa")]),
                        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            story.append(Paragraph("4. Domaines les plus exposes (A2)", h2_style))
            story.append(domain_table)
            story.append(Spacer(1, 8))

        worst_items = list(a3.get("worst_items", []) or [])
        worst_items.sort(key=lambda item: float(item.get("score") or 0))
        if worst_items:
            rows_worst = [["Domaine", "Statut", "Exigence", "Preuves manquantes"]]
            for item in worst_items[:25]:
                rows_worst.append(
                    [
                        Paragraph(_clean_pdf_text(item.get("qse_domain") or "-", max_len=28), body_style),
                        Paragraph(_clean_pdf_text(str(item.get("status") or "-").replace("_", " "), max_len=36), body_style),
                        Paragraph(_clean_pdf_text(item.get("requirement_text") or "-", max_len=190), body_style),
                        Paragraph(_clean_pdf_text(item.get("missing_proofs") or "-", max_len=180), body_style),
                    ]
                )
            worst_table = Table(
                rows_worst,
                colWidths=[2.8 * cm, 3.0 * cm, 6.5 * cm, 3.7 * cm],
                repeatRows=1,
            )
            worst_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7f1d1d")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 7.6),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fff1f2")]),
                        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.append(Paragraph("5. Top risques critiques", h2_style))
            story.append(worst_table)

        story.append(Spacer(1, 14))
        story.append(HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#e2e8f0")))
        story.append(Paragraph("Document genere automatiquement par QALITAS", caption_style))

        doc.build(story)
        buffer.seek(0)
        filename = f"executive_summary_{tenant_id}_{date.today().isoformat()}.pdf"
        return StreamingResponse(
            buffer,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"reportlab non disponible: {e}")


@app.get("/api/v2/reports/action-plan.pdf")
def get_action_plan_report_pdf(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> Any:
    """Genere un plan d'actions correctives PDF base sur A3."""
    from fastapi.responses import StreamingResponse
    from datetime import date
    import io

    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    from a3_compliance_engine import get_compliance_summary

    data = get_compliance_summary(tenant_id)
    if data.get("error"):
        raise HTTPException(status_code=404, detail=str(data.get("error")))

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate,
            Paragraph,
            Spacer,
            Table,
            TableStyle,
            HRFlowable,
        )

        severity_rank = {"CRITIQUE": 0, "MAJEURE": 1, "MINEURE": 2}
        actions_raw = list(data.get("recent_actions", []) or [])
        actions_open = [
            a
            for a in actions_raw
            if str(a.get("state") or "").upper() not in {"REALISEE", "CLOTUREE", "ANNULEE"}
        ]
        actions_open.sort(
            key=lambda a: (
                severity_rank.get(str(a.get("gap_severity") or "").upper(), 3),
                str(a.get("due_date") or "9999-12-31"),
            )
        )

        gaps_raw = list(data.get("recent_gaps", []) or [])
        gaps_nc = [g for g in gaps_raw if str(g.get("gap_type") or "").upper() == "NC_REGLEMENTAIRE"]
        gaps_nc.sort(key=lambda g: severity_rank.get(str(g.get("severity") or "").upper(), 3))

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=1.8 * cm,
            rightMargin=1.8 * cm,
            topMargin=1.6 * cm,
            bottomMargin=1.6 * cm,
        )
        styles = getSampleStyleSheet()
        story: list[Any] = []

        title_style = ParagraphStyle(
            "planTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            textColor=colors.HexColor("#1f3ea6"),
            spaceAfter=6,
        )
        h2_style = ParagraphStyle(
            "planH2",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            textColor=colors.HexColor("#0f172a"),
            spaceBefore=10,
            spaceAfter=4,
        )
        body_style = ParagraphStyle(
            "planBody",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.3,
            leading=11,
        )
        caption_style = ParagraphStyle(
            "planCaption",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8,
            textColor=colors.HexColor("#475569"),
        )

        today_str = date.today().strftime("%d/%m/%Y")
        story.append(Paragraph("QALITAS - Plan d'actions correctives", title_style))
        story.append(
            Paragraph(
                f"Tenant: <b>{_clean_pdf_text(tenant_id, max_len=80)}</b> | Date: <b>{today_str}</b> | Source A3",
                caption_style,
            )
        )
        story.append(HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#2563eb")))
        story.append(Spacer(1, 8))

        critical_count = len([g for g in gaps_nc if str(g.get("severity") or "").upper() == "CRITIQUE"])
        major_count = len([g for g in gaps_nc if str(g.get("severity") or "").upper() == "MAJEURE"])
        minor_count = len([g for g in gaps_nc if str(g.get("severity") or "").upper() == "MINEURE"])

        summary_rows = [
            ["Indicateur", "Valeur"],
            ["Actions ouvertes", str(len(actions_open))],
            ["Ecarts NC reglementaires", str(len(gaps_nc))],
            ["Critiques", str(critical_count)],
            ["Majeures", str(major_count)],
            ["Mineures", str(minor_count)],
        ]
        summary_table = Table(summary_rows, colWidths=[9 * cm, 7 * cm], repeatRows=1)
        summary_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3ea6")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(Paragraph("1. Priorisation", h2_style))
        story.append(summary_table)
        story.append(Spacer(1, 8))

        if actions_open:
            action_rows = [[
                "Priorite",
                "Etat",
                "Action",
                "Responsable",
                "Echeance",
                "Preuve attendue",
            ]]
            for action in actions_open[:90]:
                action_rows.append(
                    [
                        _clean_pdf_text(action.get("gap_severity") or "MINEURE", max_len=12),
                        _clean_pdf_text(str(action.get("state") or "-").replace("_", " "), max_len=18),
                        Paragraph(_clean_pdf_text(action.get("action_title") or "-", max_len=120), body_style),
                        Paragraph(_clean_pdf_text(action.get("responsible") or "-", max_len=38), body_style),
                        _clean_pdf_text(action.get("due_date") or "-", max_len=18),
                        Paragraph(_clean_pdf_text(action.get("expected_proof") or "-", max_len=140), body_style),
                    ]
                )
            action_table = Table(
                action_rows,
                colWidths=[2.0 * cm, 2.2 * cm, 4.9 * cm, 2.4 * cm, 1.9 * cm, 2.6 * cm],
                repeatRows=1,
            )
            action_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#166534")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 7.4),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0fdf4")]),
                        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.append(Paragraph("2. Actions correctives ouvertes", h2_style))
            story.append(action_table)
            story.append(Spacer(1, 8))
        else:
            story.append(Paragraph("2. Actions correctives ouvertes", h2_style))
            story.append(Paragraph("Aucune action corrective ouverte.", body_style))
            story.append(Spacer(1, 6))

        if gaps_nc:
            gap_rows = [["Severite", "Domaine", "Ecart", "Preuve manquante", "Priorite"]]
            for gap in gaps_nc[:100]:
                gap_rows.append(
                    [
                        _clean_pdf_text(gap.get("severity") or "-", max_len=12),
                        Paragraph(_clean_pdf_text(gap.get("qse_domain") or "-", max_len=28), body_style),
                        Paragraph(_clean_pdf_text(gap.get("description") or "-", max_len=165), body_style),
                        Paragraph(_clean_pdf_text(gap.get("missing_proof") or "-", max_len=120), body_style),
                        _clean_pdf_text(gap.get("treatment_priority") or "-", max_len=18),
                    ]
                )
            gap_table = Table(
                gap_rows,
                colWidths=[2.0 * cm, 2.5 * cm, 5.3 * cm, 4.2 * cm, 2.0 * cm],
                repeatRows=1,
            )
            gap_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7f1d1d")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 7.4),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fff1f2")]),
                        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.append(Paragraph("3. Ecarts NC reglementaires de reference", h2_style))
            story.append(gap_table)
            story.append(Spacer(1, 8))

        story.append(Paragraph("4. Cadence recommandee", h2_style))
        story.append(Paragraph("1) Revue hebdomadaire des NC critiques/majeures.", body_style))
        story.append(Paragraph("2) Suivi des preuves attendues avant echeance.", body_style))
        story.append(Paragraph("3) Relance A3 apres cloture des actions prioritaires.", body_style))

        story.append(Spacer(1, 12))
        story.append(HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#e2e8f0")))
        story.append(Paragraph("Document genere automatiquement par QALITAS", caption_style))

        doc.build(story)
        buffer.seek(0)
        filename = f"action_plan_{tenant_id}_{date.today().isoformat()}.pdf"
        return StreamingResponse(
            buffer,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"reportlab non disponible: {e}")


@app.post("/api/v2/compliance/run")
def run_compliance(
    body: ComplianceRunRequest,
    background_tasks: BackgroundTasks,
    mode: Literal["full", "delta"] | None = Query(default=None),
    force: bool | None = Query(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Lance le pipeline de conformite en arriere-plan."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, body.tenant_id)
    _require_role(session, WRITE_ROLES, "lancer l'agent A3")
    run_mode = str(mode or body.mode or "delta").lower()
    if run_mode not in {"full", "delta"}:
        raise HTTPException(status_code=400, detail="mode doit etre 'full' ou 'delta'")
    force_flag = body.force if force is None else bool(force)
    job_id = str(uuid.uuid4())
    pipeline_plan, pipeline_steps = _build_followup_pipeline_context("A3")
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "type": "compliance",
            "tenant_id": tenant_id,
            "doc_id": body.doc_id,
            "status": "PENDING",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "stop_requested": False,
            "pipeline_plan": pipeline_plan,
            "pipeline_steps": pipeline_steps,
            "current_stage": "A3",
            "stage_message": "Conformité en attente",
            "error_category": None,
            "failed_step": None,
            "run_params": {
                "tenant_id": tenant_id,
                "doc_id": body.doc_id,
                "limit": body.limit,
                "delay_between": body.delay_between,
                "mode": run_mode,
                "force": force_flag,
                "site_ids": list(body.site_ids or []),
                "process_ids": list(body.process_ids or []),
                "activity_ids": list(body.activity_ids or []),
            },
        }
        _persist_jobs()

    def _run() -> None:
        should_pause = False
        with JOBS_LOCK:
            current_item = JOBS.get(job_id) or {}
            current_status = str(current_item.get("status") or "").upper()
            stop_flag = bool(current_item.get("stop_requested"))
            if current_status == "PAUSED" or stop_flag:
                should_pause = True
        if should_pause:
            _mark_pipeline_stage(job_id, "A3", "PAUSED", stage_message="Conformité en pause")
            _update_job(job_id, status="PAUSED", stopped_at=_now_iso(), current_stage="A3", stage_message="Conformité en pause")
            return
        _mark_pipeline_stage(job_id, "A3", "RUNNING", stage_message="Conformité en cours")
        _update_job(job_id, status="RUNNING", current_stage="A3", stage_message="Conformité en cours")
        try:
            from a3_compliance_engine import run_compliance as _engine
            result = _call_with_supported_kwargs(
                _engine,
                tenant_id=tenant_id,
                doc_id=body.doc_id,
                limit=body.limit,
                delay_between=body.delay_between,
                mode=run_mode,
                force=force_flag,
                force_recompute=force_flag,
                site_ids=list(body.site_ids or []),
                process_ids=list(body.process_ids or []),
                activity_ids=list(body.activity_ids or []),
                stop_requested=lambda: _job_stop_requested(job_id),
            )
            if bool((result or {}).get("stopped")):
                _mark_pipeline_stage(job_id, "A3", "PAUSED", stage_message="Conformité en pause")
                _update_job(
                    job_id,
                    status="PAUSED",
                    result=result,
                    a3_result=result,
                    stopped_at=_now_iso(),
                    finished_at=_now_iso(),
                    stop_requested=False,
                    current_stage="A3",
                    stage_message="Conformité en pause",
                )
                return

            stage_status, stage_message, error_category = _a3_followup_outcome(result or {})
            _mark_pipeline_stage(job_id, "A3", stage_status, stage_message=stage_message)
            next_status = "FAILED" if stage_status == "ERROR" else "DONE"
            _update_job(
                job_id,
                status=next_status,
                result=result,
                a3_result=result,
                updated_at=_now_iso(),
                finished_at=_now_iso(),
                stop_requested=False,
                current_stage=None if next_status == "DONE" else "A3",
                stage_message=stage_message,
                error_category=error_category,
                failed_step="A3_PIPELINE" if next_status == "FAILED" else None,
                error=stage_message if next_status == "FAILED" else None,
            )
        except Exception as e:
            err_text = str(e)
            _mark_pipeline_stage(job_id, "A3", "ERROR", stage_message="Conformité échouée")
            _update_job(
                job_id,
                status="ERROR",
                error=err_text,
                updated_at=_now_iso(),
                finished_at=_now_iso(),
                stop_requested=False,
                current_stage="A3",
                stage_message="Conformité échouée",
                error_category=_classify_error(err_text),
                failed_step="A3_PIPELINE",
            )

    background_tasks.add_task(_run)
    return {
        "job_id": job_id,
        "status": "PENDING",
        "tenant_id": tenant_id,
        "current_stage": "A3",
        "stage_message": "Conformité en attente",
        "pipeline_plan": pipeline_plan,
        "pipeline_steps": pipeline_steps,
    }


# --- Agent 4 - Chat RAG -----------------------------------------------------
# Le chat combine le RAG reglementaire avec le contexte operationnel A2/A3:
# exigences applicables, controles, preuves, ecarts, actions et KPI.

class ChatRequest(BaseModel):
    question: str
    tenant_id: str = ""
    session_id: str | None = None
    user_role: str = "expert"
    response_format: str = "synthesis"


class ChatActionCreateRequest(BaseModel):
    tenant_id: str = ""
    action_title: str
    action_description: str = ""
    responsible: str = ""
    due_date: str | None = None
    expected_proof: str = ""


# Schemas utilises par les routes donnees entreprise et onboarding.
# Ils restent ici pour eviter un refactor de fichiers juste avant la soutenance.

class CompanySitePayload(BaseModel):
    site_id: str | None = None
    site_code: str = ""
    name: str = ""
    city: str = ""
    region: str = ""
    type: str = ""
    employee_count: int | None = None
    main_activities: str = ""


class CompanyProcessPayload(BaseModel):
    process_id: str | None = None
    site_id: str | None = None
    process_code: str = ""
    name: str = ""
    description: str = ""


class CompanyActivityPayload(BaseModel):
    activity_id: str | None = None
    site_id: str | None = None
    process_id: str | None = None
    process_name: str = ""
    name: str = ""
    code: str = ""
    description: str = ""


class CompanyProductPayload(BaseModel):
    product_id: str | None = None
    reference: str = ""
    designation: str = ""
    family: str = ""
    category: str = ""
    product_type: str = ""
    nature: str = ""
    unit: str = ""
    site_name: str = ""
    is_active: bool = True


class CompanyChemicalsUpsertRequest(BaseModel):
    tenant_id: str = ""
    chemicals: list[str] = Field(default_factory=list)


class CompanyProfileUpsertRequest(BaseModel):
    tenant_id: str = ""
    company_name: str
    sector: str = ""
    sub_sector: str = ""
    country: str = "TN"
    certifications: list[str] = Field(default_factory=list)
    headcount: int | None = None
    main_activities: str = ""
    chemicals: list[str] = Field(default_factory=list)
    site: CompanySitePayload = Field(default_factory=CompanySitePayload)
    sites: list[CompanySitePayload] = Field(default_factory=list)
    processes: list[CompanyProcessPayload] = Field(default_factory=list)
    activities: list[CompanyActivityPayload] = Field(default_factory=list)
    products: list[CompanyProductPayload] = Field(default_factory=list)


class OnboardingAdminPayload(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "ADMIN_QHSE"


class CompanyOnboardingRequest(BaseModel):
    tenant_id: str
    company_name: str
    sector: str = ""
    sub_sector: str = ""
    country: str = "TN"
    certifications: list[str] = Field(default_factory=list)
    headcount: int | None = None
    main_activities: str = ""
    chemicals: list[str] = Field(default_factory=list)
    site: CompanySitePayload = Field(default_factory=CompanySitePayload)
    sites: list[CompanySitePayload] = Field(default_factory=list)
    processes: list[CompanyProcessPayload] = Field(default_factory=list)
    activities: list[CompanyActivityPayload] = Field(default_factory=list)
    products: list[CompanyProductPayload] = Field(default_factory=list)
    initial_admin: OnboardingAdminPayload


# Routes Agent 4: question/reponse, creation d'action, historique et index RAG.

@app.post("/api/v2/chat")
def chat_endpoint(
    body: ChatRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Pose une question reglementaire au Chat Expert (Agent 4 RAG)."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, body.tenant_id)
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="Question vide")
    role = str(session.get("role") or "").upper()
    persist_history = role != "AUDITEUR"
    from a4_chat_engine import chat
    result = chat(
        question=body.question,
        tenant_id=tenant_id,
        session_id=body.session_id,
        user_role=body.user_role,
        response_format=body.response_format,
        persist_history=persist_history,
    )
    if isinstance(result, dict) and result.get("error"):
        msg = str(result.get("error") or "Erreur chat")
        if "Tenant inconnu" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "Session introuvable" in msg or "n'appartient pas" in msg:
            raise HTTPException(status_code=403, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    return result


@app.post("/api/v2/chat/actions")
def create_chat_action(
    body: ChatActionCreateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Transforme une recommandation A4 en action corrective planifiee."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, body.tenant_id)
    _require_role(session, WRITE_ROLES, "creer une action depuis l'assistant")

    title = str(body.action_title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="action_title est obligatoire")
    if len(title) > 240:
        title = title[:240].rstrip()

    due_date_value = None
    due_raw = str(body.due_date or "").strip()
    if due_raw:
        try:
            due_date_value = datetime.strptime(due_raw[:10], "%Y-%m-%d").date()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="due_date doit etre au format YYYY-MM-DD") from exc

    dsn = _load_env_dsn()
    with psycopg.connect(dsn, tenant_id=tenant_id) as conn:
        profile_id = _require_profile_id(conn, tenant_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO corrective_actions
                    (profile_id, action_title, action_description, action_type,
                     responsible, due_date, expected_proof, state,
                     scope_level, scope_key, scope_label)
                VALUES
                    (%s::uuid, %s, %s, 'CORRECTIVE',
                     %s, %s, %s, 'PLANIFIEE',
                     'ORGANIZATION', 'ORGANIZATION', 'ORGANIZATION')
                RETURNING action_id::text, created_at::text
                """,
                (
                    profile_id,
                    title,
                    str(body.action_description or "").strip() or None,
                    str(body.responsible or "").strip() or None,
                    due_date_value,
                    str(body.expected_proof or "").strip() or None,
                ),
            )
            row = cur.fetchone()
        conn.commit()

    return {
        "status": "ok",
        "tenant_id": tenant_id,
        "action": {
            "action_id": str(row[0]),
            "action_title": title,
            "state": "PLANIFIEE",
            "created_at": str(row[1]),
        },
    }


@app.get("/api/v2/chat/sessions/{session_id}/history")
def get_chat_history(
    session_id: str,
    tenant_id: str = Query(default="", description="Identifiant du tenant proprietaire de la session"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Retourne l'historique d'une session de chat (isole par tenant)."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    from a4_chat_engine import get_session_history
    history = get_session_history(session_id, tenant_id)
    return {"session_id": session_id, "messages": history, "total": len(history)}


@app.post("/api/v2/chat/index")
def index_embeddings(
    tenant_id: str = Query(default=""),
    force: bool = Query(default=False),
    background_tasks: BackgroundTasks = None,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Declenche l'indexation des embeddings pour l'Agent 4 RAG (pgvector)."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "re-indexer les embeddings A4")
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "type": "embedding_index",
            "tenant_id": tenant_id,
            "status": "PENDING",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "stop_requested": False,
            "run_params": {"tenant_id": tenant_id, "force": bool(force)},
        }
        _persist_jobs()

    def _run() -> None:
        with JOBS_LOCK:
            current_item = JOBS.get(job_id) or {}
            current_status = str(current_item.get("status") or "").upper()
            stop_flag = bool(current_item.get("stop_requested"))
            if current_status == "PAUSED" or stop_flag:
                JOBS[job_id].update({"status": "PAUSED", "updated_at": _now_iso(), "stopped_at": _now_iso()})
                _persist_jobs()
                return
            JOBS[job_id]["status"] = "RUNNING"
            _persist_jobs()
        try:
            from a4_chat_engine import index_requirements
            count = index_requirements(tenant_id, force=force)
            with JOBS_LOCK:
                JOBS[job_id].update({
                    "status": "DONE",
                    "indexed": count,
                    "updated_at": _now_iso(),
                    "finished_at": _now_iso(),
                    "stop_requested": False,
                })
                _persist_jobs()
        except Exception as e:
            with JOBS_LOCK:
                JOBS[job_id].update({
                    "status": "ERROR",
                    "error": str(e),
                    "updated_at": _now_iso(),
                    "finished_at": _now_iso(),
                    "stop_requested": False,
                })
                _persist_jobs()

    background_tasks.add_task(_run)
    return {"job_id": job_id, "status": "PENDING", "tenant_id": tenant_id}


# --- Tenants et onboarding -------------------------------------------------
# Routes admin: lister les tenants visibles et creer une nouvelle entreprise
# avec son profil initial, ses donnees de base et son premier utilisateur.

@app.get("/api/v2/tenants")
def list_tenants(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Liste les entreprises visibles pour la session courante."""
    session = _auth_from_header(authorization)
    items = _list_visible_tenants_for_session(session)
    return {
        "total": len(items),
        "active_tenant_id": _session_active_tenant(session),
        "items": items,
    }


@app.post("/api/v2/admin/onboarding/company")
def onboard_company(
    body: CompanyOnboardingRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Provisionne une nouvelle entreprise, son profil initial et son premier utilisateur admin."""
    session = _auth_from_header(authorization)
    if not _is_super_admin(session):
        raise HTTPException(status_code=403, detail="Seul SUPER_ADMIN peut onboarder une nouvelle entreprise")

    tenant_id = _parse_tenant_id(body.tenant_id, field_name="tenant_id")
    if _tenant_exists(tenant_id):
        raise HTTPException(status_code=409, detail=f"tenant_id deja utilise: {tenant_id}")

    admin_spec = _validate_onboarding_admin_payload(body.initial_admin)
    profile_payload = CompanyProfileUpsertRequest.model_validate(body.model_dump(exclude={"initial_admin"}))

    dsn = _load_env_dsn()
    with psycopg.connect(dsn, tenant_id=tenant_id) as conn:
        profile = _upsert_company_profile_in_conn(conn, tenant_id, profile_payload)
        created_admin = _create_tenant_user_in_conn(
            conn,
            tenant_id,
            username=admin_spec["username"],
            password=admin_spec["password"],
            role=admin_spec["role"],
            display_name=admin_spec["display_name"],
        )
        conn.commit()

    return {
        "status": "ok",
        "message": "Entreprise onboardee avec succes",
        "tenant_id": tenant_id,
        "company_name": profile.get("company_name") or body.company_name,
        "initial_admin": {
            "user_id": created_admin["user_id"],
            "username": created_admin["username"],
            "role": created_admin["role"],
            "display_name": created_admin["display_name"],
        },
        "profile": profile,
    }


# --- Agent 2 - Profil entreprise -------------------------------------------

def _normalize_certifications(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        item = str(raw or "").strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:20]


def _model_fields_set(value: Any) -> set[str]:
    if hasattr(value, "model_fields_set"):
        return set(getattr(value, "model_fields_set") or set())
    return set(getattr(value, "__fields_set__", set()))


def _normalize_auth_username(value: str, *, field_name: str = "username") -> str:
    username = str(value or "").strip().lower()
    if not username:
        raise HTTPException(status_code=400, detail=f"{field_name} est obligatoire")
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail=f"{field_name} invalide")
    return username


def _validate_onboarding_admin_payload(payload: OnboardingAdminPayload) -> dict[str, str]:
    username = _normalize_auth_username(payload.username, field_name="initial_admin.username")
    password = str(payload.password or "")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="initial_admin.password doit contenir au moins 8 caracteres")
    role = str(payload.role or "ADMIN_QHSE").strip().upper()
    if role not in TENANT_USER_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"initial_admin.role invalide. Valeurs autorisees: {', '.join(sorted(TENANT_USER_ROLES))}",
        )
    display_name = str(payload.display_name or "").strip() or username
    return {
        "username": username,
        "password": password,
        "role": role,
        "display_name": display_name,
    }


def _prepare_company_profile_write(body: Any) -> dict[str, Any]:
    company_name = str(body.company_name or "").strip()
    if not company_name:
        raise HTTPException(status_code=400, detail="company_name est obligatoire")

    sector = str(body.sector or "").strip()
    sub_sector = str(body.sub_sector or "").strip()
    country = str(body.country or "TN").strip().upper()[:8] or "TN"
    certifications = _normalize_certifications(body.certifications or [])

    headcount = body.headcount
    if headcount is not None and int(headcount) < 0:
        raise HTTPException(status_code=400, detail="headcount invalide")

    fields_set = _model_fields_set(body)
    site_payloads = list(body.sites or [])
    if not site_payloads:
        legacy_site = CompanySitePayload(
            site_id=body.site.site_id if body.site else None,
            site_code=body.site.site_code if body.site else "",
            name=body.site.name if body.site else "",
            city=body.site.city if body.site else "",
            region=body.site.region if body.site else "",
            type=body.site.type if body.site else "",
            employee_count=(body.site.employee_count if body.site and body.site.employee_count is not None else headcount),
            main_activities=(body.site.main_activities if body.site and body.site.main_activities else body.main_activities),
        )
        if any([
            str(legacy_site.name or "").strip(),
            legacy_site.employee_count is not None,
            str(legacy_site.main_activities or "").strip(),
        ]):
            site_payloads = [legacy_site]

    for site in site_payloads:
        if site.employee_count is not None and int(site.employee_count) < 0:
            raise HTTPException(status_code=400, detail="site.employee_count invalide")

    return {
        "company_name": company_name,
        "sector": sector,
        "sub_sector": sub_sector,
        "country": country,
        "certifications": certifications,
        "headcount": headcount,
        "main_activities": str(body.main_activities or "").strip(),
        "site_payloads": site_payloads,
        "processes": list(body.processes or []),
        "activities": list(body.activities or []),
        "products": list(body.products or []),
        "chemicals": list(body.chemicals or []),
        "fields_set": fields_set,
    }


def _upsert_company_profile_in_conn(conn: psycopg.Connection, tenant_id: str, body: Any) -> dict[str, Any]:
    prepared = _prepare_company_profile_write(body)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO company_profiles
                (tenant_id, company_name, sector, sub_sector, country, certifications, headcount_total, main_activities)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                company_name   = EXCLUDED.company_name,
                sector         = EXCLUDED.sector,
                sub_sector     = EXCLUDED.sub_sector,
                country        = EXCLUDED.country,
                certifications = EXCLUDED.certifications,
                headcount_total= EXCLUDED.headcount_total,
                main_activities= EXCLUDED.main_activities,
                updated_at     = now()
            RETURNING profile_id::text
            """,
            (
                tenant_id,
                prepared["company_name"],
                prepared["sector"] or None,
                prepared["sub_sector"] or None,
                prepared["country"],
                prepared["certifications"],
                prepared["headcount"],
                prepared["main_activities"] or None,
            ),
        )
        profile_id = str(cur.fetchone()[0])

        for site in prepared["site_payloads"]:
            site_name = str(site.name or "").strip()
            if not site_name:
                continue
            site_id = str(site.site_id or "").strip()
            site_code = str(site.site_code or "").strip()
            site_city = str(site.city or "").strip()
            site_region = str(site.region or "").strip()
            site_type = str(site.type or "").strip()
            site_main_activities = str(site.main_activities or "").strip()
            site_employee_count = site.employee_count

            target_site_id = site_id or ""
            if not target_site_id and site_code:
                cur.execute(
                    """
                    SELECT site_id::text
                    FROM company_sites
                    WHERE profile_id = %s::uuid
                      AND LOWER(COALESCE(site_code, '')) = LOWER(%s)
                    LIMIT 1
                    """,
                    (profile_id, site_code),
                )
                row = cur.fetchone()
                target_site_id = str(row[0]) if row else ""
            if not target_site_id:
                cur.execute(
                    """
                    SELECT site_id::text
                    FROM company_sites
                    WHERE profile_id = %s::uuid
                      AND LOWER(COALESCE(site_name, '')) = LOWER(%s)
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (profile_id, site_name),
                )
                row = cur.fetchone()
                target_site_id = str(row[0]) if row else ""

            if target_site_id:
                cur.execute(
                    """
                    UPDATE company_sites
                    SET site_code = %s,
                        site_name = %s,
                        city = %s,
                        region = %s,
                        site_type = %s,
                        employee_count = %s,
                        main_activities = %s
                    WHERE site_id = %s::uuid
                      AND profile_id = %s::uuid
                    """,
                    (
                        site_code or None,
                        site_name,
                        site_city or None,
                        site_region or None,
                        site_type or None,
                        site_employee_count,
                        site_main_activities or None,
                        target_site_id,
                        profile_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO company_sites
                        (profile_id, site_code, site_name, city, region, site_type, employee_count, main_activities)
                    VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        profile_id,
                        site_code or None,
                        site_name,
                        site_city or None,
                        site_region or None,
                        site_type or None,
                        site_employee_count,
                        site_main_activities or None,
                    ),
                )

        if "processes" in prepared["fields_set"]:
            for item in prepared["processes"]:
                process_name = str(item.name or "").strip()
                if not process_name:
                    continue
                process_id = str(item.process_id or "").strip()
                site_id = str(item.site_id or "").strip() or None
                process_code = str(item.process_code or "").strip()
                description = str(item.description or "").strip()
                if process_id:
                    cur.execute(
                        """
                        UPDATE company_processes
                        SET site_id = %s::uuid,
                            process_code = %s,
                            process_name = %s,
                            description = %s
                        WHERE process_id = %s::uuid
                          AND profile_id = %s::uuid
                        """,
                        (site_id, process_code or None, process_name, description or None, process_id, profile_id),
                    )
                else:
                    cur.execute(
                        """
                        SELECT process_id::text
                        FROM company_processes
                        WHERE profile_id = %s::uuid
                          AND COALESCE(site_id::text, '') = COALESCE(%s, '')
                          AND LOWER(process_name) = LOWER(%s)
                        LIMIT 1
                        """,
                        (profile_id, site_id or "", process_name),
                    )
                    row = cur.fetchone()
                    if row:
                        cur.execute(
                            """
                            UPDATE company_processes
                            SET process_code = %s,
                                description = %s
                            WHERE process_id = %s::uuid
                            """,
                            (process_code or None, description or None, str(row[0])),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO company_processes
                                (profile_id, site_id, process_code, process_name, description)
                            VALUES (%s::uuid, %s::uuid, %s, %s, %s)
                            """,
                            (profile_id, site_id, process_code or None, process_name, description or None),
                        )

        if "activities" in prepared["fields_set"]:
            for item in prepared["activities"]:
                activity_name = str(item.name or "").strip()
                if not activity_name:
                    continue
                activity_id = str(item.activity_id or "").strip()
                site_id = str(item.site_id or "").strip() or None
                process_id = str(item.process_id or "").strip() or None
                process_name = str(item.process_name or "").strip()
                activity_code = str(item.code or "").strip()
                description = str(item.description or "").strip()

                if not process_id and process_name:
                    cur.execute(
                        """
                        SELECT process_id::text
                        FROM company_processes
                        WHERE profile_id = %s::uuid
                          AND COALESCE(site_id::text, '') = COALESCE(%s, '')
                          AND LOWER(process_name) = LOWER(%s)
                        LIMIT 1
                        """,
                        (profile_id, site_id or "", process_name),
                    )
                    row = cur.fetchone()
                    if row:
                        process_id = str(row[0])
                    else:
                        cur.execute(
                            """
                            INSERT INTO company_processes
                                (profile_id, site_id, process_name)
                            VALUES (%s::uuid, %s::uuid, %s)
                            RETURNING process_id::text
                            """,
                            (profile_id, site_id, process_name),
                        )
                        process_id = str(cur.fetchone()[0])

                if activity_id:
                    cur.execute(
                        """
                        UPDATE company_activities
                        SET site_id = %s::uuid,
                            process_id = %s::uuid,
                            process_name = %s,
                            activity_name = %s,
                            activity_code = %s,
                            description = %s
                        WHERE activity_id = %s::uuid
                          AND profile_id = %s::uuid
                        """,
                        (
                            site_id,
                            process_id,
                            process_name or None,
                            activity_name,
                            activity_code or None,
                            description or None,
                            activity_id,
                            profile_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        SELECT activity_id::text
                        FROM company_activities
                        WHERE profile_id = %s::uuid
                          AND COALESCE(site_id::text, '') = COALESCE(%s, '')
                          AND LOWER(COALESCE(process_name, '')) = LOWER(%s)
                          AND LOWER(COALESCE(activity_name, '')) = LOWER(%s)
                        LIMIT 1
                        """,
                        (profile_id, site_id or "", process_name, activity_name),
                    )
                    row = cur.fetchone()
                    if row:
                        cur.execute(
                            """
                            UPDATE company_activities
                            SET process_id = %s::uuid,
                                activity_code = %s,
                                description = %s
                            WHERE activity_id = %s::uuid
                            """,
                            (process_id, activity_code or None, description or None, str(row[0])),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO company_activities
                                (profile_id, site_id, process_id, process_name, activity_name, activity_code, description)
                            VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s)
                            """,
                            (
                                profile_id,
                                site_id,
                                process_id,
                                process_name or None,
                                activity_name,
                                activity_code or None,
                                description or None,
                            ),
                        )

        if "products" in prepared["fields_set"]:
            for item in prepared["products"]:
                designation = str(item.designation or "").strip()
                if not designation:
                    continue
                product_id = str(item.product_id or "").strip()
                reference = str(item.reference or "").strip()
                family = str(item.family or "").strip()
                category_value = str(item.category or "").strip()
                product_type = str(item.product_type or "").strip()
                nature_value = str(item.nature or "").strip()
                unit = str(item.unit or "").strip()
                site_name = str(item.site_name or "").strip()
                if product_id:
                    cur.execute(
                        """
                        UPDATE company_products
                        SET reference = %s,
                            designation = %s,
                            family = %s,
                            category = %s,
                            product_type = %s,
                            nature = %s,
                            unit = %s,
                            site_name = %s,
                            is_active = %s
                        WHERE product_id = %s::uuid
                          AND profile_id = %s::uuid
                        """,
                        (
                            reference or None,
                            designation,
                            family or None,
                            category_value or None,
                            product_type or None,
                            nature_value or None,
                            unit or None,
                            site_name or None,
                            bool(item.is_active),
                            product_id,
                            profile_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        SELECT product_id::text
                        FROM company_products
                        WHERE profile_id = %s::uuid
                          AND (
                                (COALESCE(%s, '') <> '' AND LOWER(COALESCE(reference, '')) = LOWER(%s))
                             OR LOWER(COALESCE(designation, '')) = LOWER(%s)
                          )
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (profile_id, reference or "", reference or "", designation),
                    )
                    row = cur.fetchone()
                    if row:
                        cur.execute(
                            """
                            UPDATE company_products
                            SET family = %s,
                                category = %s,
                                product_type = %s,
                                nature = %s,
                                unit = %s,
                                site_name = %s,
                                is_active = %s
                            WHERE product_id = %s::uuid
                            """,
                            (
                                family or None,
                                category_value or None,
                                product_type or None,
                                nature_value or None,
                                unit or None,
                                site_name or None,
                                bool(item.is_active),
                                str(row[0]),
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO company_products
                                (profile_id, reference, designation, family, category, product_type, nature, is_active, unit, site_name)
                            VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                profile_id,
                                reference or None,
                                designation,
                                family or None,
                                category_value or None,
                                product_type or None,
                                nature_value or None,
                                bool(item.is_active),
                                unit or None,
                                site_name or None,
                            ),
                        )

        if "chemicals" in prepared["fields_set"]:
            _sync_company_chemicals(conn, profile_id, prepared["chemicals"])

    return _fetch_company_profile_payload(conn, tenant_id)


def _create_tenant_user_in_conn(
    conn: psycopg.Connection,
    tenant_id: str,
    *,
    username: str,
    password: str,
    role: str,
    display_name: str,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT user_id::text
            FROM app_users
            WHERE tenant_id = %s
              AND LOWER(username) = LOWER(%s)
            LIMIT 1
            """,
            (tenant_id, username),
        )
        row = cur.fetchone()
        if row:
            raise HTTPException(status_code=409, detail="initial_admin.username existe deja pour ce tenant")
        cur.execute(
            """
            INSERT INTO app_users
                (tenant_id, username, password_hash, role, display_name, is_active)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            RETURNING user_id::text
            """,
            (tenant_id, username, _hash_password(password), role, display_name),
        )
        created = cur.fetchone()
    return {
        "user_id": str(created[0]) if created else "",
        "tenant_id": tenant_id,
        "username": username,
        "role": role,
        "display_name": display_name,
    }


def _fetch_company_sites(conn: psycopg.Connection, profile_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                site_id::text AS site_id,
                COALESCE(site_code, '') AS site_code,
                COALESCE(site_name, '') AS name,
                COALESCE(city, '') AS city,
                COALESCE(region, '') AS region,
                COALESCE(site_type, '') AS type,
                employee_count,
                COALESCE(main_activities, '') AS main_activities,
                created_at::text AS created_at
            FROM company_sites
            WHERE profile_id = %s::uuid
            ORDER BY created_at NULLS LAST, site_name
            """,
            (profile_id,),
        )
        return _rows_to_dicts(cur)


def _fetch_company_processes(conn: psycopg.Connection, profile_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                process_id::text AS process_id,
                profile_id::text AS profile_id,
                site_id::text AS site_id,
                COALESCE(process_code, '') AS process_code,
                COALESCE(process_name, '') AS name,
                COALESCE(description, '') AS description,
                created_at::text AS created_at
            FROM company_processes
            WHERE profile_id = %s::uuid
            ORDER BY created_at NULLS LAST, process_name
            """,
            (profile_id,),
        )
        return _rows_to_dicts(cur)


def _fetch_company_activities(conn: psycopg.Connection, profile_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                ca.activity_id::text AS activity_id,
                ca.profile_id::text AS profile_id,
                ca.site_id::text AS site_id,
                ca.process_id::text AS process_id,
                COALESCE(cp.process_name, ca.process_name, '') AS process_name,
                COALESCE(ca.activity_name, '') AS name,
                COALESCE(ca.activity_code, '') AS code,
                COALESCE(ca.description, '') AS description,
                ca.created_at::text AS created_at
            FROM company_activities ca
            LEFT JOIN company_processes cp ON cp.process_id = ca.process_id
            WHERE ca.profile_id = %s::uuid
            ORDER BY ca.created_at NULLS LAST, COALESCE(cp.process_name, ca.process_name, ''), COALESCE(ca.activity_name, '')
            """,
            (profile_id,),
        )
        return _rows_to_dicts(cur)


def _fetch_company_products(conn: psycopg.Connection, profile_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                product_id::text AS product_id,
                COALESCE(reference, '') AS reference,
                COALESCE(designation, '') AS designation,
                COALESCE(family, '') AS family,
                COALESCE(category, '') AS category,
                COALESCE(product_type, '') AS product_type,
                COALESCE(nature, '') AS nature,
                COALESCE(unit, '') AS unit,
                COALESCE(site_name, '') AS site_name,
                COALESCE(is_active, TRUE) AS is_active,
                created_at::text AS created_at
            FROM company_products
            WHERE profile_id = %s::uuid
              AND COALESCE(reference, '') NOT LIKE 'chemical:%%'
            ORDER BY created_at NULLS LAST, designation
            """,
            (profile_id,),
        )
        return _rows_to_dicts(cur)


def _fetch_company_chemicals(conn: psycopg.Connection, profile_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT COALESCE(designation, '') AS designation
            FROM company_products
            WHERE profile_id = %s::uuid
              AND (
                    COALESCE(reference, '') LIKE 'chemical:%%'
                 OR UPPER(COALESCE(category, '')) = 'CHEMICAL'
                 OR UPPER(COALESCE(product_type, '')) = 'CHEMICAL'
              )
              AND COALESCE(designation, '') <> ''
            ORDER BY designation
            """,
            (profile_id,),
        )
        return [str(row[0]) for row in cur.fetchall()]


def _sync_company_chemicals(conn: psycopg.Connection, profile_id: str, chemicals: list[str]) -> None:
    names = _normalize_text_list(chemicals or [], limit=500)
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM company_products
            WHERE profile_id = %s::uuid
              AND COALESCE(reference, '') LIKE 'chemical:%%'
            """,
            (profile_id,),
        )
        for name in names:
            chemical_ref = f"chemical:{hashlib.sha1(name.lower().encode('utf-8')).hexdigest()[:12]}"
            cur.execute(
                """
                INSERT INTO company_products
                    (profile_id, reference, designation, family, category, product_type, nature, is_active)
                VALUES (%s::uuid, %s, %s, 'CHEMICAL', 'CHEMICAL', 'CHEMICAL', 'CHEMICAL', TRUE)
                """,
                (profile_id, chemical_ref, name),
            )


def _fetch_company_profile_payload(conn: psycopg.Connection, tenant_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.profile_id, p.company_name, p.sector, p.sub_sector,
                   p.country, p.certifications, p.headcount_total, p.main_activities, p.created_at
            FROM company_profiles p
            WHERE LOWER(COALESCE(p.tenant_id, '')) = LOWER(%s)
            """,
            (tenant_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Tenant inconnu: {tenant_id}")

        cur.execute("SELECT COUNT(*) FROM company_equipment WHERE profile_id=%s", (str(row[0]),))
        eq_count = int(cur.fetchone()[0] or 0)
        cur.execute("SELECT COUNT(*) FROM sst_risks WHERE profile_id=%s", (str(row[0]),))
        sst_count = int(cur.fetchone()[0] or 0)
        cur.execute("SELECT COUNT(*) FROM environmental_aspects WHERE profile_id=%s", (str(row[0]),))
        env_count = int(cur.fetchone()[0] or 0)
        cur.execute("SELECT COUNT(*) FROM nonconformities WHERE profile_id=%s", (str(row[0]),))
        nc_count = int(cur.fetchone()[0] or 0)

    profile_id = str(row[0])
    sites = _fetch_company_sites(conn, profile_id)
    processes = _fetch_company_processes(conn, profile_id)
    activities = _fetch_company_activities(conn, profile_id)
    products = _fetch_company_products(conn, profile_id)
    chemicals = _fetch_company_chemicals(conn, profile_id)
    primary_site = sites[0] if sites else {
        "site_id": None,
        "site_code": "",
        "name": "",
        "city": "",
        "region": "",
        "type": "",
        "employee_count": None,
        "main_activities": "",
        "created_at": None,
    }
    headcount_total = sum(int(site.get("employee_count") or 0) for site in sites)
    company_main_activities = next(
        (str(site.get("main_activities") or "").strip() for site in sites if str(site.get("main_activities") or "").strip()),
        "",
    )

    return {
        "profile_id": profile_id,
        "tenant_id": tenant_id,
        "company_name": row[1],
        "sector": row[2],
        "sub_sector": row[3],
        "country": row[4],
        "certifications": row[5] or [],
        "headcount_total": row[6] if row[6] is not None else headcount_total,
        "main_activities": row[7] or company_main_activities,
        "created_at": row[8].isoformat() if row[8] else None,
        "site": {
            "site_id": primary_site.get("site_id"),
            "site_code": primary_site.get("site_code"),
            "name": primary_site.get("name"),
            "city": primary_site.get("city"),
            "region": primary_site.get("region"),
            "type": primary_site.get("type"),
            "employee_count": primary_site.get("employee_count"),
            "main_activities": primary_site.get("main_activities"),
        },
        "sites": sites,
        "processes": processes,
        "activities": activities,
        "products": products,
        "chemicals": chemicals,
        "data_summary": {
            "sites": len(sites),
            "processes": len(processes),
            "activities": len(activities),
            "products": len(products),
            "chemicals": len(chemicals),
            "equipment": eq_count,
            "sst_risks": sst_count,
            "environmental_aspects": env_count,
            "nonconformities": nc_count,
        },
    }


# ---------------------------------------------------------------------------
# Routes donnees entreprise
# Ces endpoints maintiennent le contexte utilise par A2, A3 et A4:
# profil, sites, processus, activites, produits et substances chimiques.
# ---------------------------------------------------------------------------

@app.get("/api/v2/company/profile")
def get_company_profile(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Retourne le profil entreprise complet (contexte Agent 2)."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        return _fetch_company_profile_payload(conn, tenant_id)


@app.post("/api/v2/company/profile")
def upsert_company_profile(
    body: CompanyProfileUpsertRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Crée ou met à jour le profil entreprise du tenant actif."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, body.tenant_id)
    _require_role(session, WRITE_ROLES, "modifier le profil entreprise")

    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile = _upsert_company_profile_in_conn(conn, tenant_id, body)
        conn.commit()
    return {"status": "ok", "message": "Profil entreprise sauvegarde", "profile": profile}


@app.get("/api/v2/company/sites")
def list_company_sites(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _find_profile_id(conn, tenant_id)
        items = _fetch_company_sites(conn, profile_id) if profile_id else []
    return {"tenant_id": tenant_id, "total": len(items), "items": items}


@app.post("/api/v2/company/sites")
def upsert_company_site(
    body: CompanySitePayload,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "modifier les sites entreprise")
    site_name = str(body.name or "").strip()
    if not site_name:
        raise HTTPException(status_code=400, detail="site.name est obligatoire")
    if body.employee_count is not None and int(body.employee_count) < 0:
        raise HTTPException(status_code=400, detail="site.employee_count invalide")

    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _require_profile_id(conn, tenant_id)
        with conn.cursor() as cur:
            target_site_id = str(body.site_id or "").strip()
            if target_site_id:
                cur.execute(
                    """
                    UPDATE company_sites
                    SET site_code = %s,
                        site_name = %s,
                        city = %s,
                        region = %s,
                        site_type = %s,
                        employee_count = %s,
                        main_activities = %s
                    WHERE site_id = %s::uuid
                      AND profile_id = %s::uuid
                    RETURNING site_id::text
                    """,
                    (
                        str(body.site_code or "").strip() or None,
                        site_name,
                        str(body.city or "").strip() or None,
                        str(body.region or "").strip() or None,
                        str(body.type or "").strip() or None,
                        body.employee_count,
                        str(body.main_activities or "").strip() or None,
                        target_site_id,
                        profile_id,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Site introuvable pour ce tenant")
                target_site_id = str(row[0])
            else:
                cur.execute(
                    """
                    SELECT site_id::text
                    FROM company_sites
                    WHERE profile_id = %s::uuid
                      AND (
                            (COALESCE(%s, '') <> '' AND LOWER(COALESCE(site_code, '')) = LOWER(%s))
                         OR LOWER(COALESCE(site_name, '')) = LOWER(%s)
                      )
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (
                        profile_id,
                        str(body.site_code or "").strip() or "",
                        str(body.site_code or "").strip() or "",
                        site_name,
                    ),
                )
                row = cur.fetchone()
                if row:
                    target_site_id = str(row[0])
                    cur.execute(
                        """
                        UPDATE company_sites
                        SET city = %s,
                            region = %s,
                            site_type = %s,
                            employee_count = %s,
                            main_activities = %s
                        WHERE site_id = %s::uuid
                        """,
                        (
                            str(body.city or "").strip() or None,
                            str(body.region or "").strip() or None,
                            str(body.type or "").strip() or None,
                            body.employee_count,
                            str(body.main_activities or "").strip() or None,
                            target_site_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO company_sites
                            (profile_id, site_code, site_name, city, region, site_type, employee_count, main_activities)
                        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING site_id::text
                        """,
                        (
                            profile_id,
                            str(body.site_code or "").strip() or None,
                            site_name,
                            str(body.city or "").strip() or None,
                            str(body.region or "").strip() or None,
                            str(body.type or "").strip() or None,
                            body.employee_count,
                            str(body.main_activities or "").strip() or None,
                        ),
                    )
                    target_site_id = str(cur.fetchone()[0])
        conn.commit()
        item = next((it for it in _fetch_company_sites(conn, profile_id) if str(it.get("site_id")) == target_site_id), None)
    return {"status": "ok", "tenant_id": tenant_id, "site": item}


@app.delete("/api/v2/company/sites/{site_id}")
def delete_company_site(
    site_id: str,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "supprimer un site entreprise")
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _require_profile_id(conn, tenant_id)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM company_activities WHERE profile_id = %s::uuid AND site_id = %s::uuid", (profile_id, site_id))
            cur.execute("DELETE FROM company_processes WHERE profile_id = %s::uuid AND site_id = %s::uuid", (profile_id, site_id))
            cur.execute("DELETE FROM company_sites WHERE profile_id = %s::uuid AND site_id = %s::uuid", (profile_id, site_id))
            deleted = int(cur.rowcount or 0)
        conn.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="Site introuvable pour ce tenant")
    return {"status": "ok", "tenant_id": tenant_id, "deleted_site_id": site_id}


@app.get("/api/v2/company/processes")
def list_company_processes(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _find_profile_id(conn, tenant_id)
        items = _fetch_company_processes(conn, profile_id) if profile_id else []
    return {"tenant_id": tenant_id, "total": len(items), "items": items}


@app.post("/api/v2/company/processes")
def upsert_company_process(
    body: CompanyProcessPayload,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "modifier les processus entreprise")
    process_name = str(body.name or "").strip()
    if not process_name:
        raise HTTPException(status_code=400, detail="process.name est obligatoire")

    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _require_profile_id(conn, tenant_id)
        with conn.cursor() as cur:
            process_id = str(body.process_id or "").strip()
            if process_id:
                cur.execute(
                    """
                    UPDATE company_processes
                    SET site_id = %s::uuid,
                        process_code = %s,
                        process_name = %s,
                        description = %s
                    WHERE process_id = %s::uuid
                      AND profile_id = %s::uuid
                    RETURNING process_id::text
                    """,
                    (
                        str(body.site_id or "").strip() or None,
                        str(body.process_code or "").strip() or None,
                        process_name,
                        str(body.description or "").strip() or None,
                        process_id,
                        profile_id,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Processus introuvable pour ce tenant")
                process_id = str(row[0])
            else:
                cur.execute(
                    """
                    SELECT process_id::text
                    FROM company_processes
                    WHERE profile_id = %s::uuid
                      AND COALESCE(site_id::text, '') = COALESCE(%s, '')
                      AND LOWER(COALESCE(process_name, '')) = LOWER(%s)
                    LIMIT 1
                    """,
                    (profile_id, str(body.site_id or "").strip() or "", process_name),
                )
                row = cur.fetchone()
                if row:
                    process_id = str(row[0])
                    cur.execute(
                        """
                        UPDATE company_processes
                        SET process_code = %s,
                            description = %s
                        WHERE process_id = %s::uuid
                        """,
                        (
                            str(body.process_code or "").strip() or None,
                            str(body.description or "").strip() or None,
                            process_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO company_processes
                            (profile_id, site_id, process_code, process_name, description)
                        VALUES (%s::uuid, %s::uuid, %s, %s, %s)
                        RETURNING process_id::text
                        """,
                        (
                            profile_id,
                            str(body.site_id or "").strip() or None,
                            str(body.process_code or "").strip() or None,
                            process_name,
                            str(body.description or "").strip() or None,
                        ),
                    )
                    process_id = str(cur.fetchone()[0])
        conn.commit()
        item = next((it for it in _fetch_company_processes(conn, profile_id) if str(it.get("process_id")) == process_id), None)
    return {"status": "ok", "tenant_id": tenant_id, "process": item}


@app.delete("/api/v2/company/processes/{process_id}")
def delete_company_process(
    process_id: str,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "supprimer un processus entreprise")
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _require_profile_id(conn, tenant_id)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE company_activities SET process_id = NULL WHERE profile_id = %s::uuid AND process_id = %s::uuid",
                (profile_id, process_id),
            )
            cur.execute("DELETE FROM company_processes WHERE profile_id = %s::uuid AND process_id = %s::uuid", (profile_id, process_id))
            deleted = int(cur.rowcount or 0)
        conn.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="Processus introuvable pour ce tenant")
    return {"status": "ok", "tenant_id": tenant_id, "deleted_process_id": process_id}


@app.get("/api/v2/company/activities")
def list_company_activities(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _find_profile_id(conn, tenant_id)
        items = _fetch_company_activities(conn, profile_id) if profile_id else []
    return {"tenant_id": tenant_id, "total": len(items), "items": items}


@app.post("/api/v2/company/activities")
def upsert_company_activity(
    body: CompanyActivityPayload,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "modifier les activites entreprise")
    activity_name = str(body.name or "").strip()
    if not activity_name:
        raise HTTPException(status_code=400, detail="activity.name est obligatoire")

    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _require_profile_id(conn, tenant_id)
        with conn.cursor() as cur:
            process_id = str(body.process_id or "").strip() or None
            process_name = str(body.process_name or "").strip()
            site_id = str(body.site_id or "").strip() or None
            if not process_id and process_name:
                cur.execute(
                    """
                    SELECT process_id::text
                    FROM company_processes
                    WHERE profile_id = %s::uuid
                      AND COALESCE(site_id::text, '') = COALESCE(%s, '')
                      AND LOWER(COALESCE(process_name, '')) = LOWER(%s)
                    LIMIT 1
                    """,
                    (profile_id, site_id, process_name),
                )
                row = cur.fetchone()
                if row:
                    process_id = str(row[0])
                else:
                    cur.execute(
                        """
                        INSERT INTO company_processes (profile_id, site_id, process_name)
                        VALUES (%s::uuid, %s::uuid, %s)
                        RETURNING process_id::text
                        """,
                        (profile_id, site_id, process_name),
                    )
                    process_id = str(cur.fetchone()[0])

            activity_id = str(body.activity_id or "").strip()
            if activity_id:
                cur.execute(
                    """
                    UPDATE company_activities
                    SET site_id = %s::uuid,
                        process_id = %s::uuid,
                        process_name = %s,
                        activity_name = %s,
                        activity_code = %s,
                        description = %s
                    WHERE activity_id = %s::uuid
                      AND profile_id = %s::uuid
                    RETURNING activity_id::text
                    """,
                    (
                        site_id,
                        process_id,
                        process_name or None,
                        activity_name,
                        str(body.code or "").strip() or None,
                        str(body.description or "").strip() or None,
                        activity_id,
                        profile_id,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Activite introuvable pour ce tenant")
                activity_id = str(row[0])
            else:
                cur.execute(
                    """
                    SELECT activity_id::text
                    FROM company_activities
                    WHERE profile_id = %s::uuid
                      AND COALESCE(site_id::text, '') = COALESCE(%s, '')
                      AND LOWER(COALESCE(process_name, '')) = LOWER(%s)
                      AND LOWER(COALESCE(activity_name, '')) = LOWER(%s)
                    LIMIT 1
                    """,
                    (profile_id, site_id or "", process_name, activity_name),
                )
                row = cur.fetchone()
                if row:
                    activity_id = str(row[0])
                    cur.execute(
                        """
                        UPDATE company_activities
                        SET process_id = %s::uuid,
                            activity_code = %s,
                            description = %s
                        WHERE activity_id = %s::uuid
                        """,
                        (
                            process_id,
                            str(body.code or "").strip() or None,
                            str(body.description or "").strip() or None,
                            activity_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO company_activities
                            (profile_id, site_id, process_id, process_name, activity_name, activity_code, description)
                        VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s)
                        RETURNING activity_id::text
                        """,
                        (
                            profile_id,
                            site_id,
                            process_id,
                            process_name or None,
                            activity_name,
                            str(body.code or "").strip() or None,
                            str(body.description or "").strip() or None,
                        ),
                    )
                    activity_id = str(cur.fetchone()[0])
        conn.commit()
        item = next((it for it in _fetch_company_activities(conn, profile_id) if str(it.get("activity_id")) == activity_id), None)
    return {"status": "ok", "tenant_id": tenant_id, "activity": item}


@app.delete("/api/v2/company/activities/{activity_id}")
def delete_company_activity(
    activity_id: str,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "supprimer une activite entreprise")
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _require_profile_id(conn, tenant_id)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM company_activities WHERE profile_id = %s::uuid AND activity_id = %s::uuid", (profile_id, activity_id))
            deleted = int(cur.rowcount or 0)
        conn.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="Activite introuvable pour ce tenant")
    return {"status": "ok", "tenant_id": tenant_id, "deleted_activity_id": activity_id}


@app.get("/api/v2/company/products")
def list_company_products(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _find_profile_id(conn, tenant_id)
        items = _fetch_company_products(conn, profile_id) if profile_id else []
    return {"tenant_id": tenant_id, "total": len(items), "items": items}


@app.post("/api/v2/company/products")
def upsert_company_product(
    body: CompanyProductPayload,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "modifier les produits entreprise")
    designation = str(body.designation or "").strip()
    if not designation:
        raise HTTPException(status_code=400, detail="product.designation est obligatoire")

    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _require_profile_id(conn, tenant_id)
        with conn.cursor() as cur:
            product_id = str(body.product_id or "").strip()
            if product_id:
                cur.execute(
                    """
                    UPDATE company_products
                    SET reference = %s,
                        designation = %s,
                        family = %s,
                        category = %s,
                        product_type = %s,
                        nature = %s,
                        unit = %s,
                        site_name = %s,
                        is_active = %s
                    WHERE product_id = %s::uuid
                      AND profile_id = %s::uuid
                    RETURNING product_id::text
                    """,
                    (
                        str(body.reference or "").strip() or None,
                        designation,
                        str(body.family or "").strip() or None,
                        str(body.category or "").strip() or None,
                        str(body.product_type or "").strip() or None,
                        str(body.nature or "").strip() or None,
                        str(body.unit or "").strip() or None,
                        str(body.site_name or "").strip() or None,
                        bool(body.is_active),
                        product_id,
                        profile_id,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Produit introuvable pour ce tenant")
                product_id = str(row[0])
            else:
                cur.execute(
                    """
                    SELECT product_id::text
                    FROM company_products
                    WHERE profile_id = %s::uuid
                      AND (
                            (COALESCE(%s, '') <> '' AND LOWER(COALESCE(reference, '')) = LOWER(%s))
                         OR LOWER(COALESCE(designation, '')) = LOWER(%s)
                      )
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (
                        profile_id,
                        str(body.reference or "").strip() or "",
                        str(body.reference or "").strip() or "",
                        designation,
                    ),
                )
                row = cur.fetchone()
                if row:
                    product_id = str(row[0])
                    cur.execute(
                        """
                        UPDATE company_products
                        SET family = %s,
                            category = %s,
                            product_type = %s,
                            nature = %s,
                            unit = %s,
                            site_name = %s,
                            is_active = %s
                        WHERE product_id = %s::uuid
                        """,
                        (
                            str(body.family or "").strip() or None,
                            str(body.category or "").strip() or None,
                            str(body.product_type or "").strip() or None,
                            str(body.nature or "").strip() or None,
                            str(body.unit or "").strip() or None,
                            str(body.site_name or "").strip() or None,
                            bool(body.is_active),
                            product_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO company_products
                            (profile_id, reference, designation, family, category, product_type, nature, is_active, unit, site_name)
                        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING product_id::text
                        """,
                        (
                            profile_id,
                            str(body.reference or "").strip() or None,
                            designation,
                            str(body.family or "").strip() or None,
                            str(body.category or "").strip() or None,
                            str(body.product_type or "").strip() or None,
                            str(body.nature or "").strip() or None,
                            bool(body.is_active),
                            str(body.unit or "").strip() or None,
                            str(body.site_name or "").strip() or None,
                        ),
                    )
                    product_id = str(cur.fetchone()[0])
        conn.commit()
        item = next((it for it in _fetch_company_products(conn, profile_id) if str(it.get("product_id")) == product_id), None)
    return {"status": "ok", "tenant_id": tenant_id, "product": item}


@app.delete("/api/v2/company/products/{product_id}")
def delete_company_product(
    product_id: str,
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "supprimer un produit entreprise")
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _require_profile_id(conn, tenant_id)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM company_products WHERE profile_id = %s::uuid AND product_id = %s::uuid", (profile_id, product_id))
            deleted = int(cur.rowcount or 0)
        conn.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="Produit introuvable pour ce tenant")
    return {"status": "ok", "tenant_id": tenant_id, "deleted_product_id": product_id}


@app.get("/api/v2/company/chemicals")
def list_company_chemicals(
    tenant_id: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _find_profile_id(conn, tenant_id)
        items = _fetch_company_chemicals(conn, profile_id) if profile_id else []
    return {"tenant_id": tenant_id, "total": len(items), "items": items}


@app.post("/api/v2/company/chemicals")
def upsert_company_chemicals(
    body: CompanyChemicalsUpsertRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, body.tenant_id)
    _require_role(session, WRITE_ROLES, "modifier les substances chimiques")
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _require_profile_id(conn, tenant_id)
        _sync_company_chemicals(conn, profile_id, body.chemicals or [])
        conn.commit()
        items = _fetch_company_chemicals(conn, profile_id)
    return {"status": "ok", "tenant_id": tenant_id, "total": len(items), "items": items}


# ---------------------------------------------------------------------------
# Routes import entreprise
# Import CSV/XLSX via bulk_company_import.py pour alimenter rapidement les
# donnees de contexte sans passer par les formulaires un par un.
# ---------------------------------------------------------------------------

@app.get("/api/v2/company/import/types")
def list_company_import_types(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    _require_role(session, WRITE_ROLES, "consulter les types d'import entreprise")
    items = [
        {"dataset_type": dataset_type, "label": label}
        for dataset_type, label in SUPPORTED_DATASET_TYPES.items()
    ]
    return {"items": items, "total": len(items)}


@app.post("/api/v2/company/import/bulk")
async def import_company_dataset_bulk(
    import_file: UploadFile = File(...),
    dataset_type: str = Form(""),
    tenant_id: str = Form(""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "importer des donnees entreprise")

    file_name = _sanitize_filename(import_file.filename or "")
    if not file_name:
        raise HTTPException(status_code=400, detail="Nom de fichier import invalide")
    if Path(file_name).suffix.lower() not in {".csv", ".xlsx", ".xls"}:
        raise HTTPException(status_code=400, detail="Formats supportes: CSV, XLSX, XLS")
    payload = await import_file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Fichier d'import vide")

    dsn = _load_env_dsn()
    try:
        normalized_dataset_type = normalize_dataset_type(dataset_type)
        with psycopg.connect(dsn) as conn:
            report = import_company_dataset(
                conn,
                tenant_id=tenant_id,
                dataset_type=normalized_dataset_type,
                file_name=file_name,
                payload=payload,
                actor=str(session.get("username") or "unknown"),
            )
            conn.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "status": "ok",
        "tenant_id": tenant_id,
        "dataset_type": normalized_dataset_type,
        "file_name": file_name,
        "report": report,
    }


# ---------------------------------------------------------------------------
# Routes preuves documentaires
# Ces endpoints lisent et ajoutent les justificatifs utilises par l'agent A3
# pour evaluer la conformite et par A4 pour repondre sur les preuves.
# ---------------------------------------------------------------------------

@app.get("/api/v2/company/proofs")
def list_company_proofs(
    tenant_id: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Liste les preuves documentaires du tenant depuis compliance_evidence."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        profile_id = _find_profile_id(conn, tenant_id)
        if not profile_id:
            return {"tenant_id": tenant_id, "total": 0, "items": []}
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ce.evidence_id::text AS evidence_id,
                    ce.source_report_id::text AS report_id,
                    ce.requirement_id::text AS requirement_id,
                    COALESCE(ar.reference, ce.title, '') AS reference,
                    COALESCE(ce.evidence_type, ar.audit_type, '') AS audit_type,
                    COALESCE(ar.category, '') AS category,
                    COALESCE(ar.nature, '') AS nature,
                    COALESCE(ar.system_scope, '') AS system_scope,
                    COALESCE(ar.state, '') AS state,
                    COALESCE(ce.file_name, '') AS file_name,
                    COALESCE(ce.storage_path, '') AS source_file,
                    COALESCE(ce.scope_level, 'ORGANIZATION') AS scope_level,
                    ce.site_id::text AS site_id,
                    ce.process_id::text AS process_id,
                    ce.activity_id::text AS activity_id,
                    COALESCE(ce.scope_label, 'ORGANIZATION') AS scope_label,
                    COALESCE(LENGTH(ce.raw_text), 0)::int AS text_chars,
                    COALESCE(ce.uploaded_at, ce.created_at)::text AS created_at
                FROM compliance_evidence ce
                LEFT JOIN audit_reports ar ON ar.report_id = ce.source_report_id
                WHERE ce.profile_id = %s::uuid
                ORDER BY COALESCE(ce.uploaded_at, ce.created_at) DESC
                LIMIT %s
                """,
                (profile_id, int(limit)),
            )
            items = _rows_to_dicts(cur)
    return {"tenant_id": tenant_id, "total": len(items), "items": items}


@app.post("/api/v2/company/proofs/upload")
async def upload_company_proof(
    proof_pdf: UploadFile = File(...),
    tenant_id: str = Form(""),
    reference: str = Form(""),
    audit_type: str = Form(""),
    category: str = Form(""),
    nature: str = Form(""),
    system_scope: str = Form(""),
    state: str = Form(""),
    requirement_id: str = Form(""),
    scope_level: str = Form(""),
    scope_label: str = Form(""),
    site_id: str = Form(""),
    process_id: str = Form(""),
    activity_id: str = Form(""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Upload d'une preuve PDF et indexation texte dans audit_reports + compliance_evidence."""
    session = _auth_from_header(authorization)
    tenant_id = _require_tenant_access(session, tenant_id)
    _require_role(session, WRITE_ROLES, "uploader une preuve documentaire")

    filename = _sanitize_filename(proof_pdf.filename or "")
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Le fichier de preuve doit etre un PDF")
    payload = await proof_pdf.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Fichier PDF vide")

    stored_name = f"{uuid.uuid4().hex[:12]}_{filename}"
    target_dir = PROOFS_DIR / tenant_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / stored_name
    target_path.write_bytes(payload)

    raw_text = ""
    pages_count = 0
    try:
        import io
        import pdfplumber

        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            pages_count = len(pdf.pages)
            chunks: list[str] = []
            for page in pdf.pages:
                txt = str(page.extract_text() or "").strip()
                if txt:
                    chunks.append(txt)
            raw_text = "\n\n".join(chunks)[:50000]
    except Exception:
        # On conserve le PDF meme si l'extraction texte echoue.
        raw_text = ""

    ref = str(reference or "").strip() or Path(filename).stem[:120] or f"proof_{uuid.uuid4().hex[:8]}"
    payload_hash = hashlib.sha256(payload).hexdigest()
    normalized_scope_level = str(scope_level or "").strip().upper()
    normalized_site_id = str(site_id or "").strip() or None
    normalized_process_id = str(process_id or "").strip() or None
    normalized_activity_id = str(activity_id or "").strip() or None
    if normalized_activity_id:
        normalized_scope_level = "ACTIVITY"
    elif normalized_process_id:
        normalized_scope_level = "PROCESS"
    elif normalized_site_id:
        normalized_scope_level = "SITE"
    if normalized_scope_level not in {"ORGANIZATION", "SITE", "PROCESS", "ACTIVITY"}:
        normalized_scope_level = "ORGANIZATION"
    derived_scope_key = "ORGANIZATION"
    if normalized_scope_level == "SITE" and normalized_site_id:
        derived_scope_key = f"SITE:{normalized_site_id}"
    elif normalized_scope_level == "PROCESS" and normalized_process_id:
        derived_scope_key = f"PROCESS:{normalized_process_id}"
    elif normalized_scope_level == "ACTIVITY" and normalized_activity_id:
        derived_scope_key = f"ACTIVITY:{normalized_activity_id}"
    derived_scope_label = str(scope_label or "").strip() or derived_scope_key

    dsn = _load_env_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            profile_id = _require_profile_id(conn, tenant_id)

            cur.execute(
                "SELECT 1 FROM audit_reports WHERE profile_id = %s::uuid AND reference = %s LIMIT 1",
                (profile_id, ref),
            )
            if cur.fetchone():
                ref = f"{ref}_{uuid.uuid4().hex[:6]}"

            cur.execute(
                """
                INSERT INTO audit_reports
                    (profile_id, reference, audit_type, category, nature, system_scope, state, raw_text, source_file)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING report_id::text, created_at::text
                """,
                (
                    profile_id,
                    ref,
                    str(audit_type or "").strip() or None,
                    str(category or "").strip() or None,
                    str(nature or "").strip() or None,
                    str(system_scope or "").strip() or None,
                    str(state or "").strip() or None,
                    raw_text,
                    str(target_path.as_posix()),
                ),
            )
            out = cur.fetchone()
            report_id = str(out[0])
            cur.execute(
                """
                INSERT INTO compliance_evidence
                    (profile_id, source_report_id, requirement_id, scope_level, scope_key, scope_label,
                     site_id, process_id, activity_id,
                     title, file_name, mime_type, storage_path, raw_text, evidence_type,
                     source_type, uploaded_at, created_by, input_hash)
                VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s::uuid, %s::uuid, %s::uuid,
                        %s, %s, %s, %s, %s, %s, 'UPLOAD_PDF', now(), %s, %s)
                RETURNING evidence_id::text
                """,
                (
                    profile_id,
                    report_id,
                    str(requirement_id or "").strip() or None,
                    normalized_scope_level,
                    derived_scope_key,
                    derived_scope_label,
                    normalized_site_id,
                    normalized_process_id,
                    normalized_activity_id,
                    ref,
                    filename,
                    str(proof_pdf.content_type or "application/pdf"),
                    str(target_path.as_posix()),
                    raw_text,
                    str(audit_type or "").strip() or "A3_EVIDENCE",
                    str(session.get("username") or "unknown"),
                    payload_hash,
                ),
            )
            evidence_id = str(cur.fetchone()[0])

            requirement_ref = str(requirement_id or "").strip() or None
            if requirement_ref:
                if derived_scope_key == "ORGANIZATION":
                    cur.execute(
                        """
                        UPDATE compliance_checks
                        SET needs_recheck = TRUE,
                            updated_at = now()
                        WHERE profile_id = %s::uuid
                          AND requirement_id = %s::uuid
                        """,
                        (profile_id, requirement_ref),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE compliance_checks
                        SET needs_recheck = TRUE,
                            updated_at = now()
                        WHERE profile_id = %s::uuid
                          AND requirement_id = %s::uuid
                          AND COALESCE(scope_key, 'ORGANIZATION') IN (%s, 'ORGANIZATION')
                        """,
                        (profile_id, requirement_ref, derived_scope_key),
                    )
            elif derived_scope_key == "ORGANIZATION":
                cur.execute(
                    """
                    UPDATE compliance_checks
                    SET needs_recheck = TRUE,
                        updated_at = now()
                    WHERE profile_id = %s::uuid
                    """,
                    (profile_id,),
                )
            else:
                cur.execute(
                    """
                    UPDATE compliance_checks
                    SET needs_recheck = TRUE,
                        updated_at = now()
                    WHERE profile_id = %s::uuid
                      AND COALESCE(scope_key, 'ORGANIZATION') IN (%s, 'ORGANIZATION')
                    """,
                    (profile_id, derived_scope_key),
                )
        conn.commit()
    return {
        "status": "ok",
        "tenant_id": tenant_id,
        "report_id": str(out[0]),
        "evidence_id": evidence_id,
        "reference": ref,
        "scope_level": normalized_scope_level,
        "scope_key": derived_scope_key,
        "scope_label": derived_scope_label,
        "source_file": str(target_path.as_posix()),
        "pages": int(pages_count),
        "text_chars": int(len(raw_text)),
        "input_hash": payload_hash,
        "created_at": str(out[1]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run FastAPI server for QALITAS AI platform")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", choices=["on", "off"], default="off")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "qalitas_api_fastapi:app",
        host=args.host,
        port=int(args.port),
        reload=(args.reload == "on"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
