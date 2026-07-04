"""
Central configuration for the Payment Verification System.
All thresholds are explicit and deterministic - no magic numbers buried in code.
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
VISION_API_URL = os.environ.get("MEDHA_API_URL", "http://164.52.192.196:8002/v1")
VISION_API_KEY = os.environ.get("MEDHA_API_KEY", "REMOVED-SECRET")
VISION_MODEL   = os.environ.get("MEDHA_MODEL", "Medha")
VISION_STREAM  = os.environ.get("MEDHA_STREAM", "1") not in ("0", "false", "False", "")
OCR_TIMEOUT_SECONDS = int(os.environ.get("MEDHA_TIMEOUT", "120"))
IMAGE_FETCH_TIMEOUT = int(os.environ.get("IMAGE_FETCH_TIMEOUT", "30"))

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
