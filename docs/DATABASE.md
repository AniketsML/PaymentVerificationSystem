# Database

Everything lives in PostgreSQL (`DATABASE_URL`, default
`postgresql://postgres:postgres@localhost:5432/payment_verification`). Schema is
created idempotently on startup by [db/pg.py](../db/pg.py) `init_schema()`.

Connect with psql:
```bash
psql "postgresql://postgres:postgres@localhost:5432/payment_verification"
# Windows: & "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -d payment_verification
```

---

## Tables

### `jobs` — the durable work queue
One row per lead. `job_id = lead_id` is the idempotency key.

| Column | Type | Notes |
|---|---|---|
| `job_id` | TEXT PK | = `lead_id`; `ON CONFLICT DO NOTHING` on enqueue |
| `batch_id` | TEXT | the upload that created it (`batch-<epoch>`) |
| `lead_id` | TEXT | lead identifier |
| `lender` | TEXT | `institute_name` from the row |
| `image_url` | TEXT | resolved image URL / path |
| `row_json` | JSONB | **the exact input CSV/Excel row** (shown in the drawer) |
| `precomputed` | BOOL | test mode (use `ex_*` cols, no model call) |
| `status` | TEXT | `pending` · `in_progress` · `done` · `failed` |
| `attempts` / `max_attempts` | INT | retry accounting |
| `last_error` | TEXT | last failure message |
| `lease_until` | TIMESTAMPTZ | crash‑recovery lease; expired `in_progress` is re‑claimable |
| `verification_status` | TEXT | copied from the result when `done` |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

### `lead_events` — append‑only per‑stage log
The full journey of every lead. This is your primary debugging trail.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | ordering |
| `lead_id` | TEXT | indexed |
| `ts` | TIMESTAMPTZ | event time |
| `stage` | TEXT | `lead_received` · `stage_dedup` · `stage0_load_image` · `stage1_image_qc` · `stage2_ocr_classify` · `stage3_extract` · `stage4_verify` · `lead_closed` |
| `status` | TEXT | `PASS` / `FAIL` |
| `reason` | TEXT | human‑readable, esp. on FAIL |
| `ms` | DOUBLE | stage processing time |
| `metrics` | JSONB | numbers behind the decision (QC metrics, model latency, …) |
| `data` | JSONB | structured payload (raw model response, extracted fields, image_source, field_labels, outcome…) |

### `lead_results` — the final row per lead
What the dashboard and Metabase read.

| Column | Type | Notes |
|---|---|---|
| `lead_id` | TEXT PK | |
| `lender` | TEXT | |
| `verification_status` | TEXT | `verified` · `unverified` · `manual_review` · `duplicate` · `non_document` |
| `payment_method` | TEXT | PhonePe / GPay / … / empty = non‑document or duplicate |
| `outcome` | JSONB | verified fields, or the failure reason + `failed_fields` (for `manual_review`, the verify result is nested under `verification`) |
| `extracted` | JSONB | the `ExtractedDocument` (doc fields + `field_labels`); empty for `duplicate` (no OCR ran) |
| `updated_at` | TIMESTAMPTZ | |

### `processed_payments` — payment duplicate ledger (CSV identity)
| Column | Type | Notes |
|---|---|---|
| `lead_code` | TEXT | part of PK; indexed for the dedup lookup |
| `loan_acct` | TEXT | normalized (uppercase alphanumeric) |
| `amount` | NUMERIC(14,2) | normalized amount |
| `pay_ym` | TEXT | payment month, `'YYYY-MM'` |
| `lead_id` | TEXT | the first lead with this identity |
| `ts` | TIMESTAMPTZ | |

Primary key = `(lead_code, loan_acct, amount, pay_ym)` — the exact‑duplicate identity.

---

## Debugging queries

**One lead's full journey (with timings):**
```sql
SELECT stage, status, round(ms) AS ms, reason
FROM lead_events WHERE lead_id = 'LEAD-1007' ORDER BY id;
```

**A lead's final result + what the model read + the input row:**
```sql
SELECT verification_status, payment_method, outcome, extracted
FROM lead_results WHERE lead_id = 'LEAD-1007';

SELECT row_json FROM jobs WHERE job_id = 'LEAD-1007';           -- exact CSV input
SELECT data->'raw_model_response' AS raw, data->'extracted' AS fields
FROM lead_events WHERE lead_id='LEAD-1007' AND stage='stage2_ocr_classify';
```

**Status & method rollups (what the dashboard shows):**
```sql
SELECT verification_status, count(*) FROM lead_results GROUP BY 1 ORDER BY 2 DESC;
SELECT coalesce(nullif(payment_method,''),'Non-document') AS m, count(*)
FROM lead_results GROUP BY 1 ORDER BY 2 DESC;
```

**Queue health:**
```sql
SELECT status, count(*) FROM jobs GROUP BY 1;                   -- pending/in_progress/done/failed
SELECT job_id, attempts, last_error FROM jobs WHERE status='failed';
SELECT job_id, lease_until FROM jobs                            -- stuck? crashed worker
WHERE status='in_progress' AND lease_until < now();
```

**Everything that failed a given stage (e.g. why so many non‑documents):**
```sql
SELECT reason, count(*) FROM lead_events
WHERE stage='stage1_image_qc' AND status='FAIL' GROUP BY 1 ORDER BY 2 DESC;
SELECT reason, count(*) FROM lead_events
WHERE stage='stage2_ocr_classify' AND status='FAIL' GROUP BY 1 ORDER BY 2 DESC;
```

**Dedup outcomes (new / duplicate / emi / manual_review / skip):**
```sql
SELECT data->>'verdict' AS verdict, count(*) FROM lead_events
WHERE stage='stage_dedup' GROUP BY 1 ORDER BY 2 DESC;
-- the exact duplicates and their reasons:
SELECT lead_id, reason FROM lead_events
WHERE stage='stage_dedup' AND status='FAIL' ORDER BY id;
```

**Which lender rule failed (verification reasons):**
```sql
SELECT lender, outcome->>'reason' AS why, count(*)
FROM lead_results WHERE verification_status='unverified' GROUP BY 1,2 ORDER BY 3 DESC;
```

---

## Reset to empty (fresh run)

```python
python -c "from db import pg; pg.truncate_all()"     # wipes all four tables
```
or in SQL:
```sql
TRUNCATE lead_events, lead_results, processed_payments, jobs;
```

> `truncate_all()` clears the **payment** dedup ledger too. That's intended for a
> clean re‑run. In production you would instead retain `processed_payments` (and
> backfill it from payment history) so duplicates are caught across time, not just per
> batch. Seed it from existing leads with `PaymentDedup().backfill()`.
