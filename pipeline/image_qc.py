"""
STAGE 1 - Basic image validation. NO preprocessing, NO enhancement.

We do not touch the pixels. We compute a few deterministic quality metrics
(brightness, blur, contrast) purely to DISCARD images that are too broken to be
worth a vision-model call:

    - effectively black / blank white
    - unusably blurry
    - flat / no content (near-zero contrast)

There is deliberately NO minimum-resolution gate — small but legitimate receipts
and screenshots were being discarded. Resolution is still measured and logged.

Everything that passes goes straight to the model, untouched. FAIL reasons are
explicit ("too dark", "too blurry", ...) and logged, so a discarded image has a
precise, auditable cause. Thresholds live in config/settings.py (IMAGE_QC).
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from config import settings


def _to_gray_array(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L"), dtype=np.float64)


def _variance_of_laplacian(gray: np.ndarray) -> float:
    """Focus measure. Low variance => blurry. Pure-numpy 3x3 Laplacian."""
    g = gray
    lap = (4 * g[1:-1, 1:-1]
           - g[:-2, 1:-1] - g[2:, 1:-1]
           - g[1:-1, :-2] - g[1:-1, 2:])
    return float(lap.var()) if lap.size else 0.0


def metrics(img: Image.Image) -> dict:
    gray = _to_gray_array(img)
    return {
        "width": img.width,
        "height": img.height,
        "brightness": round(float(gray.mean()), 1),
        "contrast_std": round(float(gray.std()), 1),
        "blur_laplacian_var": round(_variance_of_laplacian(gray), 1),
    }


def _judge(m: dict) -> tuple[bool, str]:
    q = settings.IMAGE_QC
    # NOTE: no minimum-resolution gate. Small but legitimate receipts/screenshots were
    # being discarded; a genuinely broken tiny image is still caught by the blur/blank
    # checks below (or handled downstream by the model). Width/height are still logged.
    if m["brightness"] < q["dark_brightness_max"]:
        return False, f"image effectively black (brightness {m['brightness']})"
    # NO brightness upper bound: white-background receipts are bright but valid.
    # Blank white images are caught by low contrast_std below instead.
    if m["blur_laplacian_var"] < q["blur_laplacian_min"]:
        return False, f"image too blurry (focus {m['blur_laplacian_var']})"
    if m["contrast_std"] < q["low_contrast_std_min"]:
        return False, f"image blank / no content (contrast {m['contrast_std']})"
    return True, "image quality acceptable"


def evaluate(img: Image.Image) -> tuple[bool, str, dict]:
    """
    Returns (passed, reason, metrics) for an already-decoded PIL image.
    No enhancement is performed; the image is passed downstream untouched.
    """
    m = metrics(img)
    ok, reason = _judge(m)
    return ok, reason, m
