# Architecture

How the system is put together, how a lead flows through it, and exactly how each
decision is made. Read this once and you can debug most things.

- [1. Big picture](#1-big-picture)
- [2. Execution model — queue + workers](#2-execution-model--queue--workers)
- [3. Idempotency & resume](#3-idempotency--resume)
- [4. The pipeline (`process_lead`)](#4-the-pipeline-process_lead)
- [5. Classification — where "valid document?" is decided](#5-classification--where-valid-document-is-decided)
- [6. Verification & the final decision](#6-verification--the-final-decision)
- [7. Duplicate guard](#7-duplicate-guard)
- [8. Component map](#8-component-map)

---

## 1. Big picture

Two planes:

- **Ingestion plane** — an upload turns a CSV/Excel into one **job per lead** in a
  Postgres `jobs` table. This is durable: nothing is processed inside the HTTP
  request.
- **Processing plane** — a pool of **workers** claims jobs off the queue and runs
  each through the verification **pipeline**, writing per‑stage events and a final
  result back to Postgres. The web UI polls progress.

```
Browser ──upload──▶ /api/enqueue ──▶ [ jobs ]  ◀──claim── Worker×N ──run──▶ pipeline
   ▲                                    │  (Postgres)                         │
   └───────── poll /api/batch ──────────┘                                     ▼
                                                        lead_events · lead_results · processed_payments
```

Everything persists in **PostgreSQL** (`DATABASE_URL`). Images are **never stored** —
they stay as URLs (or local paths) and are fetched on demand.

---

## 2. Execution model — queue + workers

**Enqueue** ([pipeline/jobs.py](../pipeline/jobs.py) `enqueue_rows`)
- Reads the uploaded rows, computes each `lead_id` (the **Lead‑ID column** value, or
  `LEAD-<row index>` if none), resolves the image URL/path, and inserts one row into
  `jobs` with `INSERT … ON CONFLICT (job_id) DO NOTHING`.
- `job_id = lead_id`. So a lead already in the queue (in any state) is **not**
  re‑inserted — the enqueue reports `{enqueued, skipped}`.

**Claim** ([pipeline/jobs.py](../pipeline/jobs.py) `claim_one`) — the classic safe‑queue pattern:
```sql
UPDATE jobs SET status='in_progress', attempts=attempts+1,
       lease_until = now() + make_interval(secs => :lease)
WHERE job_id = (
  SELECT job_id FROM jobs
  WHERE status='pending' OR (status='in_progress' AND lease_until < now())
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED           -- many workers never collide
  LIMIT 1)
RETURNING *;
```
- `FOR UPDATE SKIP LOCKED` lets N workers pull different jobs concurrently.
- The **lease** is how crash recovery works: if a worker dies mid‑job, its
  `in_progress` row's `lease_until` expires and another worker re‑claims it.

**Finish**
- Success → `complete(job_id, verification_status)` sets `status='done'`.
- Error → `fail(job_id, err)` sets `status='pending'` for a retry until
  `attempts >= max_attempts`, then parks it as `status='failed'` with `last_error`.

**Workers** ([worker.py](../worker.py))
- `start_pool()` launches `WORKER_COUNT` daemon threads inside the web app on boot.
- `python worker.py` runs a standalone pool — run more processes to scale
  throughput (the Medha call is the bottleneck, so parallelism is the main lever).

---

## 3. Idempotency & resume

There are **two independent** "have I seen this?" mechanisms. Keeping them separate
is deliberate — conflating them is what makes naive systems flip good leads to
"duplicate".

| Concern | Key | Table | Effect |
|---|---|---|---|
| **Processing idempotency** (don't re‑do work) | `job_id = lead_id` | `jobs` | Re‑uploading a file skips leads already queued/done. |
| **Payment duplicate** (already‑processed payment) | `lead_code + loan_acct + amount + month` | `processed_payments` | A second lead with the same payment identity → `duplicate` (see §7). |

**Resume:** re‑upload the same file. Already‑processed `lead_id`s are skipped
(`ON CONFLICT DO NOTHING`); only the not‑yet‑done leads run. A worker/app restart
mid‑run needs no action — `pending` jobs are still there, and any `in_progress`
job whose lease expired is re‑claimed. **Requirement:** use a stable unique
**Lead‑ID column**; the default row index only resumes correctly if the file's rows
and order are unchanged.

---

## 4. The pipeline (`process_lead`)

[pipeline/orchestrator.py](../pipeline/orchestrator.py) runs the stages below. **Every** stage writes a
`lead_events` row with `status` (PASS/FAIL), `reason`, processing time `ms`,
`metrics`, and `data`. A journey always starts with `lead_received` and ends with
`lead_closed`.

| Stage | Event name | What it does | Failure → |
|---|---|---|---|
| D | `stage_dedup` | **CSV‑identity duplicate guard, runs first — before any OCR** ([pg_dedup.py](../observability/pg_dedup.py)). See §7. | `duplicate` (exact match, no model call) · continues + flags a different loan a/c (final status `unverified`) |
| 0 | `stage0_load_image` | Fetch pixels from URL/local/data‑URI and decode ([image_source.py](../pipeline/image_source.py)). Skipped in test/precomputed mode. | `non_document` ("image could not be loaded: …") |
| 1 | `stage1_image_qc` | **Basic validation only, no enhancement** ([image_qc.py](../pipeline/image_qc.py)): not near‑black, not too blurry, not blank/flat. **No minimum‑resolution gate** (small legit receipts were being discarded). | `non_document` (image discarded, reason logged) |
| 2 | `stage2_ocr_classify` | Call the **Medha** vision model, then apply the **deterministic non‑document gate** + label the payment method ([ocr_classify.py](../pipeline/ocr_classify.py)). Logs the **raw model response**, model latency, extracted fields, and the `evidence` behind the decision. | `non_document` **only** when there is zero payment evidence (see §5); anything with a hint of evidence continues to verify |
| 3 | `stage3_extract` | Build the structured `ExtractedDocument` and field labels ([extract.py](../pipeline/extract.py)). | — |
| 4 | `stage4_verify` | Deterministic match vs the CSV row + lender rules ([verify.py](../pipeline/verify.py)). | `unverified` (which field failed — or the dedup different‑loan flag) |
| — | `lead_closed` | Persist the final row to `lead_results`. | — |

**Image handling detail:** in live mode the image is downloaded once and passed as a
PIL image to the model (converted to JPEG only for transport — *not* enhancement).
In **precomputed/test mode** stages 0–1 are skipped and the "extraction" comes from
`ex_*` columns on the row, so the whole pipeline runs with **no network/model
calls** — this is how the automated tests and the *Test mode* checkbox work.

---

## 5. Classification — where "valid document?" is decided

There are **three** places that influence whether a lead is a valid payment
document. If a document is wrongly accepted/rejected, check them in this order:

1. **The Medha prompt** ([ocr/medha_client.py](../ocr/medha_client.py) `_SCHEMA_PROMPT`) — the model returns
   `is_payment_document` (true/false) and a `document_type`. This is the primary
   classifier. Loan **No‑Dues / NOC / closure** certificates are explicitly told to
   count as valid payment proof.
2. **The deterministic non‑document gate** ([ocr_classify.py](../pipeline/ocr_classify.py) `is_payment_candidate` /
   `payment_evidence`) — this decides `non_document` vs "payment candidate" from actual
   **payment content only**: a payment **marker keyword** in the OCR text
   (`_PAYMENT_MARKERS`, which includes No‑Dues terms) **or** a hard extracted field
   (**amount / reference / LAN**). It deliberately does **not** use the model's
   `is_payment_document` flag (it over‑accepts non‑payments like a keyboard photo and
   occasionally rejects real ones) and does **not** count a lone **date** (calendars,
   screenshots and product photos all carry dates). A lead is `non_document` **only
   when there is no payment content at all**; if any marker or hard field is present it
   goes to verification. A genuine payment proof always shows an amount / reference /
   LAN or a word like *paid / amount / ₹ / no dues*, so it is **never** discarded
   (`non_document` = zero false positives), while a keyboard/photo/random object with
   none of these is a true non‑document. Anything payment‑like that then fails matching
   becomes `unverified`. Reproducible from the extraction; logged under
   `metrics.evidence` (`markers`, `fields`, and `model_says_payment` for context only).
3. **Payment‑method keywords** ([config/settings.py](../config/settings.py) `PAYMENT_METHOD_KEYWORDS`) — only
   decides *which* method label to show (PhonePe/GPay/…); does not affect valid/invalid.

Image quality ([image_qc.py](../pipeline/image_qc.py)) is *not* content classification — it only discards
pixel‑broken images.

---

## 6. Verification & the final decision

[pipeline/verify.py](../pipeline/verify.py) compares the **extracted document values** against the
**CSV row** using the lender's rule. It is 100% deterministic.

**Per‑lender rule** ([config/lender_rules.json](../config/lender_rules.json)):
```json
"SMFG_PL": { "mandatory_fields": ["date","amount","receiver","loan_account_number"],
             "needs_lan": true, "date_tolerance_days": 3, "amount_tolerance_rupees": 1.0 }
```
Unknown lenders fall back to `__default__` (date + amount + receiver, ±3 days, ±₹1).

**Field checks:**
- `check_date` — parses both dates (day‑first, fuzzy); pass if `|gap| ≤ date_tolerance_days`.
- `check_amount` — numeric; pass if `|doc − system| ≤ amount_tolerance_rupees` (₹1).
- `check_receiver` — the document's receiver (or **anywhere in the OCR text**) must
  match a name in that lender's allowlist ([config/lender_receivers.json](../config/lender_receivers.json)). **Fallback:**
  if a lender has **no allowlist configured**, the check falls back to the lender's own
  name (from `institute_name`) — a document whose payee clearly *is* the lender still
  passes (strict match: the full lender name must appear; short/generic codes are
  ignored). Explicit allowlists always take precedence.
- `check_lan` (SMFG only) — digits equal, or one contains the other (len ≥ 8).

**Decision:**
- **all** mandatory checks pass → `verified`.
- any mandatory check fails (mismatch or unreadable) → `unverified`, with a reason
  string naming each failed field.
- a lead the dedup flagged (a different loan account under a known `lead_code`) is
  forced to `unverified` even if the fields match — the dedup concern is recorded in
  the outcome (`flag` + reason) and the field‑by‑field verify result is nested under
  `outcome.verification` for the reviewer.
- `reconciliation_dependent` (optional per‑lender flag) still forces `unverified`
  regardless of the fields, but **no lender currently sets it** — `SMFG_RURAL` was
  reclassified as normally verifiable. Set the flag on a lender only if a document can
  never prove its payment on its own (e.g. cash that must be reconciled against a
  deposit feed).

> Two things that legitimately cause `unverified` on a *correct* document: a receiver
> that is **neither** in the lender's allowlist **nor** the lender's own name (the
> fallback), and a date/amount that's genuinely outside tolerance (e.g. a No‑Dues
> certificate issued days after the payment). Both are config fixes, not code. See
> [DEBUGGING.md](DEBUGGING.md).

---

## 7. Duplicate guard

[observability/pg_dedup.py](../observability/pg_dedup.py) — **CSV‑identity based, runs BEFORE OCR** so
exact duplicates never cost a model call. (Reference id is **not** used — it is unfit
for this.)

**Identity** of a payment = `lead_code + loan_account_number + amount + payment‑month`,
all taken from the CSV row. Stored in `processed_payments` (PK on the 4‑tuple, indexed
on `lead_code`). Normalisation: amount → numeric (commas stripped); loan a/c →
uppercase alphanumeric; date → `YYYY‑MM` (ISO, month‑name and DD/MM/YYYY all handled);
lead_code trimmed.

**The check** — one indexed lookup by `lead_code`, then an in‑memory compare (hybrid rule):

| Situation | Verdict | What happens |
|---|---|---|
| lead_code / loan / amount / date missing | `skip` | can't identify → process normally |
| `lead_code` never seen | `new` | record identity → process normally |
| exact 4‑tuple already processed | **`duplicate`** | short‑circuit **before OCR** — final status `duplicate` |
| same lead **+ same loan**, different month/amount | `emi` | legitimate installment → process normally |
| `lead_code` seen, but a **different loan account** | **`manual_review`** *(internal verdict)* | still runs full OCR (reviewer sees the data); **final status `unverified`**, with the dedup concern in the outcome |

- Runs at stage **D** (first). Only exact duplicates short‑circuit; the different‑loan
  case continues through OCR + verify and is surfaced as `unverified` — the dedup verdict
  string (`manual_review`) still appears in the `stage_dedup` event data, but the lead's
  final `verification_status` is `unverified` and the verify result is attached under
  `outcome.verification`. (There is no separate `manual_review` final status; every such
  lead is manually checked in the `unverified` bucket.)
- `seen_payments` (the old reference ledger) is gone; `processed_payments` is
  backfilled from history so dedup works across uploads. `truncate_all()` clears it for
  a fresh run; in production you retain and backfill it from payment history.

---

## 8. Component map

| Area | Files | Responsibility |
|---|---|---|
| Config | [config/settings.py](../config/settings.py), [lender_rules.json](../config/lender_rules.json), [lender_receivers.json](../config/lender_receivers.json) | Thresholds, model + DB + worker settings, lender rules, receiver allowlists. |
| DB | [db/pg.py](../db/pg.py) | Connection pool, schema, `truncate_all()`. |
| Storage/logging | [observability/pg_logger.py](../observability/pg_logger.py), [pg_dedup.py](../observability/pg_dedup.py) | Event log + results + method/status rollups; CSV‑identity duplicate ledger. |
| Vision | [ocr/medha_client.py](../ocr/medha_client.py) | Medha VLM client (streaming) + `PrecomputedOCR` test backend. |
| Pipeline | [pipeline/](../pipeline/) `image_source · image_qc · ocr_classify · extract · verify · models · orchestrator` | The per‑lead processing stages. |
| Queue | [pipeline/jobs.py](../pipeline/jobs.py) | Enqueue, claim, complete/fail, batch progress. |
| Workers | [worker.py](../worker.py) | In‑app pool + standalone runner. |
| Web | [app/server.py](../app/server.py), [templates/index.html](../app/templates/index.html), [static/](../app/static/) | Flask APIs + single‑page console. |
| CLI | [run_batch.py](../run_batch.py), [view_logs.py](../view_logs.py) | Synchronous batch; lead journey dump. |

> `observability/lead_logger.py` and `observability/ledger.py` are the **legacy
> SQLite** implementations, kept for reference; the web app and workers use the
> Postgres variants above.
