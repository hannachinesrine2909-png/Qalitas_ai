from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from a1_error_memory import (
    error_memory_table_exists,
    ensure_error_memory_table,
    list_error_memory_replay_targets,
    mark_error_memory_replayed,
    persist_error_memory_signal,
)
from tenant_db import connect_db


load_dotenv()


DEFAULT_REPORT_DIR = ROOT_DIR / "reports" / "error_memory"


def _parse_flag(value: str | bool | None, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Valeur booleenne invalide: {value}")


def _parse_csv_list(value: str) -> list[str]:
    items: list[str] = []
    for raw_part in str(value or "").split(","):
        part = str(raw_part or "").strip().upper()
        if part and part not in items:
            items.append(part)
    return items


def _tail_text(text: str | None, *, max_lines: int = 20, max_chars: int = 4000) -> str:
    lines = str(text or "").splitlines()
    if not lines:
        return ""
    clipped = "\n".join(lines[-max_lines:])
    return clipped[-max_chars:]


def _parse_event_payload(raw_value: Any) -> dict[str, Any] | None:
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
    available_columns = {str(row[0]) for row in cur.fetchall() if row and row[0]}
    payload_column = next(
        (name for name in ("payload_json", "payload", "event_payload") if name in available_columns),
        "",
    )
    event_id_column = next((name for name in ("event_id", "id") if name in available_columns), "")
    created_at_column = "created_at" if "created_at" in available_columns else ""
    return {
        "payload_column": payload_column,
        "event_id_column": event_id_column,
        "created_at_column": created_at_column,
    }


def _build_source_event_key(
    *,
    raw_event_id: Any,
    doc_id: str,
    created_at: Any,
    payload: dict[str, Any],
) -> str:
    event_id = str(raw_event_id or "").strip()
    if event_id:
        return f"event:{event_id}"
    created_at_value = ""
    if hasattr(created_at, "isoformat"):
        created_at_value = str(created_at.isoformat())
    else:
        created_at_value = str(created_at or "").strip()
    payload_key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(
        f"{doc_id}|{created_at_value}|{payload_key}".encode("utf-8", errors="ignore")
    ).hexdigest()
    return f"hash:{digest}"


def _backfill_error_memory_from_events(
    cur: Any,
    *,
    tenant_id: str,
    doc_id: str = "",
    families: list[str] | None = None,
) -> dict[str, Any]:
    column_info = _get_events_column_info(cur)
    payload_column = column_info["payload_column"]
    if not payload_column:
        return {
            "payload_column": "",
            "event_id_column": column_info["event_id_column"],
            "created_at_column": column_info["created_at_column"],
            "events_scanned": 0,
            "signals_valid": 0,
            "signals_filtered_out": 0,
            "signals_persist_attempted": 0,
            "invalid_payloads": 0,
            "doc_ids_touched": [],
            "families_seen": [],
        }

    event_id_expr = column_info["event_id_column"] or "NULL"
    created_at_expr = column_info["created_at_column"] or "NULL"
    order_expr = column_info["created_at_column"] or column_info["event_id_column"] or "doc_id"

    sql = f"""
        SELECT
            {event_id_expr} AS source_event_id,
            doc_id,
            {created_at_expr} AS created_at,
            {payload_column} AS payload_value
        FROM events
        WHERE tenant_id=%s
          AND event_type='A1_ERROR_MEMORY_SIGNAL'
          AND {payload_column} IS NOT NULL
    """
    params: list[Any] = [tenant_id]
    if str(doc_id or "").strip():
        sql += " AND doc_id=%s"
        params.append(str(doc_id or "").strip())
    sql += f" ORDER BY {order_expr} ASC"

    cur.execute(sql, params)

    families_filter = {str(value or "").strip().upper() for value in (families or []) if str(value or "").strip()}
    touched_docs: set[str] = set()
    families_seen: set[str] = set()
    stats = {
        "payload_column": payload_column,
        "event_id_column": column_info["event_id_column"],
        "created_at_column": column_info["created_at_column"],
        "events_scanned": 0,
        "signals_valid": 0,
        "signals_filtered_out": 0,
        "signals_persist_attempted": 0,
        "invalid_payloads": 0,
        "doc_ids_touched": [],
        "families_seen": [],
    }

    for raw_event_id, row_doc_id, created_at, payload_value in cur.fetchall():
        stats["events_scanned"] += 1
        payload = _parse_event_payload(payload_value)
        if not isinstance(payload, dict):
            stats["invalid_payloads"] += 1
            continue

        family = str(payload.get("error_family") or "").strip().upper()
        if families_filter and family not in families_filter:
            stats["signals_filtered_out"] += 1
            continue

        stats["signals_valid"] += 1
        if family:
            families_seen.add(family)

        signal_doc_id = str(row_doc_id or "").strip()
        source_event_key = _build_source_event_key(
            raw_event_id=raw_event_id,
            doc_id=signal_doc_id,
            created_at=created_at,
            payload=payload,
        )
        if persist_error_memory_signal(
            cur,
            tenant_id=tenant_id,
            doc_id=signal_doc_id,
            signal=payload,
            source_event_type="A1_ERROR_MEMORY_SIGNAL",
            source_event_key=source_event_key,
        ):
            stats["signals_persist_attempted"] += 1
            if signal_doc_id:
                touched_docs.add(signal_doc_id)

    stats["doc_ids_touched"] = sorted(touched_docs)
    stats["families_seen"] = sorted(families_seen)
    return stats


def _run_extraction_replay(*, tenant_id: str, doc_id: str, timeout_sec: int) -> dict[str, Any]:
    command = [
        sys.executable,
        "a1_extract_requirements_llm.py",
        "--doc_id",
        doc_id,
        "--tenant",
        tenant_id,
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    started_at = time.time()

    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(60, int(timeout_sec)),
        )
        duration_sec = round(time.time() - started_at, 3)
        return {
            "doc_id": doc_id,
            "command": command,
            "returncode": int(completed.returncode),
            "timed_out": False,
            "duration_sec": duration_sec,
            "stdout_tail": _tail_text(completed.stdout),
            "stderr_tail": _tail_text(completed.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        duration_sec = round(time.time() - started_at, 3)
        return {
            "doc_id": doc_id,
            "command": command,
            "returncode": None,
            "timed_out": True,
            "duration_sec": duration_sec,
            "stdout_tail": _tail_text(exc.stdout),
            "stderr_tail": _tail_text(exc.stderr),
        }


def _resolve_report_path(raw_path: str) -> Path:
    if str(raw_path or "").strip():
        return Path(raw_path).expanduser().resolve()
    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return (DEFAULT_REPORT_DIR / f"a1_error_memory_replay_{timestamp}.json").resolve()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True, help="tenant_id cible")
    parser.add_argument("--doc_id", default="", help="Rejouer un doc_id precis")
    parser.add_argument("--limit_docs", type=int, default=5, help="Nombre max de doc_ids a rejouer")
    parser.add_argument("--families", default="", help="Filtrer les familles d'erreurs (CSV)")
    parser.add_argument("--backfill_events", default="on", help="on/off")
    parser.add_argument("--run_replay", default="on", help="on/off")
    parser.add_argument("--timeout_sec", type=int, default=900, help="Timeout par replay de document")
    parser.add_argument("--report_path", default="", help="Chemin du rapport JSON de replay")
    args = parser.parse_args()

    dsn = os.getenv("PG_DSN")
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")

    tenant_id = str(args.tenant or "").strip()
    if not tenant_id:
        raise RuntimeError("--tenant est obligatoire")

    families = _parse_csv_list(args.families)
    backfill_events = _parse_flag(args.backfill_events, default=True)
    run_replay = _parse_flag(args.run_replay, default=True)
    report_path = _resolve_report_path(args.report_path)

    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "requested_doc_id": str(args.doc_id or "").strip(),
        "limit_docs": max(1, int(args.limit_docs or 1)),
        "families": families,
        "backfill_events": backfill_events,
        "run_replay": run_replay,
        "timeout_sec": max(60, int(args.timeout_sec or 60)),
        "backfill": {},
        "targets": [],
        "results": [],
    }

    with connect_db(dsn, tenant_id=tenant_id) as conn:
        with conn.cursor() as cur:
            if error_memory_table_exists(cur):
                table_ready = True
            else:
                ensure_error_memory_table(cur)
                table_ready = True

            if backfill_events and table_ready:
                report["backfill"] = _backfill_error_memory_from_events(
                    cur,
                    tenant_id=tenant_id,
                    doc_id=str(args.doc_id or "").strip(),
                    families=families,
                )
                conn.commit()
            elif not backfill_events:
                report["backfill"] = {
                    "skipped": True,
                    "reason": "backfill_events=off",
                }
            else:
                report["backfill"] = {
                    "skipped": True,
                    "reason": "error_memory_table_unavailable",
                }

            if str(args.doc_id or "").strip():
                targets = [{"doc_id": str(args.doc_id or "").strip(), "families_count": 0, "last_seen_at": None}]
            else:
                targets = list_error_memory_replay_targets(
                    cur,
                    tenant_id=tenant_id,
                    limit=max(1, int(args.limit_docs or 1)),
                    error_families=families,
                )

            report["targets"] = [
                {
                    "doc_id": str(item.get("doc_id") or "").strip(),
                    "families_count": int(item.get("families_count") or 0),
                    "last_seen_at": (
                        item.get("last_seen_at").isoformat()
                        if hasattr(item.get("last_seen_at"), "isoformat")
                        else item.get("last_seen_at")
                    ),
                }
                for item in targets
                if str(item.get("doc_id") or "").strip()
            ]

            if run_replay:
                for target in report["targets"]:
                    doc_id = str(target.get("doc_id") or "").strip()
                    if not doc_id:
                        continue
                    replay_result = _run_extraction_replay(
                        tenant_id=tenant_id,
                        doc_id=doc_id,
                        timeout_sec=max(60, int(args.timeout_sec or 60)),
                    )
                    report["results"].append(replay_result)
                    replay_notes = (
                        f"returncode={replay_result['returncode']}; "
                        f"timed_out={replay_result['timed_out']}; "
                        f"duration_sec={replay_result['duration_sec']}"
                    )
                    mark_error_memory_replayed(
                        cur,
                        tenant_id=tenant_id,
                        doc_id=doc_id,
                        replay_notes=replay_notes,
                    )
                    conn.commit()

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print("\n===== A1 Error Memory Replay =====", flush=True)
    print(f"Tenant             : {tenant_id}", flush=True)
    print(f"Families           : {families}", flush=True)
    print(f"Backfill events    : {backfill_events}", flush=True)
    print(f"Replay enabled     : {run_replay}", flush=True)
    print(f"Targets            : {len(report['targets'])}", flush=True)
    print(f"Replay results     : {len(report['results'])}", flush=True)
    print(f"Report path        : {report_path}", flush=True)


if __name__ == "__main__":
    main()
