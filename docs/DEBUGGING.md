# Debugging playbook

Practical answers to "why did this lead come out like that?" and "why isn't it
processing?". Pair this with [DATABASE.md](DATABASE.md) (the SQL) and
[ARCHITECTURE.md](ARCHITECTURE.md) (how the decision is made).

- [Trace one lead](#trace-one-lead)
- [Status reference](#status-reference)
- ["This lead came out wrong"](#this-lead-came-out-wrong)
- [Queue / processing issues](#queue--processing-issues)
- [Model / image issues](#model--image-issues)
- [Reprocess or reset](#reprocess-or-reset)

---

## Trace one lead

Three ways, same data:

1. **UI** — click the lead. The drawer shows: the receipt image, the **CSV input
   (expected)**, the **extracted fields (from document)**, the **outcome**, and the
   full **lifecycle timeline** (each stage with PASS/FAIL, timing, and expandable raw
   model response / metrics). Grouped by run if the lead was processed more than once.
2. **CLI** — `python view_logs.py LEAD-1007`.
3. **SQL** — `SELECT stage,status,round(ms),reason FROM lead_events WHERE lead_id='LEAD-1007' ORDER BY id;`

The single most useful comparison: **CSV input vs Extracted fields** in the drawer —
that tells you instantly whether the model misread something or the data genuinely
disagrees.

---

## Status reference

| Status | Meaning | Decided at |
|---|---|---|
| `verified` | All mandatory fields matched on positive evidence. | `stage4_verify` |
| `unverified` | A mandatory field mismatched/unreadable, or the lender is reconciliation‑dependent. | `stage4_verify` |
| `duplicate` | Exact re‑submission — same `lead_code + loan + amount + month` already processed. Short‑circuits before OCR. | `stage_dedup` |
| `manual_review` | A different loan account under an already‑seen `lead_code` (suspicious). Full OCR still runs; verify result is under `outcome.verification`. | `stage_dedup` → `stage4_verify` |
| `non_document` | Image couldn't be loaded, was discarded at QC, or the model said it isn't a payment document. | `stage0` / `stage1` / `stage2` |
| `failed` *(job only)* | The pipeline threw an exception `max_attempts` times. See `jobs.last_error`. | worker |

---

## "This lead came out wrong"

### Expected `verified`, got `non_document`
Look at which stage FAILed (`... WHERE lead_id=X ORDER BY id`):

- **`stage0_load_image` FAIL** — the image URL couldn't be fetched/decoded (dead link,
  403, non‑image). Reason names the error. Fix the source URL/access.
- **`stage1_image_qc` FAIL** — discarded on pixel quality. Reason is precise
  ("resolution too low", "too blurry", "blank / no content", "near‑black"). If it's a
  *real* receipt being rejected, loosen the relevant threshold in
  `settings.IMAGE_QC` (see [CONFIGURATION.md](CONFIGURATION.md)).
- **`stage2_ocr_classify` FAIL** — the model said it isn't a payment document, **or**
  the deterministic marker gate found no payment markers/values. Check
  `data.raw_model_response` and `data.full_text` on that event. If it's a valid proof
  the model mislabelled, that's a prompt/marker matter (see
  [ARCHITECTURE.md §5](ARCHITECTURE.md#5-classification--where-valid-document-is-decided)).

### Expected `verified`, got `unverified`
Read `outcome.reason` on the result — it names each failed field:

- **`receiver: receiver not among accepted names for <LENDER>`** — the lender isn't in
  `lender_receivers.json`, or the printed payee isn't in its allowlist. **Most common
  cause of a "wrong" unverified.** Add the lender/name (see [CONFIGURATION.md](CONFIGURATION.md)).
- **`date: date mismatch (gap Nd > Md …)`** — document date is outside the lender's
  `date_tolerance_days`. Legit for No‑Dues/late certificates → widen the tolerance.
- **`amount: amount mismatch (doc X vs system Y)`** — the receipt amount differs from
  the CSV `payment_amount` (compare in the drawer). If the receipt shows a different
  figure (e.g. total vs instalment), it's a data question, not a bug.
- **`loan_account_number …`** *(SMFG)* — LAN unreadable or different.
- **`requires cash-deposit reconciliation …`** — `reconciliation_dependent` lender
  (e.g. `SMFG_RURAL`); never verifiable from the document alone.

> Duplicates and dedup‑flagged leads are **not** `unverified` — they get their own
> `duplicate` / `manual_review` status at `stage_dedup` (before OCR). See the status
> reference above and [ARCHITECTURE.md §7](ARCHITECTURE.md#7-duplicate-guard).

### Got `verified`, but you think it's wrong (false positive)
Open the drawer and compare **CSV input vs Extracted**. Likely causes:
- The receiver allowlist is broad and a name appeared **anywhere in the OCR text**
  (the receiver check matches document text, not just the receiver field).
- Amount/date landed within tolerance despite being a different payment.
Tighten the lender's tolerances or receiver names in config. (There is no
model‑confidence gate — the system errs toward `unverified`, but tolerant config can
still let an edge case through.)

---

## Queue / processing issues

**Nothing is processing** (jobs stay `pending`):
- Are workers running? The web app starts an in‑process pool on boot; confirm no
  startup error in the server log. Or run `python worker.py`.
- Can the process reach Postgres? A DB outage shows as loop errors on stderr; workers
  back off and retry.
```sql
SELECT status, count(*) FROM jobs GROUP BY 1;
```

**A job is stuck `in_progress`** — a worker crashed mid‑job. Its lease
(`JOB_LEASE_SECONDS`, default 300s) will expire and another worker re‑claims it
automatically. To force it now, clear the lease:
```sql
UPDATE jobs SET status='pending', lease_until=NULL WHERE status='in_progress' AND lease_until < now();
```

**Jobs are `failed`** — the pipeline raised repeatedly:
```sql
SELECT job_id, attempts, last_error FROM jobs WHERE status='failed';
```
Common `last_error`: model timeout, image host errors. Fix the cause, then requeue
(see below).

**Re‑upload didn't process anything** (`enqueued: 0`) — those `lead_id`s are already
in `jobs` (resume working as designed). To force a re‑run, delete first (below).

---

## Model / image issues

- **Model errors** surface in `stage2_ocr_classify` → `metrics.model_error` and
  `data.model_meta`. The client catches transport errors and returns a non‑document
  with the error text rather than crashing.
- **Latency**: `metrics.model_ms` per lead. The Medha call dominates runtime; scale
  with more workers/processes. First call after idle is slower (cold start).
- **Streaming**: controlled by `MEDHA_STREAM`. The client assembles streamed chunks;
  set `MEDHA_STREAM=0` to debug against a single response.
- **Offline repro**: run the lead in **Test mode** (precomputed `ex_*` columns) to
  exercise the whole pipeline with no model call.

---

## Reprocess or reset

**Re‑run a single lead** (force past job idempotency):
```sql
DELETE FROM jobs WHERE job_id='LEAD-1007';
DELETE FROM lead_results WHERE lead_id='LEAD-1007';
-- (optional) DELETE FROM lead_events WHERE lead_id='LEAD-1007';
```
then re‑upload the file (or a one‑row file) — the lead will enqueue and process fresh.

**Requeue all failed jobs:**
```sql
UPDATE jobs SET status='pending', attempts=0, last_error=NULL WHERE status='failed';
```

**Full reset to empty** (fresh run):
```bash
python -c "from db import pg; pg.truncate_all()"
```
Wipes `lead_events`, `lead_results`, `processed_payments`, `jobs`. Note this also
clears the payment duplicate ledger — intended for a clean re‑run, not for production.
To seed the ledger from existing leads instead, run `PaymentDedup().backfill()`.
