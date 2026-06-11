import argparse
import os
from collections.abc import Iterable

import psycopg
from dotenv import load_dotenv

from a1_shared_helpers import classify_qse_domain_subdomain
from tenant_db import connect_db


DEFAULT_DOC_IDS = [
    "506e45e1-288d-408b-815b-f4e20e29c991",  # Jo0282026
    "280c189b-f470-4f8f-bff7-abe5456242fd",  # Jo0292026
    "88977abf-d5c1-41dd-8d0e-d2f6ed21bb23",  # Jo0302026
    "21d0c672-04c2-470d-b857-43620d538b36",  # Jo1212025
    "fae61e0b-08d9-4692-abc9-85af1bd7e873",  # Jo1282022
]


def _iter_doc_ids(single_doc_id: str | None, raw_doc_ids_csv: str | None) -> Iterable[str]:
    if single_doc_id and single_doc_id.strip():
        yield single_doc_id.strip()
    if raw_doc_ids_csv and raw_doc_ids_csv.strip():
        for part in raw_doc_ids_csv.split(","):
            value = part.strip()
            if value:
                yield value
    if (not single_doc_id or not single_doc_id.strip()) and (
        not raw_doc_ids_csv or not raw_doc_ids_csv.strip()
    ):
        for doc_id in DEFAULT_DOC_IDS:
            yield doc_id


def _dedupe_doc_ids(doc_ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for doc_id in doc_ids:
        key = str(doc_id or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill officiel QSE (domaine/sous-domaine) pour les requirements "
            "d'un ou plusieurs documents."
        )
    )
    parser.add_argument("--doc_id", default="", help="Doc unique (prioritaire si fourni)")
    parser.add_argument(
        "--doc_ids",
        default="",
        help="Liste CSV de doc_id (optionnel). Si vide avec --doc_id vide: lot par défaut 5 docs.",
    )
    parser.add_argument("--dry_run", action="store_true", help="Simuler sans écrire en base")
    parser.add_argument("--tenant", default="", help="tenant_id pour activer le contexte RLS")
    args = parser.parse_args()

    load_dotenv()
    dsn = os.getenv("PG_DSN")
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")

    target_doc_ids = _dedupe_doc_ids(_iter_doc_ids(args.doc_id, args.doc_ids))
    if not target_doc_ids:
        raise RuntimeError("Aucun doc_id cible.")

    scanned = 0
    updated = 0
    unchanged = 0
    strategy_counts: dict[str, int] = {}

    with connect_db(dsn, tenant_id=str(args.tenant or "").strip() or None) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    requirement_id,
                    doc_id,
                    requirement_text,
                    req_type,
                    citation_snippet,
                    qse_domain,
                    qse_sub_domain
                FROM requirements
                WHERE doc_id = ANY(%s::uuid[])
                ORDER BY doc_id, requirement_no
                """,
                (target_doc_ids,),
            )
            rows = cur.fetchall()
            scanned = len(rows)

            for rid, _doc_id, req_text, req_type, snippet, old_domain, old_sub in rows:
                new_domain, new_sub, strategy = classify_qse_domain_subdomain(
                    requirement_text=str(req_text or ""),
                    req_type=str(req_type or ""),
                    citation_snippet=str(snippet or ""),
                    chunk_text="",
                )
                strategy_counts[str(strategy)] = int(strategy_counts.get(str(strategy)) or 0) + 1

                if str(old_domain or "") == new_domain and str(old_sub or "") == new_sub:
                    unchanged += 1
                    continue

                updated += 1
                if args.dry_run:
                    continue

                cur.execute(
                    """
                    UPDATE requirements
                    SET qse_domain=%s, qse_sub_domain=%s
                    WHERE requirement_id=%s
                    """,
                    (new_domain, new_sub, rid),
                )

        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

    print("==== A1 QSE BACKFILL ====")
    print(f"target_docs={len(target_doc_ids)}")
    print(f"scanned={scanned}")
    print(f"dry_run={bool(args.dry_run)}")
    print(f"updated={updated}")
    print(f"unchanged={unchanged}")
    print(f"strategy_counts={strategy_counts}")


if __name__ == "__main__":
    main()
