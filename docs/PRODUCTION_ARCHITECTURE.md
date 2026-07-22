# Production Architecture — Automated Verification Service

**Status: DESIGN — nothing in this document is implemented yet.**
Scope: deploy the console, ingest leads automatically from the production database
(the one Metabase sits on), verify them hands-free, and publish results to a table
Metabase dashboards read. Written 2026-07-12 against commit `5006011`.

---

## 0. Reliability philosophy — what "unbreakable" actually means

No system is bug-free, and any design that promises zero bugs is lying to you.
What a production system CAN guarantee — and what this design guarantees by
construction — is stronger and more useful:

1. **No lead is ever lost.** Every fetched row lands in a durable Postgres queue
   before anything else happens to it. Crash anywhere; the lead is still there.
2. **No lead is ever double-processed to double effect.** Every write path is
   idempotent (natural keys + `ON CONFLICT`), so at-least-once delivery collapses
   to exactly-once *effect*. Re-running anything is always safe.
3. **Every failure is visible and classified.** Nothing fails silently: a lead is
   either verified, unverified, unprocessed-with-reason, quarantined-with-reason,
   or sitting in a queue whose depth is monitored. There is no fifth place.
4. **Degrade, never corrupt.** When a dependency dies (model endpoint, source DB,
   image host), the system stops consuming that dependency and lets queues grow —
   it never invents data, skips stages, or marks unverified things verified.
5. **Everything self-heals on restart.** Leases expire, watermarks resume,
   migrations are idempotent, workers drain gracefully. Recovery = restart.

These five properties, not "no bugs," are what makes the workload reduction real:
when the machine is trustworthy about its failures, humans only look at the two
queues that need them (approvals + unverified review).

---

## 1. System topology

```
┌─ THEIR SIDE ────────────────┐   ┌─ THIS SYSTEM (one Linux VM, Docker Compose) ─────────────┐
│                             │   │                                                          │
│  Production app DB          │   │  ┌─────────┐   watermark    ┌─────────────────┐          │
│  (what Metabase reads)      │◄──┼──┤INGESTER │ poll Δ rows ──►│ map + validate  │          │
│  read-only replica user     │   │  └─────────┘                └───┬─────────┬───┘          │
│                             │   │       │                        ok       bad              │
│  S3 bucket                  │   │       ▼                         ▼         ▼              │
│  (payment documents)        │◄──┼── image prefetch ──► blob   jobs queue  quarantine       │
│                             │   │   (while URL fresh)  store  (Postgres,  (dead letter,    │
└─────────────────────────────┘   │                              SKIP LOCKED) daily digest)  │
                                  │                                 │                        │
┌─ MODEL ─────────────────────┐   │  ┌────────────┐   claim/lease   │                        │
│  Medha VLM endpoint         │◄──┼──┤ WORKER ×N  │◄────────────────┘                        │
│  (GPU box, moves hosts —    │   │  └────────────┘  dedup→load→QC→OCR→extract→verify        │
│   runtime-configurable)     │   │       │            circuit breaker on model errors       │
└─────────────────────────────┘   │       ▼                                                  │
                                  │  lead_results / lead_events / analytics views            │
┌─ CONSUMERS ─────────────────┐   │       ▲                                                  │
│  Metabase dashboards        │◄──┼───────┤ read-only role, analytics.* views ONLY           │
│  (adds our PG as a source)  │   │       │                                                  │
│                             │   │  ┌─────────┐  ┌───────────┐  ┌──────────────────────┐    │
│  Ops team                   │◄──┼──┤ CONSOLE │  │ SCHEDULER │  │ Caddy (TLS, rate-lim)│    │
│  (approvals + review)       │   │  │  (web)  │  │ sweeps    │  └──────────────────────┘    │
└─────────────────────────────┘   │  └─────────┘  └───────────┘                              │
                                  └──────────────────────────────────────────────────────────┘
```

**Deployment units** (Docker Compose on one Linux VM, sited network-close to the
Medha GPU box; systemd is the fallback if Docker is not allowed):

| Service     | Role                                                       | Instances |
|-------------|------------------------------------------------------------|-----------|
| `caddy`     | TLS termination, HTTP→HTTPS, rate limiting, access logs    | 1 |
| `web`       | the existing Flask console (waitress) — **workers OFF**    | 1 |
| `worker`    | pipeline workers draining the jobs queue                   | 2–4 (scale by env) |
| `ingester`  | polls the source DB, maps rows, prefetches images, enqueues| 1 (advisory-locked) |
| `scheduler` | reverify sweep, retention purge, image-refetch, digest     | 1 |
| `postgres`  | this system's database (queue + results + config)          | 1 + backups |

The single most important change from today: **workers move out of the web
process** into their own service. The console can restart/deploy without
dropping in-flight leads, and workers scale independently. The `jobs` table,
lease/attempt columns, and `SKIP LOCKED` claiming already exist — this is a
process split, not a rewrite.

**Why this shape and not more:** one VM + Compose + Postgres does 10–20k
leads/day with a huge margin (see §10). Kafka, Kubernetes, Airflow, and
microservices each add an operational surface that dwarfs this system's actual
complexity. The bottleneck is GPU inference, and no orchestration layer fixes
that. Deliberately **not** building: Kafka/Rabbit (Postgres queue is already
durable + transactional with results), K8s (one VM), CDC/Debezium (polling with
a watermark is enough at this volume), Airflow (three cron-like loops don't need
a DAG engine).

---

## 2. Ingestion — fetching from the source DB

### 2.1 Access mode

**Mode A (target): direct read-only SQL** to the database Metabase itself reads,
ideally a read replica. Ask their DBA for: a `pv_reader` role with `SELECT` on
exactly the lead/payment tables needed, connecting over TLS from this VM's IP.
This is one `SOURCE_DATABASE_URL` env var; the ingester speaks SQLAlchemy so
Postgres/MySQL both work.

**Mode B (fallback, if only Metabase access is granted): the Metabase API.**
A saved question ("card") parameterized on the watermark:
`POST /api/card/:id/query/json` with `{parameters: [{watermark_ts, watermark_id}]}`.
Caveats that make this strictly worse: session-token auth that expires (needs
re-login flow), row caps per result (paginate via the watermark predicate, not
offsets), and Metabase's query cache must be disabled for that card. Build the
ingester behind a `SourceClient` interface so A/B is a config choice, not a fork.

### 2.2 Incremental fetch — the watermark cursor

Poll every `INGEST_INTERVAL` (default 120 s) for rows newer than the last
watermark, with an overlap window to survive clock skew and late commits:

```sql
SELECT ... FROM source_leads
WHERE (created_at, id) > (:wm_ts - INTERVAL '10 minutes', :wm_id)
ORDER BY created_at, id
LIMIT :batch;          -- batch = 500, loop until short page
```

- Cursor = `(created_at, id)` tuple, persisted in `ingest_watermarks` **after**
  the batch is durably enqueued — crash between fetch and enqueue re-fetches,
  and `ON CONFLICT (job_id) DO NOTHING` makes the re-fetch free. At-least-once
  fetch + idempotent enqueue = exactly-once effect.
- The 10-minute overlap re-reads recent rows every cycle; duplicates cost one
  index lookup each. Never trust `created_at` alone — the `id` tiebreak makes
  the cursor total-ordered.
- **Backfill mode**: same code path with `--from 2025-01-01` — it is just a
  watermark reset with a rate cap (`BACKFILL_ROWS_PER_MIN`) so history doesn't
  starve the live tail. Run it once at cutover for the historical book.
- Only one ingester may run: Postgres advisory lock (`pg_advisory_lock`), the
  same pattern `db/pg.py` already uses for schema init. A second instance
  starts, fails to take the lock, sleeps. Safe to run two by accident.

### 2.3 Mapping contract — source columns → pipeline row

The pipeline consumes the same `row dict` the CSV path produces today
(`institute_name`, `payment_amount`, `payment_date`, `loan_account_number`,
image URL, lead id). The source schema WILL drift — treat the mapping as a
versioned contract, not code:

- `config/source_mapping.json`: `{source_column → canonical_field}`, plus
  required-field list and the lead-id column. Reviewed like lender config.
- Every fetched row passes **validation before enqueue**: required fields
  present, amount parses, date parses, lead id non-empty. Failures do NOT enter
  the pipeline — they land in `ingest_quarantine` with row snapshot + reason,
  surfaced in the console and the daily digest. Quarantine is the schema-drift
  alarm: 0 is normal, a spike means their schema changed, and nothing corrupt
  ever reaches verification.
- Unknown extra columns are carried through into `row_json` untouched — the
  image-column fallback scanner in `pipeline/jobs.py` already exploits this.

### 2.4 What ingestion writes

Exactly what the CSV upload writes today: one `jobs` row per lead
(`job_id = lead_id`, `ON CONFLICT DO NOTHING`), `is_test = false`, plus one
`ingest_runs` bookkeeping row per cycle. Zero new concepts downstream — the
whole existing pipeline, dedup, test-workspace isolation, and dashboard work
unchanged.

---

## 3. Image acquisition — killing the #1 failure class

Today's data: 269 of 293 unprocessed leads had no usable image URL, and the
private/expired-S3-link class is structural (signed URLs die before processing).
Ingestion-time prefetch turns this from a processing failure into a solved
supply problem:

1. **Prefetch at ingest, not at process.** The moment a row is mapped, the
   ingester downloads the image **while the signed URL is fresh** and stores the
   bytes in the blob store (`IMAGE_STORE_PATH`, a disk volume; S3/MinIO-ready
   behind the same interface). The job row gets `image_ref = blob:<sha256>`.
   Workers then load from the blob store — link expiry can no longer cause an
   unprocessed lead.
2. **Retry ladder for transient failures** (timeout, 5xx, connection reset):
   3 attempts inline (0 s/10 s/60 s), then the job still enqueues with the
   original URL and a `prefetch_failed` marker; the scheduler's refetch sweep
   retries for 24 h before the pipeline runs it (and classifies honestly if it
   never arrives). Permanent classes (no URL in any column) skip retries and
   flow straight to `unprocessed` — the existing issue classifier already names
   them.
3. **The strategic fix — ask for S3 credentials.** If their bucket grants this
   VM an IAM user with `GetObject` on the documents prefix, ingestion stores
   `bucket/key` instead of signed URLs and the expiry class disappears
   *entirely*. This is the single highest-leverage ask in this document.
4. **Blob store lifecycle**: content-addressed (sha256, dedupes resubmissions,
   pairs with the OCR cache key), encrypted volume, retention
   `IMAGE_RETENTION_DAYS` (default 90) enforced by the scheduler — these are
   customer financial documents; see §8.

---

## 4. Processing — hardening the existing pipeline

The pipeline itself (dedup → load → QC → OCR → extract → verify) is already
deterministic and journaled per stage. Production deltas:

- **Worker service**: same worker-pool code, own process, `WORKER_COUNT` sized
  to the model's concurrency (§10). Graceful drain on SIGTERM: stop claiming,
  finish the in-flight lead, exit; Compose `stop_grace_period: 120s`. A killed
  worker's lease simply expires and another worker re-claims — mid-OCR retry is
  safe because stage writes are idempotent and cached.
- **Poison pills**: `max_attempts` (exists) caps retry; a job that keeps
  crashing lands in `failed` with its error and shows in Queue & errors. Never
  infinite-retried, never silently dropped.
- **Model endpoint circuit breaker** (new, small): 5 consecutive transport
  errors → workers stop claiming OCR-bound jobs for 60 s (probe with the
  existing `/models` healthcheck), queue depth grows, alert fires. Prevents
  burning every job's attempts while the GPU box reboots or moves hosts —
  and the portal-editable endpoint (`runtime_config`) already lets ops repoint
  it with no restart. Cap in-flight OCR calls at `MODEL_MAX_CONCURRENCY` so a
  scaled-up worker fleet can't stampede the GPU.
- **Priorities**: live tail before backfill (`ORDER BY priority, created_at` on
  claim; one small column). Reverify sweeps don't touch the queue at all (no
  model calls).

---

## 5. Publishing — the table Metabase reads

**Recommended: Metabase reads FROM this system**, not write-back into their DB.
Add this Postgres as a second Metabase data source, restricted to a dedicated
schema:

```sql
CREATE SCHEMA analytics;

CREATE VIEW analytics.verification_results_v1 AS
SELECT lead_id, lender, verification_status,          -- verified|unverified|unprocessed|non_document|duplicate
       outcome->>'reason'        AS reason,
       CASE WHEN verification_status='unprocessed'
            THEN outcome->>'describes' END            AS unprocessed_issue,
       payment_method,
       extracted->>'amount'      AS doc_amount,
       extracted->>'date'        AS doc_date,
       extracted->>'receiver_name' AS doc_receiver,
       updated_at                AS verified_at
FROM lead_results_real;                               -- test rows structurally excluded

CREATE ROLE metabase_reader LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA analytics TO metabase_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO metabase_reader;
-- and NOTHING else: no base tables, no other schemas.
```

- The view is a **versioned contract** (`_v1`). Pipeline internals can change
  freely; dashboards never break. A breaking change ships as `_v2` alongside
  `_v1`, dashboards migrate, then `_v1` retires.
- It selects from `lead_results_real`, so sandbox/test rows can never leak into
  business dashboards — the same structural guarantee the console has.
- Write-back into THEIR database (a `pv_results` table we own there) is the
  fallback if Metabase can't add data sources — it needs write credentials on
  their side, an outbox-style upsert loop, and reconciliation, so only do it if
  forced. Row-level webhooks to an LOS/CRM are explicitly out of scope for v1.

---

## 6. New schema (all additive, idempotent migrations like the existing ones)

```sql
CREATE TABLE ingest_watermarks (            -- one row per source
  source      TEXT PRIMARY KEY,
  wm_ts       TIMESTAMPTZ NOT NULL,
  wm_id       TEXT NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ingest_runs (                  -- one row per poll cycle (observability)
  id BIGSERIAL PRIMARY KEY,
  source TEXT, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
  rows_seen INT, enqueued INT, duplicates INT, quarantined INT,
  images_prefetched INT, prefetch_failed INT, error TEXT
);

CREATE TABLE ingest_quarantine (            -- rows that failed the mapping contract
  id BIGSERIAL PRIMARY KEY,
  source TEXT, source_row JSONB, reason TEXT,
  first_seen TIMESTAMPTZ DEFAULT now(),
  resolved BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE image_blobs (                  -- blob-store index (bytes live on the volume)
  sha256 TEXT PRIMARY KEY,
  bytes  BIGINT, content_type TEXT,
  source_url TEXT,                          -- provenance only; never re-fetched from here
  stored_at TIMESTAMPTZ DEFAULT now(),
  last_used TIMESTAMPTZ
);

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS image_ref TEXT;      -- 'blob:<sha256>' | original URL
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority  SMALLINT NOT NULL DEFAULT 5;
```

New env (all with safe defaults, following `config/settings.py` conventions):
`SOURCE_DATABASE_URL`, `SOURCE_MODE=db|metabase`, `INGEST_INTERVAL=120`,
`INGEST_BATCH=500`, `INGEST_OVERLAP_MIN=10`, `SOURCE_MAPPING_PATH`,
`IMAGE_STORE_PATH`, `IMAGE_RETENTION_DAYS=90`, `MODEL_MAX_CONCURRENCY=4`,
`BACKFILL_ROWS_PER_MIN=600`, `ALERT_WEBHOOK_URL`, `DIGEST_EMAIL_TO`.

---

## 7. Failure-mode matrix

| Failure | System behavior | Recovery | Alert |
|---|---|---|---|
| Source DB unreachable | Ingest cycle logs error to `ingest_runs`, watermark unchanged, retries next interval | Automatic on source recovery; overlap window covers the gap | Watermark age > 15 min |
| Source schema drift | Rows fail validation → quarantine; pipeline sees nothing malformed | Update `source_mapping.json`, replay quarantine | Quarantine > 0 |
| Signed URL expired before prefetch | Refetch sweep retries 24 h; then honest `unprocessed: private/expired link` | Fix = S3 credentials (§3.3) | Unprocessed spike day-over-day |
| Image host slow/5xx | Retry ladder; job proceeds later via blob or classifies honestly | Automatic | Prefetch-failure rate > 10% |
| Medha endpoint down | Circuit breaker opens; queue grows; **nothing is misclassified** | Probe closes breaker; ops can repoint endpoint live in the console | Model errors > 5/min; queue depth |
| Medha slow | `MODEL_MAX_CONCURRENCY` caps pressure; latency panel shows it | Scale GPU or accept longer drain | p95 stage-2 latency |
| Worker crash mid-lead | Lease expires; another worker re-claims; stage writes idempotent, OCR cached | Automatic | Failed-job count |
| Poison job (crashes every time) | `max_attempts` → `failed` with error, visible in Queue & errors | Human inspects; `retry` action | Failed > 0 for 30 min |
| Web container dies | Workers unaffected (separate service); Caddy 502s briefly | Compose restart policy | Uptime probe |
| This Postgres down | Everything pauses (no partial state is possible — it IS the state) | Restart / restore; all services reconnect | DB probe |
| Disk full (blobs) | Prefetch fails → retry ladder; pipeline unaffected for cached/queued work | Retention purge; volume alarm at 80% | Disk > 80% |
| Duplicate ingestion / watermark replay | `ON CONFLICT DO NOTHING`; dedup ledger unchanged | None needed — by design | — |
| Deploy during processing | Workers drain gracefully; jobs queue survives; migrations idempotent | None needed | — |
| Both ingesters started | Advisory lock: second one idles | None needed | — |

The invariant behind every row: **state lives only in Postgres, and every
transition into it is idempotent.** Processes are disposable.

---

## 8. Security & compliance (this is lending PII — treat it that way)

- **Transport**: Caddy TLS everywhere; source DB and Metabase connections over
  TLS; the Medha endpoint should at minimum live on a private network segment.
- **Least privilege**: `pv_reader` (their DB, SELECT-only) · `metabase_reader`
  (our DB, `analytics.*` only) · app role owns only its own schema. No shared
  superuser anywhere.
- **Secrets**: env-file on the VM with 0600 perms (already the pattern); never
  in git (already enforced); rotate the Medha key and console password at
  deploy. Console password ≥ 16 random chars (current one is 8 — rotate it).
- **PII at rest**: encrypted disk volume for Postgres + blob store; image
  retention 90 days default; `lead_events` retention already exists; quarantine
  rows contain raw source rows → same retention policy. The repo history
  incident is already cleaned; keep `samples/` synthetic forever.
- **Audit**: reviews + approvals ledgers already record who/when/what. Add the
  console user to ingest-replay and retry actions.
- **Access**: console behind auth (exists); add fail2ban-style lockout on the
  login route (Caddy rate limit is the cheap version); per-user accounts are a
  fast follow so approvals aren't all attributed to one shared login.
- **DPDP awareness**: customer financial documents + phone numbers means Indian
  DPDP obligations apply — retention limits, breach notification, and the data
  should stay in-region. Deployment target should be an India-region VM.

---

## 9. Observability, alerting, and the daily rhythm

The console's Observability tab already computes latency, throughput, stage
funnel, accuracy telemetry, queue state, and cache hit-rate. Production adds
**push**, because dashboards only work when someone is looking:

- **Alerts** (scheduler evaluates each minute, posts to `ALERT_WEBHOOK_URL` —
  Slack/Teams/email; dedup + cooldown so a bad night is 3 pings, not 300):
  - watermark age > 15 min (ingestion stalled)
  - queue depth > 30 min of drain capacity, or oldest pending > 1 h
  - model error rate > 5/min for 5 min (breaker open)
  - failed jobs > 0 for 30 min · quarantine > 0 · disk > 80% · DB/web probe down
  - accuracy guard: verified-precision (from the review ledger) drops below 97%
- **Daily digest** (email, one screen): yesterday's intake, verified %,
  unverified count, unprocessed by issue class ("269 rows had no image URL —
  forward to the data owner"), quarantine, pending approvals, top lenders. The
  digest IS the workload-reduction interface: two numbers to act on, everything
  else FYI.
- **Runbook** (one page in `docs/RUNBOOK.md`, written during Phase 4): model
  moved hosts → repoint in console; source creds rotated → update env, restart
  ingester; queue stuck → check breaker, check leases; restore drill steps.
- **Backups**: nightly `pg_dump` + WAL archiving (PITR) to off-VM storage,
  30-day retention, **quarterly restore drill** — an untested backup is a wish,
  not a backup. Blob store rsync'd nightly (or lives on S3 and needs nothing).

---

## 10. Capacity — the honest bottleneck math

Everything is bounded by Medha GPU inference; nothing else in this system is
within an order of magnitude of it.

```
daily ceiling ≈ MODEL_MAX_CONCURRENCY × 86,400 / t_ocr_seconds × (1 / (1 − cache_hit))
e.g. 4 concurrent × 86,400 / 6 s  ≈ 57,600 leads/day ceiling → run at ≤ 30% = ~15–17k/day
```

- Current observed: ~6–8 s/image on the existing endpoint; cache hit-rate on
  re-runs was 100% (665/665) — resubmission-heavy books cost almost nothing.
- VM sizing: 4 vCPU / 8 GB / 100 GB SSD carries web + workers + ingester +
  Postgres at this volume comfortably; the GPU box is a separate machine and the
  real budget line. Postgres at 20k leads/day ≈ 130k event rows/day → the
  existing retention purge keeps it flat.
- Scale story if volume 10×: raise worker count + `MODEL_MAX_CONCURRENCY` with
  a second GPU behind a load balancer; Postgres partitions `lead_events` by
  month. No architectural change — that's the point of the queue-centric shape.

---

## 11. Rollout plan — six phases, each with a gate

| Phase | What ships | Gate to advance |
|---|---|---|
| **0. Foundations** | India-region Linux VM; Docker; TLS domain; secrets rotated (console password!); repo private; Postgres + nightly backups | Restore drill passes on a scratch VM |
| **1. Lift the console** | Compose: caddy + web + worker split + postgres; current CSV flow in prod | 1 full batch through prod UI; deploy-during-processing loses nothing |
| **2. Shadow ingestion** | Ingester runs in **dry-run**: fetch, map, validate, prefetch — writes `ingest_runs` + quarantine, enqueues NOTHING | 7 days: quarantine ≈ 0, watermark never stalls, prefetch success > 99% on fresh links |
| **3. Live ingestion** | Enqueue on, capped (e.g. 2k/day), CSV path stays as manual override; nightly reverify sweep on | 7 days: results match a manually-uploaded control sample 100%; alerts quiet |
| **4. Publish + backfill** | `analytics.verification_results_v1` + Metabase source + dashboards; historical backfill at capped rate; runbook written | Dashboard numbers reconcile with console; backfill completes with unprocessed classified, not erroring |
| **5. Hardening week** | Chaos drills (below); alert tuning; per-user console accounts; S3-credential switch if granted | All drills recover hands-free; digest running 7 days |

**Chaos drills (Phase 5, in prod, on purpose):** kill a worker mid-OCR · stop
the Medha endpoint 10 min under load · revoke source-DB access one cycle ·
fill the blob volume · restart Postgres mid-batch · deploy during a 1k-lead run
· run two ingesters. Each must recover with zero data loss and a correct alert
trail — that list is the acceptance test for "unbreakable" as defined in §0.

---

## 12. The workload-reduction flywheel (already half-built — lean into it)

1. **Approvals teach permanently** — 25 decisions have already become config;
   every approval removes that receiver's failures for all future leads. Weekly
   metric in the digest: "approvals this week → leads auto-verified since."
2. **Nightly reverify sweep** (scheduler): re-run stage-4 (no model calls) on
   receiver-only unverified leads after config/UPI-tier changes — decisions keep
   paying off retroactively without anyone clicking.
3. **UPI tier + soft matching** already collapse the biggest receiver-mismatch
   classes; watch verified-rate per lender in the funnel to prove it.
4. **Unprocessed digest closes the loop upstream** — "no image URL" is a data-
   entry/export defect on their side; the digest gives the data owner a daily
   number to drive to zero. Prefetch (§3) kills the expiry class on our side.
5. **Review ritual**: 30 leads/week spot-review keeps the accuracy telemetry
   honest — it's the only ground truth the precision alert has.

Target steady state: humans touch **approvals (minutes/day)** and **flagged
unverified leads** — everything else is machine + digest.

---

## 13. Open questions — answer before build (each changes real decisions)

1. **Source DB engine + access**: Postgres or MySQL? Direct read-only user
   possible, or Metabase-API-only (Mode B)? Replica available?
2. **Volume**: leads/day today and 12-month projection? (Sizes workers, GPU.)
3. **S3 credentials** for the documents bucket — yes/no? (Decides whether the
   expired-link class dies structurally or only shrinks.)
4. **Deployment target**: which cloud/VM, and is the Medha GPU box reachable
   from it privately? India region confirmed?
5. **Metabase**: can it add our Postgres as a data source (Mode 5a), or must
   results be written back into their DB (5b)?
6. **Source of truth for "new lead"**: which table/columns, and does anything
   UPDATE rows after creation (would need an `updated_at` watermark variant)?
7. **Who owns the mapping** when their schema changes — named person for the
   quarantine digest?
8. **Retention/compliance numbers**: how long must documents and results be
   kept, per their DPDP stance?

---

*Design complete. Implementation deliberately deferred — nothing in the
codebase was changed for this document.*
