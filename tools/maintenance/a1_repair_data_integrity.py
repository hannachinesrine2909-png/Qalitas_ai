import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psycopg
from dotenv import load_dotenv

load_dotenv()


CITATION_REF_RE = re.compile(r"^\s*(?P<label>.+?)\s*\(p\.(?P<sp>\d+)-(?P<ep>\d+)\)\s*$", re.IGNORECASE)


@dataclass
class CitationContext:
    label: str
    start_page_zero_based: Optional[int]
    end_page_zero_based: Optional[int]


def normalize_text(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def parse_citation_ref(citation_ref: str) -> CitationContext:
    raw = (citation_ref or "").strip()
    if not raw:
        return CitationContext(label="", start_page_zero_based=None, end_page_zero_based=None)

    match = CITATION_REF_RE.match(raw)
    if not match:
        return CitationContext(label=raw, start_page_zero_based=None, end_page_zero_based=None)

    label = match.group("label").strip()
    sp = int(match.group("sp")) - 1
    ep = int(match.group("ep")) - 1
    return CitationContext(label=label, start_page_zero_based=max(sp, 0), end_page_zero_based=max(ep, 0))


def candidate_score(
    *,
    article_label: str,
    article_ref: str,
    article_start_page: Optional[int],
    article_end_page: Optional[int],
    article_text: str,
    citation_ctx: CitationContext,
    citation_snippet: str,
) -> int:
    score = 0

    article_label_n = normalize_text(article_label)
    article_ref_n = normalize_text(article_ref)
    citation_label_n = normalize_text(citation_ctx.label)

    if citation_label_n:
        if article_label_n == citation_label_n:
            score += 60
        if article_ref_n == citation_label_n:
            score += 45
        if article_label_n.startswith(citation_label_n):
            score += 20
        if article_ref_n.startswith(citation_label_n):
            score += 15

    if citation_ctx.start_page_zero_based is not None and citation_ctx.end_page_zero_based is not None:
        start_page = article_start_page if article_start_page is not None else citation_ctx.start_page_zero_based
        end_page = article_end_page if article_end_page is not None else citation_ctx.end_page_zero_based
        overlaps = not (
            end_page < citation_ctx.start_page_zero_based or start_page > citation_ctx.end_page_zero_based
        )
        if overlaps:
            score += 35
            if start_page == citation_ctx.start_page_zero_based:
                score += 8
            if end_page == citation_ctx.end_page_zero_based:
                score += 8

    snippet = (citation_snippet or "").strip()
    if snippet:
        snippet_n = normalize_text(snippet)
        article_text_n = normalize_text(article_text)
        if snippet_n and snippet_n in article_text_n:
            score += 25

    return score


def backfill_requirements_article_id(cur, dry_run: bool) -> dict:
    cur.execute(
        """
        SELECT requirement_id, doc_id, citation_ref, citation_snippet
        FROM requirements
        WHERE article_id IS NULL
          AND doc_id IS NOT NULL
        ORDER BY created_at ASC
        """
    )
    rows = cur.fetchall()

    summary = {
        "target_rows": len(rows),
        "resolved": 0,
        "unresolved": 0,
        "ambiguous": 0,
        "updated_requirement_ids": [],
        "unresolved_requirement_ids": [],
        "ambiguous_requirement_ids": [],
    }

    for requirement_id, doc_id, citation_ref, citation_snippet in rows:
        citation_ctx = parse_citation_ref(citation_ref or "")
        if not citation_ctx.label and citation_ctx.start_page_zero_based is None:
            summary["unresolved"] += 1
            summary["unresolved_requirement_ids"].append(str(requirement_id))
            continue

        like_prefix = f"{citation_ctx.label}%"
        cur.execute(
            """
            SELECT article_id, article_label, article_ref, start_page, end_page, article_text
            FROM articles
            WHERE doc_id=%s
              AND (
                lower(article_label) = lower(%s)
                OR lower(article_ref) = lower(%s)
                OR lower(article_label) LIKE lower(%s)
                OR lower(article_ref) LIKE lower(%s)
              )
            ORDER BY start_page NULLS LAST, end_page NULLS LAST
            """,
            (doc_id, citation_ctx.label, citation_ctx.label, like_prefix, like_prefix),
        )
        candidates = cur.fetchall()

        if not candidates and citation_ctx.start_page_zero_based is not None and citation_ctx.end_page_zero_based is not None:
            cur.execute(
                """
                SELECT article_id, article_label, article_ref, start_page, end_page, article_text
                FROM articles
                WHERE doc_id=%s
                  AND COALESCE(start_page, -1) <= %s
                  AND COALESCE(end_page, 999999) >= %s
                ORDER BY start_page NULLS LAST, end_page NULLS LAST
                """,
                (doc_id, citation_ctx.end_page_zero_based, citation_ctx.start_page_zero_based),
            )
            candidates = cur.fetchall()

        if not candidates:
            summary["unresolved"] += 1
            summary["unresolved_requirement_ids"].append(str(requirement_id))
            continue

        scored = []
        for candidate in candidates:
            c_article_id, c_label, c_ref, c_start_page, c_end_page, c_text = candidate
            score = candidate_score(
                article_label=c_label or "",
                article_ref=c_ref or "",
                article_start_page=c_start_page,
                article_end_page=c_end_page,
                article_text=c_text or "",
                citation_ctx=citation_ctx,
                citation_snippet=citation_snippet or "",
            )
            scored.append((score, candidate))

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_candidate = scored[0]

        if best_score <= 0:
            summary["unresolved"] += 1
            summary["unresolved_requirement_ids"].append(str(requirement_id))
            continue

        top_score_count = sum(1 for score, _ in scored if score == best_score)
        if top_score_count > 1:
            summary["ambiguous"] += 1
            summary["ambiguous_requirement_ids"].append(str(requirement_id))
            continue

        best_article_id = best_candidate[0]
        if not dry_run:
            cur.execute(
                """
                UPDATE requirements
                SET article_id=%s
                WHERE requirement_id=%s
                """,
                (best_article_id, requirement_id),
            )

        summary["resolved"] += 1
        summary["updated_requirement_ids"].append(str(requirement_id))

    return summary


def ensure_integrity_guards(cur, dry_run: bool) -> dict:
    result = {
        "index_documents_tenant_sha256": "skipped_dry_run" if dry_run else "applied",
        "requirements_article_id_constraint": "skipped_dry_run" if dry_run else "applied_or_already_present",
    }

    if dry_run:
        return result

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_documents_tenant_sha256
        ON documents(tenant_id, sha256)
        """
    )

    cur.execute("SELECT COUNT(*) FROM requirements WHERE article_id IS NULL")
    remaining_nulls = cur.fetchone()[0]
    if remaining_nulls > 0:
        result["requirements_article_id_constraint"] = (
            f"not_applied_remaining_nulls={remaining_nulls}"
        )
        return result

    cur.execute(
        """
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'requirements_article_id_not_null_chk'
        """
    )
    exists = cur.fetchone() is not None

    if not exists:
        cur.execute(
            """
            ALTER TABLE requirements
            ADD CONSTRAINT requirements_article_id_not_null_chk
            CHECK (article_id IS NOT NULL) NOT VALID
            """
        )

    cur.execute("ALTER TABLE requirements VALIDATE CONSTRAINT requirements_article_id_not_null_chk")
    return result


def collect_summary(cur) -> dict:
    cur.execute("SELECT COUNT(*) FROM requirements WHERE article_id IS NULL")
    requirements_article_id_null = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT tenant_id, sha256, COUNT(*)
            FROM documents
            GROUP BY tenant_id, sha256
            HAVING COUNT(*) > 1
        ) t
        """
    )
    duplicate_document_sha_groups = cur.fetchone()[0]

    cur.execute(
        """
        SELECT tenant_id, sha256, COUNT(*) AS cnt
        FROM documents
        GROUP BY tenant_id, sha256
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC, tenant_id
        """
    )
    duplicate_groups = [
        {"tenant_id": row[0], "sha256": row[1], "count": row[2]}
        for row in cur.fetchall()
    ]

    return {
        "requirements_article_id_null": requirements_article_id_null,
        "duplicate_document_sha_groups": duplicate_document_sha_groups,
        "duplicate_document_sha_details": duplicate_groups,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry_run", action="store_true", help="Ne modifie pas la base")
    parser.add_argument("--skip_backfill", action="store_true")
    parser.add_argument("--skip_constraints", action="store_true")
    parser.add_argument(
        "--report_path",
        default="reports/preflight/a1_integrity_repair_latest.json",
        help="Chemin du rapport JSON",
    )
    parser.add_argument(
        "--fail_on_remaining_null",
        action="store_true",
        help="Retourne code 1 si des requirements sans article_id restent",
    )
    args = parser.parse_args()

    dsn = os.getenv("PG_DSN")
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")

    report = {
        "dry_run": bool(args.dry_run),
        "backfill": None,
        "guards": None,
        "post_checks": None,
    }

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            if not args.skip_backfill:
                report["backfill"] = backfill_requirements_article_id(cur, dry_run=args.dry_run)

            if not args.skip_constraints:
                report["guards"] = ensure_integrity_guards(cur, dry_run=args.dry_run)

            report["post_checks"] = collect_summary(cur)

        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))

    remaining_nulls = int(report["post_checks"]["requirements_article_id_null"])
    if args.fail_on_remaining_null and remaining_nulls > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
