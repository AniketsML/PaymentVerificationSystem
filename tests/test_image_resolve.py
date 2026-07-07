"""
Golden-set for image-source resolution (pipeline/jobs._clean_source / _resolve_image).

Locks the robust handling of the JSON-array-encoded link format some exports use, and
the multi-column fallback that finds the link even when the wrong column was selected.
"""
from pipeline.jobs import _clean_source, _resolve_image
from pipeline.image_source import _diagnose_non_image

REAL = ("https://crworkspace.s3.ap-south-1.amazonaws.com/crdhs/private/"
        "1781603324891_WhatsApp%20Image%202026-06-16%20at%2015.04.44.jpeg")


def test_json_array_encoded_url_is_unwrapped():
    assert _clean_source(f'["{REAL}"]') == REAL


def test_double_escaped_json_array_url():
    assert _clean_source(r'["\"' + REAL + r'\""]') == REAL


def test_plain_url_passthrough():
    assert _clean_source(REAL) == REAL


def test_first_url_when_multiple():
    two = f'["{REAL}", "https://example.com/b.jpg"]'
    assert _clean_source(two) == REAL


def test_empty_and_blank():
    assert _clean_source("") == ""
    assert _clean_source(None) == ""
    assert _clean_source("   ") == ""


def test_local_path_array():
    assert _clean_source('["C:/images/receipt_1.jpg"]') == "C:/images/receipt_1.jpg"


def test_local_path_passthrough():
    assert _clean_source("C:/images/receipt_1.jpg") == "C:/images/receipt_1.jpg"


def test_resolve_falls_back_to_plural_column():
    # user selected the singular 'payment_document', but the link is in 'payment_documents'
    row = {"payment_documents": f'["{REAL}"]'}
    assert _resolve_image(row, "payment_document", "") == REAL


def test_resolve_prefers_selected_column():
    row = {"payment_document": REAL, "payment_documents": "https://other/x.jpg"}
    assert _resolve_image(row, "payment_document", "") == REAL


def test_resolve_local_root_joined_only_for_paths():
    row = {"payment_document": "sub/a.jpg"}
    out = _resolve_image(row, "payment_document", "C:/root")
    assert out.replace("\\", "/") == "C:/root/sub/a.jpg"
    # a URL is never prefixed with the local root
    row2 = {"payment_document": REAL}
    assert _resolve_image(row2, "payment_document", "C:/root") == REAL


# ── private/expired URL diagnosis (better than "corrupt image") ───────────────
def test_private_url_reason_from_aws_xml():
    body = b'<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>'
    assert "private" in _diagnose_non_image(body, "application/xml").lower()


def test_expired_signature_reason():
    body = b'<Error><Code>SignatureDoesNotMatch</Code></Error>'
    assert "private" in _diagnose_non_image(body, "").lower()


def test_html_login_page_reason():
    body = b'<!DOCTYPE html><html><body>please sign in</body></html>'
    assert "web page" in _diagnose_non_image(body, "text/html").lower()


def test_genuinely_corrupt_image_falls_back_to_original():
    # random binary (a truncated jpeg) is NOT an error page -> empty, so load() keeps
    # the original "unreadable/corrupt image" reason unchanged.
    assert _diagnose_non_image(bytes(range(256)) * 4, "image/jpeg") == ""


# ── dynamic fallback: find a link in ANY column when named ones are empty ──────
def test_dynamic_finds_link_in_unknown_column():
    row = {"lead_code": "LC1", "receipt_link": f'["{REAL}"]'}   # not a known column name
    assert _resolve_image(row, "payment_document", "") == REAL


def test_dynamic_prefers_image_url_over_unrelated_url():
    row = {"profile_url": "https://example.com/user/123",
           "attachment": "https://cdn.example.com/receipt_9.png"}
    assert _resolve_image(row, "payment_document", "") == "https://cdn.example.com/receipt_9.png"


def test_dynamic_uses_sole_non_image_url():
    row = {"some_link": "https://cdn.example.com/abc123"}      # no extension, but only one
    assert _resolve_image(row, "payment_document", "") == "https://cdn.example.com/abc123"


def test_dynamic_does_not_guess_among_ambiguous_non_image_urls():
    row = {"callback_url": "https://a.com/x", "source_url": "https://b.com/y"}
    assert _resolve_image(row, "payment_document", "") == ""   # ambiguous -> no guess


def test_named_column_still_wins_over_dynamic_scan():
    row = {"payment_document": REAL, "other_link": "https://cdn.example.com/z.png"}
    assert _resolve_image(row, "payment_document", "") == REAL
