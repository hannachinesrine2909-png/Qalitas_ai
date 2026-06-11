from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


DOC_ID_RE = re.compile(r"doc_id=([0-9a-fA-F\-]{36})")


@dataclass
class FileResult:
    pdf_path: str
    file_name: str
    return_code: int
    elapsed_sec: float
    status: str  # created | reused | error
    doc_id: str
    message: str
    stdout_tail: str
    stderr_tail: str


def _discover_pdfs(root: Path) -> list[Path]:
    found: dict[str, Path] = {}
    for pattern in ("*.pdf", "*.PDF"):
        for p in root.rglob(pattern):
            found[str(p.resolve())] = p.resolve()
    return sorted(found.values(), key=lambda p: str(p).lower())


def _parse_doc_id(stdout: str) -> str:
    m = DOC_ID_RE.search(stdout or "")
    return str(m.group(1)) if m else ""


def _status_from_stdout(rc: int, stdout: str) -> str:
    txt = (stdout or "").lower()
    if rc == 0 and "doublon detecte" in txt:
        return "reused"
    if rc == 0 and "ok doc_id=" in txt:
        return "created"
    return "error"


def _run_ingest(
    *,
    script_dir: Path,
    pdf_path: Path,
    tenant: str,
    source: str,
    jurisdiction: str,
    document_family: str,
    on_duplicate: str,
    extract_mode: str,
) -> FileResult:
    cmd = [
        sys.executable,
        "a1_ingest_pdf_min.py",
        "--pdf",
        str(pdf_path),
        "--tenant",
        tenant,
        "--source",
        source,
        "--jurisdiction",
        jurisdiction,
        "--document_family",
        document_family,
        "--on_duplicate",
        on_duplicate,
        "--extract_mode",
        extract_mode,
    ]
    start = time.perf_counter()
    cp = subprocess.run(
        cmd,
        cwd=str(script_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.perf_counter() - start
    stdout = cp.stdout or ""
    stderr = cp.stderr or ""
    doc_id = _parse_doc_id(stdout)
    status = _status_from_stdout(cp.returncode, stdout)
    message = ""
    if status == "reused":
        message = "duplicate_reused"
    elif status == "created":
        message = "ingested"
    else:
        message = "failed"
    return FileResult(
        pdf_path=str(pdf_path),
        file_name=pdf_path.name,
        return_code=int(cp.returncode),
        elapsed_sec=round(elapsed, 3),
        status=status,
        doc_id=doc_id,
        message=message,
        stdout_tail="\n".join(stdout.splitlines()[-20:]),
        stderr_tail="\n".join(stderr.splitlines()[-20:]),
    )


def _run_segment(
    *,
    script_dir: Path,
    doc_id: str,
    max_chars: int,
) -> tuple[bool, str]:
    if not doc_id:
        return False, "missing_doc_id"
    cmd = [
        sys.executable,
        "a1_segment_articles_chunks.py",
        "--doc_id",
        doc_id,
        "--max_chars",
        str(int(max_chars)),
    ]
    cp = subprocess.run(
        cmd,
        cwd=str(script_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    ok = cp.returncode == 0
    tail = "\n".join((cp.stdout or "").splitlines()[-12:])
    if not ok:
        tail = "\n".join((cp.stderr or "").splitlines()[-12:]) or tail
    return ok, tail


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch ingest JORT PDFs from a directory.")
    ap.add_argument("--pdf_root", default=r"C:\Users\MEGA-PC\Desktop\TIM\JORT")
    ap.add_argument("--tenant", default="tim")
    ap.add_argument("--source", default="JORT")
    ap.add_argument("--jurisdiction", default="TN")
    ap.add_argument("--document_family", default="legal_text")
    ap.add_argument("--on_duplicate", choices=["reuse", "fail", "reinject"], default="reuse")
    ap.add_argument("--extract_mode", choices=["auto", "blocks", "sort_text"], default="auto")
    ap.add_argument("--segment_after", choices=["on", "off"], default="on")
    ap.add_argument("--max_chars", type=int, default=1200)
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument("--report_json", default="reports/preflight/a1_batch_ingest_jort_latest.json")
    args = ap.parse_args()

    root = Path(args.pdf_root).resolve()
    if not root.exists():
        raise FileNotFoundError(str(root))
    script_dir = Path(__file__).resolve().parents[2]
    pdfs = _discover_pdfs(root)
    if args.limit and int(args.limit) > 0:
        pdfs = pdfs[: int(args.limit)]
    if not pdfs:
        raise RuntimeError(f"No PDFs found under: {root}")

    started_at = datetime.now(timezone.utc).isoformat()
    run_start = time.perf_counter()
    results: list[FileResult] = []
    segment_ok = 0
    segment_fail = 0

    for idx, pdf in enumerate(pdfs, start=1):
        print(f"[{idx}/{len(pdfs)}] ingest {pdf.name} ...", flush=True)
        res = _run_ingest(
            script_dir=script_dir,
            pdf_path=pdf,
            tenant=args.tenant,
            source=args.source,
            jurisdiction=args.jurisdiction,
            document_family=args.document_family,
            on_duplicate=args.on_duplicate,
            extract_mode=args.extract_mode,
        )
        results.append(res)
        if args.segment_after == "on" and res.status in {"created", "reused"} and res.doc_id:
            ok, tail = _run_segment(script_dir=script_dir, doc_id=res.doc_id, max_chars=args.max_chars)
            if ok:
                segment_ok += 1
            else:
                segment_fail += 1
                res.message = f"{res.message};segment_failed"
                if res.stderr_tail:
                    res.stderr_tail += "\n"
                res.stderr_tail += f"[segment]\n{tail}"

    elapsed_total = time.perf_counter() - run_start
    created = sum(1 for r in results if r.status == "created")
    reused = sum(1 for r in results if r.status == "reused")
    failed = sum(1 for r in results if r.status == "error")

    report = {
        "started_at_utc": started_at,
        "elapsed_total_sec": round(elapsed_total, 3),
        "pdf_root": str(root),
        "tenant": args.tenant,
        "source": args.source,
        "jurisdiction": args.jurisdiction,
        "document_family": args.document_family,
        "on_duplicate": args.on_duplicate,
        "extract_mode": args.extract_mode,
        "segment_after": args.segment_after,
        "max_chars": int(args.max_chars),
        "pdfs_total": len(pdfs),
        "ingest_created": created,
        "ingest_reused": reused,
        "ingest_failed": failed,
        "segment_ok": segment_ok,
        "segment_failed": segment_fail,
        "results": [asdict(r) for r in results],
    }

    out = Path(args.report_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    hist = out.parent.parent / "history" / "preflight" / f"a1_batch_ingest_jort_{stamp}.json"
    hist.parent.mkdir(parents=True, exist_ok=True)
    hist.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== BATCH INGEST JORT =====", flush=True)
    print(f"pdfs_total     : {len(pdfs)}", flush=True)
    print(f"ingest_created : {created}", flush=True)
    print(f"ingest_reused  : {reused}", flush=True)
    print(f"ingest_failed  : {failed}", flush=True)
    print(f"segment_ok     : {segment_ok}", flush=True)
    print(f"segment_failed : {segment_fail}", flush=True)
    print(f"report_latest  : {out}", flush=True)
    print(f"report_history : {hist}", flush=True)
    return 0 if failed == 0 and segment_fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
