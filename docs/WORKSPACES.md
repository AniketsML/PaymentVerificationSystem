# Multi-Workspace Architecture

The Intelligent OCR Platform supports domain-specific **workspaces**. Each workspace is dedicated to a distinct document type and business use-case, with 100% data, pipeline, queue, and functionality isolation, while sharing a unified design system, theme engine, database pool, and authentication layer.

---

## 1. Registered Workspaces

### 1.1 Payment Verification (`payment`)
- **Purpose**: Verify loan repayment proofs, bank transaction receipts, UPI confirmation screenshots, and No-Dues / NOC certificates against lender rules with **zero false positives**.
- **Default Route**: `/ws/payment` (or `/`)
- **Key Tables**: `jobs`, `lead_events`, `lead_results`, `processed_payments`, `lead_reviews`, `receiver_approvals`, `ocr_cache`.
- **Views**:
  - **01 Dashboard**: Real-time status distribution, payment method breakdowns, lead inspection table.
  - **02 Verify**: Batch CSV/Excel queue processing and single-receipt trial check.
  - **03 Observability**: Latency percentiles, throughput, stage-by-stage timings, fill rates.
  - **04 Approvals**: Human loop for payee/receiver name allowlisting and re-verification.

### 1.2 Legal Notice Extraction (`legal`)
- **Purpose**: High-precision extraction of legal notices, demand letters, summons, and statutory documents preserving **verbatim text with exact layout formatting**, numbered clause breakdowns, and structured legal entities.
- **Default Route**: `/ws/legal`
- **Key Tables**: `legal_jobs`, `legal_notice_events`, `legal_notice_results`, `legal_reviews`, `legal_ocr_cache`.
- **Views**:
  - **01 Dashboard**: Notice counts (Extracted, Needs Review, Low Quality, Non-notice), notice types distribution, top advocates & law firms, filterable notice results table.
  - **02 Extract**:
    - **Batch Extraction**: Upload CSV/Excel list of notice URLs/paths for asynchronous durable worker draining.
    - **Single Notice Extractor**: Real-time side-by-side inspection showing notice image alongside verbatim formatted text (with copy button), key entities grid, structured clauses, and summary/quality metadata.
  - **03 Observability**: VLM model latency percentiles (P50/P95), notice type coverage, advocate breakdown, queue metrics.
  - **04 Notice Standards**: Reference guide for Section 138 N.I. Act, SARFAESI 13(2), Arbitration § 21, IBC Demand Notices, Eviction, and Loan Recall notices.

---

## 2. Workspace Navigation & Selection

### 2.1 Login Time Selection
On the `/login` screen, users can select their target workspace (`Payment Verification` or `Legal Notice Extraction`) before authenticating. The selection is preserved across sessions.

### 2.2 Top-Rail Switcher
Both workspace consoles feature a workspace dropdown in the navigation sidebar rail. Clicking the workspace pill allows 1-click switching between `Payment Verification` and `Legal Notice Extraction`.

### 2.3 Direct Routing
- `/` routes to the active session workspace (default `/ws/payment`).
- `/ws/payment` opens the Payment Verification console.
- `/ws/legal` opens the Legal Notice Extraction console.
- `/ws/legal/notice/<notice_id>` deep-links to a specific legal notice in the inspection drawer.
- `/lead/<lead_id>` deep-links to a specific payment lead drawer.

---

## 3. Developer Extensibility: Adding a New Workspace

Creating a new workspace is a Python developer task (clean backend module + registration):

1. **Subclass `Workspace`** in `workspaces/base.py`:
   ```python
   from workspaces.base import Workspace

   class InvoiceWorkspace(Workspace):
       @property
       def id(self) -> str:
           return "invoice"
       
       @property
       def name(self) -> str:
           return "Invoice & Tax Extraction"

       @property
       def short_name(self) -> str:
           return "Invoice"

       @property
       def description(self) -> str:
           return "Extract line items, tax breakdown, and GST numbers"

       @property
       def icon(self) -> str:
           return '<svg width="16" height="16">...</svg>'

       @property
       def default_route(self) -> str:
           return "/ws/invoice"

       def init_schema(self) -> None:
           # Run DDL for invoice_jobs, invoice_results, etc.
           pass

       def register_routes(self, app) -> None:
           # Register Flask blueprint
           app.register_blueprint(invoice_bp)

       def claim_worker_job(self):
           # Claim pending invoice job
           pass

       def process_worker_job(self, job):
           # Execute invoice extraction pipeline
           pass
   ```

2. **Register in `workspaces/__init__.py`**:
   ```python
   from workspaces.registry import register_workspace
   from workspaces.invoice.workspace import InvoiceWorkspace

   register_workspace(InvoiceWorkspace())
   ```

All schema initialization, route registration, and worker queue draining will automatically include the new workspace without touching other workspaces.
