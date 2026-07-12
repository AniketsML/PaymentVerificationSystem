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
    non_document   - the model saw the image and said: not a valid payment document
    unprocessed    - the image never reached OCR (URL missing / private / broken / QC discard)
    duplicate      - exact re-submission (lead_code + loan + amount + month) — no OCR spent
"""
from __future__ import annotations

import time

from pipeline import image_qc, image_source, ocr_classify, extract, verify
from pipeline.models import (VERIFIED, UNVERIFIED, NON_DOCUMENT, UNPROCESSED,
                             DUPLICATE, PASS, FAIL)


def _elapsed_ms(t0: float) -> float:
    return round((time.time() - t0) * 1000, 1)


def process_lead(lead_id: str, lender: str, image_path: str, row: dict,
                 ocr_client, logger, skip_image_qc: bool = False, dedup=None,
                 is_test: bool = False) -> dict:
    # every write for this lead carries is_test, so a sandbox row is NEVER momentarily
    # visible as real (no post-hoc flag update, no race).
    def log(*a, **k):
        logger.log(*a, is_test=is_test, **k)

    log(lead_id, "lead_received", PASS,
        f"lender={lender}", data={"image_source": image_path}, ms=0.0)

    # ── STAGE D : CSV-identity dedup — runs BEFORE OCR so duplicates cost nothing ─
    manual_reason = None
    if dedup is not None:
        t0 = time.time()
        # self_lead_id: a retry must never see its own attempt-1 record as a duplicate
        verdict, reason, ident = dedup.evaluate(row, is_test, self_lead_id=lead_id)
        if verdict != "duplicate" and ident:
            # ATOMIC claim — the unique index arbitrates two same-identity leads racing:
            # exactly one wins; the loser learns the winner's lead_id and is a duplicate.
            owner = dedup.claim(ident, lead_id, is_test)
            if owner != lead_id:
                verdict = "duplicate"
                reason = (f"already processed (lead {owner}, loan {ident['loan_acct']}, "
                          f"amount {ident['amount']}, month {ident['pay_ym']})")
        log(lead_id, "stage_dedup", FAIL if verdict == "duplicate" else PASS,
            f"{verdict}: {reason}", ms=_elapsed_ms(t0),
            data={"verdict": verdict, "identity": _ident_str(ident)})
        if verdict == "duplicate":
            # exact re-submission — short-circuit, no image load / no model call
            return _finish(lead_id, lender, DUPLICATE,
                           {"reason": reason, "verdict": "duplicate",
                            "identity": _ident_str(ident)}, "", {}, logger, is_test)
        if verdict == "manual_review":
            manual_reason = reason                 # still OCR it so a reviewer sees the data

    image = None

    # ── STAGE 0/1 : load pixels + basic validation (skipped in precomputed mode) ─
    if skip_image_qc:
        log(lead_id, "stage1_image_qc", PASS, "skipped (precomputed mode)", ms=0.0)
    else:
        t0 = time.time()
        image, _raw, err = image_source.load(image_path)
        if err:
            log(lead_id, "stage0_load_image", FAIL, err,
                data={"image_source": image_path}, ms=_elapsed_ms(t0))
            return _finish(lead_id, lender, UNPROCESSED,
                           {"describes": f"image could not be loaded: {err}",
                            "stage_failed": "load_image"},
                           "", {}, logger, is_test)
        log(lead_id, "stage0_load_image", PASS,
            f"loaded {image.width}x{image.height} {image.format or ''}".strip(),
            ms=_elapsed_ms(t0))

        t0 = time.time()
        ok, reason, qc_metrics = image_qc.evaluate(image)
        log(lead_id, "stage1_image_qc", PASS if ok else FAIL, reason,
            metrics=qc_metrics, ms=_elapsed_ms(t0))
        if not ok:
            # bad-quality images are DISCARDED (no preprocessing/salvage attempt)
            return _finish(lead_id, lender, UNPROCESSED,
                           {"describes": f"image discarded at validation: {reason}",
                            "stage_failed": "image_qc"},
                           "", {}, logger, is_test)

    # ── STAGE 2 : vision model OCR + document classification ───────────────────
    t0 = time.time()
    is_doc, reason2, extraction, m2 = ocr_classify.run(image, row, ocr_client)
    log(lead_id, "stage2_ocr_classify", PASS if is_doc else FAIL, reason2,
        metrics=m2, ms=_elapsed_ms(t0),
        data={"raw_model_response": (extraction.get("_raw") or "")[:4000],
              "model_meta": extraction.get("_meta", {}),
              "extracted": {k: extraction.get(k) for k in (
                  "is_payment_document", "document_type", "loan_account_number",
                  "amount", "date", "time", "reference_id", "receiver_name",
                  "payer_name")},
              "full_text": (extraction.get("full_text") or "")[:3000]})
    if not is_doc:
        return _finish(lead_id, lender, NON_DOCUMENT,
                       {"describes": extraction.get("describes") or reason2,
                        "document_type": extraction.get("document_type", ""),
                        "stage_failed": "ocr_classify"},
                       extraction.get("payment_method", "Other"),
                       {"raw_text": (extraction.get("full_text") or "")[:400]}, logger, is_test)

    # ── STAGE 3 : structured extraction + field labelling ─────────────────────
    t0 = time.time()
    doc = extract.run(extraction, row)
    log(lead_id, "stage3_extract", PASS, "fields extracted & labelled",
        ms=_elapsed_ms(t0),
        data={"field_labels": doc.field_labels, "payment_method": doc.payment_method})

    # ── STAGE 4 : deterministic verification ──────────────────────────────────
    t0 = time.time()
    status, outcome = verify.run(doc, row)
    log(lead_id, "stage4_verify", PASS if status == VERIFIED else FAIL,
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
                   doc.to_dict(), logger, is_test)


def _ident_str(ident) -> str:
    if not ident:
        return ""
    return f"{ident['lead_code']} | {ident['loan_acct']} | {ident['amount']} | {ident['pay_ym']}"


def _finish(lead_id, lender, status, outcome, payment_method, extracted, logger,
            is_test=False) -> dict:
    logger.log(lead_id, "lead_closed", PASS, f"status={status}",
               data={"verification_status": status}, ms=0.0, is_test=is_test)
    logger.save_result(lead_id, lender, status, payment_method, outcome, extracted,
                       is_test=is_test)
    return {
        "lead_id": lead_id,
        "lender": lender,
        "verification_status": status,
        "payment_method": payment_method,
        "outcome": outcome,
        "extracted": extracted,
    }
