"""TypeSafe API key lookup: env first, then HKCU\\Environment; never leaks the value."""
from __future__ import annotations

import logging

import pytest

from memorymaster.decisions import credentials
from memorymaster.decisions.credentials import ApiKey, CredentialError, get_api_key

SECRET = "ts-test-" + "Q" * 24  # synthetic


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1

    def __init__(self, values=None, fail=False):
        self.values = values or {}
        self.fail = fail
        self.opened = []

    def OpenKey(self, root, path, reserved=0, access=0):
        if self.fail:
            raise OSError("registry unavailable")
        self.opened.append((root, path))
        return _FakeKey()

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], 1


def test_env_key_wins_over_registry():
    reg = FakeWinreg({"TYPESAFE_API_KEY": "from-registry"})
    key = get_api_key({"TYPESAFE_API_KEY": SECRET}, winreg_module=reg)
    assert key is not None and key.reveal() == SECRET
    assert reg.opened == []


def test_registry_fallback_when_env_missing_or_blank():
    reg = FakeWinreg({"TYPESAFE_API_KEY": "  " + SECRET + "  "})
    key = get_api_key({"TYPESAFE_API_KEY": "   "}, winreg_module=reg)
    assert key is not None and key.reveal() == SECRET
    assert reg.opened and reg.opened[0][1] == "Environment"


def test_missing_everywhere_returns_none():
    assert get_api_key({}, winreg_module=FakeWinreg()) is None
    assert get_api_key({}, winreg_module=FakeWinreg(fail=True)) is None


def test_non_windows_skips_registry(monkeypatch):
    monkeypatch.setattr(credentials.sys, "platform", "linux")
    assert get_api_key({}) is None


def test_api_key_never_renders_its_value():
    key = ApiKey(SECRET)
    for rendered in (repr(key), str(key), f"{key}", "%s" % key, repr([key]), repr({"k": key})):
        assert SECRET not in rendered
    assert key.bearer_header() == {"Authorization": f"Bearer {SECRET}"}


def test_api_key_rejects_empty():
    with pytest.raises(CredentialError) as excinfo:
        ApiKey("  ")
    assert "Bearer" not in str(excinfo.value)


def test_registry_errors_do_not_echo_value(caplog):
    class Exploding(FakeWinreg):
        def QueryValueEx(self, key, name):
            raise RuntimeError(f"boom {SECRET}")

    with caplog.at_level(logging.DEBUG):
        assert get_api_key({}, winreg_module=Exploding()) is None
    assert SECRET not in caplog.text


def test_scrub_removes_key_from_arbitrary_text():
    key = ApiKey(SECRET)
    assert SECRET not in key.scrub(f"failed calling with Bearer {SECRET} at host")
    assert key.scrub("nothing here") == "nothing here"
