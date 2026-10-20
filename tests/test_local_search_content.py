"""Bounded content search for ``local_search`` (T-1645).

Hermetic: every file lives under ``tmp_path``, the registered root is
monkeypatched to ``tmp_path``, Everything is a fake provider and the rg
subprocess is either faked or (parity test only) the real binary on PATH.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

from memorymaster.bridges.local_search import content
from memorymaster.bridges.local_search.provider import PathHit

SECRET_LINE = "token = sk-ant-" + "A1b2C3d4E5f6G7h8I9j0K1"  # synthetic, 20+ chars


class FakeProvider:
    def __init__(self, paths: list[str], *, available: bool = True) -> None:
        self.paths = paths
        self._available = available
        self.calls: list[dict] = []

    def available(self) -> bool:
        return self._available

    def search(self, query, *, limit=50, kind="any", whole_name=False, under=None):
        call = {"query": query, "limit": limit, "kind": kind, "whole_name": whole_name}
        if under is not None:
            call["under"] = under
        self.calls.append(call)
        return [PathHit(path=p, kind="file", size=None, modified=None) for p in self.paths]


@pytest.fixture()
def tree(tmp_path):
    """A small tree with good files and every kind of file that must be skipped."""
    files = {
        "src/app.py": "import os\nneedle in app\nprint('x')\n",
        "docs/notes.md": "# Notes\nThe NEEDLE is here\n",
        "src/data.txt": "no match at all\n",
        ".git/config": "needle in git\n",
        "node_modules/pkg/index.js": "needle in node_modules\n",
        "pkg/__pycache__/m.py": "needle in pycache\n",
        ".venv/lib/x.py": "needle in venv\n",
        "venv/lib/x.py": "needle in venv2\n",
        ".ssh/config": "needle in ssh\n",
        ".aws/credentials": "needle in aws\n",
        ".gemini/a.md": "needle in gemini\n",
        ".codex/a.md": "needle in codex\n",
        ".claude/a.md": "needle in claude\n",
        ".memorymaster/state.txt": "needle in state\n",
        ".config/gcloud/a.txt": "needle in config\n",
        "AppData/Local/x.txt": "needle in appdata\n",
        ".env": "needle=1\n",
        "keys/id_rsa": "needle in key\n",
        "keys/server.pem": "needle in pem\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (tmp_path / "src" / "blob.bin").write_bytes(b"needle\x00\x01\x02binary")
    return tmp_path


def _roots(tmp_path):
    return [("workspace", str(tmp_path))]


def _all_files(tmp_path) -> list[str]:
    out = []
    for dirpath, _dirs, names in os.walk(tmp_path):
        for name in names:
            out.append(os.path.join(dirpath, name))
    return out


def _search(tmp_path, paths=None, *, query="needle", provider=None, **kw):
    provider = provider or FakeProvider(paths if paths is not None else _all_files(tmp_path))
    return content.run_content_search(
        provider,
        _roots(tmp_path),
        name_query=kw.pop("name_query", "*"),
        content_query=query,
        workspace=kw.pop("workspace", str(tmp_path)),
        **kw,
    )


@pytest.fixture(autouse=True)
def _python_engine(monkeypatch):
    """Default to the Python line scan; rg tests opt in explicitly."""
    monkeypatch.setattr(content, "rg_binary", lambda: None)
    for var in (
        "MEMORYMASTER_LOCAL_SEARCH_EXCLUDE_DIRS",
        "MEMORYMASTER_LOCAL_SEARCH_MAX_FILE_BYTES",
        "MEMORYMASTER_LOCAL_SEARCH_MAX_RESPONSE_BYTES",
        "MEMORYMASTER_LOCAL_SEARCH_TIMEOUT",
    ):
        monkeypatch.delenv(var, raising=False)


def _tokens(result) -> set[str]:
    return {h["path"] for h in result["hits"]}


def test_python_engine_returns_path_line_snippet(tree):
    result = _search(tree)
    assert result["engine"] == "python"
    by_path = {h["path"]: h for h in result["hits"]}
    assert set(by_path) == {"workspace/src/app.py", "workspace/docs/notes.md"}
    assert by_path["workspace/src/app.py"]["line"] == 2
    assert by_path["workspace/src/app.py"]["snippet"] == "needle in app"
    # case-insensitive literal match
    assert by_path["workspace/docs/notes.md"]["line"] == 2
    assert set(by_path["workspace/src/app.py"]) == {"path", "line", "snippet"}


def test_default_exclusions_are_never_read_or_listed(tree):
    result = _search(tree)
    blob = json.dumps(result)
    for skipped in (".git", "node_modules", "__pycache__", ".venv", "venv", ".ssh", ".aws",
                    ".gemini", ".codex", ".claude", ".memorymaster", ".config", "AppData", ".env", "id_rsa", "server.pem",
                    "blob.bin"):
        assert skipped not in blob, skipped


def test_exclusions_apply_when_a_scan_would_otherwise_reach_the_file(tree, monkeypatch):
    opened: list[str] = []
    real_open = open

    def spy(path):
        opened.append(str(path))
        return real_open(path, "rb")

    monkeypatch.setattr(content, "_open_binary", spy, raising=False)
    _search(tree)
    joined = "\n".join(opened).replace("\\", "/")
    for skipped in ("/.git/", "/node_modules/", "/.ssh/", "/.aws/", "/.claude/", "id_rsa", ".env"):
        assert skipped not in joined, skipped


def test_extra_excluded_dir_from_env(tree, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_LOCAL_SEARCH_EXCLUDE_DIRS", "docs;other")
    result = _search(tree)
    assert _tokens(result) == {"workspace/src/app.py"}


def test_env_cannot_weaken_default_exclusions(tree, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_LOCAL_SEARCH_EXCLUDE_DIRS", "")
    assert not any(".ssh" in p for p in _tokens(_search(tree)))


def test_binary_files_are_skipped(tree):
    assert not any(p.endswith("blob.bin") for p in _tokens(_search(tree)))


def test_per_file_byte_cap_skips_oversized_files(tree, monkeypatch):
    big = tree / "src" / "big.txt"
    big.write_text("x" * 5000 + "\nneedle far away\n", encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_LOCAL_SEARCH_MAX_FILE_BYTES", "1000")
    assert "workspace/src/big.txt" not in _tokens(_search(tree))
    monkeypatch.setenv("MEMORYMASTER_LOCAL_SEARCH_MAX_FILE_BYTES", "100000")
    assert "workspace/src/big.txt" in _tokens(_search(tree))


def test_max_hits_caps_results_and_flags_truncation(tmp_path):
    f = tmp_path / "many.txt"
    f.write_text("\n".join(f"needle {i}" for i in range(50)), encoding="utf-8")
    result = _search(tmp_path, [str(f)], max_hits=5, max_per_file=100)
    assert len(result["hits"]) == 5
    assert result["truncated"] is True


def test_response_byte_cap(tmp_path, monkeypatch):
    f = tmp_path / "many.txt"
    f.write_text("\n".join(f"needle {i} " + "y" * 150 for i in range(100)), encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_LOCAL_SEARCH_MAX_RESPONSE_BYTES", "1500")
    result = _search(tmp_path, [str(f)], max_hits=100, max_per_file=100)
    assert result["truncated"] is True
    assert 0 < len(result["hits"]) < 100
    assert len(json.dumps(result["hits"])) <= 1500


def test_total_timeout_stops_the_scan(tree):
    result = _search(tree, timeout=0)
    assert result["timed_out"] is True
    assert result["hits"] == []


def test_path_outside_registered_roots_is_neither_read_nor_listed(tmp_path):
    root = tmp_path / "registered"
    outside = tmp_path / "private-person" / "proj"
    root.mkdir()
    outside.mkdir(parents=True)
    (root / "a.txt").write_text("needle ok\n", encoding="utf-8")
    (outside / "b.txt").write_text("needle secret place\n", encoding="utf-8")
    provider = FakeProvider([str(root / "a.txt"), str(outside / "b.txt")])
    result = content.run_content_search(
        provider, [("workspace", str(root))], name_query="*", content_query="needle",
        workspace=str(root),
    )
    assert _tokens(result) == {"workspace/a.txt"}
    assert "private-person" not in json.dumps(result)


def test_symlink_escaping_the_root_is_rejected(tmp_path):
    root = tmp_path / "registered"
    outside = tmp_path / "elsewhere"
    root.mkdir()
    outside.mkdir()
    (outside / "b.txt").write_text("needle outside\n", encoding="utf-8")
    link = root / "link.txt"
    try:
        os.symlink(outside / "b.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    result = content.run_content_search(
        FakeProvider([str(link)]), [("workspace", str(root))], name_query="*",
        content_query="needle", workspace=str(root),
    )
    assert result["hits"] == []


def test_symlink_escape_is_rejected_by_realpath_check(tmp_path, monkeypatch):
    """Same as the symlink test but simulated, so it runs without symlink privilege."""
    root = tmp_path / "registered"
    outside = tmp_path / "elsewhere"
    root.mkdir()
    outside.mkdir()
    link = root / "link.txt"
    link.write_text("needle inside the root, but the link points out\n", encoding="utf-8")
    real_target = outside / "b.txt"
    real_target.write_text("needle outside\n", encoding="utf-8")
    real_realpath = os.path.realpath
    monkeypatch.setattr(
        os.path, "realpath",
        lambda p, *a, **k: str(real_target) if str(p) == str(link) else real_realpath(p, *a, **k),
    )
    result = content.run_content_search(
        FakeProvider([str(link)]), [("workspace", str(root))], name_query="*",
        content_query="needle", workspace=str(root),
    )
    assert result["hits"] == []


def test_snippet_secret_is_redacted(tmp_path):
    f = tmp_path / "cfg.txt"
    f.write_text(SECRET_LINE + "\n", encoding="utf-8")
    result = _search(tmp_path, [str(f)], query="token")
    assert len(result["hits"]) == 1
    snippet = result["hits"][0]["snippet"]
    assert "sk-ant-" not in snippet.replace("[REDACTED:anthropic_key]", "")
    assert "REDACTED" in snippet


def test_snippet_home_path_is_redacted(tmp_path):
    f = tmp_path / "cfg.txt"
    f.write_text("see C:\\Users\\SomePerson\\proj\\x.txt for needle\n", encoding="utf-8")
    snippet = _search(tmp_path, [str(f)])["hits"][0]["snippet"]
    assert "SomePerson" not in snippet


def test_snippet_is_bounded_and_centred_on_the_match(tmp_path):
    f = tmp_path / "min.js"
    f.write_text("a" * 3000 + " needle " + "b" * 3000 + "\n", encoding="utf-8")
    snippet = _search(tmp_path, [str(f)])["hits"][0]["snippet"]
    assert len(snippet) <= content.MAX_SNIPPET_CHARS == 200
    assert "needle" in snippet


def test_secret_straddling_the_cut_is_not_half_exposed(tmp_path):
    f = tmp_path / "min.js"
    key = "sk-ant-" + "Z9y8X7w6V5u4T3s2R1q0P9o8"
    f.write_text("a" * 150 + " needle " + key + " " + "b" * 400 + "\n", encoding="utf-8")
    snippet = _search(tmp_path, [str(f)])["hits"][0]["snippet"]
    assert "sk-ant-" not in snippet
    assert "Z9y8X7w6" not in snippet


def test_extension_filter(tree):
    result = _search(tree, extension="md")
    assert _tokens(result) == {"workspace/docs/notes.md"}
    result = _search(tree, extension=".py;.md")
    assert _tokens(result) == {"workspace/src/app.py", "workspace/docs/notes.md"}


def test_extension_is_validated(tree):
    with pytest.raises(content.ContentSearchError) as exc:
        _search(tree, extension="py;../../etc")
    assert exc.value.field == "extension"


def test_folder_filter_limits_candidates(tree):
    result = _search(tree, folder=str(tree / "docs"))
    assert _tokens(result) == {"workspace/docs/notes.md"}


def test_folder_accepts_a_redacted_token(tree):
    result = _search(tree, folder="workspace/docs")
    assert _tokens(result) == {"workspace/docs/notes.md"}


def test_folder_outside_roots_is_rejected(tree, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere")
    with pytest.raises(content.ContentSearchError) as exc:
        _search(tree, folder=str(other))
    assert exc.value.field == "folder"
    with pytest.raises(content.ContentSearchError):
        _search(tree, folder=str(tree / ".." / ".."))


def test_everything_receives_file_kind_and_hints(tree):
    provider = FakeProvider(_all_files(tree))
    _search(tree, provider=provider, name_query="app", extension="py", folder=str(tree / "src"))
    call = provider.calls[0]
    assert call["kind"] == "file"
    assert call["query"] == "app ext:py"
    assert call["under"] == os.path.realpath(tree / "src")
    assert call["limit"] <= content.HARD_MAX_CANDIDATES


def test_default_scope_is_the_workspace_not_the_whole_machine(tree):
    other = tree / "docs"
    provider = FakeProvider(_all_files(tree))
    result = _search(tree, provider=provider, workspace=str(other))
    assert provider.calls[0]["under"] == os.path.realpath(other)
    assert _tokens(result) == {"workspace/docs/notes.md"}


def test_provider_without_under_support_still_works(tree):
    class Legacy(FakeProvider):
        def search(self, query, *, limit=50, kind="any", whole_name=False):
            return super().search(query, limit=limit, kind=kind, whole_name=whole_name)

    provider = Legacy(_all_files(tree))
    result = _search(tree, provider=provider, folder=str(tree / "docs"))
    assert _tokens(result) == {"workspace/docs/notes.md"}
    assert "under" not in provider.calls[0]


def test_exact_mode_sends_the_bare_name_so_wfn_is_not_broken(tree):
    provider = FakeProvider(_all_files(tree))
    _search(tree, provider=provider, name_query="app.py", extension="py", whole_name=True)
    assert provider.calls[0]["query"] == "app.py"
    assert provider.calls[0]["whole_name"] is True


def test_python_filters_are_authoritative_even_if_provider_ignores_hints(tree):
    result = _search(tree, extension="md", folder=str(tree / "docs"))
    assert _tokens(result) == {"workspace/docs/notes.md"}


def test_degraded_provider_walks_the_workspace_with_name_filter(tree):
    provider = FakeProvider([], available=False)
    result = _search(tree, provider=provider, name_query="app*")
    assert result["degraded"] is True
    assert _tokens(result) == {"workspace/src/app.py"}
    assert provider.calls == []


def test_degraded_walk_prunes_excluded_dirs(tree):
    provider = FakeProvider([], available=False)
    result = _search(tree, provider=provider, name_query="*")
    assert _tokens(result) == {"workspace/src/app.py", "workspace/docs/notes.md"}


def test_workspace_outside_roots_is_rejected_when_walking(tree, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere")
    with pytest.raises(content.ContentSearchError) as exc:
        _search(tree, provider=FakeProvider([], available=False), workspace=str(other))
    assert exc.value.field == "workspace"


def test_nothing_is_written(tree):
    before = sorted(_all_files(tree))
    _search(tree)
    assert sorted(_all_files(tree)) == before


# ---------------------------------------------------------------- rg engine


def _rg_match(path, line, text):
    return json.dumps({"type": "match", "data": {
        "path": {"text": path}, "lines": {"text": text + "\n"}, "line_number": line,
        "absolute_offset": 0, "submatches": [],
    }}).encode()


def test_rg_engine_argv_and_json_parsing(tree, monkeypatch):
    seen: list[list[str]] = []
    app = str(tree / "src" / "app.py")
    foreign = str(tree / "src" / "not-in-batch.py")

    def fake_stream(argv, timeout):
        seen.append(argv)
        yield json.dumps({"type": "begin", "data": {"path": {"text": app}}}).encode()
        yield _rg_match(app, 2, "needle in app")
        yield json.dumps({"type": "match", "data": {"path": {"bytes": "AAAA"}, "lines": {"text": "x"},
                                                    "line_number": 1}}).encode()
        yield json.dumps({"type": "match", "data": {"path": {"text": app}, "lines": {"bytes": "AAAA"},
                                                    "line_number": 3}}).encode()
        yield _rg_match(foreign, 1, "needle must be ignored (not a candidate)")
        yield b"not json at all"
        yield json.dumps({"type": "summary", "data": {}}).encode()

    monkeypatch.setattr(content, "rg_binary", lambda: "rg-fake")
    monkeypatch.setattr(content, "_rg_stream", fake_stream)
    result = _search(tree, [app, str(tree / "src" / "data.txt")], query="--needle")
    assert result["engine"] == "rg"
    assert [(h["path"], h["line"], h["snippet"]) for h in result["hits"]] == [
        ("workspace/src/app.py", 2, "needle in app")
    ]
    argv = seen[0]
    assert argv[0] == "rg-fake"
    for flag in ("--json", "--fixed-strings", "--ignore-case", "--no-config"):
        assert flag in argv
    assert argv[argv.index("-e") + 1] == "--needle"
    sep = argv.index("--")
    assert argv.index("-e") < sep
    assert argv[sep + 1:] == [app, str(tree / "src" / "data.txt")]
    assert "--max-filesize" in argv


def test_rg_failure_falls_back_to_python_scan(tree, monkeypatch):
    def broken(argv, timeout):
        raise OSError("rg vanished")
        yield b""  # pragma: no cover

    monkeypatch.setattr(content, "rg_binary", lambda: "rg-fake")
    monkeypatch.setattr(content, "_rg_stream", broken)
    result = _search(tree)
    assert result["engine"] == "python"
    assert _tokens(result) == {"workspace/src/app.py", "workspace/docs/notes.md"}


def test_rg_availability_is_detected_once(monkeypatch):
    calls = []
    monkeypatch.undo()  # drop the autouse stub so the real cached function runs
    monkeypatch.delenv("MEMORYMASTER_LOCAL_SEARCH_RG_PATH", raising=False)
    monkeypatch.setattr(content.shutil, "which", lambda name: calls.append(name) or "/x/rg")
    content.rg_binary.cache_clear()
    try:
        assert content.rg_binary() == "/x/rg"
        assert content.rg_binary() == "/x/rg"
        assert calls == ["rg"]
    finally:
        content.rg_binary.cache_clear()


def test_rg_can_be_disabled_by_env(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setenv("MEMORYMASTER_LOCAL_SEARCH_RG_PATH", "none")
    content.rg_binary.cache_clear()
    try:
        assert content.rg_binary() is None
    finally:
        content.rg_binary.cache_clear()


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_real_rg_and_python_engines_agree(tree, monkeypatch):
    paths = [str(tree / "src" / "app.py"), str(tree / "docs" / "notes.md"), str(tree / "src" / "data.txt")]
    (tree / "src" / "multi.txt").write_text("Needle one\nx\nneedle two\r\nNEEDLE three\n", encoding="utf-8")
    paths.append(str(tree / "src" / "multi.txt"))

    py = _search(tree, paths)
    monkeypatch.setattr(content, "rg_binary", lambda: shutil.which("rg"))
    rg = _search(tree, paths)

    assert py["engine"] == "python" and rg["engine"] == "rg"
    key = lambda r: sorted((h["path"], h["line"], h["snippet"]) for h in r["hits"])  # noqa: E731
    assert key(py) == key(rg)
    assert len(key(py)) == 5


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_real_rg_handles_a_query_that_looks_like_a_flag(tree, monkeypatch):
    (tree / "src" / "flag.txt").write_text("use --version here\n", encoding="utf-8")
    monkeypatch.setattr(content, "rg_binary", lambda: shutil.which("rg"))
    result = _search(tree, [str(tree / "src" / "flag.txt")], query="--version")
    assert [h["line"] for h in result["hits"]] == [1]


def test_content_search_module_has_no_write_or_shell_calls():
    src = open(content.__file__, encoding="utf-8").read()
    assert "shell=True" not in src
    assert "os.remove" not in src and "unlink" not in src
    assert 'open(path, "w' not in src and "mode=\"w" not in src
    assert sys.modules[content.__name__] is content


# ------------------------------------------------- review fixes (PR 281)


def test_scan_cap_marks_the_result_truncated(tmp_path, monkeypatch):
    paths = []
    for i in range(5):
        f = tmp_path / f"f{i}.txt"
        f.write_text("needle\n", encoding="utf-8")
        paths.append(str(f))
    monkeypatch.setattr(content, "MAX_FILES_SCANNED", 2)
    result = _search(tmp_path, paths)
    assert result["files_scanned"] == 2
    assert result["truncated"] is True
    # exactly at the cap with nothing left over is not truncated
    monkeypatch.setattr(content, "MAX_FILES_SCANNED", 5)
    assert _search(tmp_path, paths)["truncated"] is False


def test_provider_candidate_cap_marks_the_result_truncated(tmp_path, monkeypatch):
    paths = []
    for i in range(3):
        f = tmp_path / f"f{i}.txt"
        f.write_text("needle\n", encoding="utf-8")
        paths.append(str(f))
    monkeypatch.setattr(content, "HARD_MAX_CANDIDATES", 3)
    provider = FakeProvider(paths)
    result = _search(tmp_path, provider=provider)
    assert provider.calls[0]["limit"] == 3
    assert provider.calls[0]["under"] == os.path.realpath(tmp_path)
    assert result["truncated"] is True


def test_symlinked_root_matches_by_its_resolved_path(tmp_path, monkeypatch):
    """Registered root is an alias; scope and candidates come back resolved."""
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    (real / "sub" / "a.txt").write_text("needle here\n", encoding="utf-8")
    (real / ".ssh").mkdir()
    (real / ".ssh" / "k.txt").write_text("needle in ssh\n", encoding="utf-8")
    alias = tmp_path / "alias"
    real_realpath = os.path.realpath

    def fake_realpath(p, *a, **k):
        text = str(p)
        if os.path.normcase(text).startswith(os.path.normcase(str(alias))):
            text = str(real) + text[len(str(alias)):]
        return real_realpath(text, *a, **k)

    monkeypatch.setattr(os.path, "realpath", fake_realpath)
    roots = [("workspace", str(alias))]
    walked = content.run_content_search(
        FakeProvider([], available=False), roots, name_query="*", content_query="needle",
        workspace=str(alias),
    )
    assert _tokens(walked) == {"workspace/sub/a.txt"}
    resolved = [str(real / "sub" / "a.txt"), str(real / ".ssh" / "k.txt")]
    via_provider = content.run_content_search(
        FakeProvider(resolved), roots, name_query="*", content_query="needle", workspace=str(alias),
    )
    assert _tokens(via_provider) == {"workspace/sub/a.txt"}
    assert str(real) not in json.dumps(via_provider)


def test_extension_match_is_case_insensitive_for_the_file_suffix(tmp_path):
    f = tmp_path / "README.MD"
    f.write_text("needle\n", encoding="utf-8")
    assert _tokens(_search(tmp_path, [str(f)], extension="md")) == {"workspace/README.MD"}


def test_rg_error_exit_raises_instead_of_looking_like_no_results():
    bad = [sys.executable, "-c", "import sys; sys.exit(2)"]
    with pytest.raises(OSError):
        list(content._rg_stream(bad, 10))
    no_match = [sys.executable, "-c", "import sys; sys.exit(1)"]
    assert list(content._rg_stream(no_match, 10)) == []
    ok = [sys.executable, "-c", "print('x')"]
    assert [line.strip() for line in content._rg_stream(ok, 10)] == [b"x"]


def test_rg_killed_by_timeout_is_not_reported_as_an_error():
    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    assert list(content._rg_stream(slow, 0.3)) == []


def test_rg_exiting_2_falls_back_to_the_python_scan(tree, monkeypatch):
    # ``python --json ...`` exits 2 immediately, standing in for a failing rg.
    monkeypatch.setattr(content, "rg_binary", lambda: sys.executable)
    result = _search(tree)
    assert result["engine"] == "python"
    assert _tokens(result) == {"workspace/src/app.py", "workspace/docs/notes.md"}
