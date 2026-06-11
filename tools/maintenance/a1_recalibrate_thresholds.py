"""
a1_recalibrate_thresholds.py — Recalibration mensuelle des seuils de promotion
depuis les décisions de validation humaine.

Usage:
    python tools/maintenance/a1_recalibrate_thresholds.py [--days 30] [--out_json reports/calibration/thresholds_latest.json]

Logique:
    1. Charger les décisions (APPROVE/REJECT/FLAG) de requirement_validations (derniers N jours)
    2. Joindre avec requirements pour obtenir confidence, req_type
    3. Pour chaque seuil candidat 0.70-0.92, calculer precision/recall/F1
    4. Recommander le seuil qui maximise F1 avec recall >= 0.65
    5. Émettre le rapport JSON + les lignes .env à appliquer
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_dsn() -> str:
    load_dotenv()
    dsn = os.getenv("PG_DSN", "").strip()
    if not dsn:
        raise RuntimeError("PG_DSN manquant dans .env")
    return dsn


def _fetch_decisions(dsn: str, since: datetime) -> list[dict]:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    v.decision,
                    COALESCE(r.confidence, 0)::float        AS confidence,
                    COALESCE(r.req_type, 'UNKNOWN')         AS req_type,
                    v.created_at::text                      AS decided_at
                FROM requirement_validations v
                JOIN requirements r ON r.requirement_id = v.requirement_id
                WHERE v.created_at >= %s
                ORDER BY v.created_at DESC
                """,
                (since,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _bucket_label(confidence: float, step: float = 0.05) -> str:
    low = round(int(confidence / step) * step, 2)
    return f"[{low:.2f},{low + step:.2f}["


def _analyse(decisions: list[dict]) -> dict:
    if not decisions:
        return {"note": "Aucune décision dans la fenêtre — recalibration impossible."}

    total = len(decisions)
    total_approve = sum(1 for d in decisions if d["decision"] == "APPROVE")
    total_reject_flag = total - total_approve

    # Distribution par bucket de confiance
    buckets: dict[str, dict[str, int]] = {}
    for d in decisions:
        b = _bucket_label(float(d["confidence"] or 0))
        buckets.setdefault(b, {"approve": 0, "reject_flag": 0})
        if d["decision"] == "APPROVE":
            buckets[b]["approve"] += 1
        else:
            buckets[b]["reject_flag"] += 1

    # Analyse par seuil candidat
    thresholds = [round(t, 2) for t in [0.70, 0.72, 0.74, 0.76, 0.78, 0.80, 0.82, 0.84, 0.86, 0.88, 0.90, 0.92]]
    threshold_analysis = []
    for thresh in thresholds:
        above = [d for d in decisions if float(d["confidence"] or 0) >= thresh]
        if not above:
            continue
        tp = sum(1 for d in above if d["decision"] == "APPROVE")
        fp = len(above) - tp
        fn = total_approve - tp
        precision = round(tp / (tp + fp), 4) if (tp + fp) > 0 else 0.0
        recall    = round(tp / total_approve, 4) if total_approve > 0 else 0.0
        f1        = round(2 * precision * recall / (precision + recall), 4) if (precision + recall) > 0 else 0.0
        threshold_analysis.append({
            "threshold": thresh,
            "total_above": len(above),
            "tp": tp, "fp": fp, "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        })

    # Seuil optimal : F1 max avec recall >= 0.65
    eligible = [r for r in threshold_analysis if r["recall"] >= 0.65]
    best = max(eligible, key=lambda x: x["f1"]) if eligible else (
        max(threshold_analysis, key=lambda x: x["f1"]) if threshold_analysis else None
    )

    return {
        "total_decisions": total,
        "total_approve": total_approve,
        "total_reject_flag": total_reject_flag,
        "overall_fp_rate": round(total_reject_flag / total, 4) if total else 0.0,
        "confidence_buckets": buckets,
        "threshold_analysis": threshold_analysis,
        "recommended_threshold": best["threshold"] if best else None,
        "recommended_metrics": {k: best[k] for k in ("precision", "recall", "f1")} if best else {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Recalibration mensuelle des seuils A1")
    parser.add_argument("--days",     type=int,   default=30,                                                 help="Fenêtre temporelle (jours)")
    parser.add_argument("--out_json", default="reports/calibration/thresholds_latest.json")
    args = parser.parse_args()

    dsn  = _load_dsn()
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"[RECALIB] Décisions depuis {since.date()} ({args.days}j)...")

    decisions = _fetch_decisions(dsn, since)
    print(f"[RECALIB] {len(decisions)} décisions chargées.")
    if len(decisions) < 10:
        print("[RECALIB] WARN: < 10 décisions — résultat indicatif uniquement.")

    analysis = _analyse(decisions)

    load_dotenv()
    current_env = {
        "A1_PROMOTION_TO_DRAFT_MIN_CONFIDENCE": float(os.getenv("A1_PROMOTION_TO_DRAFT_MIN_CONFIDENCE", "0.82")),
        "A1_PROMOTION_LIMIT_CONF":              float(os.getenv("A1_PROMOTION_LIMIT_CONF", "0.75")),
    }

    recommended_env: dict = {}
    if analysis.get("recommended_threshold"):
        t = analysis["recommended_threshold"]
        recommended_env = {
            "A1_PROMOTION_TO_DRAFT_MIN_CONFIDENCE": t,
            "A1_PROMOTION_LIMIT_CONF":              round(t - 0.07, 2),
        }
        m = analysis["recommended_metrics"]
        print(f"\n[RECALIB] Seuil optimal : {t}")
        print(f"           Precision : {m.get('precision')} | Recall : {m.get('recall')} | F1 : {m.get('f1')}")
        if t != current_env["A1_PROMOTION_TO_DRAFT_MIN_CONFIDENCE"]:
            print(f"\n[RECALIB] Mise à jour .env recommandée :")
            print(f"  A1_PROMOTION_TO_DRAFT_MIN_CONFIDENCE={t}")
            print(f"  A1_PROMOTION_LIMIT_CONF={round(t - 0.07, 2)}")
        else:
            print("[RECALIB] Seuil actuel déjà optimal — aucun changement nécessaire.")
    else:
        print("[RECALIB] Pas assez de données pour une recommandation fiable.")

    report = {
        "generated_at":   _now_utc(),
        "window_days":    args.days,
        "since":          since.isoformat(),
        "current_env":    current_env,
        "recommended_env": recommended_env,
        **analysis,
    }

    out = Path(args.out_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[RECALIB] Rapport → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
