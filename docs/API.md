# HTTP API

Served by [app/server.py](../app/server.py) on `http://localhost:8000`. The single‑page console
uses these same endpoints.

## UI

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | The console (SPA). |
| GET | `/lead/<lead_id>` | Same SPA, deep‑links straight into a lead's drawer. |

## Processing

### `POST /api/enqueue`
Upload a CSV/Excel; enqueues one job per lead. **Does not process synchronously** —
workers drain the queue.

Form fields (multipart): `file` (required), `image_col` (default `payment_document`),
`id_col` (optional — the stable Lead‑ID column), `image_root` (optional, for local
filenames), `precomputed` (`on` = test mode).

```json
→ { "batch_id": "batch-1782988492", "total": 1010, "enqueued": 1002, "skipped": 8 }
```
`skipped` = leads already in the queue (resume/idempotency).

### `POST /api/verify_image`
Single‑image quick test — runs **synchronously** for instant feedback.
Form fields: `image` (required), `institute_name`, `payment_amount`, `payment_date`,
`loan_account_number`, `transaction_id`, `lead_id` (optional).
```json
→ { "lead_id": "IMG-1782988500", "verification_status": "unverified" }
```

### `GET /api/batch/<batch_id>`
Live batch progress (the UI polls this).
```json
→ { "batch_id":"…", "total":1010, "pending":40, "in_progress":4, "done":964, "failed":2,
    "verdicts": {"verified":186,"unverified":360,"non_document":418},
    "open_work": 44,
    "leads": [ { "lead_id":"LEAD-3","lender":"…","job_status":"done",
                 "verification_status":"verified","payment_method":"PhonePe",
                 "outcome_text":"Verified fields: date, amount, receiver" }, … ] }
```

## Dashboard / data

### `GET /api/stats`
```json
→ { "counts": {"verified":186,"unverified":360,"non_document":464,"total":1010},
    "methods": [ {"method":"PhonePe","n":316}, {"method":"Non-document","n":464}, … ],
    "model": {"model":"Medha","url":"…","stream":true,"workers":4} }
```

### `GET /api/leads?status=&q=&limit=`
Filtered result rows. `status` ∈ `all|verified|unverified|duplicate|non_document|unprocessed`; `q`
matches lead id / lender / method; `limit` ≤ 2000. Each row includes an `outcome_text`.
The dashboard table also offers **client‑side per‑column filters** (click the **Lead**,
**Lender**, **Method**, or **Outcome** header to pick values) applied on top of these
server‑side `status`/`q` filters — no extra endpoint involved.

### `GET /api/lead/<lead_id>`
Everything about one lead (this powers the drawer). `404` if unknown.
```json
→ { "lead_id":"LEAD-1007",
    "final": { "verification_status":"unverified","lender":"VARTHANA","payment_method":"Google Pay","updated_at":"…" },
    "journey": [ {"stage":"lead_received","status":"PASS","ms":0, …}, … ],
    "extracted": { "amount":"18587","date":"29 Apr 2026","receiver_name":"…","field_labels":{…} },
    "outcome": { "reason":"date: date mismatch (gap 1d …)", "failed_fields":["date"], … },
    "image_source":"https://…","image_url":"https://…",
    "csv_row": { "institute_name":"VARTHANA","payment_amount":"18,587","payment_date":"…", … } }
```

## Files

| Method | Path | Purpose |
|---|---|---|
| GET | `/download/<batch_id>` | Results CSV for a batch (sorted work‑first). |
| GET | `/uploaded/<name>` | Serve a locally‑uploaded single‑image file. |

## Integrations / raw

| Method | Path | Purpose |
|---|---|---|
| GET | `/logs/<lead_id>` | Raw journey JSON (`{lead_id, final, journey}`). |
| GET | `/api/results` | All `lead_results` rows (for Metabase / export). |
| POST | `/api/verify` | Verify one record synchronously. Body: `{ "lead_id", "image_path", "record": {…row…}, "precomputed": false }` → the full result object. |

---

### Quick curl examples
```bash
# enqueue a batch
curl -F "file=@input.csv" -F "id_col=id" http://localhost:8000/api/enqueue

# watch it
curl -s http://localhost:8000/api/batch/batch-1782988492 | jq '{done,pending,in_progress,verdicts}'

# inspect a lead end-to-end
curl -s http://localhost:8000/api/lead/LEAD-1007 | jq '{final,outcome,csv_row}'
```
