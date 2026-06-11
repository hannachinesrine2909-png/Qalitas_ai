import argparse
import csv
import os
from collections import Counter
from datetime import datetime, UTC
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from tenant_db import connect_db

load_dotenv()


def _safe_slug(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in (text or "").strip().lower())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "document"


def fetch_registry_rows(cur, doc_id: str):
    cur.execute(
        """
        SELECT
            d.doc_id::text AS doc_id,
            d.title AS document_title,
            COALESCE(d.source, '') AS source,
            COALESCE(d.document_family, '') AS document_family,
            CASE
              WHEN lower(COALESCE(d.title, '')) ~ 'arr[êe]t[ée]' THEN 'ARRETE'
              WHEN lower(COALESCE(d.title, '')) LIKE '%%decret%%' THEN 'DECRET'
              WHEN lower(COALESCE(d.title, '')) LIKE '%%loi%%' THEN 'LOI'
              ELSE 'JORT_ACTE'
            END AS act_type,
            COALESCE(a.article_label, a.article_code, '(no_label)') AS article_label,
            COALESCE(a.article_code, '') AS article_code,
            r.requirement_no,
            r.req_type,
            r.requirement_text,
            COALESCE(r.status, '') AS status,
            COALESCE(r.confidence, 0)::float AS confidence,
            COALESCE(r.citation_ref, '') AS citation_ref,
            COALESCE(r.citation_snippet, '') AS citation_snippet
        FROM requirements r
        LEFT JOIN documents d ON d.doc_id = r.doc_id
        LEFT JOIN articles a ON a.article_id = r.article_id
        WHERE r.doc_id=%s
        ORDER BY
            COALESCE(a.start_page, 999999),
            COALESCE(a.start_char, 999999),
            COALESCE(r.requirement_no, 999999),
            r.requirement_id
        """,
        (doc_id,),
    )
    cols = [desc[0] for desc in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return rows


def compute_summary(rows: list[dict]) -> dict:
    total = len(rows)
    by_status = Counter((r.get("status") or "UNKNOWN").upper() for r in rows)
    by_type = Counter((r.get("req_type") or "UNKNOWN").upper() for r in rows)
    confidences = [float(r.get("confidence") or 0.0) for r in rows]
    avg_conf = round(sum(confidences) / len(confidences), 4) if confidences else 0.0

    normalized = []
    for r in rows:
        key = " ".join(str(r.get("requirement_text") or "").strip().lower().split())
        if key:
            normalized.append(key)
    duplicates = len(normalized) - len(set(normalized))
    duplicate_rate = round((duplicates / total), 4) if total else 0.0

    # Proxy opérationnel (pas un recall oracle).
    # Sert de signal santé run quand il n'y a pas de golden.
    draft_ratio = round(by_status.get("DRAFT", 0) / total, 4) if total else 0.0
    to_validate_ratio = round(by_status.get("TO_VALIDATE", 0) / total, 4) if total else 0.0
    proxy_quality_score = round(
        max(
            0.0,
            min(
                1.0,
                (0.45 * (1.0 - duplicate_rate))
                + (0.35 * avg_conf)
                + (0.20 * (1.0 - to_validate_ratio)),
            ),
        ),
        4,
    )

    return {
        "requirements_total": total,
        "by_status": dict(sorted(by_status.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))),
        "avg_confidence": avg_conf,
        "duplicate_count": int(duplicates),
        "duplicate_rate": duplicate_rate,
        "draft_ratio": draft_ratio,
        "to_validate_ratio": to_validate_ratio,
        "proxy_quality_score": proxy_quality_score,
    }


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "doc_id",
                    "document_title",
                    "source",
                    "document_family",
                    "act_type",
                    "article_label",
                    "article_code",
                    "requirement_no",
                    "req_type",
                    "requirement_text",
                    "status",
                    "confidence",
                    "citation_ref",
                    "citation_snippet",
                ]
            )
        return

    fieldnames = [
        "doc_id",
        "document_title",
        "source",
        "document_family",
        "act_type",
        "article_label",
        "article_code",
        "requirement_no",
        "req_type",
        "requirement_text",
        "status",
        "confidence",
        "citation_ref",
        "citation_snippet",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_markdown(rows: list[dict], summary: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# A1 Registry Report")
    lines.append("")
    if rows:
        doc_title = rows[0].get("document_title") or ""
        doc_id = rows[0].get("doc_id") or ""
        lines.append(f"- document: **{doc_title}**")
        lines.append(f"- doc_id: `{doc_id}`")
    lines.append(f"- requirements_total: **{summary['requirements_total']}**")
    lines.append(f"- avg_confidence: **{summary['avg_confidence']}**")
    lines.append(f"- duplicate_rate: **{summary['duplicate_rate']}**")
    lines.append(f"- draft_ratio: **{summary['draft_ratio']}**")
    lines.append(f"- to_validate_ratio: **{summary['to_validate_ratio']}**")
    lines.append(f"- proxy_quality_score: **{summary['proxy_quality_score']}**")
    lines.append("")
    lines.append("## Status")
    for k, v in summary["by_status"].items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Types")
    for k, v in summary["by_type"].items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Requirements")
    lines.append("")
    lines.append("| article | type | status | conf | text |")
    lines.append("|---|---|---|---:|---|")
    for row in rows:
        article = str(row.get("article_label") or "").replace("|", "/")
        req_type = str(row.get("req_type") or "").replace("|", "/")
        status = str(row.get("status") or "").replace("|", "/")
        conf = row.get("confidence") or 0
        text = " ".join(str(row.get("requirement_text") or "").split()).replace("|", "/")
        lines.append(f"| {article} | {req_type} | {status} | {conf} | {text} |")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc_id", required=True)
    parser.add_argument("--outdir", default="reports/registry")
    parser.add_argument("--tenant", default="", help="tenant_id pour activer le contexte RLS")
    args = parser.parse_args()

    dsn = os.getenv("PG_DSN")
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")

    with connect_db(dsn, tenant_id=str(args.tenant or "").strip() or None) as conn:
        with conn.cursor() as cur:
            rows = fetch_registry_rows(cur, args.doc_id)

    if rows:
        doc_title = rows[0].get("document_title") or ""
    else:
        doc_title = "document"

    summary = compute_summary(rows)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    slug = _safe_slug(doc_title)
    outdir = Path(args.outdir)
    latest_csv = outdir / "registry_latest.csv"
    latest_md = outdir / "registry_latest.md"
    hist_csv = outdir / "history" / f"registry_{slug}_{stamp}.csv"
    hist_md = outdir / "history" / f"registry_{slug}_{stamp}.md"

    write_csv(rows, latest_csv)
    write_csv(rows, hist_csv)
    write_markdown(rows, summary, latest_md)
    write_markdown(rows, summary, hist_md)

    print("===== A1 REGISTRY EXPORT =====")
    print(f"doc_id             : {args.doc_id}")
    print(f"requirements_total : {summary['requirements_total']}")
    print(f"avg_confidence     : {summary['avg_confidence']}")
    print(f"duplicate_rate     : {summary['duplicate_rate']}")
    print(f"proxy_quality_score: {summary['proxy_quality_score']}")
    print(f"latest_csv         : {latest_csv.resolve()}")
    print(f"latest_md          : {latest_md.resolve()}")
    print(f"history_csv        : {hist_csv.resolve()}")
    print(f"history_md         : {hist_md.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
