"""
Worker: drains the durable job queue through the verification pipeline.

Run standalone (scale by launching more):     python worker.py
Or let the web app start an in-process pool:  start_pool()  (called on app boot)

Each worker loops: claim one job -> run the full pipeline -> mark done/failed.
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

_pool_started = False
_stop = threading.Event()


def process_job(job: dict, logger, dedup) -> str:
    row = job["row_json"] or {}
    precomputed = bool(job["precomputed"])
    ocr = PrecomputedOCR() if precomputed else MedhaVisionOCR()
    res = process_lead(job["lead_id"], job.get("lender", ""), job.get("image_url", ""),
                       row, ocr, logger, skip_image_qc=precomputed, dedup=dedup)
    return res["verification_status"]


def run_worker_loop(name="worker", idle_sleep=0.75):
    logger, dedup = PgLeadLogger(), PaymentDedup()
    while not _stop.is_set():
        try:
            job = jobs.claim_one()
            if not job:
                _stop.wait(idle_sleep)
                continue
            try:
                status = process_job(job, logger, dedup)
                jobs.complete(job["job_id"], status)
            except Exception as e:  # noqa: BLE001 - one bad job must not kill the worker
                jobs.fail(job["job_id"], f"{type(e).__name__}: {e}")
                sys.stderr.write(f"[{name}] job {job['job_id']} failed: {e}\n")
        except Exception as e:  # noqa: BLE001 - transient DB error: back off, keep going
            sys.stderr.write(f"[{name}] loop error: {e}\n{traceback.format_exc()}")
            _stop.wait(2.0)


def start_pool(n: int | None = None):
    """Start N daemon worker threads inside the current process (idempotent)."""
    global _pool_started
    if _pool_started:
        return
    pg.init_schema()
    n = n or settings.WORKER_COUNT
    for i in range(n):
        threading.Thread(target=run_worker_loop, args=(f"worker-{i}",),
                         daemon=True).start()
    _pool_started = True
    print(f"[workers] started in-process pool of {n}")


def main():
    pg.init_schema()
    n = settings.WORKER_COUNT
    print(f"[workers] standalone pool of {n} draining {settings.DATABASE_URL}")
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
