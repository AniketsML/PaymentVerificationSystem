# Configuration

Everything tunable lives in config — no code edits needed to onboard a lender,
change a tolerance, or point at a different model/database.

- [Environment variables](#environment-variables)
- [`config/settings.py`](#configsettingspy)
- [`config/lender_rules.json`](#configlender_rulesjson)
- [`config/lender_receivers.json`](#configlender_receiversjson)
- [Common tasks](#common-tasks)

---

## Environment variables

All optional — sensible defaults are baked in. Override for production.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/payment_verification` | Postgres connection (local or managed). |
| `WORKER_COUNT` | `4` | Worker threads per process (in‑app pool and each `worker.py`). |
| `JOB_MAX_ATTEMPTS` | `3` | Retries before a job is parked as `failed`. |
| `JOB_LEASE_SECONDS` | `300` | Crash‑recovery lease; an `in_progress` job idle this long is re‑claimable. |
| `MEDHA_API_URL` | `http://164.52.192.196:8002/v1` | Vision model endpoint (OpenAI‑compatible). |
| `MEDHA_API_KEY` | `REMOVED-SECRET` | Bearer token. |
| `MEDHA_MODEL` | `Medha` | Model name. |
| `MEDHA_STREAM` | `1` | `1` = stream the response; `0` = single response. |
| `MEDHA_TIMEOUT` | `120` | Per‑request timeout (seconds). |
| `IMAGE_FETCH_TIMEOUT` | `30` | Image download timeout (seconds). |

---

## `config/settings.py`

**Image QC thresholds** (`IMAGE_QC`) — pixel‑quality gate only; images that fail are
discarded as `non_document`. **No brightness upper bound** — white‑background
receipts (GPay/PhonePe cards) are bright but valid; blank white is caught by low
contrast instead.

| Key | Default | Discards when |
|---|---|---|
| `min_width` / `min_height` | `300` / `500` | image smaller than this (thumbnail/corrupt) |
| `dark_brightness_max` | `15` | mean gray below this (near‑black) |
| `blur_laplacian_min` | `50.0` | focus measure below this (unusably blurry) |
| `low_contrast_std_min` | `8.0` | contrast std below this (blank/flat, any brightness) |

**`PAYMENT_METHOD_KEYWORDS`** — ordered `(label, [keywords])` list used to name the
payment method from the OCR text (PhonePe, Google Pay, Paytm, BHIM, NEFT/IMPS/RTGS,
Cash, Cheque, e‑NACH, …). Labelling only; does not affect the verdict.

---

## `config/lender_rules.json`

One object per lender code (`institute_name`). Verification reads this.

```json
"SMFG_PL": {
  "mandatory_fields": ["date", "amount", "receiver", "loan_account_number"],
  "needs_lan": true,
  "date_tolerance_days": 3,
  "amount_tolerance_rupees": 1.0
}
```

| Key | Meaning |
|---|---|
| `mandatory_fields` | Which checks must all pass for `verified`. Every lender has `date, amount, receiver`; SMFG lenders add `loan_account_number`. |
| `needs_lan` | Whether the LAN check runs. |
| `date_tolerance_days` | Max allowed gap between document date and system `payment_date`. |
| `amount_tolerance_rupees` | Max allowed difference in amount (default ₹1). |
| `reconciliation_dependent` | *(optional)* If `true`, the lender is **never** auto‑verified from a document (e.g. `SMFG_RURAL` cash collection) — always `unverified`. |

`__default__` is used for any lender code not present (date + amount + receiver,
±3 days, ±₹1, no LAN).

---

## `config/lender_receivers.json`

The **accepted receiver names** per lender — the allowlist the receiver check
matches against.

```json
"BAJAJ_AUTO": ["Bajaj Auto Credit Limited", "Bharat Connect"]
```

Matching is normalized (lowercase, alphanumeric only) and lenient: a lead's receiver
passes if any listed name equals/contains the document's receiver field **or** appears
anywhere in the OCR text. A lender **missing** from this file has an empty allowlist,
so its receiver check can never pass → those leads are `unverified` even with a
correct document.

---

## Common tasks

**Onboard a new lender** (e.g. `VARTHANA`):
1. Add its accepted payee names to `lender_receivers.json`.
2. Optionally add a rule to `lender_rules.json` (else `__default__` applies).
No restart of the pipeline logic needed beyond restarting the process so the JSON is
re‑read (the files are loaded at import in [pipeline/verify.py](../pipeline/verify.py)).

**Loosen a lender's date window** (e.g. No‑Dues certificates issued days late):
set `"date_tolerance_days": 15` on that lender in `lender_rules.json`.

**Point at a managed Postgres:** set `DATABASE_URL`. Schema auto‑creates on first
boot.

**Swap the vision model / run offline test:** set the `MEDHA_*` vars, or use *Test
mode* (precomputed `ex_*` columns) to run with no model calls.
