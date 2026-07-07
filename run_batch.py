"""
Batch runner: a CSV in -> a labeled output CSV out, plus full per-lead logs.

    python run_batch.py input.csv --image-col payment_document --out result.csv
    python run_batch.py input.csv --precomputed        # test using ex_* columns, no API

Output columns (exactly as specified):
    lead_id, lender, verification_status, outcome, payment_method
where:
    verification_status = verified | unverified | non_document
    outcome             = verified   -> the fields that were verified
                          unverified -> the reason it was not verified
                          non_document-> what the image consists of
"""
import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import settings
from observability.pg_logger import PgLeadLogger
from observability.pg_dedup import PaymentDedup
from ocr.medha_client import MedhaVisionOCR, PrecomputedOCR
from pipeline.orchestrator import process_lead


def outcome_text(status, outcome):
    if status == "verified":
        return "Verified fields: " + ", ".join(outcome.get("verified_fields", []))
    if status in ("unverified", "duplicate"):
        return outcome.get("reason", status)
    return outcome.get("describes", "not a valid payment document")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_csv")
    ap.add_argument("--image-col", default="payment_document")
    ap.add_argument("--id-col", default=None, help="column to use as lead_id (default: row index)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--precomputed", action="store_true",
                    help="use ex_* columns instead of calling the vision API")
    ap.add_argument("--image-root", default="",
                    help="prefix for local image paths if the CSV holds filenames")
    args = ap.parse_args()

    df = pd.read_csv(args.input_csv, dtype=str).fillna("")
    logger = PgLeadLogger()
    dedup = PaymentDedup()
    ocr = PrecomputedOCR() if args.precomputed else MedhaVisionOCR()

    rows_out = []
    for idx, row in df.iterrows():
        row = row.to_dict()
        lead_id = str(row.get(args.id_col) if args.id_col else f"LEAD-{idx}")
        lender = row.get("institute_name", "")
        img = row.get(args.image_col, "")
        if args.image_root and img and not os.path.isabs(img):
            img = os.path.join(args.image_root, img)

        res = process_lead(lead_id, lender, img, row, ocr, logger,
                           skip_image_qc=args.precomputed, dedup=dedup)
        rows_out.append({
            "lead_id": lead_id,
            "lender": lender,
            "verification_status": res["verification_status"],
            "outcome": outcome_text(res["verification_status"], res["outcome"]),
            "payment_method": res["payment_method"],
            "outcome_detail": json.dumps(res["outcome"], ensure_ascii=False),
        })
        if (idx + 1) % 200 == 0:
            print(f"  processed {idx+1}/{len(df)}")

    out_df = pd.DataFrame(rows_out)
    # verified at bottom, review/non-document at top (work-first ordering)
    order = {"unverified": 0, "non_document": 1, "duplicate": 2, "verified": 3}
    out_df["_o"] = out_df["verification_status"].map(order).fillna(0)
    out_df = out_df.sort_values("_o").drop(columns="_o")

    out_path = args.out or os.path.join(settings.OUTPUT_DIR,
                    os.path.splitext(os.path.basename(args.input_csv))[0] + "_verified.csv")
    out_df.to_csv(out_path, index=False, encoding="utf-8-sig")

    vc = out_df["verification_status"].value_counts()
    print("\n=== DONE ===")
    for k in ("verified", "unverified", "duplicate", "non_document"):
        print(f"  {k:14s}: {int(vc.get(k,0))}")
    print(f"\nOutput : {out_path}")
    print(f"Logs   : {settings.DATABASE_URL}  (query any lead with view_logs.py <lead_id>)")


if __name__ == "__main__":
    main()
