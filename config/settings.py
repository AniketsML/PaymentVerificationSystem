"""
Central configuration for the Payment Verification System.
All thresholds are explicit and deterministic - no magic numbers buried in code.

Secrets (Medha API key, DB URL) are read from the environment or a gitignored `.env`
at the project root — they are NEVER hardcoded here. See `.env.example`.
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv():
    """Populate os.environ from a gitignored `.env` at the project root (dependency-free).
    Existing environment variables always win, so real deploys can override the file."""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)
    except Exception:
        pass


_load_dotenv()
CONFIG_DIR = os.path.join(BASE_DIR, "config")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
OUTPUT_DIR = os.path.join(DATA_DIR, "outputs")
for _d in (DATA_DIR, LOG_DIR, UPLOAD_DIR, OUTPUT_DIR):
    os.makedirs(_d, exist_ok=True)

LENDER_RECEIVERS_PATH = os.path.join(CONFIG_DIR, "lender_receivers.json")
LENDER_RULES_PATH = os.path.join(CONFIG_DIR, "lender_rules.json")

# central store used by the app, the logs, and (later) Metabase
DB_PATH = os.path.join(DATA_DIR, "verification.db")   # legacy SQLite (unused by the web app)

# ── PostgreSQL (production datastore + durable job queue) ─────────────────────
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/payment_verification")
# background worker pool that drains the job queue
WORKER_COUNT = int(os.environ.get("WORKER_COUNT", "4"))
JOB_MAX_ATTEMPTS = int(os.environ.get("JOB_MAX_ATTEMPTS", "3"))
JOB_LEASE_SECONDS = int(os.environ.get("JOB_LEASE_SECONDS", "300"))   # crashed-worker recovery

# ── Stage 1: basic image validation (NO preprocessing/enhancement) ────────────
# Philosophy: we do NOT touch the pixels. We only DISCARD images that are too
# broken to be worth a model call (unreadable, too small, blank, or clearly
# unusable quality). Everything that passes goes straight to the vision model.
#
# NOTE: there is deliberately NO brightness upper bound. A white-background
# receipt (GPay/PhonePe white-card UI) is bright but NOT blank; brightness alone
# cannot tell real white UI from a blank white image. Blank detection is done via
# contrast_std instead - true blanks are near-flat regardless of brightness.
IMAGE_QC = {
    # NOTE: no min_width/min_height gate — small but legitimate receipts/screenshots
    # were being discarded. Broken tiny images are still caught by blur/contrast below.
    "dark_brightness_max": 15,   # mean gray below this = near-black
    "blur_laplacian_min": 50.0,  # variance-of-Laplacian below this = unusably blurry
    "low_contrast_std_min": 8.0,  # near-flat image = blank/no content (any brightness)
}

# ── Vision model (Medha - OpenAI-compatible chat/completions) ─────────────────
# Endpoint + key come from the environment / .env — no secret is committed here.
VISION_API_URL = os.environ.get("MEDHA_API_URL", "http://164.52.192.196:8002/v1")
VISION_API_KEY = os.environ.get("MEDHA_API_KEY", "")
VISION_MODEL   = os.environ.get("MEDHA_MODEL", "Medha")
VISION_STREAM  = os.environ.get("MEDHA_STREAM", "1") not in ("0", "false", "False", "")
OCR_TIMEOUT_SECONDS = int(os.environ.get("MEDHA_TIMEOUT", "120"))
IMAGE_FETCH_TIMEOUT = int(os.environ.get("IMAGE_FETCH_TIMEOUT", "30"))

# Extraction cache: skip the model call for an image we've already read (keyed on image
# content + model + prompt version). Safe for re-running the same leads to test the
# deterministic classification/verify logic — only a prompt/model change re-invokes.
OCR_CACHE = os.environ.get("OCR_CACHE", "1") not in ("0", "false", "False", "")
OCR_CACHE_TTL_DAYS = int(os.environ.get("OCR_CACHE_TTL_DAYS", "30"))   # cap cache growth (0 = keep forever)

# Retention for the heavy per-stage event log (lead_events). Verdicts (lead_results) are
# NEVER touched — they persist for Metabase/history. lead_events is the operational/debug
# trail and is the only table that grows unbounded under continuous ingestion. 0 = keep
# forever (default, safe for testing); set e.g. 90 in continuous production.
LEAD_EVENTS_TTL_DAYS = int(os.environ.get("LEAD_EVENTS_TTL_DAYS", "0"))

# Circuit breaker: when Medha starts dropping connections (10054) / timing out under
# load, retry-storming makes it worse. After N consecutive failures the breaker "opens"
# and callers wait a growing cooldown, applying backpressure instead of hammering it.
OCR_BREAKER_THRESHOLD = int(os.environ.get("OCR_BREAKER_THRESHOLD", "5"))
OCR_BREAKER_COOLDOWN  = float(os.environ.get("OCR_BREAKER_COOLDOWN", "8"))    # base seconds
OCR_BREAKER_MAX_WAIT  = float(os.environ.get("OCR_BREAKER_MAX_WAIT", "60"))   # cap

# ── Web serving / access ──────────────────────────────────────────────────────
WEB_THREADS = int(os.environ.get("WEB_THREADS", "8"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "64"))   # reject bigger uploads (OOM guard)
# Optional HTTP Basic auth on the dashboard (the app carries borrower financial docs).
# Enforced only when BOTH are set — set them to lock the LAN dashboard down. NEVER
# commit real values; pass via env. /health stays open for probes.
AUTH_USER = os.environ.get("PV_AUTH_USER", "")
AUTH_PASS = os.environ.get("PV_AUTH_PASS", "")
# signs the login session cookie; set a stable value in .env so sessions survive restarts
SECRET_KEY = os.environ.get("PV_SECRET_KEY", "")

# Test Workspace data is disposable — it never becomes long-term storage. Test rows
# older than this (days) are auto-purged on startup, and the "Clear workspace" button
# wipes it on demand. 0 disables the auto-purge.
TEST_TTL_DAYS = float(os.environ.get("TEST_TTL_DAYS", "1"))

# payment-method keyword map (deterministic labelling)
PAYMENT_METHOD_KEYWORDS = [
    ("PhonePe",     ["phonepe", "phon epe", "@ybl", "ppe"]),
    ("Google Pay",  ["google pay", "gpay", "g pay", "@okaxis", "@oksbi", "@okhdfcbank", "@okicici"]),
    ("Paytm",       ["paytm", "@paytm", "ptm"]),
    ("Amazon Pay",  ["amazon pay", "amazonpay"]),
    ("BHIM UPI",    ["bhim", "@upi"]),
    ("Cred",        ["cred "]),
    ("Bharat Connect (BBPS)", ["bharat connect", "bbps", "bharatbillpay"]),
    ("Bank Transfer (NEFT/IMPS/RTGS)", ["neft", "imps", "rtgs", "fund transfer"]),
    ("Cash Receipt", ["cash receipt", "mode of payment : cash", "mode of payment cash", "paid by cash"]),
    ("Cheque", ["cheque", "chq no"]),
    ("e-NACH", ["e-nach", "enach", "e nach", "auto debit", "mandate"]),
]
