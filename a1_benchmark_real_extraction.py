import argparse
import ast
import collections
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from tenant_db import connect_db

load_dotenv()


SUMMARY_RE = {
    "chunks_ok": re.compile(r"^Chunks OK\s*:\s*(\d+)\s*$", re.MULTILINE),
    "chunks_empty": re.compile(r"^Chunks empty\s*:\s*(\d+)\s*$", re.MULTILINE),
    "chunks_error": re.compile(r"^Chunks error\s*:\s*(\d+)\s*$", re.MULTILINE),
    "raw_llm_requirements": re.compile(r"^LLM raw outputs\s*:\s*(\d+)\s*$", re.MULTILINE),
    "inserted": re.compile(r"^Inserted\s*:\s*(\d+)\s*$", re.MULTILINE),
    "rejected_low_value": re.compile(r"^Rejected low value:\s*(\d+)\s*$", re.MULTILINE),
    "rejected_scope": re.compile(r"^Rejected scope\s*:\s*(\d+)\s*$", re.MULTILINE),
    "rejected_exception": re.compile(r"^Rejected except\.\s*:\s*(\d+)\s*$", re.MULTILINE),
    "rejected_invented": re.compile(r"^Rejected invented\s*:\s*(\d+)\s*$", re.MULTILINE),
    "rejected_oos": re.compile(r"^Rejected oos\s*:\s*(\d+)\s*$", re.MULTILINE),
    "deduplicated": re.compile(r"^Deduplicated\s*:\s*(\d+)\s*$", re.MULTILINE),
    "doc_gate_policy": re.compile(r"^Doc gate policy\s*:\s*(\S+)\s*$", re.MULTILINE),
    "doc_gate_type": re.compile(r"^Doc gate type\s*:\s*(\S+)\s*$", re.MULTILINE),
    "doc_gate_reasons": re.compile(r"^Doc gate reasons\s*:\s*(.+?)\s*$", re.MULTILINE),
    "article_policy_counts": re.compile(r"^Article policy cnt:\s*(.+?)\s*$", re.MULTILINE),
    "article_reason_counts": re.compile(r"^Article reason cnt:\s*(.+?)\s*$", re.MULTILINE),
    "article_drop_reason_counts": re.compile(r"^Article drop rsn\s*:\s*(.+?)\s*$", re.MULTILINE),
    "article_dropped_by_policy_total": re.compile(r"^Article dropped\s*:\s*(\d+)\s*$", re.MULTILINE),
    "units_dropped_limited_policy_total": re.compile(r"^Units drop limited:\s*(\d+)\s*$", re.MULTILINE),
    "limited_policy_forced_units_total": re.compile(r"^Limited forced\s*:\s*(\d+)\s*$", re.MULTILINE),
    "limited_data_articles_total": re.compile(r"^Limited data arts\s*:\s*(\d+)\s*$", re.MULTILINE),
    "limited_data_objects_total": re.compile(r"^Limited data objs\s*:\s*(\d+)\s*$", re.MULTILINE),
    "limited_data_object_type_counts": re.compile(r"^Limited data types:\s*(.+?)\s*$", re.MULTILINE),
    "policy_forced_to_validate_total": re.compile(r"^Policy forced TV\s*:\s*(\d+)\s*$", re.MULTILINE),
    "promotion_reviewed_to_validate_total": re.compile(r"^Promo reviewed TV\s*:\s*(\d+)\s*$", re.MULTILINE),
    "promoted_to_draft_total": re.compile(r"^Promo to DRAFT\s*:\s*(\d+)\s*$", re.MULTILINE),
    "promotion_blocked_policy_total": re.compile(r"^Promo block policy\s*:\s*(\d+)\s*$", re.MULTILINE),
    "promotion_blocked_type_total": re.compile(r"^Promo block type\s*:\s*(\d+)\s*$", re.MULTILINE),
    "promotion_blocked_low_conf_total": re.compile(r"^Promo block conf\s*:\s*(\d+)\s*$", re.MULTILINE),
    "promotion_blocked_short_snippet_total": re.compile(r"^Promo block snip\s*:\s*(\d+)\s*$", re.MULTILINE),
    "promotion_blocked_low_overlap_total": re.compile(r"^Promo block overlap\s*:\s*(\d+)\s*$", re.MULTILINE),
}


@dataclass
class DocRun:
    doc_id: str
    tenant_id: str
    title: str
    chunks_before: int
    requirements_before: int
    elapsed_sec: float
    return_code: int
    timed_out: bool
    chunks_ok: int
    chunks_empty: int
    chunks_error: int
    raw_llm_requirements: int
    inserted: int
    rejected_low_value: int
    rejected_scope: int
    rejected_exception: int
    rejected_invented: int
    rejected_oos: int
    deduplicated: int
    doc_gate_policy: str
    doc_gate_type: str
    doc_gate_reason_codes: list[str]
    article_policy_counts: dict[str, int]
    article_reason_counts: dict[str, int]
    article_drop_reason_counts: dict[str, int]
    article_dropped_by_policy_total: int
    units_dropped_limited_policy_total: int
    limited_policy_forced_units_total: int
    limited_data_articles_total: int
    limited_data_objects_total: int
    limited_data_object_type_counts: dict[str, int]
    policy_forced_to_validate_total: int
    promotion_reviewed_to_validate_total: int
    promoted_to_draft_total: int
    promotion_blocked_policy_total: int
    promotion_blocked_type_total: int
    promotion_blocked_low_conf_total: int
    promotion_blocked_short_snippet_total: int
    promotion_blocked_low_overlap_total: int
    requirements_after: int
    requirements_final_per_document: int
    to_validate_count: int
    to_validate_share: float
    raw_to_final_conversion_rate: float
    postcall_rejected_total: int
    stdout_tail: str
    stderr_tail: str


def _parse_int(pattern: re.Pattern, text: str) -> int:
    m = pattern.search(text or "")
    return int(m.group(1)) if m else 0


def _parse_literal_counts(pattern: re.Pattern, text: str) -> dict[str, int]:
    m = pattern.search(text or "")
    if not m:
        return {}
    raw = str(m.group(1) or "").strip()
    if not raw:
        return {}
    try:
        parsed = ast.literal_eval(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in parsed.items():
        key = str(k or "").strip()
        if not key:
            continue
        try:
            out[key] = int(v)
        except Exception:
            continue
    return out


def _parse_literal_list(pattern: re.Pattern, text: str) -> list[str]:
    m = pattern.search(text or "")
    if not m:
        return []
    raw = str(m.group(1) or "").strip()
    if not raw:
        return []
    try:
        parsed = ast.literal_eval(raw)
    except Exception:
        return []
    if not isinstance(parsed, (list, tuple)):
        return []
    out: list[str] = []
    for item in parsed:
        s = str(item or "").strip()
        if s:
            out.append(s)
    return out


def count_requirements_and_status(cur, doc_id: str) -> tuple[int, int]:
    cur.execute(
        """
        SELECT COUNT(*) AS total,
                COUNT(*) FILTER (WHERE UPPER(COALESCE(status, '')) = 'TO_VALIDATE') AS to_validate_total
        FROM requirements
        WHERE doc_id=%s
        """,
        (doc_id,),
    )
    row = cur.fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def list_docs(cur, tenant: str | None, limit: int, jort_only: bool = False):
    params = []
    where = []
    if tenant:
        where.append("d.tenant_id=%s")
        params.append(tenant)
    if jort_only:
        where.append("lower(COALESCE(d.title, '')) LIKE 'jo%%'")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    cur.execute(
        f"""
        SELECT
            d.doc_id::text,
            d.tenant_id,
            d.title,
            COALESCE(c.cnt_chunks, 0) AS chunks_count,
            COALESCE(r.cnt_requirements, 0) AS requirements_count
        FROM documents d
        LEFT JOIN (
            SELECT doc_id, COUNT(*) AS cnt_chunks
            FROM chunks
            GROUP BY doc_id
        ) c ON c.doc_id = d.doc_id
        LEFT JOIN (
            SELECT doc_id, COUNT(*) AS cnt_requirements
            FROM requirements
            GROUP BY doc_id
        ) r ON r.doc_id = d.doc_id
        {where_sql}
        ORDER BY c.cnt_chunks DESC, d.created_at DESC NULLS LAST
        LIMIT %s
        """,
        (*params, limit),
    )
    return cur.fetchall()


def _benchmark_title_key(title: str) -> str:
    normalized = re.sub(r"\s+", " ", str(title or "").strip().lower())
    return normalized


def _dedupe_docs_by_title(rows: list[tuple], limit: int) -> list[tuple]:
    if limit <= 0:
        return []
    deduped: list[tuple] = []
    seen_titles: set[str] = set()
    seen_doc_ids: set[str] = set()
    for row in rows:
        doc_id = str(row[0] or "").strip()
        title_key = _benchmark_title_key(str(row[2] or ""))
        dedupe_key = title_key or doc_id
        if not doc_id or dedupe_key in seen_titles or doc_id in seen_doc_ids:
            continue
        seen_titles.add(dedupe_key)
        seen_doc_ids.add(doc_id)
        deduped.append(row)
        if len(deduped) >= limit:
            break
    return deduped


def count_requirements(cur, doc_id: str) -> int:
    cur.execute("SELECT COUNT(*) FROM requirements WHERE doc_id=%s", (doc_id,))
    return int(cur.fetchone()[0])


def count_downstream_requirement_refs(cur, doc_id: str) -> int:
    cur.execute(
        """
        SELECT COUNT(*)
        FROM compliance_checks cc
        JOIN requirements r ON r.requirement_id = cc.requirement_id
        WHERE r.doc_id=%s
        """,
        (doc_id,),
    )
    return int(cur.fetchone()[0] or 0)


def run_doc(
    doc_id: str,
    tenant_id: str | None,
    timeout_sec: int,
    extract_limit_chunks: int | None = None,
    force_full_rebuild: bool = False,
):
    cmd = [
        sys.executable,
        "a1_extract_requirements_llm.py",
        "--doc_id",
        doc_id,
    ]
    if tenant_id:
        cmd.extend(["--tenant", str(tenant_id)])
    if extract_limit_chunks is not None and int(extract_limit_chunks) > 0:
        cmd.extend(["--limit", str(int(extract_limit_chunks))])
    if force_full_rebuild:
        cmd.append("--force_full_rebuild")
    start = time.perf_counter()
    try:
        cp = subprocess.run(
            cmd,
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
        elapsed = time.perf_counter() - start
        return cp.returncode, False, elapsed, cp.stdout, cp.stderr
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - start
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return 124, True, elapsed, stdout, stderr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", default=None, help="Limiter a un tenant")
    parser.add_argument("--doc_id", default=None, help="Executer un document precis")
    parser.add_argument("--limit_docs", type=int, default=5, help="Nombre de documents a benchmarker")
    parser.add_argument(
        "--jort_only",
        choices=["on", "off"],
        default="off",
        help="on: limite aux documents dont le titre commence par 'jo' (JORT).",
    )
    parser.add_argument("--timeout_sec", type=int, default=900, help="Timeout par document (secondes)")
    parser.add_argument(
        "--extract_limit_chunks",
        type=int,
        default=None,
        help="Limiter le nombre de chunks traites par document (anti-timeout).",
    )
    parser.add_argument(
        "--force_full_rebuild",
        choices=["on", "off"],
        default="on",
        help="on: rebuild complet requirements/doc avant run (recommande pour KPI stables).",
    )
    parser.add_argument(
        "--report_path",
        default="reports/preflight/a1_real_extraction_benchmark_latest.json",
    )
    args = parser.parse_args()

    dsn = os.getenv("PG_DSN")
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")

    runs: list[DocRun] = []
    global_start = time.perf_counter()

    tenant_ctx = str(args.tenant or "").strip() or None
    with connect_db(dsn, tenant_id=tenant_ctx) as conn:
        with conn.cursor() as cur:
            if args.doc_id:
                cur.execute(
                    """
                    SELECT
                        d.doc_id::text,
                        d.tenant_id,
                        d.title,
                        COALESCE(c.cnt_chunks, 0) AS chunks_count,
                        COALESCE(r.cnt_requirements, 0) AS requirements_count
                    FROM documents d
                    LEFT JOIN (
                        SELECT doc_id, COUNT(*) AS cnt_chunks
                        FROM chunks
                        GROUP BY doc_id
                    ) c ON c.doc_id = d.doc_id
                    LEFT JOIN (
                        SELECT doc_id, COUNT(*) AS cnt_requirements
                        FROM requirements
                        GROUP BY doc_id
                    ) r ON r.doc_id = d.doc_id
                    WHERE d.doc_id=%s
                    LIMIT 1
                    """,
                    (args.doc_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError(f"doc_id introuvable: {args.doc_id}")
                docs = [row]
            else:
                docs = list_docs(
                    cur,
                    tenant=args.tenant,
                    limit=max(int(args.limit_docs) * 5, int(args.limit_docs)),
                    jort_only=(args.jort_only == "on"),
                )
                docs = _dedupe_docs_by_title(list(docs), int(args.limit_docs))

            docs = [d for d in docs if int(d[3]) > 0]

            for idx, (doc_id, tenant_id, title, chunks_count, req_before) in enumerate(docs, start=1):
                print(f"[{idx}/{len(docs)}] doc_id={doc_id} title={title} chunks={chunks_count}", flush=True)
                downstream_refs = count_downstream_requirement_refs(cur, doc_id)
                effective_force_full_rebuild = (args.force_full_rebuild == "on") and downstream_refs == 0
                if (args.force_full_rebuild == "on") and downstream_refs > 0:
                    print(
                        f"  note: force_full_rebuild desactive pour doc_id={doc_id} "
                        f"(downstream_refs={downstream_refs})",
                        flush=True,
                    )
                rc, timed_out, elapsed_sec, stdout, stderr = run_doc(
                    doc_id=doc_id,
                    tenant_id=tenant_id or tenant_ctx,
                    timeout_sec=args.timeout_sec,
                    extract_limit_chunks=args.extract_limit_chunks,
                    force_full_rebuild=effective_force_full_rebuild,
                )
                req_after = count_requirements(cur, doc_id)
                req_total, to_validate_total = count_requirements_and_status(cur, doc_id)
                to_validate_share = round((to_validate_total / req_total), 4) if req_total else 0.0
                raw_llm_candidates_total = _parse_int(SUMMARY_RE["raw_llm_requirements"], stdout)
                inserted_total = _parse_int(SUMMARY_RE["inserted"], stdout)
                postcall_rejected_total = (
                    _parse_int(SUMMARY_RE["rejected_low_value"], stdout)
                    + _parse_int(SUMMARY_RE["rejected_scope"], stdout)
                    + _parse_int(SUMMARY_RE["rejected_exception"], stdout)
                    + _parse_int(SUMMARY_RE["rejected_invented"], stdout)
                    + _parse_int(SUMMARY_RE["rejected_oos"], stdout)
                )
                raw_to_final_conversion_rate = (
                    round((inserted_total / raw_llm_candidates_total), 4) if raw_llm_candidates_total else 0.0
                )
                doc_gate_policy = (
                    (SUMMARY_RE["doc_gate_policy"].search(stdout or "") or [None, "UNKNOWN"])[1]
                    if SUMMARY_RE["doc_gate_policy"].search(stdout or "")
                    else "UNKNOWN"
                )
                doc_gate_type = (
                    (SUMMARY_RE["doc_gate_type"].search(stdout or "") or [None, "UNKNOWN"])[1]
                    if SUMMARY_RE["doc_gate_type"].search(stdout or "")
                    else "UNKNOWN"
                )

                run = DocRun(
                    doc_id=doc_id,
                    tenant_id=tenant_id,
                    title=title or "",
                    chunks_before=int(chunks_count),
                    requirements_before=int(req_before),
                    elapsed_sec=round(elapsed_sec, 3),
                    return_code=int(rc),
                    timed_out=bool(timed_out),
                    chunks_ok=_parse_int(SUMMARY_RE["chunks_ok"], stdout),
                    chunks_empty=_parse_int(SUMMARY_RE["chunks_empty"], stdout),
                    chunks_error=_parse_int(SUMMARY_RE["chunks_error"], stdout),
                    raw_llm_requirements=raw_llm_candidates_total,
                    inserted=inserted_total,
                    rejected_low_value=_parse_int(SUMMARY_RE["rejected_low_value"], stdout),
                    rejected_scope=_parse_int(SUMMARY_RE["rejected_scope"], stdout),
                    rejected_exception=_parse_int(SUMMARY_RE["rejected_exception"], stdout),
                    rejected_invented=_parse_int(SUMMARY_RE["rejected_invented"], stdout),
                    rejected_oos=_parse_int(SUMMARY_RE["rejected_oos"], stdout),
                    deduplicated=_parse_int(SUMMARY_RE["deduplicated"], stdout),
                    doc_gate_policy=str(doc_gate_policy or "UNKNOWN"),
                    doc_gate_type=str(doc_gate_type or "UNKNOWN"),
                    doc_gate_reason_codes=_parse_literal_list(SUMMARY_RE["doc_gate_reasons"], stdout),
                    article_policy_counts=_parse_literal_counts(SUMMARY_RE["article_policy_counts"], stdout),
                    article_reason_counts=_parse_literal_counts(SUMMARY_RE["article_reason_counts"], stdout),
                    article_drop_reason_counts=_parse_literal_counts(
                        SUMMARY_RE["article_drop_reason_counts"], stdout
                    ),
                    article_dropped_by_policy_total=_parse_int(
                        SUMMARY_RE["article_dropped_by_policy_total"], stdout
                    ),
                    units_dropped_limited_policy_total=_parse_int(
                        SUMMARY_RE["units_dropped_limited_policy_total"], stdout
                    ),
                    limited_policy_forced_units_total=_parse_int(
                        SUMMARY_RE["limited_policy_forced_units_total"], stdout
                    ),
                    limited_data_articles_total=_parse_int(SUMMARY_RE["limited_data_articles_total"], stdout),
                    limited_data_objects_total=_parse_int(SUMMARY_RE["limited_data_objects_total"], stdout),
                    limited_data_object_type_counts=_parse_literal_counts(
                        SUMMARY_RE["limited_data_object_type_counts"], stdout
                    ),
                    policy_forced_to_validate_total=_parse_int(
                        SUMMARY_RE["policy_forced_to_validate_total"], stdout
                    ),
                    promotion_reviewed_to_validate_total=_parse_int(
                        SUMMARY_RE["promotion_reviewed_to_validate_total"], stdout
                    ),
                    promoted_to_draft_total=_parse_int(SUMMARY_RE["promoted_to_draft_total"], stdout),
                    promotion_blocked_policy_total=_parse_int(
                        SUMMARY_RE["promotion_blocked_policy_total"], stdout
                    ),
                    promotion_blocked_type_total=_parse_int(SUMMARY_RE["promotion_blocked_type_total"], stdout),
                    promotion_blocked_low_conf_total=_parse_int(
                        SUMMARY_RE["promotion_blocked_low_conf_total"], stdout
                    ),
                    promotion_blocked_short_snippet_total=_parse_int(
                        SUMMARY_RE["promotion_blocked_short_snippet_total"], stdout
                    ),
                    promotion_blocked_low_overlap_total=_parse_int(
                        SUMMARY_RE["promotion_blocked_low_overlap_total"], stdout
                    ),
                    requirements_after=req_after,
                    requirements_final_per_document=req_total,
                    to_validate_count=to_validate_total,
                    to_validate_share=to_validate_share,
                    raw_to_final_conversion_rate=raw_to_final_conversion_rate,
                    postcall_rejected_total=postcall_rejected_total,
                    stdout_tail="\n".join((stdout or "").splitlines()[-50:]),
                    stderr_tail="\n".join((stderr or "").splitlines()[-50:]),
                )
                runs.append(run)

    elapsed_total = time.perf_counter() - global_start
    total_inserted = sum(r.inserted for r in runs)
    total_chunks = sum(r.chunks_before for r in runs)
    success_docs = sum(1 for r in runs if r.return_code == 0 and not r.timed_out)
    failed_docs = len(runs) - success_docs
    req_final_values = [r.requirements_final_per_document for r in runs]
    to_validate_shares = [r.to_validate_share for r in runs if r.requirements_final_per_document > 0]
    promotion_reviewed_total = sum(r.promotion_reviewed_to_validate_total for r in runs)
    promoted_to_draft_total = sum(r.promoted_to_draft_total for r in runs)
    promotion_blocked_policy_total = sum(r.promotion_blocked_policy_total for r in runs)
    promotion_blocked_type_total = sum(r.promotion_blocked_type_total for r in runs)
    promotion_blocked_low_conf_total = sum(r.promotion_blocked_low_conf_total for r in runs)
    promotion_blocked_short_snippet_total = sum(r.promotion_blocked_short_snippet_total for r in runs)
    promotion_blocked_low_overlap_total = sum(r.promotion_blocked_low_overlap_total for r in runs)

    drop_reason_totals: collections.Counter[str] = collections.Counter()
    for r in runs:
        for rc, count in (r.article_drop_reason_counts or {}).items():
            try:
                drop_reason_totals[str(rc)] += int(count)
            except Exception:
                continue
    drop_total = int(sum(drop_reason_totals.values()))
    drop_share_by_reason_code = {
        rc: round((cnt / drop_total), 4) if drop_total else 0.0
        for rc, cnt in sorted(drop_reason_totals.items(), key=lambda kv: (-kv[1], kv[0]))
    }

    doc_gate_policy_counts: collections.Counter[str] = collections.Counter(
        [str(r.doc_gate_policy or "UNKNOWN") for r in runs]
    )

    report = {
        "tenant_filter": args.tenant,
        "limit_docs": args.limit_docs,
        "jort_only": args.jort_only,
        "timeout_sec_per_doc": args.timeout_sec,
        "extract_limit_chunks": args.extract_limit_chunks,
        "force_full_rebuild": args.force_full_rebuild,
        "docs_executed": len(runs),
        "docs_success": success_docs,
        "docs_failed_or_timeout": failed_docs,
        "elapsed_total_sec": round(elapsed_total, 3),
        "elapsed_total_min": round(elapsed_total / 60.0, 3),
        "total_chunks": int(total_chunks),
        "total_inserted_requirements": int(total_inserted),
        "avg_requirements_per_doc": round((total_inserted / len(runs)), 3) if runs else 0.0,
        "requirements_final_per_document_avg": round((sum(req_final_values) / len(req_final_values)), 3)
        if req_final_values
        else 0.0,
        "to_validate_share_avg": round((sum(to_validate_shares) / len(to_validate_shares)), 4)
        if to_validate_shares
        else 0.0,
        "promotion_reviewed_to_validate_total": int(promotion_reviewed_total),
        "promoted_to_draft_total": int(promoted_to_draft_total),
        "promotion_success_rate": round((promoted_to_draft_total / promotion_reviewed_total), 4)
        if promotion_reviewed_total
        else 0.0,
        "promotion_blocked_policy_total": int(promotion_blocked_policy_total),
        "promotion_blocked_type_total": int(promotion_blocked_type_total),
        "promotion_blocked_low_conf_total": int(promotion_blocked_low_conf_total),
        "promotion_blocked_short_snippet_total": int(promotion_blocked_short_snippet_total),
        "promotion_blocked_low_overlap_total": int(promotion_blocked_low_overlap_total),
        "drop_reason_total": drop_total,
        "drop_share_by_reason_code": drop_share_by_reason_code,
        "doc_gate_policy_counts": dict(sorted(doc_gate_policy_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "avg_requirements_per_chunk": round((total_inserted / total_chunks), 4) if total_chunks else 0.0,
        "throughput_requirements_per_min": round((total_inserted / (elapsed_total / 60.0)), 3)
        if elapsed_total > 0
        else 0.0,
        "throughput_chunks_per_min": round((total_chunks / (elapsed_total / 60.0)), 3)
        if elapsed_total > 0
        else 0.0,
        "runs": [asdict(r) for r in runs],
    }

    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== A1 REAL EXTRACTION BENCHMARK =====")
    print(f"docs_executed                : {report['docs_executed']}")
    print(f"docs_success                 : {report['docs_success']}")
    print(f"docs_failed_or_timeout       : {report['docs_failed_or_timeout']}")
    print(f"elapsed_total_min            : {report['elapsed_total_min']}")
    print(f"total_chunks                 : {report['total_chunks']}")
    print(f"total_inserted_requirements  : {report['total_inserted_requirements']}")
    print(f"avg_requirements_per_doc     : {report['avg_requirements_per_doc']}")
    print(f"requirements_final/doc_avg   : {report['requirements_final_per_document_avg']}")
    print(f"to_validate_share_avg        : {report['to_validate_share_avg']}")
    print(f"promotion_reviewed_total     : {report['promotion_reviewed_to_validate_total']}")
    print(f"promoted_to_draft_total      : {report['promoted_to_draft_total']}")
    print(f"promotion_success_rate       : {report['promotion_success_rate']}")
    print(f"promotion_blocked_policy     : {report['promotion_blocked_policy_total']}")
    print(f"promotion_blocked_type       : {report['promotion_blocked_type_total']}")
    print(f"promotion_blocked_low_conf   : {report['promotion_blocked_low_conf_total']}")
    print(f"promotion_blocked_short_snip : {report['promotion_blocked_short_snippet_total']}")
    print(f"promotion_blocked_low_overlap: {report['promotion_blocked_low_overlap_total']}")
    print(f"drop_reason_total            : {report['drop_reason_total']}")
    print(f"drop_share_by_reason_code    : {report['drop_share_by_reason_code']}")
    print(f"doc_gate_policy_counts       : {report['doc_gate_policy_counts']}")
    print(f"avg_requirements_per_chunk   : {report['avg_requirements_per_chunk']}")
    print(f"throughput_req_per_min       : {report['throughput_requirements_per_min']}")
    print(f"throughput_chunks_per_min    : {report['throughput_chunks_per_min']}")
    print(f"report_path                  : {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
