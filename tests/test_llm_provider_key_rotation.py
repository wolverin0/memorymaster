import memorymaster.key_rotator as file_key_rotator
import memorymaster.llm_provider as llm_provider


def _reset_env_rotator() -> None:
    llm_provider._GOOGLE_ENV_ROTATOR = None
    llm_provider._GOOGLE_ENV_ROTATOR_KEYSET = ()


def _key_from_url(url: str) -> str:
    return url.rsplit("key=", 1)[1]


def test_google_rotation_off_uses_single_gemini_key(monkeypatch):
    _reset_env_rotator()
    calls: list[str] = []

    def fake_http_post(url, payload, extractor, **kwargs):
        key = _key_from_url(url)
        calls.append(key)
        return f"ok-{key}"

    monkeypatch.delenv("MEMORYMASTER_LLM_KEY_ROTATION", raising=False)
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("MEMORYMASTER_API_KEYS", raising=False)
    monkeypatch.delenv("MEMORYMASTER_LLM_API_KEYS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "single-key")
    monkeypatch.setattr(file_key_rotator, "get_rotator", lambda provider: None)
    monkeypatch.setattr(llm_provider, "_http_post", fake_http_post)

    assert llm_provider._call_google("prompt", "text") == "ok-single-key"
    assert calls == ["single-key"]


def test_google_rotation_on_round_robins_across_configured_keys(monkeypatch):
    _reset_env_rotator()
    calls: list[str] = []

    def fake_http_post(url, payload, extractor, **kwargs):
        key = _key_from_url(url)
        calls.append(key)
        return f"ok-{key}"

    monkeypatch.setenv("MEMORYMASTER_LLM_KEY_ROTATION", "1")
    monkeypatch.setenv("GEMINI_API_KEYS", "key-1,key-2,key-3")
    monkeypatch.setattr(file_key_rotator, "get_rotator", lambda provider: None)
    monkeypatch.setattr(llm_provider, "_http_post", fake_http_post)

    results = [llm_provider._call_google("prompt", "text") for _ in range(5)]

    assert results == [
        "ok-key-1",
        "ok-key-2",
        "ok-key-3",
        "ok-key-1",
        "ok-key-2",
    ]
    assert calls == ["key-1", "key-2", "key-3", "key-1", "key-2"]


def test_google_rotation_tries_next_key_when_one_fails(monkeypatch):
    _reset_env_rotator()
    calls: list[str] = []
    failures = {"key-1": 1}

    def fake_http_post(url, payload, extractor, **kwargs):
        key = _key_from_url(url)
        calls.append(key)
        if failures.get(key, 0) > 0:
            failures[key] -= 1
            return ""
        return f"ok-{key}"

    monkeypatch.setenv("MEMORYMASTER_LLM_KEY_ROTATION", "1")
    monkeypatch.setenv("GEMINI_API_KEYS", "key-1,key-2,key-3")
    monkeypatch.setattr(file_key_rotator, "get_rotator", lambda provider: None)
    monkeypatch.setattr(llm_provider, "_http_post", fake_http_post)

    assert llm_provider._call_google("prompt", "text") == "ok-key-2"
    assert calls == ["key-1", "key-2"]
