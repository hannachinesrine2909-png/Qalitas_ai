import argparse
import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

import fitz

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from a1_ingest_pdf_min import extract_page_text
from a1_segment_articles_chunks import segment_document

load_dotenv()


def _doc_ids_from_eval_latest(path: str) -> list[str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("results") or []
    doc_ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        raw = str(row.get("resolved_doc_id") or row.get("doc_id") or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        doc_ids.append(raw)
    return doc_ids


def _safe_insert_event(cur, tenant_id: str, doc_id: str, event_type: str, payload: dict) -> None:
    payload_json = json.dumps(payload, ensure_ascii=False)
    for sql in (
        """
        INSERT INTO events(tenant_id, doc_id, event_type, payload)
        VALUES (%s, %s, %s, %s::jsonb)
        """,
        """
        INSERT INTO events(tenant_id, doc_id, event_type, payload_json)
        VALUES (%s, %s, %s, %s::jsonb)
        """,
        """
        INSERT INTO events(tenant_id, doc_id, event_type, event_payload)
        VALUES (%s, %s, %s, %s::jsonb)
        """,
    ):
        try:
            cur.execute(sql, (tenant_id, doc_id, event_type, payload_json))
            return
        except Exception:
            continue


def _reextract_pages(cur, doc_id: str, *, extract_mode: str = "auto") -> dict:
    cur.execute(
        """
        SELECT tenant_id, title, file_path
        FROM documents
        WHERE doc_id=%s
        """,
        (doc_id,),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"doc_id introuvable: {doc_id}")

    tenant_id, title, file_path = row
    pdf_path = Path(str(file_path or "")).resolve()
    if not pdf_path.exists():
        raise RuntimeError(f"fichier PDF introuvable pour doc_id={doc_id}: {pdf_path}")

    cur.execute("DELETE FROM document_pages WHERE doc_id=%s", (doc_id,))

    page_count = 0
    non_empty_pages = 0
    with fitz.open(str(pdf_path)) as pdf:
        page_count = pdf.page_count
        for page_no in range(page_count):
            page = pdf.load_page(page_no)
            text = extract_page_text(page, extract_mode=extract_mode)
            if text:
                non_empty_pages += 1
            cur.execute(
                """
                INSERT INTO document_pages(doc_id, page_no, page_text)
                VALUES (%s, %s, %s)
                """,
                (doc_id, page_no, text),
            )

    _safe_insert_event(
        cur,
        tenant_id=str(tenant_id),
        doc_id=doc_id,
        event_type="PDF_PAGES_REINGESTED",
        payload={
            "mode": "inplace",
            "title": title,
            "pdf_path": str(pdf_path),
            "page_count": int(page_count),
            "non_empty_pages": int(non_empty_pages),
            "extract_engine": f"pymupdf_{extract_mode}",
        },
    )

    return {
        "doc_id": doc_id,
        "title": title,
        "pdf_path": str(pdf_path),
        "page_count": int(page_count),
        "non_empty_pages": int(non_empty_pages),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc_id", action="append", default=[], help="doc_id ciblé (répéter l'option)")
    parser.add_argument(
        "--from_eval_latest",
        action="store_true",
        help="Charge les doc_id uniques depuis reports/eval_latest.json",
    )
    parser.add_argument(
        "--eval_latest_path",
        default="reports/eval_latest.json",
        help="Chemin vers eval_latest.json",
    )
    parser.add_argument("--max_docs", type=int, default=None, help="Limiter le nombre de docs traités")
    parser.add_argument(
        "--resegment",
        type=int,
        choices=[0, 1],
        default=1,
        help="Relancer la segmentation articles+chunks après ré-ingestion des pages",
    )
    parser.add_argument(
        "--max_chars",
        type=int,
        default=1200,
        help="max_chars pour la re-segmentation",
    )
    parser.add_argument(
        "--report_path",
        default="reports/preflight/a1_reingest_pages_latest.json",
        help="Rapport JSON de sortie",
    )
    parser.add_argument(
        "--extract_mode",
        choices=["auto", "blocks", "sort_text"],
        default="auto",
        help="Mode d'extraction texte page (auto=recommande, sort_text pour debug OCR).",
    )
    args = parser.parse_args()

    dsn = os.getenv("PG_DSN")
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")

    doc_ids: list[str] = []
    seen: set[str] = set()

    for raw in args.doc_id:
        doc_id = str(raw or "").strip()
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            doc_ids.append(doc_id)

    if args.from_eval_latest:
        for doc_id in _doc_ids_from_eval_latest(args.eval_latest_path):
            if doc_id not in seen:
                seen.add(doc_id)
                doc_ids.append(doc_id)

    if args.max_docs is not None:
        doc_ids = doc_ids[: max(0, int(args.max_docs))]

    if not doc_ids:
        raise RuntimeError("Aucun doc_id fourni. Utiliser --doc_id ou --from_eval_latest.")

    runs: list[dict] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for idx, doc_id in enumerate(doc_ids, start=1):
                item: dict = {"doc_id": doc_id, "ok": False}
                try:
                    print(f"[{idx}/{len(doc_ids)}] Re-ingest pages {doc_id} ...", flush=True)
                    page_summary = _reextract_pages(cur, doc_id, extract_mode=args.extract_mode)
                    item.update(page_summary)
                    if args.resegment == 1:
                        seg = segment_document(doc_id=doc_id, max_chars=args.max_chars)
                        item["segmentation"] = {
                            "articles_inserted": int(seg.get("articles_inserted") or 0),
                            "chunks_created": int(seg.get("chunks_created") or 0),
                            "event_inserted": bool(seg.get("event_inserted")),
                        }
                    item["ok"] = True
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    item["error"] = str(exc)
                runs.append(item)

    summary = {
        "docs_total": len(doc_ids),
        "docs_ok": sum(1 for r in runs if r.get("ok")),
        "docs_failed": sum(1 for r in runs if not r.get("ok")),
        "runs": runs,
    }

    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== REINGEST PAGES INPLACE SUMMARY =====", flush=True)
    print(f"docs_total  : {summary['docs_total']}", flush=True)
    print(f"docs_ok     : {summary['docs_ok']}", flush=True)
    print(f"docs_failed : {summary['docs_failed']}", flush=True)
    print(f"report_path : {report_path}", flush=True)
    return 0 if summary["docs_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
