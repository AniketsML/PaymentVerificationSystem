"""
Regression for model routing (workspaces/legal/llm.py): single vs hybrid, the scanned-page policy,
re-reading a page that turned out not to be English, retry on the other model, and the Gemini REST
client's handling of replies. No network — calls are stubbed.
"""
import json

import pytest

from workspaces.legal import llm


def _settings(routing="single", main="medha", multilingual="gemini", scanned="english_first",
              fallback=True, gemini_key="k-123", medha_url="http://medha/v1"):
    return {"routing": routing, "main": main, "multilingual": multilingual, "scanned": scanned,
            "fallback": fallback, "medha": {"url": medha_url, "key": "", "model": "Medha"},
            "gemini": {"key": gemini_key, "model": "gemini-2.5-flash"}}


class Stub:
    """Stands in for llm._call: replies per provider, and records the order of calls."""

    def __init__(self, replies):
        self.replies, self.order = replies, []

    def __call__(self, provider, s, prompt, images, max_tokens, timeout):
        self.order.append(provider)
        r = self.replies[provider]
        if isinstance(r, Exception):
            raise r
        return r, {"provider": provider, "model": llm.model_name(provider, s), "route": f"{provider}_vlm",
                   "ms": 10.0, "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


ENGLISH = json.dumps({"borrower_name": "Ravi Kumar"})
HINDI_EVIDENCE = json.dumps({"borrower_name": "Ravi Kumar",
                             "_field_evidence": {"borrower_name": {"original": "रवि कुमार"}}})
DEVANAGARI = json.dumps({"borrower_name": "रवि कुमार"})


# ── language ──────────────────────────────────────────────────────────────────
def test_text_script():
    assert llm.text_script("This loan agreement is made between the borrower and the bank.") == "english"
    assert llm.text_script("यह ऋण समझौता उधारकर्ता और बैंक के बीच किया गया है और यह मान्य है") == "multilingual"
    assert llm.text_script("") == "unknown"
    assert llm.text_script("Page 3") == "unknown"            # too little text to judge
    # accented Latin is still "english" for routing purposes
    assert llm.text_script("Société Générale émet une lettre de sanction pour le prêt immobilier") == "english"


def test_reply_not_english():
    assert not llm.reply_not_english(json.loads(ENGLISH))
    assert llm.reply_not_english(json.loads(HINDI_EVIDENCE))
    assert llm.reply_not_english(json.loads(DEVANAGARI))
    assert not llm.reply_not_english({"_notes": "रवि"})       # bookkeeping keys don't count


# ── planning ──────────────────────────────────────────────────────────────────
def test_plan_single_ignores_language():
    s = _settings(routing="single", main="gemini")
    for lang in ("english", "multilingual", "unknown"):
        assert llm.plan(s, lang)[:2] == ("gemini", None)


def test_plan_hybrid():
    s = _settings(routing="hybrid")
    assert llm.plan(s, "english")[:2] == ("medha", None)
    assert llm.plan(s, "multilingual")[:2] == ("gemini", None)
    assert llm.plan(s, "unknown")[:2] == ("medha", "gemini")          # english_first
    s["scanned"] = "multilingual"
    assert llm.plan(s, "unknown")[:2] == ("gemini", None)


# ── extraction ────────────────────────────────────────────────────────────────
def test_single_one_call(monkeypatch):
    stub = Stub({"medha": ENGLISH, "gemini": ENGLISH})
    monkeypatch.setattr(llm, "_call", stub)
    out = llm.extract("p", [b"x"], "multilingual", s=_settings())
    assert stub.order == ["medha"]
    assert out["borrower_name"] == "Ravi Kumar"
    assert [c["status"] for c in out["_calls"]] == ["OK"]
    assert out["_ocr_route"] == "medha_vlm"


def test_hybrid_scan_escalates_when_not_english(monkeypatch):
    stub = Stub({"medha": HINDI_EVIDENCE, "gemini": json.dumps({"borrower_name": "Ravi Kumaar"})})
    monkeypatch.setattr(llm, "_call", stub)
    out = llm.extract("p", [b"x"], "unknown", s=_settings(routing="hybrid"))
    assert stub.order == ["medha", "gemini"]
    assert out["borrower_name"] == "Ravi Kumaar"                      # the re-reading is used
    assert [c["status"] for c in out["_calls"]] == ["SUPERSEDED", "OK"]
    assert out["_meta"]["provider"] == "gemini"
    assert "re-read" in out["_routing"]


def test_hybrid_scan_english_stays(monkeypatch):
    stub = Stub({"medha": ENGLISH, "gemini": ENGLISH})
    monkeypatch.setattr(llm, "_call", stub)
    out = llm.extract("p", [b"x"], "unknown", s=_settings(routing="hybrid"))
    assert stub.order == ["medha"]
    assert len(out["_calls"]) == 1


def test_hybrid_escalation_failure_keeps_first_reading(monkeypatch):
    stub = Stub({"medha": HINDI_EVIDENCE, "gemini": llm.ModelError("Gemini HTTP 429: quota")})
    monkeypatch.setattr(llm, "_call", stub)
    out = llm.extract("p", [b"x"], "unknown", s=_settings(routing="hybrid"))
    assert "_error" not in out
    assert out["_meta"]["provider"] == "medha"
    assert [c["status"] for c in out["_calls"]] == ["OK", "FAIL"]


def test_text_layer_routes_directly(monkeypatch):
    stub = Stub({"medha": DEVANAGARI, "gemini": DEVANAGARI})
    monkeypatch.setattr(llm, "_call", stub)
    llm.extract("p", [b"x"], "english", s=_settings(routing="hybrid"))
    llm.extract("p", [b"x"], "multilingual", s=_settings(routing="hybrid"))
    assert stub.order == ["medha", "gemini"]                         # no re-reads on known language


def test_fallback_on_failure(monkeypatch):
    stub = Stub({"medha": llm.ModelError("Medha HTTP 502: bad gateway"), "gemini": ENGLISH})
    monkeypatch.setattr(llm, "_call", stub)
    out = llm.extract("p", [b"x"], "english", s=_settings())
    assert stub.order == ["medha", "gemini"]
    assert out["_meta"]["provider"] == "gemini"
    assert [c["status"] for c in out["_calls"]] == ["FAIL", "OK"]


def test_fallback_on_unparseable_reply(monkeypatch):
    stub = Stub({"medha": "sorry, I can't", "gemini": ENGLISH})
    monkeypatch.setattr(llm, "_call", stub)
    out = llm.extract("p", [b"x"], "english", s=_settings())
    assert out["_meta"]["provider"] == "gemini"


def test_list_reply_is_a_failure_not_a_crash(monkeypatch):
    stub = Stub({"medha": "[1, 2]", "gemini": "[3]"})
    monkeypatch.setattr(llm, "_call", stub)
    out = llm.extract("p", [b"x"], "english", s=_settings())
    assert out["_ocr_route"] == "failed"


def test_no_fallback_when_off_or_unconfigured(monkeypatch):
    stub = Stub({"medha": llm.ModelError("down"), "gemini": ENGLISH})
    monkeypatch.setattr(llm, "_call", stub)
    out = llm.extract("p", [b"x"], "english", s=_settings(fallback=False))
    assert stub.order == ["medha"] and out["_ocr_route"] == "failed"
    assert "down" in out["_error"]
    stub.order.clear()
    out = llm.extract("p", [b"x"], "english", s=_settings(gemini_key=""))
    assert stub.order == ["medha"] and out["_ocr_route"] == "failed"


def test_unconfigured_provider_is_named():
    ok, why = llm.is_configured("gemini", _settings(gemini_key=""))
    assert not ok and "API key" in why
    with pytest.raises(llm.ModelError):
        llm._call("gemini", _settings(gemini_key=""), "p", [], 10, 1)


def test_call_text_uses_main_then_other(monkeypatch):
    stub = Stub({"medha": llm.ModelError("down"), "gemini": "{\"ok\": true}"})
    monkeypatch.setattr(llm, "_call", stub)
    assert llm.call_text("plan", s=_settings()) == "{\"ok\": true}"
    assert stub.order == ["medha", "gemini"]


# ── the Gemini REST client ────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def _gemini(monkeypatch, status, body):
    import httpx
    seen = {}

    def post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, json=json)
        return _Resp(status, body)
    monkeypatch.setattr(httpx, "post", post)
    return seen


def test_gemini_request_and_usage(monkeypatch):
    seen = _gemini(monkeypatch, 200, {
        "candidates": [{"content": {"parts": [{"text": "thinking…", "thought": True}, {"text": ENGLISH}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 50, "thoughtsTokenCount": 200,
                          "totalTokenCount": 1250}})
    raw, meta = llm._call_gemini(_settings(), "prompt", [b"img"], 512, 5)
    assert raw == ENGLISH                                              # thought parts are not the answer
    assert meta["prompt_tokens"] == 1000 and meta["completion_tokens"] == 250
    assert seen["url"].endswith("/models/gemini-2.5-flash:generateContent")
    assert "k-123" not in seen["url"] and seen["headers"]["x-goog-api-key"] == "k-123"
    parts = seen["json"]["contents"][0]["parts"]
    assert parts[0]["inlineData"]["mimeType"] == "image/jpeg" and parts[-1]["text"] == "prompt"
    assert seen["json"]["generationConfig"]["responseMimeType"] == "application/json"


def test_gemini_errors_are_readable(monkeypatch):
    _gemini(monkeypatch, 400, {"error": {"message": "API key not valid"}})
    with pytest.raises(llm.ModelError, match="API key not valid"):
        llm._call_gemini(_settings(), "p", [], 10, 1)
    _gemini(monkeypatch, 200, {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(llm.ModelError, match="SAFETY"):
        llm._call_gemini(_settings(), "p", [], 10, 1)
    _gemini(monkeypatch, 200, {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]})
    with pytest.raises(llm.ModelError, match="MAX_TOKENS"):
        llm._call_gemini(_settings(), "p", [], 10, 1)


# ── saving the setup (DB-backed; skipped without Postgres) ────────────────────
def _have_db():
    try:
        from db import pg
        with pg.pool().connection() as c:
            c.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture
def saved_config():
    """Snapshot runtime_config's extraction rows and restore them afterwards."""
    if not _have_db():
        pytest.skip("Postgres not available")
    from config import runtime
    from db import pg
    keys = list(runtime._EXTRACTION_KEYS) + ["medha_api_url", "medha_api_key", "medha_model"]
    with pg.pool().connection() as c:
        # the pool hands back dict rows — read them by name, never by unpacking
        rows = c.execute("SELECT key, value FROM runtime_config WHERE key = ANY(%s)", (keys,)).fetchall()
    before = {r["key"]: r["value"] for r in rows}
    assert set(before) <= set(keys)
    yield runtime
    with pg.pool().connection() as c:
        c.execute("DELETE FROM runtime_config WHERE key = ANY(%s)", (keys,))
        for k, v in before.items():
            c.execute("INSERT INTO runtime_config(key, value) VALUES(%s, %s)", (k, v))
    runtime.extraction_config(fresh=True)


def test_refuses_gemini_without_key(saved_config):
    rt = saved_config
    rt.set_extraction_config(gemini_key="", clear_gemini_key=True, routing="single", main="medha")
    with pytest.raises(rt.ConfigError, match="Gemini"):
        rt.set_extraction_config(routing="single", main="gemini")
    with pytest.raises(rt.ConfigError, match="Gemini"):
        rt.set_extraction_config(routing="hybrid", main="medha", multilingual="gemini")


def test_refuses_hybrid_with_one_model(saved_config):
    rt = saved_config
    with pytest.raises(rt.ConfigError, match="two different models"):
        rt.set_extraction_config(routing="hybrid", main="medha", multilingual="medha")


def test_saves_hybrid_and_masks_key(saved_config):
    rt = saved_config
    out = rt.set_extraction_config(routing="hybrid", main="medha", multilingual="gemini",
                                   gemini_key="AIzaTESTKEY9876", gemini_model="models/gemini-2.5-flash")
    assert out["routing"] == "hybrid" and out["gemini"]["configured"]
    assert out["gemini"]["model"] == "gemini-2.5-flash"                 # "models/" prefix dropped
    assert "AIzaTESTKEY9876" not in json.dumps(out) and out["gemini"]["key_masked"].endswith("9876")
    # a blank key on the next save keeps the stored one
    out = rt.set_extraction_config(gemini_key="", scanned="multilingual")
    assert out["gemini"]["has_key"] and out["scanned"] == "multilingual"
    assert rt.extraction_config(fresh=True)["gemini"]["key"] == "AIzaTESTKEY9876"


def test_blank_medha_url_never_clears_shared_endpoint(saved_config):
    rt = saved_config
    url = rt.model_config(fresh=True)["url"]
    if not url:
        pytest.skip("no Medha endpoint configured")
    rt.set_extraction_config(medha_url="", routing="single", main="medha")
    assert rt.model_config(fresh=True)["url"] == url
