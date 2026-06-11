import os
import json
import argparse
import hashlib
import shutil
import math
from pathlib import Path

import fitz  # PyMuPDF
import psycopg
from dotenv import load_dotenv
from tenant_db import connect_db

load_dotenv()


def _bootstrap_ocr_runtime() -> None:
    # 1) Tesseract binary path
    cwd = Path(__file__).resolve().parent
    bin_candidates: list[Path] = []
    env_bin = str(os.getenv("QALITAS_TESSERACT_DIR", "")).strip()
    if env_bin:
        bin_candidates.append(Path(env_bin))
    bin_candidates.extend(
        [
            cwd / ".tools" / "Tesseract-OCR",
            Path("C:/Program Files/Tesseract-OCR"),
        ]
    )
    for c in bin_candidates:
        try:
            exe = c / "tesseract.exe"
            if not exe.exists():
                continue
            current = os.getenv("PATH", "")
            marker = str(c).lower()
            if marker not in current.lower():
                os.environ["PATH"] = f"{c};{current}" if current else str(c)
            break
        except Exception:
            continue

    # 2) Tessdata path (priority to project local dir)
    tess_candidates: list[Path] = []
    env_tess = str(os.getenv("QALITAS_TESSDATA_DIR", "")).strip()
    if env_tess:
        tess_candidates.append(Path(env_tess))
    tess_candidates.extend(
        [
            cwd / ".tools" / "tessdata",
            Path("C:/Program Files/Tesseract-OCR/tessdata"),
        ]
    )
    if not str(os.getenv("TESSDATA_PREFIX", "")).strip():
        for t in tess_candidates:
            try:
                if t.exists() and t.is_dir():
                    os.environ["TESSDATA_PREFIX"] = str(t)
                    break
            except Exception:
                continue


_bootstrap_ocr_runtime()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_text(s: str) -> str:
    if not s:
        return ""

    s = s.replace("\u00a0", " ")
    s = s.replace("\xad", "")  # soft hyphen éventuel
    s = "\n".join(line.rstrip() for line in s.splitlines())
    s = "\n".join(line for line in s.splitlines())
    s = "\n".join(line for line in s.splitlines())
    return s.strip()


def _text_quality_metrics(text: str) -> dict[str, float]:
    src = str(text or "")
    total = len(src)
    if total <= 0:
        return {
            "total_chars": 0.0,
            "alpha_ratio": 0.0,
            "digit_ratio": 0.0,
            "space_ratio": 0.0,
            "pua_ratio": 0.0,
        }
    alpha = 0
    digits = 0
    spaces = 0
    pua = 0
    for ch in src:
        o = ord(ch)
        if ch.isalpha():
            alpha += 1
        if ch.isdigit():
            digits += 1
        if ch.isspace():
            spaces += 1
        if 0xE000 <= o <= 0xF8FF:
            pua += 1
    denom = float(total)
    return {
        "total_chars": float(total),
        "alpha_ratio": float(alpha / denom),
        "digit_ratio": float(digits / denom),
        "space_ratio": float(spaces / denom),
        "pua_ratio": float(pua / denom),
    }


def _is_text_suspicious_for_pipeline(text: str) -> bool:
    m = _text_quality_metrics(text)
    total = int(m["total_chars"])
    # Ne jamais traiter une page vide ou quasi vide comme "saine":
    # sinon l'OCR page-par-page ne se declenche pas et on obtient un faux 0 exigence.
    if total <= 0:
        return True
    if total < 40:
        return m["alpha_ratio"] < 0.20
    if total < 120:
        if m["alpha_ratio"] <= 0.05 and m["digit_ratio"] <= 0.35:
            return True
        return False
    # Cas typique observé: caractères U+F0xx (polices PDF non mappées Unicode).
    if m["pua_ratio"] >= 0.35:
        return True
    # Très peu de lettres et pas un document essentiellement numérique.
    if m["alpha_ratio"] <= 0.01 and m["digit_ratio"] <= 0.35:
        return True
    return False


def _extract_page_text_ocr(page: fitz.Page, *, language: str, dpi: int) -> str:
    # PyMuPDF OCR nécessite Tesseract installé côté système.
    try:
        text_page = page.get_textpage_ocr(language=language, dpi=max(72, int(dpi)), full=True)
    except TypeError:
        text_page = page.get_textpage_ocr(language=language, full=True)
    return normalize_text(page.get_text("text", textpage=text_page, sort=True))


def _sort_blocks_top_to_bottom(blocks: list[tuple]) -> list[tuple]:
    return sorted(blocks, key=lambda b: (float(b[1]), float(b[0])))


def _reconstruct_page_text_from_blocks(blocks: list[tuple], page_width: float) -> str:
    """
    Reconstruit le texte d'une page en limitant le mélange inter-colonnes
    fréquent sur les PDFs juridiques en double colonne (JORT).
    """
    if not blocks:
        return ""

    cleaned: list[tuple] = []
    for block in blocks:
        if len(block) < 5:
            continue
        x0, y0, x1, y1, text = block[:5]
        t = normalize_text(str(text or ""))
        if not t:
            continue
        cleaned.append((float(x0), float(y0), float(x1), float(y1), t))

    if not cleaned:
        return ""

    split_x = float(page_width) * 0.52
    left = [b for b in cleaned if ((b[0] + b[2]) / 2.0) <= split_x]
    right = [b for b in cleaned if ((b[0] + b[2]) / 2.0) > split_x]

    has_two_columns = False
    if len(left) >= 3 and len(right) >= 3:
        left_right_edge = max((b[2] for b in left), default=0.0)
        right_left_edge = min((b[0] for b in right), default=float(page_width))
        # Séparation horizontale minimale pour éviter un faux split.
        if (right_left_edge - left_right_edge) >= (float(page_width) * 0.04):
            has_two_columns = True

    if has_two_columns:
        ordered = _sort_blocks_top_to_bottom(left) + _sort_blocks_top_to_bottom(right)
    else:
        ordered = _sort_blocks_top_to_bottom(cleaned)

    return normalize_text("\n\n".join(b[4] for b in ordered if b[4]))


def extract_page_text(page: fitz.Page, extract_mode: str = "auto") -> str:
    mode = (extract_mode or "auto").strip().lower()
    if mode == "sort_text":
        return normalize_text(page.get_text("text", sort=True))
    if mode == "blocks":
        blocks = page.get_text("blocks")
        return normalize_text(
            _reconstruct_page_text_from_blocks(
                blocks=blocks if isinstance(blocks, list) else [],
                page_width=float(page.rect.width),
            )
        )

    blocks = page.get_text("blocks")
    text = _reconstruct_page_text_from_blocks(
        blocks=blocks if isinstance(blocks, list) else [],
        page_width=float(page.rect.width),
    )
    if not text:
        text = page.get_text("text", sort=True)
    return normalize_text(text)


def infer_title(pdf_path: Path, explicit_title: str | None) -> str:
    if explicit_title and explicit_title.strip():
        return explicit_title.strip()

    return pdf_path.stem


def infer_source(explicit_source: str | None) -> str:
    if explicit_source and explicit_source.strip():
        return explicit_source.strip()

    return "manual_pdf"


def _safe_unlink(path: Path | None) -> None:
    target = path if isinstance(path, Path) else None
    if not target:
        return
    try:
        if target.exists() and target.is_file():
            target.unlink()
    except Exception:
        pass


def find_existing_document_by_sha(cur, tenant_id: str, sha256: str):
    cur.execute(
        """
        SELECT doc_id, title, file_path, created_at
        FROM documents
        WHERE tenant_id=%s AND sha256=%s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (tenant_id, sha256),
    )
    return cur.fetchone()


def _count_requirements_for_doc(cur, doc_id) -> int:
    cur.execute("SELECT COUNT(*) FROM requirements WHERE doc_id=%s", (doc_id,))
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def _count_suspicious_pages_for_doc(cur, doc_id) -> tuple[int, int]:
    cur.execute(
        """
        SELECT page_text
        FROM document_pages
        WHERE doc_id=%s
        ORDER BY page_no
        """,
        (doc_id,),
    )
    rows = cur.fetchall()
    total = len(rows or [])
    suspicious = 0
    for row in rows or []:
        text = str((row or [""])[0] or "")
        if _is_text_suspicious_for_pipeline(text):
            suspicious += 1
    return suspicious, total


def _existing_doc_is_unreadable_and_empty(cur, doc_id) -> bool:
    req_count = _count_requirements_for_doc(cur, doc_id)
    suspicious_pages, total_pages = _count_suspicious_pages_for_doc(cur, doc_id)
    if req_count > 0:
        return False
    if total_pages <= 0:
        return False
    threshold = max(1, int(math.ceil(float(total_pages) * 0.60)))
    return suspicious_pages >= threshold


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="Chemin vers le PDF")
    ap.add_argument("--tenant", required=True, help="tenant_id (ex: textile)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--source", default=None)
    ap.add_argument("--jurisdiction", default="TN")
    ap.add_argument("--doc_code", default=None)
    ap.add_argument("--doc_version", default=None)
    ap.add_argument("--document_family", default="REGLEMENTAIRE")
    ap.add_argument("--issuer", default=None)
    ap.add_argument("--effective_date", default=None)
    ap.add_argument("--system_scope", default=None)
    ap.add_argument(
        "--extract_mode",
        choices=["auto", "blocks", "sort_text"],
        default="auto",
        help="Mode d'extraction texte page (auto=recommande, sort_text pour debug OCR).",
    )
    ap.add_argument(
        "--ocr_fallback",
        choices=["auto", "off", "force"],
        default="auto",
        help="Fallback OCR si texte PDF illisible (auto=recommande).",
    )
    ap.add_argument(
        "--ocr_language",
        default="fra+ara",
        help="Langues OCR Tesseract (ex: fra+ara).",
    )
    ap.add_argument(
        "--ocr_dpi",
        type=int,
        default=300,
        help="DPI OCR (PyMuPDF/Tesseract).",
    )
    ap.add_argument(
        "--fail_on_unreadable",
        choices=["on", "off"],
        default="on",
        help="Bloquer l'ingestion si le texte reste illisible (évite les faux 0 exigence).",
    )
    ap.add_argument(
        "--on_duplicate",
        choices=["reuse", "fail", "reinject"],
        default="reuse",
        help="Comportement si un document de meme SHA256 existe deja pour le tenant",
    )
    args = ap.parse_args()

    dsn = os.getenv("PG_DSN")
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    title = infer_title(pdf_path, args.title)
    source = infer_source(args.source)
    sha = sha256_file(str(pdf_path))

    storage_dir = Path("storage/pdfs") / args.tenant
    storage_dir.mkdir(parents=True, exist_ok=True)

    with connect_db(dsn, tenant_id=args.tenant) as conn:
        with conn.cursor() as cur:
            existing_doc = find_existing_document_by_sha(
                cur=cur,
                tenant_id=args.tenant,
                sha256=sha,
            )
            force_reinject = False
            if existing_doc and args.on_duplicate == "reuse":
                existing_doc_id = existing_doc[0]
                if _existing_doc_is_unreadable_and_empty(cur, existing_doc_id):
                    force_reinject = True
                    print(
                        "WARNING: document doublon existant détecté avec texte illisible "
                        "et 0 exigence; bascule automatique vers reinject."
                    )

            if existing_doc and args.on_duplicate != "reinject" and not force_reinject:
                existing_doc_id, existing_title, existing_path, existing_created_at = existing_doc

                if args.on_duplicate == "fail":
                    raise RuntimeError(
                        "Document en doublon detecte "
                        f"(doc_id={existing_doc_id}, title={existing_title}, created_at={existing_created_at}). "
                        "Utiliser --on_duplicate reuse ou --on_duplicate reinject."
                    )

                recovered_storage = False
                existing_path_str = str(existing_path or "").strip()
                existing_file_missing = (
                    not existing_path_str
                    or existing_path_str == "PENDING"
                    or not Path(existing_path_str).exists()
                )
                if existing_file_missing:
                    dest_pdf = storage_dir / f"{existing_doc_id}.pdf"
                    shutil.copy2(pdf_path, dest_pdf)
                    cur.execute(
                        "UPDATE documents SET file_path=%s WHERE doc_id=%s",
                        (str(dest_pdf), existing_doc_id),
                    )
                    recovered_storage = True

                payload = {
                    "mode": "manual_ingest_duplicate_reuse",
                    "source_filename": pdf_path.name,
                    "duplicate_of_doc_id": str(existing_doc_id),
                    "duplicate_created_at": str(existing_created_at),
                    "duplicate_title": existing_title,
                    "on_duplicate": args.on_duplicate,
                    "storage_recovered": recovered_storage,
                }
                cur.execute(
                    """
                    INSERT INTO events(tenant_id, doc_id, event_type, payload)
                    VALUES (%s, %s, %s, %s::jsonb)
                    """,
                    (
                        args.tenant,
                        existing_doc_id,
                        "PDF_DUPLICATE_DETECTED",
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
                conn.commit()

                print(f"WARNING: Doublon detecte, document existant reutilise (doc_id={existing_doc_id})")
                print(f"Titre existant : {existing_title}")
                print(f"Cree le        : {existing_created_at}")
                return

            # 1) créer document en DB
            cur.execute(
                """
                INSERT INTO documents(tenant_id, title, source, jurisdiction, file_path, sha256,
                document_code, document_version, document_family, issuer, effective_date, system_scope
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING doc_id
                """,
            (
                args.tenant,
                title,
                source,
                args.jurisdiction,
                "PENDING",
                sha,
                args.doc_code,
                args.doc_version,
                args.document_family,
                args.issuer,
                args.effective_date,
                args.system_scope,
            ),
            )
            doc_id = cur.fetchone()[0]

            # 2) copier PDF dans storage
            dest_pdf = storage_dir / f"{doc_id}.pdf"
            shutil.copy2(pdf_path, dest_pdf)

            # 3) update file_path
            cur.execute(
                "UPDATE documents SET file_path=%s WHERE doc_id=%s",
                (str(dest_pdf), doc_id),
            )

            # 4) extraire texte page par page
            with fitz.open(str(dest_pdf)) as pdf:
                page_count = pdf.page_count
                suspicious_pages = 0
                ocr_pages = 0
                ocr_attempted = False
                ocr_available: bool | None = None
                ocr_error = ""
                ocr_applied_pages: list[int] = []  # pages ayant réellement utilisé l'OCR

                for page_no in range(page_count):
                    page = pdf.load_page(page_no)
                    text = extract_page_text(page, extract_mode=args.extract_mode)
                    suspicious = _is_text_suspicious_for_pipeline(text)
                    page_used_ocr = False

                    if suspicious and args.ocr_fallback in {"auto", "force"}:
                        ocr_attempted = True
                        if ocr_available is not False:
                            try:
                                ocr_text = _extract_page_text_ocr(
                                    page,
                                    language=str(args.ocr_language or "fra+ara"),
                                    dpi=int(args.ocr_dpi),
                                )
                                ocr_available = True
                                if ocr_text:
                                    # On bascule OCR si meilleur (ou au moins moins "suspect").
                                    if (not _is_text_suspicious_for_pipeline(ocr_text)) or (
                                        _text_quality_metrics(ocr_text)["alpha_ratio"]
                                        >= (_text_quality_metrics(text)["alpha_ratio"] * 1.5)
                                    ):
                                        text = ocr_text
                                        suspicious = _is_text_suspicious_for_pipeline(text)
                                        ocr_pages += 1
                                        page_used_ocr = True
                            except Exception as exc:
                                ocr_available = False
                                ocr_error = str(exc or "").strip()

                    if suspicious:
                        suspicious_pages += 1

                    if page_used_ocr:
                        ocr_applied_pages.append(page_no)

                    cur.execute(
                        """
                        INSERT INTO document_pages(doc_id, page_no, page_text)
                        VALUES (%s, %s, %s)
                        """,
                        (doc_id, page_no, text),
                    )

                fail_unreadable = str(args.fail_on_unreadable).strip().lower() == "on"
                suspicious_threshold = max(1, int(math.ceil(float(page_count) * 0.60)))
                if fail_unreadable and suspicious_pages >= suspicious_threshold:
                    details = (
                        f"pages={page_count}, suspicious_pages={suspicious_pages}, "
                        f"ocr_attempted={ocr_attempted}, ocr_pages={ocr_pages}, "
                        f"ocr_available={ocr_available}"
                    )
                    if ocr_attempted and ocr_available is False:
                        _safe_unlink(dest_pdf)
                        raise RuntimeError(
                            "Texte PDF illisible (encodage non Unicode) et OCR indisponible. "
                            "Installez Tesseract (fra+ara) puis relancez, ou utilisez un PDF OCR. "
                            f"Details: {details}. OCR error: {ocr_error or 'n/a'}"
                        )
                    _safe_unlink(dest_pdf)
                    raise RuntimeError(
                        "Texte PDF illisible: extraction non fiable (risque de 0 exigence erroné). "
                        f"Details: {details}"
                    )

            # 5) event interne enrichi
            payload = {
                "mode": "manual_ingest",
                "source_filename": pdf_path.name,
                "stored_filename": dest_pdf.name,
                "page_count": page_count,
                "extract_engine": f"pymupdf_{args.extract_mode}",
                "ocr_fallback": args.ocr_fallback,
                "ocr_language": args.ocr_language,
                "jurisdiction": args.jurisdiction,
                "source": source,
                "suspicious_pages": int(suspicious_pages),
                "ocr_pages": int(ocr_pages),
                "ocr_applied_pages": ocr_applied_pages,
            }

            cur.execute(
                """
                INSERT INTO events(tenant_id, doc_id, event_type, payload)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (args.tenant, doc_id, "PDF_ADDED", json.dumps(payload, ensure_ascii=False)),
            )

        conn.commit()

    print(f"OK doc_id={doc_id}")
    print(f"PDF stocké : {dest_pdf}")
    print(f"Pages extraites : {page_count}")


if __name__ == "__main__":
    main()
