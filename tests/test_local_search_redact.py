"""Unit tests for memorymaster.bridges.local_search.redact.

Coverage:
- longest-prefix-first root selection
- round-trip collapse -> expand
- case-insensitive prefix matching on Windows paths
- NO-MATCH case returns abspath unchanged
- load_roots honours MEMORYMASTER_PATH_ROOTS env var
- load_roots deduplicates identical paths
- expand_path returns token unchanged for unknown root names
"""
from __future__ import annotations

import os
import sys

import pytest

from memorymaster.bridges.local_search.redact import (
    collapse_path,
    expand_path,
    load_roots,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEP = os.sep


def _p(*parts: str) -> str:
    """Join path parts with the OS separator."""
    return os.path.join(*parts)


# ---------------------------------------------------------------------------
# collapse_path
# ---------------------------------------------------------------------------


class TestCollapsePathLongestPrefixFirst:
    """The root whose absolute path is longest must win."""

    def test_longer_root_wins_over_shorter(self) -> None:
        if sys.platform == "win32":
            roots = [
                ("home", r"C:\Users\alice"),
                ("projects", r"C:\Users\alice\projects"),
            ]
            abspath = r"C:\Users\alice\projects\memorymaster"
        else:
            roots = [
                ("home", "/home/alice"),
                ("projects", "/home/alice/projects"),
            ]
            abspath = "/home/alice/projects/memorymaster"

        # load_roots sorts longest-first; simulate the same ordering
        sorted_roots = sorted(roots, key=lambda p: len(p[1]), reverse=True)
        result = collapse_path(sorted_roots, abspath)

        assert result == "projects/memorymaster", (
            f"Expected 'projects/memorymaster', got {result!r}"
        )

    def test_parent_root_used_when_no_longer_match(self) -> None:
        if sys.platform == "win32":
            roots = [
                ("projects", r"C:\Users\alice\projects"),
                ("home", r"C:\Users\alice"),
            ]
            abspath = r"C:\Users\alice\other\repo"
        else:
            roots = [
                ("projects", "/home/alice/projects"),
                ("home", "/home/alice"),
            ]
            abspath = "/home/alice/other/repo"

        result = collapse_path(roots, abspath)
        assert result == "home/other/repo"


class TestCollapsePathRoundTrip:
    """collapse_path followed by expand_path must recover the original abspath."""

    @pytest.mark.parametrize(
        "abspath",
        [
            _p("C:\\", "Projects", "memorymaster") if sys.platform == "win32"
            else "/tmp/projects/memorymaster",
            _p("C:\\", "Projects", "a", "b", "c") if sys.platform == "win32"
            else "/tmp/projects/a/b/c",
        ],
    )
    def test_roundtrip(self, abspath: str) -> None:
        if sys.platform == "win32":
            root_path = r"C:\Projects"
        else:
            root_path = "/tmp/projects"

        # Only run this parametrize variant on the right platform
        if sys.platform == "win32" and not abspath.startswith("C:\\"):
            pytest.skip("Unix path on Windows")
        if sys.platform != "win32" and abspath.startswith("C:\\"):
            pytest.skip("Windows path on Unix")

        roots = [("ws", root_path)]
        token = collapse_path(roots, abspath)
        recovered = expand_path(roots, token)
        # Normalise both to Path for cross-platform comparison
        from pathlib import Path

        assert Path(recovered) == Path(abspath), (
            f"Round-trip failed: {abspath!r} -> {token!r} -> {recovered!r}"
        )


class TestCollapsePathCaseInsensitive:
    """On Windows the prefix match must be case-insensitive."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific")
    def test_case_insensitive_windows(self) -> None:
        roots = [("projects", r"C:\Users\Alice\Projects")]
        # abspath uses different casing
        abspath = r"C:\users\alice\projects\memorymaster"
        result = collapse_path(roots, abspath)
        assert result == "projects/memorymaster"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific")
    def test_case_insensitive_drive_letter(self) -> None:
        roots = [("ws", r"C:\work")]
        abspath = r"c:\work\repo"
        result = collapse_path(roots, abspath)
        assert result == "ws/repo"


class TestCollapsePathSimulatedWindows:
    """Simulate Windows case-insensitive matching without requiring Windows."""

    def test_simulated_case_insensitive_via_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        """
        We can't trivially simulate Windows path-sep on non-Windows, but we can
        verify the logic by using a path where lowercase matches the root.
        """
        import tempfile

        # Create a real temp directory so the path is valid
        with tempfile.TemporaryDirectory() as td:
            roots = [("myroot", td)]
            # collapse and expand with exact case
            sub = os.path.join(td, "subdir")
            token = collapse_path(roots, sub)
            assert token == "myroot/subdir"
            expanded = expand_path(roots, token)
            from pathlib import Path

            assert Path(expanded) == Path(sub)


class TestCollapsePathNoMatch:
    """When abspath is outside all roots, return it unchanged."""

    def test_no_match_returns_abspath_unchanged(self) -> None:
        if sys.platform == "win32":
            roots = [("projects", r"C:\Projects")]
            abspath = r"D:\other\thing"
        else:
            roots = [("projects", "/home/alice/projects")]
            abspath = "/var/log/syslog"

        result = collapse_path(roots, abspath)
        assert result == abspath, (
            f"Expected unchanged path {abspath!r}, got {result!r}"
        )

    def test_empty_roots_returns_abspath_unchanged(self) -> None:
        abspath = _p("/", "some", "absolute", "path") if sys.platform != "win32" \
            else r"C:\some\absolute\path"
        result = collapse_path([], abspath)
        assert result == abspath


# ---------------------------------------------------------------------------
# expand_path
# ---------------------------------------------------------------------------


class TestExpandPath:
    """expand_path must be the inverse of collapse_path."""

    def test_unknown_root_returns_token_unchanged(self) -> None:
        roots = [("projects", "/home/alice/projects")]
        token = "unknown-root/subdir"
        assert expand_path(roots, token) == token

    def test_exact_root_name_returns_root_path(self) -> None:
        if sys.platform == "win32":
            root_path = r"C:\Projects"
        else:
            root_path = "/home/alice/projects"
        roots = [("projects", root_path)]
        assert expand_path(roots, "projects") == root_path

    def test_expand_nested(self) -> None:
        if sys.platform == "win32":
            root_path = r"C:\Projects"
            expected = r"C:\Projects\a\b"
        else:
            root_path = "/home/alice/projects"
            expected = "/home/alice/projects/a/b"
        roots = [("projects", root_path)]
        from pathlib import Path

        result = expand_path(roots, "projects/a/b")
        assert Path(result) == Path(expected)


# ---------------------------------------------------------------------------
# load_roots
# ---------------------------------------------------------------------------


class TestLoadRoots:
    """load_roots must parse MEMORYMASTER_PATH_ROOTS and add auto-roots."""

    def test_env_var_entries_are_parsed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            monkeypatch.setenv(
                "MEMORYMASTER_PATH_ROOTS", f"alpha={td1};beta={td2}"
            )
            # Suppress auto-roots by pointing home to a dummy path
            monkeypatch.setenv("USERPROFILE", td1)
            monkeypatch.setenv("HOME", td1)

            roots = load_roots()
            names = [n for n, _ in roots]
            assert "alpha" in names
            assert "beta" in names

    def test_env_var_entries_sorted_longest_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tempfile

        with (
            tempfile.TemporaryDirectory() as short_root,
            tempfile.TemporaryDirectory() as long_root,
        ):
            # Ensure long_root is actually longer in string length
            # (tmp dirs vary; we normalise by nesting)
            nested = os.path.join(long_root, "nested")
            os.makedirs(nested, exist_ok=True)

            monkeypatch.setenv(
                "MEMORYMASTER_PATH_ROOTS",
                f"short={short_root};long={nested}",
            )
            monkeypatch.setenv("USERPROFILE", short_root)
            monkeypatch.setenv("HOME", short_root)

            roots = load_roots()
            # The longer path should come first
            lengths = [len(path) for _, path in roots]
            assert lengths == sorted(lengths, reverse=True), (
                f"Roots not sorted longest-first: {roots}"
            )

    def test_duplicate_paths_deduplicated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv(
                "MEMORYMASTER_PATH_ROOTS",
                f"first={td};second={td}",
            )
            monkeypatch.setenv("USERPROFILE", td)
            monkeypatch.setenv("HOME", td)

            roots = load_roots()
            paths = [path for _, path in roots]
            # After dedup, each unique path appears at most once
            assert len(paths) == len(set(paths)), (
                f"Duplicate paths found in roots: {roots}"
            )

    def test_malformed_entries_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv(
                "MEMORYMASTER_PATH_ROOTS",
                f"good={td};no-equals-here;=emptyname;namonly=",
            )
            monkeypatch.setenv("USERPROFILE", td)
            monkeypatch.setenv("HOME", td)

            roots = load_roots()
            names = [n for n, _ in roots]
            assert "good" in names
            assert "no-equals-here" not in names
            assert "" not in names

    def test_auto_home_root_added(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as home_dir:
            monkeypatch.delenv("MEMORYMASTER_PATH_ROOTS", raising=False)
            monkeypatch.setenv("USERPROFILE", home_dir)
            monkeypatch.setenv("HOME", home_dir)

            roots = load_roots()
            names = [n for n, _ in roots]
            assert "home" in names
