"""
CSV manifest intake — a spreadsheet of document links instead of a folder upload.

The sheet carries one row per document:

    LAN, DIRECT_FILE_LINK_ONLY, DOCUMENT_TYPE, FILE_NAME
    HL00474200000005031699, https://drive.google.com/file/d/<id>/view?usp=sharing, SOA, HL...._SOA.pdf

The same LAN appears once per document, so the rows are grouped by LAN and each group becomes
one dossier folder named after the LAN — exactly the shape the folder/ZIP upload already
produces. From there nothing is special: `scan_and_enqueue_folder` registers the lead and its
documents, and the normal extraction pipeline runs.

Downloads happen in the background, LAN by LAN, and each dossier is queued for extraction as
soon as its own documents have landed — so work starts while the rest is still downloading.
Every row's outcome is recorded in `legal_manifest_runs`, so a link that could not be fetched
is visible instead of silently missing.
"""
from __future__ import annotations

import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from psycopg.types.json import Jsonb
from werkzeug.utils import secure_filename

from db import pg
from workspaces.legal.jobs import SUPPORTED_EXTENSIONS, scan_and_enqueue_folder

MAX_FILE_MB = int(os.environ.get("LEGAL_MANIFEST_MAX_MB", "120"))
FETCH_TIMEOUT = int(os.environ.get("LEGAL_MANIFEST_TIMEOUT", "120"))
_UA = {"User-Agent": "Mozilla/5.0 (SARFAESI-Legal/1.0)"}

# accepted spellings for each column, checked as "does the header contain this"
_COLS = {
    "lan": ("lan", "loan account", "loan_account", "account no", "account_no", "accountnumber", "lead"),
    "url": ("direct_file_link", "direct file link", "link", "url", "document_link", "file_link", "drive"),
    "doc_type": ("document_type", "document type", "doc_type", "doc type", "type", "category"),
    "file_name": ("file_name", "file name", "filename", "name"),
}


def _match_columns(headers: List[str]) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {k: None for k in _COLS}
    lowered = [(h, str(h).strip().lower()) for h in headers]
    for key, needles in _COLS.items():
        for original, low in lowered:
            if any(n in low for n in needles) and original not in out.values():
                out[key] = original
                break
    return out


def parse_manifest(path_or_stream, filename: str = "") -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """Read a CSV/Excel manifest into rows of {lan, url, doc_type, file_name}.
    Returns (rows, info) where info explains what was found — or why nothing was."""
    import pandas as pd
    name = (filename or getattr(path_or_stream, "filename", "") or str(path_or_stream)).lower()
    try:
        if name.endswith((".xlsx", ".xls")):
            df = pd.read_excel(path_or_stream, dtype=str)
        else:
            df = pd.read_csv(path_or_stream, dtype=str, sep=None, engine="python")
    except Exception as e:  # noqa: BLE001 — a bad sheet is a user error, not a crash
        return [], {"error": f"could not read the sheet: {e}"}
    df = df.fillna("")
    cols = _match_columns(list(df.columns))
    if not cols["lan"] or not cols["url"]:
        return [], {"error": "the sheet needs a LAN column and a document link column",
                    "headers": [str(c) for c in df.columns][:12]}

    rows: List[Dict[str, str]] = []
    skipped = 0
    for _, r in df.iterrows():
        lan = str(r[cols["lan"]]).strip()
        url = str(r[cols["url"]]).strip()
        if not lan or not url or not url.lower().startswith("http"):
            skipped += 1
            continue
        rows.append({
            "lan": lan,
            "url": url,
            "doc_type": str(r[cols["doc_type"]]).strip() if cols["doc_type"] else "",
            "file_name": str(r[cols["file_name"]]).strip() if cols["file_name"] else "",
        })
    lans = sorted({r["lan"] for r in rows})
    return rows, {"rows": len(rows), "lans": len(lans), "skipped": skipped,
                  "columns": {k: (str(v) if v else None) for k, v in cols.items()}}


# ── links ────────────────────────────────────────────────────────────────────
_DRIVE_ID = re.compile(r"(?:/file/d/|[?&]id=|/d/)([A-Za-z0-9_-]{16,})")


def direct_link(url: str) -> str:
    """A Google Drive share link points at a viewer page; turn it into the file itself.
    Everything else is returned untouched."""
    if "drive.google.com" not in url and "docs.google.com" not in url:
        return url
    m = _DRIVE_ID.search(url)
    return f"https://drive.google.com/uc?export=download&id={m.group(1)}" if m else url


def _safe_name(file_name: str, lan: str, doc_type: str, index: int, url: str) -> str:
    base = secure_filename(file_name or "") or secure_filename(f"{lan}_{doc_type or 'document'}_{index}")
    ext = os.path.splitext(base)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        guess = os.path.splitext(url.split("?")[0])[1].lower()
        base += guess if guess in SUPPORTED_EXTENSIONS else ".pdf"
    return base


def fetch_document(url: str, dest: str) -> Tuple[bool, str]:
    """Download one document. Returns (ok, reason-if-not)."""
    target = direct_link(url)
    cap = MAX_FILE_MB * 1024 * 1024
    try:
        with requests.Session() as s:
            s.headers.update(_UA)
            r = s.get(target, timeout=FETCH_TIMEOUT, stream=True, allow_redirects=True)
            r.raise_for_status()
            ctype = (r.headers.get("Content-Type") or "").lower()
            first = next(r.iter_content(8192), b"")
            # Drive answers big files with a confirmation page instead of the bytes
            if "text/html" in ctype:
                page = first + b"".join(r.iter_content(65536))
                text = page.decode("utf-8", "ignore")
                token = re.search(r"confirm=([0-9A-Za-z_\-]+)", text)
                if "accounts.google.com" in text or "Sign in" in text[:2000]:
                    return False, "the link is not shared publicly (Google asks for sign-in)"
                if not token:
                    return False, "the link returned a web page, not a file"
                r = s.get(target + f"&confirm={token.group(1)}", timeout=FETCH_TIMEOUT, stream=True)
                r.raise_for_status()
                first = next(r.iter_content(8192), b"")
            size = 0
            tmp = dest + ".part"
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(tmp, "wb") as fh:
                if first:
                    fh.write(first)
                    size += len(first)
                for chunk in r.iter_content(262144):
                    fh.write(chunk)
                    size += len(chunk)
                    if size > cap:
                        fh.close()
                        os.remove(tmp)
                        return False, f"file is larger than {MAX_FILE_MB} MB"
            if size == 0:
                os.remove(tmp)
                return False, "the link returned an empty file"
            os.replace(tmp, dest)
            return True, ""
    except requests.exceptions.RequestException as e:
        return False, f"download failed ({type(e).__name__})"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# ── progress record ──────────────────────────────────────────────────────────
def start_run(batch_id: str, batch_name: str, total_rows: int, total_lans: int, is_test: bool) -> None:
    with pg.pool().connection() as c:
        c.execute(
            "INSERT INTO legal_manifest_runs(batch_id, batch_name, total_rows, total_lans, is_test) "
            "VALUES(%s,%s,%s,%s,%s) ON CONFLICT (batch_id) DO UPDATE SET total_rows=EXCLUDED.total_rows, "
            "total_lans=EXCLUDED.total_lans, batch_name=EXCLUDED.batch_name",
            (batch_id, batch_name, total_rows, total_lans, is_test))


def _update(batch_id: str, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=%s" for k in fields)
    with pg.pool().connection() as c:
        c.execute(f"UPDATE legal_manifest_runs SET {sets} WHERE batch_id=%s",
                  tuple(fields.values()) + (batch_id,))


def status(batch_id: str) -> Optional[Dict[str, Any]]:
    with pg.pool().connection() as c:
        r = c.execute("SELECT * FROM legal_manifest_runs WHERE batch_id=%s", (batch_id,)).fetchone()
    if not r:
        return None
    r = dict(r)
    for k in ("created_at", "finished_at"):
        if r.get(k):
            r[k] = r[k].isoformat()
    return r


# ── the run ──────────────────────────────────────────────────────────────────
def run_manifest(rows: List[Dict[str, str]], batch_dir: str, batch_id: str, batch_name: str,
                 is_test: bool = False, extraction_prompt: str = "") -> Dict[str, Any]:
    """Download every document, grouped by LAN, queueing each dossier as its files land."""
    by_lan: Dict[str, List[Dict[str, str]]] = {}
    for r in rows:
        by_lan.setdefault(r["lan"], []).append(r)

    fetched = failed = leads = 0
    failures: List[Dict[str, str]] = []
    start_run(batch_id, batch_name, len(rows), len(by_lan), is_test)

    for lan, docs in by_lan.items():
        lan_dir = os.path.join(batch_dir, secure_filename(lan) or "UNKNOWN_LAN")
        os.makedirs(lan_dir, exist_ok=True)
        got = 0
        used: set = set()
        for i, row in enumerate(docs, start=1):
            name = _safe_name(row["file_name"], lan, row["doc_type"], i, row["url"])
            stem, ext = os.path.splitext(name)
            while name.lower() in used:                      # two rows, same file name
                name = f"{stem}_{i}{ext}"
                i += 1
            used.add(name.lower())
            ok, why = fetch_document(row["url"], os.path.join(lan_dir, name))
            if ok:
                fetched += 1
                got += 1
            else:
                failed += 1
                failures.append({"lan": lan, "file": row["file_name"] or name,
                                 "type": row["doc_type"], "url": row["url"], "reason": why})
            _update(batch_id, fetched=fetched, failed=failed, failures=Jsonb(failures[:200]))

        if got:
            try:
                res = scan_and_enqueue_folder(lan_dir, is_test=is_test,
                                              extraction_prompt=extraction_prompt,
                                              batch_name=batch_name, batch_id=batch_id)
                leads += res.get("leads_enqueued", 0)
                _update(batch_id, leads=leads)
            except Exception as e:  # noqa: BLE001 — one bad dossier must not stop the batch
                sys.stderr.write(f"[csv_intake] enqueue failed for {lan}: {e}\n")
                failures.append({"lan": lan, "file": "", "type": "", "url": "",
                                 "reason": f"could not queue the dossier: {e}"})
                _update(batch_id, failures=Jsonb(failures[:200]))

    _update(batch_id, fetched=fetched, failed=failed, leads=leads,
            failures=Jsonb(failures[:200]), finished_at=time.strftime("%Y-%m-%d %H:%M:%S%z"))
    return {"batch_id": batch_id, "fetched": fetched, "failed": failed,
            "leads_enqueued": leads, "failures": failures[:50]}
