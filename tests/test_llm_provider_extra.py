"""Coverage hardening for memorymaster.core.llm_provider.

These tests pin the *contracts* the rest of the system relies on, not
implementation details:

- ``parse_json_response`` must recover JSON from the four messy shapes LLMs
  actually emit (raw, fenced, prose+fenced, prose+raw) and degrade to ``[]``
  on garbage, because every entity/wiki caller assumes a list back.
- The fallback chain in ``call_llm`` must: return the primary on a clean
  response, roll over to the configured fallback on an empty OR quota-shaped
  primary response, and preserve the legacy "empty string on failure" contract
  when both fail. The telemetry counters must reflect what actually happened so
  operators can see cost/attribution.
- Quota-error detection must fire on real provider error *shapes*
  (RESOURCE_EXHAUSTED, HTTP 429, "quota exceeded") and NOT on benign content
  that merely mentions the word "quota" — a false-positive there spuriously
  double-bills the fallback (claim 11907).
- Response extractors must tolerate empty / malformed provider payloads
  without raising.

All tests are fully offline: provider dispatch is patched in ``_PROVIDERS`` and
the per-cycle budget hooks are neutralised, so no network or API keys run.
"""

import pytest

import memorymaster.core.llm_provider as lp
from memorymaster.core.llm_provider import (
    call_llm,
    get_fallback_stats,
    parse_json_response,
    reset_fallback_stats,
    _coerce_to_list,
    _extract_anthropic,
    _extract_google,
    _extract_ollama,
    _extract_openai,
    _looks_like_quota_error,
)


@pytest.fixture(autouse=True)
def _neutralise_budget(monkeypatch):
    """The per-cycle budget gate is exercised by its own suite; here it must be
    a no-op so fallback-chain assertions aren't perturbed by budget side effects.
    """
    monkeypatch.setattr(lp.llm_budget, "check_before_call", lambda *a, **k: None)
    monkeypatch.setattr(lp.llm_budget, "record_call", lambda *a, **k: None)
    monkeypatch.setattr(lp.llm_budget, "record_failure", lambda *a, **k: None)
    monkeypatch.setattr(lp.llm_budget, "estimate_tokens", lambda *a, **k: 0)


@pytest.fixture(autouse=True)
def _clean_env_and_stats(monkeypatch):
    for key in (
        "MEMORYMASTER_LLM_PROVIDER",
        "MEMORYMASTER_LLM_FALLBACK_PROVIDER",
        "MEMORYMASTER_LLM_FALLBACK_MODEL",
        "MEMORYMASTER_LLM_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_fallback_stats()
    yield
    reset_fallback_stats()


# --------------------------------------------------------------------------
# parse_json_response — the four shapes + degraded path
# --------------------------------------------------------------------------
class TestParseJsonResponse:
    def test_raw_array(self):
        assert parse_json_response('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]

    def test_fenced_json_block(self):
        text = '```json\n[{"a": 1}]\n```'
        assert parse_json_response(text) == [{"a": 1}]

    def test_fenced_block_without_lang(self):
        text = '```\n[{"x": 9}]\n```'
        assert parse_json_response(text) == [{"x": 9}]

    def test_prose_then_fenced(self):
        text = 'Here is the answer:\n```json\n[{"ok": true}]\n```\nthanks'
        # WHY: models routinely wrap JSON in prose; we must still recover it.
        assert parse_json_response(text) == [{"ok": True}]

    def test_prose_then_raw_array_greedy(self):
        text = 'The entities are: [{"n": "alice"}] -- done.'
        assert parse_json_response(text) == [{"n": "alice"}]

    def test_single_object_is_coerced_to_list(self):
        # WHY: callers iterate the result; a bare dict must become a 1-elem list.
        assert parse_json_response('{"a": 1}') == [{"a": 1}]

    def test_garbage_degrades_to_empty_list(self):
        # WHY: unparseable input must yield [] (documented degraded shape),
        # never raise — every caller assumes a list.
        assert parse_json_response("not json at all <<<") == []

    def test_empty_string(self):
        assert parse_json_response("") == []

    def test_scalar_json_coerces_to_empty_list(self):
        # A bare number is valid JSON but not list/dict -> [].
        assert parse_json_response("42") == []


class TestCoerceToList:
    def test_list_passthrough(self):
        assert _coerce_to_list([1, 2]) == [1, 2]

    def test_dict_wrapped(self):
        assert _coerce_to_list({"a": 1}) == [{"a": 1}]

    def test_scalar_dropped(self):
        assert _coerce_to_list("x") == []


# --------------------------------------------------------------------------
# Quota-error detection (claim 11907: defensive, shape-based)
# --------------------------------------------------------------------------
class TestQuotaDetection:
    @pytest.mark.parametrize(
        "body",
        [
            '{"error": {"status": "RESOURCE_EXHAUSTED"}}',
            '{"error": {"code": 429}}',
            "HTTP 429 Too Many Requests",
            "quota exceeded for this project",
            "rate-limit exceeded, retry later",
        ],
    )
    def test_true_on_error_shapes(self, body):
        assert _looks_like_quota_error(body) is True

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "The user asked about their monthly quota of API tokens.",
            "Here is a summary of rate limiting best practices.",
            '{"result": "ok"}',
        ],
    )
    def test_false_on_benign_or_empty(self, body):
        # WHY: a false-positive here spuriously fires the (paid) fallback on
        # legitimate content that merely mentions "quota"/"rate limit".
        assert _looks_like_quota_error(body) is False


# --------------------------------------------------------------------------
# Fallback chain in call_llm
# --------------------------------------------------------------------------
class TestFallbackChain:
    def test_unknown_primary_returns_empty(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "nope-not-real")
        assert call_llm("p", "t") == ""

    def test_primary_success_short_circuits(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "openai")
        calls = []
        monkeypatch.setitem(
            lp._PROVIDERS, "openai", lambda p, t: calls.append("openai") or "GOOD"
        )
        out = call_llm("p", "t")
        assert out == "GOOD"
        # WHY: a clean primary must not consult the fallback (would double-bill).
        assert calls == ["openai"]
        assert get_fallback_stats()["primary_ok"] == 1
        assert get_fallback_stats()["fired"] == 0

    def test_quota_primary_rolls_over_to_fallback(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")
        monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "openai")
        monkeypatch.setitem(
            lp._PROVIDERS, "google", lambda p, t: "RESOURCE_EXHAUSTED quota gone"
        )
        monkeypatch.setitem(lp._PROVIDERS, "openai", lambda p, t: "FALLBACK_OK")
        out = call_llm("p", "t")
        assert out == "FALLBACK_OK"
        # WHY: a quota-shaped primary is a failure; the configured fallback must
        # answer, and telemetry must record the fire.
        assert get_fallback_stats()["fired"] == 1
        assert get_fallback_stats()["primary_ok"] == 0

    def test_empty_primary_rolls_over_to_fallback(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")
        monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "ollama")
        monkeypatch.setitem(lp._PROVIDERS, "google", lambda p, t: "")
        monkeypatch.setitem(lp._PROVIDERS, "ollama", lambda p, t: "FROM_OLLAMA")
        assert call_llm("p", "t") == "FROM_OLLAMA"
        assert get_fallback_stats()["fired"] == 1

    def test_no_fallback_configured_returns_primary_response(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")
        monkeypatch.setitem(lp._PROVIDERS, "google", lambda p, t: "")
        # WHY: legacy contract — empty primary with no fallback returns "".
        assert call_llm("p", "t") == ""
        assert get_fallback_stats()["fired"] == 0

    def test_unknown_fallback_provider_returns_primary(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")
        monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "bogus")
        monkeypatch.setitem(lp._PROVIDERS, "google", lambda p, t: "")
        assert call_llm("p", "t") == ""
        # Misconfig must not be counted as a real fallback fire.
        assert get_fallback_stats()["fired"] == 0

    def test_both_fail_returns_primary_response(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")
        monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "openai")
        monkeypatch.setitem(lp._PROVIDERS, "google", lambda p, t: "")
        monkeypatch.setitem(lp._PROVIDERS, "openai", lambda p, t: "")
        # WHY: when both fail, the legacy empty-string contract must hold.
        assert call_llm("p", "t") == ""

    def test_fallback_model_env_swapped_and_restored(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")
        monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", "openai")
        monkeypatch.setenv("MEMORYMASTER_LLM_MODEL", "primary-model")
        monkeypatch.setenv("MEMORYMASTER_LLM_FALLBACK_MODEL", "fb-model")
        seen = {}

        def fb(p, t):
            import os

            # Real providers read the model via lp._env (which honors the
            # contextvar override); os.environ must stay at the primary model
            # so concurrent threads never see the fallback model mid-call.
            seen["model"] = lp._env("MEMORYMASTER_LLM_MODEL")
            seen["os_environ"] = os.environ.get("MEMORYMASTER_LLM_MODEL")
            return "OK"

        monkeypatch.setitem(lp._PROVIDERS, "google", lambda p, t: "")
        monkeypatch.setitem(lp._PROVIDERS, "openai", fb)
        call_llm("p", "t")
        # WHY: the fallback must run under the fallback model...
        assert seen["model"] == "fb-model"
        # ...delivered WITHOUT mutating shared os.environ...
        assert seen["os_environ"] == "primary-model"
        # ...and the primary model is still the effective one afterwards.
        import os

        assert os.environ.get("MEMORYMASTER_LLM_MODEL") == "primary-model"
        assert lp._env("MEMORYMASTER_LLM_MODEL") == "primary-model"


# --------------------------------------------------------------------------
# Fallback-stats telemetry helpers
# --------------------------------------------------------------------------
class TestFallbackStats:
    def test_reset_zeroes_counters(self):
        lp._FALLBACK_STATS["fired"] = 5
        reset_fallback_stats()
        assert get_fallback_stats() == {"attempts": 0, "fired": 0, "primary_ok": 0}

    def test_get_returns_copy_not_live_ref(self):
        snap = get_fallback_stats()
        snap["fired"] = 999
        # WHY: callers must not be able to corrupt the live counters.
        assert lp._FALLBACK_STATS["fired"] != 999


# --------------------------------------------------------------------------
# Response extractors — malformed payload tolerance
# --------------------------------------------------------------------------
class TestExtractors:
    def test_google_happy_and_empty(self):
        data = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
        assert _extract_google(data) == "hi"
        assert _extract_google({"candidates": []}) == ""

    def test_google_skips_thought_parts(self):
        data = {
            "candidates": [
                {"content": {"parts": [{"text": "T", "thought": True}, {"text": "real"}]}}
            ]
        }
        # WHY: Gemini thinking parts must be dropped from user-facing text.
        assert _extract_google(data) == "real"

    def test_openai_happy_and_empty(self):
        data = {"choices": [{"message": {"content": "yo"}}]}
        assert _extract_openai(data) == "yo"
        assert _extract_openai({"choices": []}) == ""

    def test_anthropic_happy_and_empty(self):
        data = {"content": [{"type": "text", "text": "claude"}]}
        assert _extract_anthropic(data) == "claude"
        assert _extract_anthropic({"content": []}) == ""

    def test_anthropic_ignores_non_text_blocks(self):
        data = {"content": [{"type": "tool_use", "text": "x"}, {"type": "text", "text": "y"}]}
        assert _extract_anthropic(data) == "y"

    def test_ollama(self):
        assert _extract_ollama({"response": "local"}) == "local"
        assert _extract_ollama({}) == ""


# --------------------------------------------------------------------------
# Provider call bodies — offline (HTTP layer patched)
# --------------------------------------------------------------------------
class TestProviderBodies:
    """Each provider builds a request and delegates to _http_post. We patch
    _http_post so no network runs, and assert the contract: missing API key
    short-circuits to "" (never a network call), and a configured key builds a
    well-formed URL.
    """

    def test_openai_missing_key_returns_empty(self, monkeypatch):
        # setenv("") rather than delenv: the real CI/dev env may export a key,
        # and an empty value is the falsy "no key" signal _call_openai checks.
        monkeypatch.setenv("OPENAI_API_KEY", "")
        called = []
        monkeypatch.setattr(lp, "_http_post", lambda *a, **k: called.append(1) or "x")
        # WHY: without a key we must not even attempt a (doomed, logged) request.
        assert lp._call_openai("p", "t") == ""
        assert called == []

    def test_openai_with_key_posts(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(lp, "_http_post", lambda url, *a, **k: f"URL::{url}")
        out = lp._call_openai("p", "t")
        assert out.startswith("URL::") and "/chat/completions" in out

    def test_anthropic_missing_key_returns_empty(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        called = []
        monkeypatch.setattr(lp, "_http_post", lambda *a, **k: called.append(1) or "x")
        assert lp._call_anthropic("p", "t") == ""
        assert called == []

    def test_anthropic_with_key_posts(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ak-test")
        monkeypatch.setattr(lp, "_http_post", lambda url, *a, **k: f"URL::{url}")
        out = lp._call_anthropic("p", "t")
        assert "api.anthropic.com" in out

    def test_ollama_posts_to_configured_base(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_URL", "http://example:1234")
        monkeypatch.setattr(lp, "_http_post", lambda url, *a, **k: url)
        # WHY: ollama needs no key; base URL must come from OLLAMA_URL.
        assert lp._call_ollama("p", "t") == "http://example:1234/api/generate"

    def test_google_no_key_no_rotator_returns_empty(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "")
        monkeypatch.setenv("MEMORYMASTER_API_KEY", "")
        monkeypatch.setattr(lp, "_call_google_with_env_rotation", lambda *a, **k: None)
        # get_rotator is imported locally inside _call_google from key_rotator,
        # so patch it at its source module, not on lp.
        monkeypatch.setattr("memorymaster.core.key_rotator.get_rotator", lambda name: None)
        called = []
        monkeypatch.setattr(lp, "_http_post", lambda *a, **k: called.append(1) or "x")
        assert lp._call_google("p", "t") == ""
        assert called == []

    def test_google_single_key_path_posts(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "g-key")
        monkeypatch.setattr(lp, "_call_google_with_env_rotation", lambda *a, **k: None)
        monkeypatch.setattr("memorymaster.core.key_rotator.get_rotator", lambda name: None)
        monkeypatch.setattr(lp, "_http_post", lambda url, *a, **k: url)
        out = lp._call_google("p", "t")
        # WHY: the single-key path must hit the generateContent endpoint.
        assert "generativelanguage.googleapis.com" in out
        assert ":generateContent" in out


# --------------------------------------------------------------------------
# _http_post — success and error branches (urlopen patched, no network)
# --------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestHttpPost:
    def test_success_invokes_extractor(self, monkeypatch):
        monkeypatch.setattr(
            lp.urllib.request,
            "urlopen",
            lambda req, timeout=10: _FakeResp(b'{"response": "hi"}'),
        )
        out = lp._http_post("http://x", {"a": 1}, lp._extract_ollama)
        assert out == "hi"

    def test_http_error_returns_empty_and_does_not_raise(self, monkeypatch):
        import urllib.error
        import io

        def boom(req, timeout=10):
            raise urllib.error.HTTPError(
                "http://x", 500, "err", {}, io.BytesIO(b"server error")
            )

        monkeypatch.setattr(lp.urllib.request, "urlopen", boom)
        # WHY: an HTTP error must degrade to "" (callers treat empty as failure),
        # never propagate and crash the steward cycle.
        assert lp._http_post("http://x", {}, lp._extract_openai) == ""

    def test_generic_exception_returns_empty(self, monkeypatch):
        def boom(req, timeout=10):
            raise OSError("connection refused")

        monkeypatch.setattr(lp.urllib.request, "urlopen", boom)
        assert lp._http_post("http://x", {}, lp._extract_openai) == ""

    def test_http_429_marks_rotator_rate_limited(self, monkeypatch):
        import urllib.error
        import io

        body = b'{"error": {"status": "RESOURCE_EXHAUSTED"}}'

        def boom(req, timeout=10):
            raise urllib.error.HTTPError("http://x", 429, "rl", {}, io.BytesIO(body))

        monkeypatch.setattr(lp.urllib.request, "urlopen", boom)

        class FakeRotator:
            def __init__(self):
                self.banned = []
                self.rate_limited = []

            def mark_banned(self, label, reason=None):
                self.banned.append(label)

            def mark_rate_limited(self, label, retry_after=None):
                self.rate_limited.append((label, retry_after))

        rot = FakeRotator()
        out = lp._http_post(
            "http://x", {}, lp._extract_google, rotator_label="k1", rotator=rot
        )
        assert out == ""
        # WHY: a 429 with RESOURCE_EXHAUSTED is a soft rate-limit, not a ban —
        # the key should be cooled down and reused, not permanently dropped.
        assert rot.rate_limited and rot.rate_limited[0][0] == "k1"
        assert rot.banned == []

    def test_http_403_marks_rotator_banned(self, monkeypatch):
        import urllib.error
        import io

        body = b'{"error": {"status": "PERMISSION_DENIED"}}'

        def boom(req, timeout=10):
            raise urllib.error.HTTPError("http://x", 403, "denied", {}, io.BytesIO(body))

        monkeypatch.setattr(lp.urllib.request, "urlopen", boom)

        class FakeRotator:
            def __init__(self):
                self.banned = []

            def mark_banned(self, label, reason=None):
                self.banned.append(label)

            def mark_rate_limited(self, label, retry_after=None):
                raise AssertionError("403 must ban, not rate-limit")

        rot = FakeRotator()
        lp._http_post("http://x", {}, lp._extract_google, rotator_label="k2", rotator=rot)
        # WHY: 403/PERMISSION_DENIED is a dead key — it must be banned so the
        # rotator stops wasting calls on it.
        assert rot.banned == ["k2"]


# --------------------------------------------------------------------------
# retry-after + google status parsing
# --------------------------------------------------------------------------
class TestRetryAfterAndStatus:
    def test_retry_after_from_header(self):
        # _parse_retry_after calls dict(headers), matching a real
        # email.message.Message (HTTPError.headers); a plain dict satisfies it.
        assert lp._parse_retry_after("", {"Retry-After": "12"}) == 12.0

    def test_retry_after_from_body_phrase(self):
        body = "Please retry in 7.5s due to load"
        assert lp._parse_retry_after(body, None) == 7.5

    def test_retry_after_absent_returns_none(self):
        assert lp._parse_retry_after("no hint here", None) is None

    def test_extract_google_status(self):
        assert (
            lp._extract_google_status('{"error": {"status": "RESOURCE_EXHAUSTED"}}')
            == "RESOURCE_EXHAUSTED"
        )

    def test_extract_google_status_bad_json(self):
        assert lp._extract_google_status("not json") == ""


# --------------------------------------------------------------------------
# Google env-key helpers + rotation paths
# --------------------------------------------------------------------------
class TestGoogleKeyHelpers:
    def test_split_keys_trims_and_drops_blanks(self):
        assert lp._split_keys(" a , b ,, c ") == ["a", "b", "c"]

    @pytest.mark.parametrize("val,expected", [("1", True), ("TRUE", True), ("on", True), ("0", False), ("", False)])
    def test_truthy_env(self, monkeypatch, val, expected):
        monkeypatch.setenv("X_FLAG", val)
        assert lp._truthy_env("X_FLAG") is expected

    def test_rotation_keys_prefers_csv_list(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LLM_API_KEYS", "k1,k2")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert lp._google_rotation_keys() == ["k1", "k2"]

    def test_rotation_keys_falls_back_to_single(self, monkeypatch):
        for k in ("MEMORYMASTER_LLM_API_KEYS", "GEMINI_API_KEYS", "MEMORYMASTER_API_KEYS"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "solo")
        # WHY: single-key users (no rotation configured) must still resolve a key.
        assert lp._google_rotation_keys() == ["solo"]

    def test_rotation_keys_empty_when_unset(self, monkeypatch):
        for k in (
            "MEMORYMASTER_LLM_API_KEYS",
            "GEMINI_API_KEYS",
            "MEMORYMASTER_API_KEYS",
            "GEMINI_API_KEY",
            "MEMORYMASTER_API_KEY",
        ):
            monkeypatch.delenv(k, raising=False)
        assert lp._google_rotation_keys() == []

    def test_env_rotator_disabled_without_flag(self, monkeypatch):
        monkeypatch.delenv("MEMORYMASTER_LLM_KEY_ROTATION", raising=False)
        assert lp._get_google_env_rotator() is None

    def test_env_rotator_none_when_no_keys(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LLM_KEY_ROTATION", "1")
        for k in (
            "MEMORYMASTER_LLM_API_KEYS",
            "GEMINI_API_KEYS",
            "MEMORYMASTER_API_KEYS",
            "GEMINI_API_KEY",
            "MEMORYMASTER_API_KEY",
        ):
            monkeypatch.delenv(k, raising=False)
        # WHY: rotation flag on but zero keys must not build a degenerate rotator.
        assert lp._get_google_env_rotator() is None


class _StubEnvRotator:
    """Minimal stand-in for KeyRotator used by _call_google_with_env_rotation."""

    def __init__(self, keys, results):
        self.keys = keys
        self.key_count = len(keys)
        self._results = list(results)
        self._idx = 0
        self.cleared = []
        self.rate_limited = []

    def get_key(self):
        k = self.keys[self._idx % len(self.keys)]
        self._idx += 1
        return k

    def clear_cooldown(self, key):
        self.cleared.append(key)

    def mark_rate_limited(self, key):
        self.rate_limited.append(key)


class TestGoogleEnvRotationCall:
    def test_returns_none_when_no_rotator(self, monkeypatch):
        monkeypatch.setattr(lp, "_get_google_env_rotator", lambda: None)
        # WHY: None (not "") signals "rotation not configured" so _call_google
        # falls through to the file-rotator / single-key paths.
        assert lp._call_google_with_env_rotation("m", {}) is None

    def test_first_key_success_clears_cooldown(self, monkeypatch):
        rot = _StubEnvRotator(["k1", "k2"], [])
        monkeypatch.setattr(lp, "_get_google_env_rotator", lambda: rot)
        monkeypatch.setattr(lp, "_http_post", lambda *a, **k: "OK")
        assert lp._call_google_with_env_rotation("m", {}) == "OK"
        assert rot.cleared == ["k1"]
        assert rot.rate_limited == []

    def test_rotates_past_rate_limited_key(self, monkeypatch):
        rot = _StubEnvRotator(["k1", "k2"], [])
        # First key returns a genuine 429 (status_sink=429), second succeeds.
        responses = iter([("", 429), ("OK", None)])
        monkeypatch.setattr(lp, "_get_google_env_rotator", lambda: rot)

        def fake_post(*a, **k):
            body, status = next(responses)
            sink = k.get("status_sink")
            if sink is not None:
                sink["http_status"] = status
            return body

        monkeypatch.setattr(lp, "_http_post", fake_post)
        out = lp._call_google_with_env_rotation("m", {})
        assert out == "OK"
        # WHY: a key that hit a real 429 must be cooled and the next key tried;
        # an empty-200 (no 429) must NOT cool a healthy key — see the test below.
        assert rot.rate_limited == ["k1"]
        assert rot.cleared == ["k2"]

    def test_empty_200_does_not_rate_limit_healthy_keys(self, monkeypatch):
        rot = _StubEnvRotator(["k1", "k2"], [])
        monkeypatch.setattr(lp, "_get_google_env_rotator", lambda: rot)

        def fake_post(*a, **k):
            # Empty body but a successful HTTP 200 — status_sink stays None.
            sink = k.get("status_sink")
            if sink is not None:
                sink["http_status"] = None
            return ""

        monkeypatch.setattr(lp, "_http_post", fake_post)
        # WHY: returns "" (a value, not None) so env rotation owns the call and
        # _call_google does NOT fall through to other paths.
        assert lp._call_google_with_env_rotation("m", {}) == ""
        # CRITICAL: empty-200 must leave every key UNCOOLED. Cooling healthy keys
        # here would cool the whole set across a batch and make get_key falsely
        # sleep on "all keys rate-limited".
        assert rot.rate_limited == []

    def test_all_keys_429_returns_empty_and_cools_each(self, monkeypatch):
        rot = _StubEnvRotator(["k1", "k2"], [])
        monkeypatch.setattr(lp, "_get_google_env_rotator", lambda: rot)

        def fake_post(*a, **k):
            sink = k.get("status_sink")
            if sink is not None:
                sink["http_status"] = 429
            return ""

        monkeypatch.setattr(lp, "_http_post", fake_post)
        # WHY: when every env key hits a real 429 we return "" (a value, not
        # None) and each key is correctly cooled.
        assert lp._call_google_with_env_rotation("m", {}) == ""
        assert rot.rate_limited == ["k1", "k2"]


class _StubFileRotator:
    def __init__(self, pairs):
        self._pairs = list(pairs)
        self._idx = 0

    def __len__(self):
        return len(self._pairs)

    def next_key(self):
        if self._idx >= len(self._pairs):
            return None
        p = self._pairs[self._idx]
        self._idx += 1
        return p


class TestCallGoogleDispatch:
    def test_gemini3_sets_thinking_level(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LLM_MODEL", "gemini-3.1-flash-lite-preview")
        monkeypatch.setattr(lp, "_call_google_with_env_rotation", lambda *a, **k: None)
        monkeypatch.setattr("memorymaster.core.key_rotator.get_rotator", lambda name: None)
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        captured = {}

        def fake_post(url, payload, *a, **k):
            captured["payload"] = payload
            return "x"

        monkeypatch.setattr(lp, "_http_post", fake_post)
        lp._call_google("p", "t")
        # WHY: gemini-3 models must request minimal thinking, not a budget=0.
        assert captured["payload"]["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": "minimal"
        }

    def test_gemini25_sets_thinking_budget_zero(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LLM_MODEL", "gemini-2.5-flash-lite")
        monkeypatch.setattr(lp, "_call_google_with_env_rotation", lambda *a, **k: None)
        monkeypatch.setattr("memorymaster.core.key_rotator.get_rotator", lambda name: None)
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        captured = {}
        monkeypatch.setattr(
            lp, "_http_post", lambda url, payload, *a, **k: captured.update(payload=payload) or "x"
        )
        lp._call_google("p", "t")
        assert captured["payload"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}

    def test_env_rotation_result_short_circuits(self, monkeypatch):
        monkeypatch.setattr(lp, "_call_google_with_env_rotation", lambda *a, **k: "ENVROT")
        # If env rotation returns a non-None value, no other path runs.
        monkeypatch.setattr(
            "memorymaster.core.key_rotator.get_rotator",
            lambda name: (_ for _ in ()).throw(AssertionError("must not reach file rotator")),
        )
        assert lp._call_google("p", "t") == "ENVROT"

    def test_file_rotator_path_returns_first_success(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LLM_MODEL", "gemini-3.1-flash-lite-preview")
        monkeypatch.setattr(lp, "_call_google_with_env_rotation", lambda *a, **k: None)
        rot = _StubFileRotator([("label1", "key1")])
        monkeypatch.setattr("memorymaster.core.key_rotator.get_rotator", lambda name: rot)
        monkeypatch.setattr(lp, "_http_post", lambda *a, **k: "FILE_OK")
        # WHY: the file-rotator path is the desktop default; a success on the
        # first labelled key must be returned directly.
        assert lp._call_google("p", "t") == "FILE_OK"
