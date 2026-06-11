import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


DOC_ID_RE = re.compile(r"doc_id=([0-9a-fA-F-]{36})")


def _run(cmd: list[str], timeout_sec: int) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    cp = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout_sec,
    )
    return int(cp.returncode), cp.stdout or "", cp.stderr or ""


def _safe_print(text: str) -> None:
    msg = str(text or "").replace("\ufffd", "?")
    try:
        print(msg)
        return
    except UnicodeEncodeError:
        pass

    stream = sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    payload = (msg + "\n").encode(encoding, errors="replace")
    if hasattr(stream, "buffer"):
        stream.buffer.write(payload)
        stream.flush()
        return
    stream.write(payload.decode(encoding, errors="replace"))
    stream.flush()


def _print_step(step_title: str, out: str, err: str) -> None:
    _safe_print(step_title)
    out_block = str(out or "").strip()
    err_block = str(err or "").strip()
    if out_block:
        _safe_print(out_block)
    if err_block:
        _safe_print(err_block)


def _extract_doc_id(stdout: str) -> str | None:
    m = DOC_ID_RE.search(stdout or "")
    if not m:
        return None
    return m.group(1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pipeline one-click: ingest PDF -> segment -> extract -> backfill QSE -> export registry"
        )
    )
    parser.add_argument("--pdf", required=True, help="Chemin du PDF JORT")
    parser.add_argument("--tenant", required=True, help="tenant_id")
    parser.add_argument("--title", default=None)
    parser.add_argument("--source", default="jort_manual")
    parser.add_argument("--jurisdiction", default="TN")
    parser.add_argument("--document_family", default="REGLEMENTAIRE")
    parser.add_argument("--extract_limit", type=int, default=None, help="Limiter nb chunks extraction")
    parser.add_argument("--sleep_ms", type=int, default=0)
    parser.add_argument("--on_duplicate", choices=["reuse", "fail", "reinject"], default="reuse")
    parser.add_argument("--timeout_ingest_sec", type=int, default=900)
    parser.add_argument("--timeout_segment_sec", type=int, default=7200)
    parser.add_argument("--timeout_extract_sec", type=int, default=3600)
    parser.add_argument("--timeout_backfill_sec", type=int, default=900)
    parser.add_argument("--timeout_export_sec", type=int, default=900)
    parser.add_argument(
        "--enable_qse_backfill",
        choices=["on", "off"],
        default="on",
        help="Rafraichir qse_domain/qse_sub_domain après extraction",
    )
    parser.add_argument("--max_chars", type=int, default=1200, help="Taille max chunk segmentation")
    parser.add_argument("--registry_outdir", default="reports/registry")
    args = parser.parse_args()
    args.timeout_segment_sec = max(300, int(args.timeout_segment_sec))
    args.timeout_extract_sec = max(600, int(args.timeout_extract_sec))
    backfill_enabled = str(args.enable_qse_backfill).strip().lower() == "on"
    total_steps = 5 if backfill_enabled else 4
    step_no = 1

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    py = sys.executable

    ingest_cmd = [
        py,
        "a1_ingest_pdf_min.py",
        "--pdf",
        str(pdf_path),
        "--tenant",
        args.tenant,
        "--source",
        args.source,
        "--jurisdiction",
        args.jurisdiction,
        "--document_family",
        args.document_family,
        "--on_duplicate",
        args.on_duplicate,
    ]
    if args.title:
        ingest_cmd.extend(["--title", args.title])

    rc, out, err = _run(ingest_cmd, timeout_sec=int(args.timeout_ingest_sec))
    _print_step(f"===== STEP {step_no}/{total_steps} INGEST =====", out, err)
    if rc != 0:
        raise RuntimeError("Echec ingestion PDF")
    step_no += 1

    doc_id = _extract_doc_id(out)
    if not doc_id:
        raise RuntimeError("Impossible de lire doc_id depuis la sortie ingestion.")

    segment_cmd = [
        py,
        "a1_segment_articles_chunks.py",
        "--doc_id",
        doc_id,
        "--tenant",
        args.tenant,
        "--max_chars",
        str(args.max_chars),
    ]
    rc, out, err = _run(segment_cmd, timeout_sec=int(args.timeout_segment_sec))
    _print_step(f"===== STEP {step_no}/{total_steps} SEGMENT =====", out, err)
    if rc != 0:
        raise RuntimeError("Echec segmentation")
    step_no += 1

    extract_cmd = [
        py,
        "a1_extract_requirements_llm.py",
        "--doc_id",
        doc_id,
        "--tenant",
        args.tenant,
        "--sleep_ms",
        str(int(args.sleep_ms)),
    ]
    if args.extract_limit is not None and int(args.extract_limit) > 0:
        extract_cmd.extend(["--limit", str(int(args.extract_limit))])

    rc, out, err = _run(extract_cmd, timeout_sec=int(args.timeout_extract_sec))
    _print_step(f"===== STEP {step_no}/{total_steps} EXTRACT =====", out, err)
    if rc != 0:
        raise RuntimeError("Echec extraction requirements")
    step_no += 1

    if backfill_enabled:
        backfill_cmd = [
            py,
            "a1_backfill_qse_fields.py",
            "--doc_id",
            doc_id,
            "--tenant",
            args.tenant,
        ]
        rc, out, err = _run(backfill_cmd, timeout_sec=int(args.timeout_backfill_sec))
        _print_step(f"===== STEP {step_no}/{total_steps} QSE BACKFILL =====", out, err)
        if rc != 0:
            raise RuntimeError("Echec backfill QSE")
        step_no += 1

    export_cmd = [
        py,
        "a1_export_registry.py",
        "--doc_id",
        doc_id,
        "--tenant",
        args.tenant,
        "--outdir",
        args.registry_outdir,
    ]
    rc, out, err = _run(export_cmd, timeout_sec=int(args.timeout_export_sec))
    _print_step(f"===== STEP {step_no}/{total_steps} REGISTRY =====", out, err)
    if rc != 0:
        raise RuntimeError("Echec export registry")

    _safe_print("")
    _safe_print("===== PIPELINE DONE =====")
    _safe_print(f"doc_id: {doc_id}")
    if backfill_enabled:
        _safe_print("Ingestion/segmentation/extraction/backfill QSE/export terminés.")
    else:
        _safe_print("Ingestion/segmentation/extraction/export terminés.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
