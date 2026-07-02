"""Unit tests for memorymaster.bridges.local_search (provider + everything).

All tests are fully offline: subprocess.run is monkeypatched so no real
ES.exe or filesystem access is required.

Markers: @pytest.mark.unit
"""
from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest

from memorymaster.bridges.local_search.everything import (
    EverythingProvider,
    _parse_es_output,
    _timeout,
)
from memorymaster.bridges.local_search.provider import (
    LocalSearchProvider,
    PathHit,
    ResolveMatch,
    ResolveResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed(stdout: str = "", returncode: int = 0) -> MagicMock:
    """Return a mock CompletedProcess-like object."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.stdout = stdout
    result.stderr = ""
    result.returncode = returncode
    return result


def _make_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    es_path: str = "C:/tools/es.exe",
    probe_stdout: str = "Everything 1.4.1",
    probe_returncode: int = 0,
    search_stdout: str = "",
    search_returncode: int = 0,
    file_exists: bool = True,
) -> EverythingProvider:
    """Build an EverythingProvider with subprocess.run and os.path.isfile mocked."""
    monkeypatch.setenv("MEMORYMASTER_EVERYTHING_ES_PATH", es_path)

    call_count: dict[str, int] = {"n": 0}

    def _fake_run(args: list[str], **kwargs: Any) -> MagicMock:
        # Reject shell=True — the test asserts this never happens implicitly
        # by checking kwargs does NOT contain shell=True.
        assert kwargs.get("shell") is not True, "shell=True must never be used"
        call_count["n"] += 1
        if "-version" in args:
            return _make_completed(probe_stdout, probe_returncode)
        return _make_completed(search_stdout, search_returncode)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.everything.os.path.isfile",
        lambda _path: file_exists,
    )

    return EverythingProvider()


# ---------------------------------------------------------------------------
# PathHit / DTO tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_path_hit_is_namedtuple() -> None:
    hit = PathHit(path="/some/path", kind="file", size=1024, modified=1_700_000_000.0)
    assert hit.path == "/some/path"
    assert hit.kind == "file"
    assert hit.size == 1024
    assert hit.modified == 1_700_000_000.0


@pytest.mark.unit
def test_path_hit_accepts_none_fields() -> None:
    hit = PathHit(path="D:/projects/foo", kind="dir", size=None, modified=None)
    assert hit.size is None
    assert hit.modified is None


@pytest.mark.unit
def test_resolve_match_is_frozen() -> None:
    match = ResolveMatch(
        path="/projects/mm",
        confidence=0.95,
        evidence=["slug match"],
        source="memory",
    )
    with pytest.raises(Exception):
        match.confidence = 0.5  # type: ignore[misc]


@pytest.mark.unit
def test_resolve_result_is_frozen() -> None:
    result = ResolveResult(
        query="memorymaster",
        canonical_slug="memorymaster",
        matches=[],
        best=None,
        degraded=True,
    )
    with pytest.raises(Exception):
        result.degraded = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Protocol structural check
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_everything_provider_satisfies_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _make_provider(monkeypatch)
    assert isinstance(provider, LocalSearchProvider)


# ---------------------------------------------------------------------------
# EverythingProvider — available()
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_available_true_when_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _make_provider(monkeypatch, probe_returncode=0)
    assert provider.available() is True


@pytest.mark.unit
def test_available_false_when_es_path_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMORYMASTER_EVERYTHING_ES_PATH", raising=False)
    provider = EverythingProvider()
    assert provider.available() is False


@pytest.mark.unit
def test_available_false_when_file_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _make_provider(monkeypatch, file_exists=False)
    assert provider.available() is False


@pytest.mark.unit
def test_available_false_when_probe_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _make_provider(monkeypatch, probe_returncode=1)
    assert provider.available() is False


@pytest.mark.unit
def test_available_caches_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call to available() must NOT re-run the probe subprocess."""
    call_log: list[str] = []

    monkeypatch.setenv("MEMORYMASTER_EVERYTHING_ES_PATH", "C:/es.exe")
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.everything.os.path.isfile",
        lambda _: True,
    )

    def _fake_run(args: list[str], **kwargs: Any) -> MagicMock:
        call_log.append("probe")
        return _make_completed("Everything 1.4", 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    provider = EverythingProvider()
    provider.available()
    provider.available()
    assert len(call_log) == 1, "Probe should only run once"


# ---------------------------------------------------------------------------
# EverythingProvider — search()
# ---------------------------------------------------------------------------

CANNED_ES_OUTPUT = "\n".join([
    r"C:\Projects\memorymaster",
    r"C:\Projects\memorymaster\memorymaster\service.py",
    r"C:\Projects\other-project",
    "",
])


@pytest.mark.unit
def test_search_parses_canned_output(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _make_provider(monkeypatch, search_stdout=CANNED_ES_OUTPUT)
    hits = provider.search("memorymaster")
    assert len(hits) == 3
    paths = [h.path for h in hits]
    assert r"C:\Projects\memorymaster" in paths
    assert r"C:\Projects\memorymaster\memorymaster\service.py" in paths


@pytest.mark.unit
def test_search_kind_filter_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _make_provider(monkeypatch, search_stdout=CANNED_ES_OUTPUT)
    hits = provider.search("memorymaster", kind="dir")
    # Paths without a dot in the last segment are inferred as dirs
    for hit in hits:
        assert hit.kind == "dir"


@pytest.mark.unit
def test_search_kind_filter_file(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _make_provider(monkeypatch, search_stdout=CANNED_ES_OUTPUT)
    hits = provider.search("memorymaster", kind="file")
    for hit in hits:
        assert hit.kind == "file"


@pytest.mark.unit
def test_search_passes_real_es_folder_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock the real ES 1.1.0.27 contract: kind=dir -> /ad, kind=file -> /a-d,
    kind=any -> neither, and the query is ALWAYS the last argv item.

    Verified against the live CLI: `-folder` is NOT a valid switch (Error 6);
    folders-only is `/ad`. This test fails if the arg-building regresses.
    """
    monkeypatch.setenv("MEMORYMASTER_EVERYTHING_ES_PATH", "C:/es.exe")
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.everything.os.path.isfile",
        lambda _: True,
    )
    captured: dict[str, list[str]] = {}

    def _fake_run(args: list[str], **kwargs: Any) -> MagicMock:
        if "-version" in args:
            return _make_completed("1.1.0.27", 0)
        captured["args"] = args
        return _make_completed("", 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    provider = EverythingProvider()

    provider.search("foo", kind="dir")
    assert "/ad" in captured["args"] and "/a-d" not in captured["args"]
    assert captured["args"][-1] == "foo"

    provider.search("foo", kind="file")
    assert "/a-d" in captured["args"]
    assert captured["args"][-1] == "foo"

    provider.search("foo", kind="any")
    assert "/ad" not in captured["args"] and "/a-d" not in captured["args"]
    assert captured["args"][-1] == "foo"

    # whole_name -> wfn: function prefix (Everything whole-filename match)
    provider.search("foo", kind="dir", whole_name=True)
    assert captured["args"][-1] == "wfn:foo"
    provider.search("my project", kind="dir", whole_name=True)
    assert captured["args"][-1] == 'wfn:"my project"'  # spaces quoted


@pytest.mark.unit
def test_search_returns_empty_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMORYMASTER_EVERYTHING_ES_PATH", raising=False)
    provider = EverythingProvider()
    hits = provider.search("anything")
    assert hits == []


@pytest.mark.unit
def test_search_returns_empty_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _make_provider(
        monkeypatch,
        search_stdout="",
        search_returncode=2,
    )
    hits = provider.search("memorymaster")
    assert hits == []


@pytest.mark.unit
def test_search_returns_empty_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_EVERYTHING_ES_PATH", "C:/es.exe")
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.everything.os.path.isfile",
        lambda _: True,
    )
    probe_called = {"n": 0}

    def _fake_run(args: list[str], **kwargs: Any) -> MagicMock:
        assert kwargs.get("shell") is not True
        if "-version" in args:
            probe_called["n"] += 1
            return _make_completed("Everything 1.4", 0)
        raise subprocess.TimeoutExpired(cmd=args, timeout=5.0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    provider = EverythingProvider()
    hits = provider.search("memorymaster")
    assert hits == []
    # After a timeout the provider marks itself unavailable
    assert provider.available() is False


@pytest.mark.unit
def test_search_returns_empty_on_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORYMASTER_EVERYTHING_ES_PATH", "C:/es.exe")
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.everything.os.path.isfile",
        lambda _: True,
    )

    def _fake_run(args: list[str], **kwargs: Any) -> MagicMock:
        assert kwargs.get("shell") is not True
        if "-version" in args:
            return _make_completed("Everything 1.4", 0)
        raise FileNotFoundError("es.exe missing")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    provider = EverythingProvider()
    hits = provider.search("memorymaster")
    assert hits == []


@pytest.mark.unit
def test_search_returns_empty_on_os_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORYMASTER_EVERYTHING_ES_PATH", "C:/es.exe")
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.everything.os.path.isfile",
        lambda _: True,
    )

    def _fake_run(args: list[str], **kwargs: Any) -> MagicMock:
        assert kwargs.get("shell") is not True
        if "-version" in args:
            return _make_completed("Everything 1.4", 0)
        raise OSError("permission denied")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    provider = EverythingProvider()
    hits = provider.search("memorymaster")
    assert hits == []


# ---------------------------------------------------------------------------
# shell=False guarantee
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_no_shell_true_in_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit assertion that shell=True is never passed to subprocess.run."""
    shell_calls: list[bool] = []

    monkeypatch.setenv("MEMORYMASTER_EVERYTHING_ES_PATH", "C:/es.exe")
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.everything.os.path.isfile",
        lambda _: True,
    )

    def _fake_run(args: list[str], **kwargs: Any) -> MagicMock:
        shell_calls.append(kwargs.get("shell", False))
        return _make_completed("Everything 1.4.1\nC:\\Projects\\foo", 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    provider = EverythingProvider()
    provider.available()
    provider.search("foo")

    assert all(s is not True for s in shell_calls), (
        f"shell=True detected in subprocess.run calls: {shell_calls}"
    )


# ---------------------------------------------------------------------------
# _parse_es_output (pure function)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_parse_es_output_empty_string() -> None:
    assert _parse_es_output("", "any") == []


@pytest.mark.unit
def test_parse_es_output_blank_lines_skipped() -> None:
    output = "\n\n   \n"
    assert _parse_es_output(output, "any") == []


@pytest.mark.unit
def test_parse_es_output_infers_dir_for_no_extension() -> None:
    output = r"C:\Projects\memorymaster" + "\n"
    hits = _parse_es_output(output, "any")
    assert len(hits) == 1
    assert hits[0].kind == "dir"


@pytest.mark.unit
def test_parse_es_output_infers_file_for_extension() -> None:
    output = r"C:\Projects\memorymaster\service.py" + "\n"
    hits = _parse_es_output(output, "any")
    assert len(hits) == 1
    assert hits[0].kind == "file"


@pytest.mark.unit
def test_parse_es_output_size_and_modified_are_none() -> None:
    """ES plain-path mode does not emit size/modified columns."""
    output = r"C:\Projects\foo" + "\n"
    hits = _parse_es_output(output, "any")
    assert hits[0].size is None
    assert hits[0].modified is None


# ---------------------------------------------------------------------------
# _timeout() helper
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORYMASTER_EVERYTHING_TIMEOUT", raising=False)
    assert _timeout() == 5.0


@pytest.mark.unit
def test_timeout_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_EVERYTHING_TIMEOUT", "10")
    assert _timeout() == 10.0


@pytest.mark.unit
def test_timeout_invalid_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_EVERYTHING_TIMEOUT", "not-a-number")
    assert _timeout() == 5.0


# ---------------------------------------------------------------------------
# Multi-word term splitting (ES argv bug, fixed 2026-07-02)
# ---------------------------------------------------------------------------

def _capture_provider(monkeypatch: pytest.MonkeyPatch, captured: list) -> EverythingProvider:
    """Provider whose subprocess.run records the search argv."""
    monkeypatch.setenv("MEMORYMASTER_EVERYTHING_ES_PATH", "C:/tools/es.exe")

    def _fake_run(args: list, **kwargs: Any) -> MagicMock:
        if "-version" in args:
            return _make_completed("Everything 1.4.1", 0)
        captured.append(list(args))
        return _make_completed("", 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.everything.os.path.isfile",
        lambda _path: True,
    )
    return EverythingProvider()


@pytest.mark.unit
def test_multiword_query_terms_are_separate_argv_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """WHY: ES.exe never re-splits an argv containing spaces — Windows arg
    joining quotes it, so ES matched ONE literal phrase and EVERY multi-word
    query returned 0 hits (observed live: 'path:projects jsonl' as one argv
    -> 0; split -> 100). This silently made whole subtrees 'unsearchable' and
    was misdiagnosed as the redactor dropping C:/Users hits."""
    captured: list = []
    provider = _capture_provider(monkeypatch, captured)
    provider.search("path:projects jsonl dm:today", limit=10)
    args = captured[-1]
    assert "path:projects" in args
    assert "jsonl" in args
    assert "dm:today" in args
    assert "path:projects jsonl dm:today" not in args


@pytest.mark.unit
def test_user_quoted_phrase_stays_one_term(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user-quoted phrase must remain a single term (ES phrase semantics)."""
    captured: list = []
    provider = _capture_provider(monkeypatch, captured)
    provider.search('ext:log "error report" 2026', limit=10)
    args = captured[-1]
    assert '"error report"' in args
    assert "ext:log" in args
    assert "2026" in args


@pytest.mark.unit
def test_whole_name_query_remains_single_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """whole_name resolves via wfn: and must NOT be split (regression guard for
    the resolver path, which worked before this fix)."""
    captured: list = []
    provider = _capture_provider(monkeypatch, captured)
    provider.search("my project name", limit=10, whole_name=True)
    args = captured[-1]
    assert 'wfn:"my project name"' in args
