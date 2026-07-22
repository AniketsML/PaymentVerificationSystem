# Payment Verification System — System Documentation & Deployment Guide

**Complete functional architecture, backend internals, and step-by-step deployment & integration reference for the automated payment-proof verification service. Written for the engineering team taking the system to production.**

Baseline commit `5006011` · Runtime Python 3.11+ · Datastore PostgreSQL · Serving Flask + waitress · Status Production v1

---

## Contents

**Understand:** 1. System overview · 2. Architecture & processes · 3. Technology stack · 4. Repository layout · 5. Processing pipeline · 6. Core subsystems · 7. Database schema

**Operate:** 8. Configuration reference · 9. HTTP / API surface · 10. Deployment guide · 11. Database & Metabase integration · 12. Operations & runbook · 13. Security & compliance · 14. Limitations & roadmap

---

## 1. System overview

*What the system does, who uses it, and the single idea the whole design rests on.*

**The problem it solves.** Borrowers submit proof-of-payment images — UPI screenshots (PhonePe, Google Pay, Paytm), bank NEFT/IMPS receipts, cash receipts, and loan No-Dues / closure certificates. Verifying each one by hand against the loan record is slow and error-prone. This system reads each image with a vision model and then **deterministically** checks it against what the loan record says should be true — correct amount, date, receiver, and loan account — returning an auditable verdict.

**The five verdicts.** Every lead ends in exactly one terminal state:

| Status | Meaning | Who acts |
|---|---|---|
| `verified` | All mandatory fields matched on positive evidence. | No one — done. |
| `unverified` | A mandatory field failed or was unreadable; reason recorded. | Human review queue. |
| `duplicate` | Exact re-submission of an already-processed payment. No model call spent. | No one — auto. |
| `non_document` | The model saw the image and it is not a payment proof. | No one — auto. |
| `unprocessed` | The image never reached the model (URL missing, private/expired link, download failure, quality-discarded). | Data-source owner (fix the link/URL). |

> **THE ONE IDEA TO REMEMBER.** All durable state lives in PostgreSQL, every write into it is idempotent, and the processes are disposable. Kill any process at any moment and restart it — no lead is lost, nothing is double-counted, recovery equals restart. Every guarantee in this document is a consequence of that principle.

---

## 2. Architecture & processes

*Four kinds of process that communicate only through one database.*

The system is not a monolith and not microservices — it is a small set of stateless processes coordinating exclusively through Postgres. They never call each other directly.

```
┌─ WEB ───────────┐   ┌─ WORKER ×N ─────┐   ┌─ POSTGRES ──────┐   ┌─ MEDHA VLM ─────┐
│ Console + API   │   │ Pipeline runner │   │ Single source   │   │ Vision model    │
│ Flask/waitress  │──►│ Claims jobs,    │──►│ of truth: queue │◄──│ Separate GPU    │
│ Serves UI/JSON  │   │ runs 6 stages,  │   │ + results +     │   │ host. OpenAI-   │
│ Optional pool   │   │ writes results  │   │ events + config │   │ compatible.     │
└─────────────────┘   └─────────────────┘   └─────────────────┘   └─────────────────┘
        every process talks ONLY to Postgres — never directly to each other
```

### The process model

| Process | Entry point | Responsibility |
|---|---|---|
| `web` | `python -m app.server` | Console UI + HTTP API on `0.0.0.0:8000` (waitress, `WEB_THREADS`). Runs startup housekeeping and — unless disabled — an in-process worker pool. |
| `worker` | `python worker.py` | Standalone pool of `WORKER_COUNT` threads draining the job queue. Run one or more for horizontal scale. |
| `reverify` | `python reverify.py` | Re-runs *only* the verdict stage on already-extracted leads — no model calls. Maintenance path after a rules/config change. |
| `run_batch` | `python run_batch.py` | CLI driver for offline / precomputed batches. Same pipeline, no web. |

> **DEPLOYMENT TIP — SPLITTING WEB AND WORKERS.** The web process starts an in-process worker pool on boot. To run workers as a separate service (recommended for production so the console can restart without dropping in-flight leads), set `WORKER_COUNT=0` on the `web` container and `WORKER_COUNT=4` (or more) on the dedicated `worker` container. **No code change is required** — it is purely an environment split.

### Why this shape, and not more

At this volume (tens of thousands of leads/day, see §12) one VM running Docker Compose + Postgres has a large margin. The genuine bottleneck is GPU inference, which no orchestration layer fixes. The design deliberately avoids Kafka/RabbitMQ (the Postgres queue is already durable and transactional with the results), Kubernetes (one host), and Airflow (three cron-like loops are not a DAG engine). Scaling is "add workers + model concurrency," never a rewrite.

---

## 3. Technology stack

*Everything is standard, boring, and boring on purpose.*

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | No compiled extensions beyond wheels. |
| Web framework | Flask | Served by **waitress** (production WSGI), not the dev server. |
| Database | PostgreSQL | Driver `psycopg[binary]` + `psycopg_pool` connection pool. |
| Data / imaging | pandas, numpy, Pillow | CSV/Excel parsing, image QC math, image decode. |
| HTTP / dates | requests, python-dateutil | Model calls + image fetch; day-first Indian date parsing. |
| Spreadsheets | openpyxl | Excel upload support. |
| Frontend | Vanilla JS + Chart.js | One `app.js`, one `style.css`. No build step, no framework. |
| Vision model | "Medha" VLM | External GPU host, OpenAI-compatible `/chat/completions`. Not part of this repo. |

The complete Python dependency set is in `requirements.txt`. There is no Node build, no bundler, and no CSS framework — the UI ships as static files served by Flask.

---

## 4. Repository layout

*Where everything lives, so the team can navigate the code on day one.*

```
payment_verification_system/
├─ app/
│  ├─ server.py            # Flask app: all HTTP routes, auth, entry point (waitress)
│  ├─ templates/           # index.html (SPA shell), login.html
│  └─ static/              # app.js (the SPA), style.css
├─ pipeline/
│  ├─ orchestrator.py      # process_lead() — runs the 6 stages, the spine
│  ├─ jobs.py              # durable queue: enqueue, claim (SKIP LOCKED), complete/fail
│  ├─ image_source.py      # stage 0 — fetch + decode image, classify load failures
│  ├─ image_qc.py          # stage 1 — deterministic quality gate (no pixel edits)
│  ├─ ocr_classify.py      # stage 2 — model call + non-document decision
│  ├─ extract.py           # stage 3 — structure + field labelling
│  ├─ verify.py            # stage 4 — deterministic verdict + receiver tiers
│  ├─ approvals.py         # receiver-approval queue (the human teaching loop)
│  └─ models.py            # ExtractedDocument dataclass + status constants
├─ ocr/
│  └─ medha_client.py      # vision client: cache, circuit breaker, streaming, coercion
├─ observability/
│  ├─ pg_logger.py         # event log + result writes + review ledger + queries
│  ├─ pg_dedup.py          # payment de-duplication (identity + atomic claim)
│  └─ metrics.py           # Observability-tab aggregates (scope-aware, TTL-cached)
├─ db/
│  └─ pg.py                # connection pool, schema + migrations, reset helpers
├─ config/
│  ├─ settings.py          # all thresholds + env var loading
│  ├─ runtime.py           # live-editable model endpoint (runtime_config table)
│  ├─ lender_rules.json    # per-lender mandatory fields + tolerances
│  └─ lender_receivers.json# per-lender accepted-receiver allowlists
├─ worker.py               # standalone worker pool entry point
├─ reverify.py             # stage-4-only re-verification (no model calls)
├─ run_batch.py            # CLI batch driver
├─ requirements.txt        # Python dependencies
├─ .env.example            # environment template (copy to .env)
└─ docs/                   # API.md, PRODUCTION_ARCHITECTURE.md, this file
```

---

## 5. Processing pipeline

*The six stages every lead flows through, in `pipeline/orchestrator.py`.*

```
 [D] Dedup  →  [0] Load image  →  [1] Image QC  →  [2] OCR+classify  →  [3] Extract  →  [4] Verify
 identity      fetch+decode       discard broken   the only model      structure &     deterministic
 pre-cost      classify fails     images           call                label fields    verdict
```

Every stage is written to the `lead_events` log with its timing, so any lead's full journey is reconstructable. The `is_test` flag is threaded into every write, so a sandbox lead is never momentarily visible as real.

### Stage D — Deduplication (before any expensive work)

Builds a payment's identity from the CSV row — `lead_code + loan account + amount + payment-month` — and looks it up in the ledger. An exact match short-circuits to `duplicate` with **no image download and no model call**. Five verdicts: `new`, `emi` (same loan, new installment → process), `duplicate`, `manual_review` (same lead-code, different loan → folded into `unverified` for a human), and `skip` (insufficient identity).

### Stage 0 — Load image

Fetches bytes from an HTTP(S) URL, local path, or `data:` URI and decodes with Pillow. Failures are precisely classified — a URL that returns 200 but is not an image is diagnosed as *private / access-denied* or *expired link* rather than a generic error. Any load failure ends the lead as `unprocessed` with the exact reason.

### Stage 1 — Image QC

Pure-numpy quality metrics (brightness, blur via variance-of-Laplacian, contrast) with **no pixel modification**. Discards only genuinely broken images: near-black, unusably blurry, or blank/flat. Deliberately *no* minimum-resolution gate (small but legitimate receipts were being wrongly dropped) and *no* brightness upper bound (white-card UPI receipts are bright but valid). A fail → `unprocessed`.

### Stage 2 — OCR + classification (the only model call)

Runs the vision model, labels the payment method from the document type + keyword map, then makes the non-document decision. **Crucially, it does not trust the model's own yes/no flag** (fallible both ways; logged but ignored). Instead it decides from concrete evidence — a payment keyword in the OCR text (`paid`, `UTR`, `₹`, `no dues`…) or a hard field (amount, reference, loan account). Evidence present → treat as a payment document; entirely absent → genuine `non_document`. Tuned for zero false discards of real receipts.

### Stage 3 — Extract & label

Builds the structured record and cross-references each extracted value against the loan record — e.g. a number equal to the system's loan account gets labelled `loan_account_number, matches_system: true`. This labelling powers the reviewer drawer.

### Stage 4 — Verify (pure rules, no model)

The deterministic verdict — detailed in §6. Checks each mandatory field for the lender (date within tolerance, amount within ±₹1, receiver via the tiered match, and loan account for SMFG lenders), plus guards against incoming-credit receipts.

---

## 6. Core subsystems

*The engineered pieces that make the pipeline robust and self-improving.*

### The durable job queue (pipeline/jobs.py)

This is where crash-safety lives. Workers claim jobs with a single atomic query:

```sql
WITH picked AS (
  SELECT job_id, status AS prev_status, attempts AS prev_attempts FROM jobs
  WHERE status='pending' OR (status='in_progress' AND lease_until < now())
  ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
UPDATE jobs j SET status='in_progress', attempts=j.attempts+1,
  lease_until = now() + make_interval(secs => 300), updated_at=now()
FROM picked WHERE j.job_id = picked.job_id
RETURNING j.*, picked.prev_status, picked.prev_attempts
```

- **FOR UPDATE SKIP LOCKED** — two workers never grab the same row; the second skips and takes the next. Lock-free scaling to N workers.
- **The lease** — a claimed job expires after `JOB_LEASE_SECONDS` (300s). A crashed worker's job is re-claimed by the same query and logs a `lease_reclaim` event.
- **Self-limiting failure** — `fail()` re-queues until `attempts >= max_attempts` (3), then parks the job in `failed` with its error text. Poison jobs never loop forever; they surface in Queue & errors.
- **Idempotent enqueue** — `ON CONFLICT (job_id) DO NOTHING`, so re-uploading or re-reading rows is free and harmless.

### Receiver matching — the verification core (pipeline/verify.py)

"Did the money go to the right party?" is answered by a strict three-tier hierarchy; the first tier that passes wins.

1. **Lender's own name** — derived from the lender code (tokens ≥ 4 chars). The payee *is* the lender.
2. **Approved allowlist** — the lender's list of accepted receiver names in `lender_receivers.json`, grown by the Approvals loop.
3. **Receiver UPI ID on the document** — if a UPI handle (e.g. `smfgindia@icici`) decomposes into the accepted name's own tokens, it passes — verifying a receipt payable to a shop name whose UPI proves it went to the lender.

Matching is **token-anchored, not fuzzy-score**: generic words (bank, finance, services, limited…) are stripped, then a *distinctive* token (`hdb`, `smfg`, `jana`) must match — exactly, or within one typo for tokens ≥ 4 chars. So "HDB Fin Services" matches "HDB Financial Services Limited," but "Ram Financial Services" never does, and a payer `janardhan@oksbi` never matches "Jana." Two further guards: an **incoming-credit direction guard** refuses to auto-verify a receipt that reads like money arriving into the borrower's own account, and reconciliation-dependent lenders never auto-verify.

### The vision-model client (ocr/medha_client.py)

- **Extraction cache** — keyed on `sha256(image) : model : prompt_version`. Identical images are never sent twice; only successful reads are cached. A prompt change auto-invalidates old entries.
- **Circuit breaker** — shared across worker threads. After 5 consecutive model failures it opens and applies a growing, capped cooldown — backpressure instead of a retry-storm against a struggling GPU box.
- **Runtime-repointable** — endpoint URL/key/model are read live from `runtime_config` (5s TTL). Ops can move the model host from the console with no restart.
- **Robust coercion** — pulls the JSON object out of the response even if wrapped in prose; degrades to a safe non-document on unparseable output. Transport errors never crash the pipeline.

### Human loops — where workload keeps shrinking

- **Approvals** (`pipeline/approvals.py`) — receiver-only failures grouped by `(lender, receiver)`. One approval atomically appends the name to `lender_receivers.json`, hot-reloads config in every worker, and re-verifies every affected lead. Keyed by receiver (not lead), so it survives data wipes. Live-data only — disabled in the Test workspace.
- **Review** — a reviewer confirms or overturns any verdict; the append-only `lead_reviews` ledger is the *only* ground truth for accuracy telemetry, and is test-scoped so sandbox reviews never poison real precision.
- **Reverify** (`reverify.py`) — re-runs only stage 4 on already-extracted leads with current rules, **no model calls**. Used after any config change to bring existing data in line for free.

### Observability (observability/metrics.py)

A single `snapshot(scope)` fans out ~15 aggregates over the pipeline's own tables — latency (real model time), throughput, stage funnel, unverified reasons, extraction fill-rates (drift signal), accuracy telemetry, queue depth, cache hit-rate — behind an 8-second TTL cache. A thread-local scope switch rewrites `_real`→`_test` views so every query is workspace-aware with no rewrites.

---

## 7. Database schema

*Eight tables + scope views. Created idempotently on boot — no migration tool to run.*

> **SCHEMA MANAGEMENT.** `db/pg.py :: init_schema()` runs the full `CREATE TABLE IF NOT EXISTS` schema plus additive `ALTER … IF NOT EXISTS` migrations on every boot, guarded by a Postgres advisory lock so concurrent starts create it exactly once. **Deploying a schema change is just a restart.**

| Table | Role | Key columns |
|---|---|---|
| `jobs` | Durable work queue | `job_id` PK (= lead_id), `status`, `attempts`, `lease_until`, `row_json`, `is_test` |
| `lead_results` | Final verdict per lead (dashboards read this) | `lead_id` PK, `verification_status`, `outcome` JSONB, `extracted` JSONB |
| `lead_events` | Append-only per-stage audit log | `lead_id`, `stage`, `status`, `ms`, `metrics`/`data` JSONB |
| `processed_payments` | De-duplication ledger | unique `(lead_code, loan_acct, amount, pay_ym, is_test)` |
| `lead_reviews` | Human-review audit trail (accuracy ground truth) | `system_status`, `decision`, `corrected_status`, `reviewer` |
| `receiver_approvals` | Receiver-name decisions | unique `(lender, receiver_norm)`, `decision`, `affected`, `flipped` |
| `runtime_config` | Live-editable model endpoint | `key` PK, `value` |
| `ocr_cache` | Extraction cache | `cache_key` PK, `extraction` JSONB, `hits` |

### Scope views — the test-isolation guarantee

Every operational table has an `is_test` flag and a pair of views: `lead_results_real` / `lead_results_test` (and the same for `lead_events`, `jobs`, `lead_reviews`). Real-scope reads go through the `_real` view, so **sandbox data physically cannot leak into production metrics** — the guarantee is structural, enforced by the database, not by remembering a `WHERE` clause.

> **JSONB IS QUERYABLE — METABASE CAN READ INSIDE IT.** `outcome` and `extracted` are real JSONB, so dashboards and SQL can reach fields directly (`extracted->>'amount'`). Prefer the published `analytics` view (§11) over querying base tables so schema changes never break dashboards.

---

## 8. Configuration reference

*Every environment variable, with defaults. Secrets come from a gitignored `.env`; real environment variables always win.*

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | postgresql://…localhost:5432/… | **Required in prod.** This system's Postgres. |
| `WORKER_COUNT` | 4 | Worker threads. Set `0` on the web service to disable its in-process pool. |
| `JOB_MAX_ATTEMPTS` | 3 | Retries before a job is parked as `failed`. |
| `JOB_LEASE_SECONDS` | 300 | Crashed-worker recovery window. |
| `MEDHA_API_URL` | http://…:8002/v1 | Vision endpoint (also live-editable in the console). |
| `MEDHA_API_KEY` | — | **Secret.** Bearer token for the model. |
| `MEDHA_MODEL` | Medha | Model name sent in the request. |
| `MEDHA_STREAM` | 1 | Stream the model response. |
| `MEDHA_TIMEOUT` | 120 | Per-call timeout (seconds). |
| `IMAGE_FETCH_TIMEOUT` | 30 | Image download timeout (seconds). |
| `OCR_CACHE` | 1 | Enable the extraction cache. |
| `OCR_CACHE_TTL_DAYS` | 30 | Cache retention (`0` = forever). |
| `LEAD_EVENTS_TTL_DAYS` | 0 | Event-log retention. **Set ~90 in continuous prod** — this is the only unbounded table. |
| `OCR_BREAKER_THRESHOLD` | 5 | Consecutive model failures before the breaker opens. |
| `OCR_BREAKER_COOLDOWN` / `_MAX_WAIT` | 8 / 60 | Backoff base and cap (seconds). |
| `WEB_THREADS` | 8 | waitress worker threads for HTTP. |
| `MAX_UPLOAD_MB` | 64 | Upload size cap (OOM guard). |
| `PV_AUTH_USER` / `PV_AUTH_PASS` | — | **Secret.** Console login. Auth is on only when both are set. |
| `PV_SECRET_KEY` | — | **Secret.** Signs the session cookie — set a stable value so logins survive restarts. |
| `TEST_TTL_DAYS` | 1 | Auto-purge of sandbox data on boot. |

### Lender configuration (JSON, hot-reloadable)

- `config/lender_rules.json` — per lender: mandatory fields, `needs_lan`, `date_tolerance_days`, `amount_tolerance_rupees`. A `__default__` entry covers unconfigured lenders.
- `config/lender_receivers.json` — per lender: the accepted-receiver allowlist. The Approvals loop writes here automatically.

Both are read once at startup and re-read live when the Approvals loop or a manual edit changes them — no restart needed.

---

## 9. HTTP / API surface

*Serving on port `8000`. All routes except `/health` and the login pages require a session when auth is enabled. Full request/response shapes are in `docs/API.md`.*

| Method | Route | Purpose |
|---|---|---|
| GET | `/`, `/lead/<id>` | Console SPA (deep-links to a lead). |
| POST | `/api/enqueue` | Upload CSV/Excel → enqueue jobs → `{batch_id, enqueued, skipped}`. |
| GET | `/api/batch/<batch_id>` | Live batch progress + per-lead rows. |
| GET | `/api/stats` | Status counts, method breakdown, unprocessed issue split. |
| GET | `/api/leads` | Filtered/paginated result rows (`?status=&q=&scope=`). |
| GET | `/api/lead/<id>` | One lead: final verdict + full stage journey + image. |
| POST | `/api/lead/<id>/review` | Record a reviewer confirm/overturn decision. |
| GET | `/api/approvals` | Receiver-approval queue + recent decisions. |
| POST | `/api/approvals/decide` | Approve/reject a `(lender, receiver)` pair. |
| GET | `/api/observability` | All operational + quality metrics for one scope. |
| GET | `/api/config/model` | Current model endpoint (key masked). |
| POST | `/api/config/model` /test | Update / probe the model endpoint (no restart). |
| POST | `/api/verify` | **Integration hook** — verify one record synchronously (JSON in/out). |
| GET | `/download/<batch_id>` | Results CSV for a batch. |
| GET | `/health` | Liveness/readiness: DB + queue. `200`/`503`. Open for probes. |

> **TWO INTEGRATION ENTRY POINTS FOR THE SOURCE SYSTEM.** **Batch:** `POST /api/enqueue` with a CSV/Excel (durable, async, resumable). **Synchronous single record:** `POST /api/verify` with a JSON body — ideal for wiring into an existing service that already has the image and loan fields. Automated pull-from-source-DB ingestion is the next phase (§11).

---

## 10. Deployment guide

*A single Linux VM running Docker Compose, sited network-close to the Medha GPU host. Recommended path below; a systemd alternative follows.*

### Step 1 — Provision

- Linux VM, **India region** (data residency — see §13), 4 vCPU / 8 GB / 100 GB SSD is comfortable at current volume.
- Private network path to the Medha GPU endpoint.
- A DNS name + TLS (Caddy issues certificates automatically below).
- Docker + Docker Compose installed.

### Step 2 — Environment file

`.env` — copy from `.env.example`, fill secrets:

```bash
DATABASE_URL=postgresql://pv_app:CHANGE_ME@postgres:5432/payment_verification
MEDHA_API_URL=http://<gpu-host>:8002/v1
MEDHA_API_KEY=<model-key>
MEDHA_MODEL=Medha
PV_AUTH_USER=ops
PV_AUTH_PASS=<16+ random chars>
PV_SECRET_KEY=<python -c "import secrets;print(secrets.token_hex(32))">
LEAD_EVENTS_TTL_DAYS=90
WORKER_COUNT=4
```

### Step 3 — Containerize

The repo has no Dockerfile yet; add these two reference files. Both services run the same image with different commands and `WORKER_COUNT`.

**Dockerfile (reference):**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      libjpeg62-turbo zlib1g && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "-m", "app.server"]
```

**docker-compose.yml (reference):**

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: payment_verification
      POSTGRES_USER: pv_app
      POSTGRES_PASSWORD: ${PGPASSWORD}
    volumes: ["pgdata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U pv_app"]
      interval: 10s

  web:
    build: .
    env_file: .env
    environment: { WORKER_COUNT: "0" }      # web runs NO in-process workers
    depends_on: { postgres: { condition: service_healthy } }
    expose: ["8000"]

  worker:
    build: .
    command: python worker.py
    env_file: .env
    environment: { WORKER_COUNT: "4" }      # the dedicated worker fleet
    depends_on: { postgres: { condition: service_healthy } }
    deploy: { replicas: 1 }                 # scale this number for throughput
    stop_grace_period: 120s                 # let an in-flight lead finish

  caddy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes: ["./Caddyfile:/etc/caddy/Caddyfile", "caddy_data:/data"]
    depends_on: [web]

volumes: { pgdata: {}, caddy_data: {} }
```

**Caddyfile (reference) — TLS + reverse proxy:**

```
verify.yourdomain.com {
    encode gzip
    reverse_proxy web:8000
}
```

### Step 4 — Launch & verify

The schema auto-creates on first boot (advisory-locked, idempotent). Then smoke-test:

```bash
docker compose up -d --build

# health (expect {"ok":true,"db":true,...})
curl -s https://verify.yourdomain.com/health

# log in, then confirm stats respond
curl -s -c cookies.txt https://verify.yourdomain.com/login > /dev/null
curl -s -c cookies.txt -b cookies.txt -X POST \
     -d "username=$PV_AUTH_USER" -d "password=$PV_AUTH_PASS" \
     https://verify.yourdomain.com/login > /dev/null
curl -s -b cookies.txt https://verify.yourdomain.com/api/stats
```

> **ACCEPTANCE FOR PHASE 1.** Upload one real batch through the console; confirm leads flow to verdicts, then **redeploy the web container mid-run** — the queue survives and no lead is lost. That single test proves the crash-safety design end to end.

### Alternative — systemd (no Docker)

Install Postgres and Python 3.11 on the host, create a venv from `requirements.txt`, and run two units: one `ExecStart=python -m app.server` (with `WORKER_COUNT=0`) and one `ExecStart=python worker.py` (with `WORKER_COUNT=4`), both with `EnvironmentFile=/opt/pv/.env` and `Restart=always`. Put nginx or Caddy in front for TLS.

---

## 11. Database & Metabase integration

*Two directions: publishing results to Metabase (do this now), and automated pull-ingestion from the source DB (designed, next phase).*

### Publishing — Metabase reads from this system (recommended)

Add this system's Postgres to Metabase as a second data source, restricted to a dedicated `analytics` schema exposed through a **versioned view** and a **read-only role**. Dashboards bind to the view, so pipeline internals can change without breaking them.

```sql
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE OR REPLACE VIEW analytics.verification_results_v1 AS
SELECT lead_id, lender, verification_status,
       outcome->>'reason'                                   AS reason,
       CASE WHEN verification_status='unprocessed'
            THEN outcome->>'describes' END                  AS unprocessed_issue,
       payment_method,
       extracted->>'amount'                                 AS doc_amount,
       extracted->>'date'                                   AS doc_date,
       extracted->>'receiver_name'                          AS doc_receiver,
       updated_at                                           AS verified_at
FROM lead_results_real;          -- test rows structurally excluded

CREATE ROLE metabase_reader LOGIN PASSWORD 'CHANGE_ME';
GRANT USAGE ON SCHEMA analytics TO metabase_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO metabase_reader;
-- grant NOTHING else: no base tables, no other schemas.
```

In Metabase: *Admin → Databases → Add*, point at this Postgres with the `metabase_reader` credentials, and build dashboards on `analytics.verification_results_v1`. A breaking change later ships as `_v2` alongside `_v1`; dashboards migrate, then `_v1` retires.

> **IF METABASE CANNOT ADD A SECOND DATA SOURCE.** Fallback is writing results back into *their* database (a `pv_results` table you own there) via an outbox-style upsert loop. It needs write credentials on their side and reconciliation — only do it if adding a data source is genuinely blocked.

### Ingestion — pulling leads from the source DB (designed, phase 2)

**Current intake:** CSV/Excel upload (`POST /api/enqueue`) and the synchronous `POST /api/verify` hook. That is enough to go live and integrate an existing service today.

**Automated intake** — an `ingester` that polls the production database behind Metabase on a watermark cursor, maps rows to the pipeline contract, prefetches images while their signed URLs are fresh, and enqueues into the same `jobs` table — is **fully designed but not yet built**. The complete design (watermark mechanics, image blob store, quarantine for schema drift, backfill, failure matrix, six-phase rollout) is in `docs/PRODUCTION_ARCHITECTURE.md`. Build it after Phase 1 is stable.

> **ANSWER THESE BEFORE BUILDING INGESTION.** Source DB engine & whether a read-only replica user is available · daily lead volume · whether S3 credentials for the documents bucket can be granted (kills the expired-link failure class) · which table/columns define a "new lead." These are §13 of the architecture doc and each changes a real decision.

---

## 12. Operations & runbook

*Day-two tasks, monitoring signals, and capacity.*

### Monitoring signals

| Signal | Where | Healthy |
|---|---|---|
| Liveness | `GET /health` | `200`, `db:true` |
| Queue depth / oldest pending | Observability → Queue & errors | Draining; oldest < 1h |
| Model error rate | Observability (breaker) | ≈ 0; breaker closed |
| Failed jobs | Queue & errors | 0 (investigate any) |
| Unprocessed by issue | Dashboard → Unprocessed | Watch "no image URL" — it's an upstream data defect |
| Verified precision | Observability → Accuracy | From the review ledger; alert < 97% |

### Common tasks

```bash
# Re-verify all leads after a rules/config change (no model calls)
python reverify.py                 # apply
python reverify.py --dry-run       # report only

# Repoint the model endpoint — do it in the console (Model API panel),
# it takes effect on the next lead with no restart.

# Nightly backup (run from cron / a sidecar)
pg_dump "$DATABASE_URL" | gzip > /backups/pv_$(date +%F).sql.gz

# Scale throughput
docker compose up -d --scale worker=3
```

### Backups & retention

- Nightly `pg_dump` + WAL archiving (point-in-time recovery) to off-VM storage, 30-day retention, and a **quarterly restore drill** — an untested backup is a wish, not a backup.
- Set `LEAD_EVENTS_TTL_DAYS=90` in continuous production — `lead_events` is the one table that grows unbounded; verdicts in `lead_results` are never purged.
- Sandbox data auto-purges after `TEST_TTL_DAYS`.

### Capacity

The only real ceiling is GPU inference: `concurrency × 86,400 / seconds_per_image × 1/(1−cache_hit)`. At ~6–8s/image and 4-way concurrency the ceiling is ~50k/day; run at ≤30% for headroom → comfortably 15k+/day. Scale by adding workers + model concurrency behind a second GPU — no architectural change.

---

## 13. Security & compliance

*This system carries lending PII — treat it accordingly.*

- **Transport** — TLS everywhere via Caddy; the model endpoint should live on a private network segment; source DB / Metabase connections over TLS.
- **Least privilege** — `metabase_reader` sees only the `analytics` schema; a future `pv_reader` on the source DB is SELECT-only; no shared superuser anywhere.
- **Secrets** — env file at `0600`, never in git (already enforced; repo history was cleaned). Rotate the model key and console password at deploy.
- **PII at rest** — encrypted disk volume for Postgres (and the image blob store once ingestion is built); event-log retention as above.
- **Audit** — `lead_reviews` and `receiver_approvals` already record who/when/what.
- **DPDP** — Indian financial documents + phone numbers means DPDP obligations: retention limits, in-region hosting, breach process. Choose an India-region VM.

> **HARDEN BEFORE GO-LIVE.** The console login is a single shared credential. Rotate `PV_AUTH_PASS` to 16+ random characters at deploy, and add per-user accounts as a fast follow so approvals and reviews are attributable. Put a rate-limit / lockout on the login route (Caddy can do the cheap version).

---

## 14. Limitations & roadmap

*Known gaps, stated honestly, so the receiving team plans around them.*

| Item | State | Plan |
|---|---|---|
| Automated source-DB ingestion | Designed, not built | Phase 2 — follow `docs/PRODUCTION_ARCHITECTURE.md` after Phase 1 is stable. |
| Console authentication | Single shared login | Per-user accounts + login rate-limit as a fast follow. |
| Web/worker split | Env-configurable (`WORKER_COUNT=0`) | Documented above; adopt in Compose from day one. |
| Dockerfile / Compose | Reference templates in §10 | Team adds the two files and commits them. |
| Expired image links | Classified as `unprocessed` | Ingestion-time image prefetch (+ S3 credentials) eliminates the class. |
| Backups | Manual `pg_dump` | Automate nightly + WAL PITR + quarterly restore drill. |

> **COMPANION DOCUMENTS.** `docs/PRODUCTION_ARCHITECTURE.md` — the full ingestion/deployment design and 6-phase rollout. · `docs/API.md` — endpoint request/response detail. · `README.md` — pipeline summary and local run.

---

*Payment Verification System — Engineering handoff documentation. Baseline commit `5006011`. Prepared for the deployment & integration team. All statements verified against the codebase at time of writing; nothing in this document changes the code.*
