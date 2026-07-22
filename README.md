# Payment Verification System

A deterministic, production-oriented pipeline that decides whether an uploaded
loan‑repayment document actually proves a payment — with a hard bias toward
**zero false positives**. It only marks a lead `verified` on positive, matched
evidence; everything uncertain is routed to review with a logged reason.

Every payment ("lead") is processed as a **durable job** on PostgreSQL: uploads
enqueue jobs, a worker pool drains them, and a restart mid‑run resumes
automatically. Every stage of every lead is logged end‑to‑end for full
traceability.

---

## What it does (one screen)

```
CSV / Excel upload
      │  one job per lead (job_id = lead_id, idempotent)
      ▼
┌──────────────┐  Postgres  ┌───────────────┐
│  jobs queue  │──────────▶ │  worker pool  │   (claim → process → done/fail)
└──────────────┘            └───────┬───────┘
                                    ▼  process_lead(...)
   D  DEDUP       CSV identity (lead_code+loan+amount+month) — BEFORE any OCR
                    exact re-submission ............ → duplicate  (no model call)
                    different loan a/c, same lead ... → unverified (flagged for review)
   0  LOAD IMAGE   fetch URL/local, decode ......... fail → unprocessed
   1  IMAGE QC     resolution/brightness/blur/contrast (NO enhancement) → unprocessed
   2  MEDHA VLM    OCR + payment method; non_document ONLY on zero payment
                    evidence (model + text + fields all negative) — else → verify
   3  EXTRACT      structured fields (amount/date/receiver/LAN) + labels
   4  VERIFY       deterministic match vs the CSV row + lender rules
                    all mandatory matched → verified
                    a mandatory mismatch  → unverified
```

**Outcomes:** `verified` · `unverified` · `duplicate` · `non_document` · `unprocessed` (image never reached OCR). A lead is
only `verified` on positively matched evidence and only `non_document` when there is
**no** payment evidence at all; everything in between (including dedup‑flagged and
formerly `manual_review` leads) is `unverified` for a human to check.
**Mandatory fields:** every lender → date + amount + receiver; SMFG lenders → also loan account number (LAN).
**Duplicate identity:** `lead_code + loan_account_number + amount + payment-month` (see [docs/ARCHITECTURE.md §7](docs/ARCHITECTURE.md#7-duplicate-guard)).

---

## Quickstart — one command

```bash
# Windows:            run.bat
# Linux / macOS:      ./run.sh
# any platform:       python run.py
```

That's it. The launcher runs a **preflight** (Python version, dependencies, `.env`,
Postgres reachability, schema, free port, model endpoint, auth), reports anything wrong
as a plain sentence with the fix, then starts the **console + worker pool** (and the
**ingester** too, if `SOURCE_MODE` is configured) and opens the browser.

```bash
python run.py --check          # preflight only — tell me what's wrong, start nothing
python run.py --port 8010      # use another port (auto-falls-back if the default is busy)
python run.py --no-browser     # don't open a browser
```

First run with no `.env` copies `.env.example` for you — fill in `DATABASE_URL`,
`MEDHA_API_KEY`, and `PV_AUTH_USER`/`PV_AUTH_PASS`, then run again. `run.bat`/`run.sh`
use [uv](https://docs.astral.sh/uv/) when present (no virtualenv setup needed).

<details><summary>Manual / advanced start</summary>

```bash
pip install -r requirements.txt          # includes psycopg (Postgres driver) + SQLAlchemy
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/payment_verification

python -m app.server                     # console + in-process workers → http://localhost:8000
python worker.py                         # optional: extra standalone workers
python ingester.py                       # optional: automated source-DB ingestion (see docs/INGESTION.md)

# CLI batch (synchronous, no queue) + single-lead log dump
python run_batch.py input.csv --image-col payment_document
python view_logs.py LEAD-42
```
</details>

Upload a CSV/Excel in the **Verify** tab. Set the **Lead‑ID column** to a stable
unique id (e.g. your `id`) so re‑uploads resume cleanly. Leave **Test mode** off
to call the live Medha model.

---

## Documentation

| Doc | What's in it |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, execution model (queue/workers/idempotency), every pipeline stage, verification & decision logic, classification sources, duplicate guard. |
| [docs/DATABASE.md](docs/DATABASE.md) | The four tables, every column, and ready‑to‑run SQL for debugging. |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | `settings.py`, `lender_rules.json`, `lender_receivers.json`, and all env vars. |
| [docs/API.md](docs/API.md) | Every HTTP endpoint (UI + JSON API). |
| [docs/DEBUGGING.md](docs/DEBUGGING.md) | "A lead came out wrong — how do I trace it?" playbook, status/reason reference, common issues. |

---

## Layout

```
payment_verification_system/
├── config/          settings.py · lender_rules.json · lender_receivers.json
├── db/              pg.py            (Postgres pool + schema + truncate_all)
├── ocr/             medha_client.py  (live Medha VLM + PrecomputedOCR for tests)
├── pipeline/        image_source · image_qc · ocr_classify · extract · verify
│                    · models · jobs (queue) · orchestrator (process_lead)
├── observability/   pg_logger.py · pg_dedup.py    (+ legacy sqlite variants)
├── app/             server.py (Flask) · templates/index.html · static/{app.js,style.css}
├── worker.py        durable-queue worker (in-app pool + standalone runner)
├── run_batch.py     CLI: CSV → results (synchronous)
├── view_logs.py     CLI: print a lead's full journey
└── docs/            this documentation
```

---

## Design principles

1. **Precision over recall.** A field counts only on positive evidence. Anything
   unreadable/mismatched stays out of `verified`.
2. **Deterministic decisions.** The verdict ([pipeline/verify.py](pipeline/verify.py)) is pure rules — no
   model call, reproducible from the extracted values.
3. **No pixel tampering.** Image QC only *discards* clearly-broken images; it never
   enhances. Content classification is the model's job.
4. **Config‑driven.** Lender rules, receiver allowlists, mandatory fields, tolerances,
   and thresholds live in config, not code.
5. **Total traceability.** Per‑stage status, processing time, reasons, raw model
   output, and the exact input row are all logged and queryable by lead id.
6. **Resilient by construction.** Durable queue + idempotent enqueue + worker leases
   mean a crash or re‑upload never double‑processes or loses work.
