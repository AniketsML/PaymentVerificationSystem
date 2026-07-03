"""
The lead-cycle flow. Runs one lead through the stages, logging the outcome,
processing time and full input/output of EACH stage so the whole journey is
reconstructable by lead id.

    Stage D  dedup            -> CSV-identity duplicate guard, BEFORE any OCR
    Stage 0  load_image       -> fetch pixels (URL/local); reject if unreadable
    Stage 1  image_qc         -> good enough to bother the model? (NO enhancement)
    Stage 2  ocr_classify     -> vision model -> valid payment doc? (+ raw output logged)
    Stage 3  extract          -> structured JSON + field labels
    Stage 4  verify           -> verified | unverified (+ reason)

Final statuses:
    verified       - all mandatory fields matched (only on positive evidence)
    unverified     - a mandatory field failed / unreadable (reason logged)
    non_document   - not a valid payment document, or image discarded at validation
    duplicate      - exact re-submission (lead_code + loan + amount + month) — no OCR spent
"""
from __future__ import annotations

import time

from pipeline import image_qc, image_source, ocr_classify, extract, verify
from pipeline.models import (VERIFIED, UNVERIFIED, NON_DOCUMENT,
                             DUPLICATE, PASS, FAIL)


def _elapsed_ms(t0: float) -> float:
    return round((time.time() - t0) * 1000, 1)


def process_lead(lead_id: str, lender: str, image_path: str, row: dict,
                 ocr_client, logger, skip_image_qc: bool = False, dedup=None) -> dict:
    logger.log(lead_id, "lead_received", PASS,
               f"lender={lender}", data={"image_source": image_path}, ms=0.0)

    # ── STAGE D : CSV-identity dedup — runs BEFORE OCR so duplicates cost nothing ─
    manual_reason = None
    if dedup is not None:
        t0 = time.time()
        verdict, reason, ident = dedup.evaluate(row)
        logger.log(lead_id, "stage_dedup", FAIL if verdict == "duplicate" else PASS,
                   f"{verdict}: {reason}", ms=_elapsed_ms(t0),
                   data={"verdict": verdict, "identity": _ident_str(ident)})
        if verdict == "duplicate":
            # exact re-submission — short-circuit, no image load / no model call
            return _finish(lead_id, lender, DUPLICATE,
                           {"reason": reason, "verdict": "duplicate",
                            "identity": _ident_str(ident)}, "", {}, logger)
        if ident:
            dedup.record(ident, lead_id)          # remember this payment for the future
        if verdict == "manual_review":
            manual_reason = reason                 # still OCR it so a reviewer sees the data

    image = None

    # ── STAGE 0/1 : load pixels + basic validation (precomputed mode skips) ────
    if skip_image_qc:
        logger.log(lead_id, "stage1_image_qc", PASS, "skipped (precomputed/test mode)", ms=0.0)
    else:
        t0 = time.time()
        image, _raw, err = image_source.load(image_path)
        if err:
            logger.log(lead_id, "stage0_load_image", FAIL, err,
                       data={"image_source": image_path}, ms=_elapsed_ms(t0))
            return _finish(lead_id, lender, NON_DOCUMENT,
                           {"describes": f"image could not be loaded: {err}",
                            "stage_failed": "load_image"},
                           "", {}, logger)
        logger.log(lead_id, "stage0_load_image", PASS,
                   f"loaded {image.width}x{image.height} {image.format or ''}".strip(),
                   ms=_elapsed_ms(t0))

        t0 = time.time()
        ok, reason, qc_metrics = image_qc.evaluate(image)
        logger.log(lead_id, "stage1_image_qc", PASS if ok else FAIL, reason,
                   metrics=qc_metrics, ms=_elapsed_ms(t0))
        if not ok:
            # bad-quality images are DISCARDED (no preprocessing/salvage attempt)
            return _finish(lead_id, lender, NON_DOCUMENT,
                           {"describes": f"image discarded at validation: {reason}",
                            "stage_failed": "image_qc"},
                           "", {}, logger)

    # ── STAGE 2 : vision model OCR + document classification ───────────────────
    t0 = time.time()
    is_doc, reason2, extraction, m2 = ocr_classify.run(image, row, ocr_client)
    logger.log(lead_id, "stage2_ocr_classify", PASS if is_doc else FAIL, reason2,
               metrics=m2, ms=_elapsed_ms(t0),
               data={"raw_model_response": (extraction.get("_raw") or "")[:8000],
                     "model_meta": extraction.get("_meta", {}),
                     "extracted": {k: extraction.get(k) for k in (
                         "is_payment_document", "document_type", "loan_account_number",
                         "amount", "date", "time", "reference_id", "receiver_name",
                         "payer_name")},
                     "full_text": (extraction.get("full_text") or "")[:4000]})
    if not is_doc:
        return _finish(lead_id, lender, NON_DOCUMENT,
                       {"describes": extraction.get("describes") or reason2,
                        "document_type": extraction.get("document_type", ""),
                        "stage_failed": "ocr_classify"},
                       extraction.get("payment_method", "Other"),
                       {"raw_text": (extraction.get("full_text") or "")[:400]}, logger)

    # ── STAGE 3 : structured extraction + field labelling ─────────────────────
    t0 = time.time()
    doc = extract.run(extraction, row)
    logger.log(lead_id, "stage3_extract", PASS, "fields extracted & labelled",
               ms=_elapsed_ms(t0),
               data={"field_labels": doc.field_labels, "payment_method": doc.payment_method})

    # ── STAGE 4 : deterministic verification ──────────────────────────────────
    t0 = time.time()
    status, outcome = verify.run(doc, row)
    logger.log(lead_id, "stage4_verify", PASS if status == VERIFIED else FAIL,
               outcome.get("reason", "all mandatory fields matched"),
               data=outcome, ms=_elapsed_ms(t0))

    # dedup flagged a different loan a/c under a known lead_code. It needs a human
    # look, and every `unverified` lead is manually checked anyway, so we surface it
    # as `unverified` (with the dedup concern) rather than a separate bucket. The full
    # verification result stays nested under `verification` for the reviewer.
    if manual_reason:
        vreason = outcome.get("reason")
        outcome = {"reason": manual_reason + (f"; {vreason}" if vreason else ""),
                   "flag": "different_loan_account_same_lead_code",
                   "verification": outcome, "verification_status": status}
        status = UNVERIFIED

    return _finish(lead_id, lender, status, outcome, doc.payment_method,
                   doc.to_dict(), logger)


def _ident_str(ident) -> str:
    if not ident:
        return ""
    return f"{ident['lead_code']} | {ident['loan_acct']} | {ident['amount']} | {ident['pay_ym']}"


def _finish(lead_id, lender, status, outcome, payment_method, extracted, logger) -> dict:
    logger.log(lead_id, "lead_closed", PASS, f"status={status}",
               data={"verification_status": status}, ms=0.0)
    logger.save_result(lead_id, lender, status, payment_method, outcome, extracted)
    return {
        "lead_id": lead_id,
        "lender": lender,
        "verification_status": status,
        "payment_method": payment_method,
        "outcome": outcome,
        "extracted": extracted,
    }
