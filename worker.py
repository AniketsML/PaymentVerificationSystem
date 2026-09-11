"""
Worker: drains the durable job queue through the verification pipeline across all workspaces.

Run standalone (scale by launching more):     python worker.py
Or let the web app start an in-process pool:  start_pool()  (called on app boot)

Each worker loops: claim pending jobs across registered workspaces -> execute -> mark done/failed.
A crash just leaves the job's lease to expire; another worker re-claims it. So
processing is resilient and resumable with no re-upload.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import settings
from db import pg
from observability.pg_logger import PgLeadLogger
from observability.pg_dedup import PaymentDedup
from ocr.medha_client import MedhaVisionOCR, PrecomputedOCR
from pipeline import jobs
from pipeline.orchestrator import process_lead
import workspaces

_pool_started = False
_stop = threading.Event()


def process_job(job: dict, logger=None, dedup=None) -> str:
    """Legacy helper for processing payment jobs directly."""
    logger = logger or PgLeadLogger()
    dedup = dedup or PaymentDedup()
    row = job["row_json"] or {}
    precomputed = bool(job["precomputed"])
    is_test = bool(job.get("is_test"))
    ocr = PrecomputedOCR() if precomputed else MedhaVisionOCR()
    res = process_lead(
        job["lead_id"],
        job.get("lender", ""),
        job.get("image_url", ""),
        row,
        ocr,
        logger,
        skip_image_qc=precomputed,
        dedup=dedup,
        is_test=is_test,
    )
    return res["verification_status"]


def run_worker_loop(name="worker", idle_sleep=0.75):
    all_ws = workspaces.get_all_workspaces()
    while not _stop.is_set():
        found_any = False
        try:
            for ws in all_ws:
                job = ws.claim_worker_job()
                if not job:
                    continue
                found_any = True
                job_id = job.get("job_id") or job.get("notice_id") or job.get("lead_id")
                try:
                    status = ws.process_worker_job(job)
                    ws.complete_worker_job(job_id, status)
                except Exception as e:  # noqa: BLE001 - one bad job must not kill the worker
                    ws.fail_worker_job(job_id, f"{type(e).__name__}: {e}")
                    sys.stderr.write(f"[{name}] {ws.id} job {job_id} failed: {e}\n")

            if not found_any:
                _stop.wait(idle_sleep)
        except Exception as e:  # noqa: BLE001 - transient DB error: back off, keep going
            sys.stderr.write(f"[{name}] loop error: {e}\n{traceback.format_exc()}")
            _stop.wait(2.0)


def start_pool(n: int | None = None):
    """Start N daemon worker threads inside the current process (idempotent)."""
    global _pool_started
    if _pool_started:
        return
    workspaces.init_all_schemas()
    n = n or settings.WORKER_COUNT
    for i in range(n):
        threading.Thread(target=run_worker_loop, args=(f"worker-{i}",),
                         daemon=True).start()
    _pool_started = True
    print(f"[workers] started in-process pool of {n} for workspaces: {', '.join(workspaces.get_workspace_ids())}")


def main():
    workspaces.init_all_schemas()
    n = settings.WORKER_COUNT
    print(f"[workers] standalone pool of {n} draining {settings.DATABASE_URL} for {', '.join(workspaces.get_workspace_ids())}")
    threads = [threading.Thread(target=run_worker_loop, args=(f"worker-{i}",), daemon=True)
               for i in range(n)]
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _stop.set()
        print("\n[workers] stopping…")


if __name__ == "__main__":
    main()
