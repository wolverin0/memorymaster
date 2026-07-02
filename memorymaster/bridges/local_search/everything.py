"""EverythingProvider — Windows Everything (ES.exe) search backend.

Uses the Everything command-line client (ES.exe / es.exe) via subprocess to
perform fast filename/path lookups across the machine.  All I/O is read-only;
no writes to the filesystem are performed.

Configuration (env vars, all optional):
    MEMORYMASTER_EVERYTHING_ES_PATH:  Full path to ES.exe.
                                      If unset, provider is always degraded.
    MEMORYMASTER_EVERYTHING_TIMEOUT:  Subprocess timeout in seconds (float).
                                      Default: 5.0.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import ClassVar

from memorymaster.bridges.local_search.provider import PathHit

# Splits a query into ES terms: whitespace-separated tokens, but a double-quoted
# span (optionally glued to a prefix like ext: or wfn:) stays one term.
_TERM_SPLIT_RE = re.compile(r'[^\s"]*"[^"]*"[^\s"]*|[^\s"]+')

__all__ = ["EverythingProvider"]

_LOG = logging.getLogger(__name__)

_ENV_ES_PATH = "MEMORYMASTER_EVERYTHING_ES_PATH"
_ENV_TIMEOUT = "MEMORYMASTER_EVERYTHING_TIMEOUT"
_DEFAULT_TIMEOUT = 5.0


def _timeout() -> float:
    """Read the subprocess timeout from env; fall back to default."""
    raw = os.environ.get(_ENV_TIMEOUT, "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT
    try:
        value = float(raw)
        return value if value > 0 else _DEFAULT_TIMEOUT
    except ValueError:
        _LOG.warning("Invalid %s=%r — using default %.1fs", _ENV_TIMEOUT, raw, _DEFAULT_TIMEOUT)
        return _DEFAULT_TIMEOUT


def _es_path() -> str | None:
    """Return the ES.exe path from env, or None if unset."""
    return os.environ.get(_ENV_ES_PATH, "").strip() or None


def _parse_es_output(stdout: str, kind_filter: str) -> list[PathHit]:
    """Parse ES.exe plain-path output (one full path per line) into PathHits.

    When ``kind_filter`` is ``"dir"`` or ``"file"`` the query already passed
    ``/ad`` or ``/a-d`` to ES, so every line is known to be that kind — we
    trust it rather than re-guessing (a folder named ``foo.v2`` would be
    mis-dropped by a dot heuristic). For ``"any"`` we fall back to the
    extension heuristic purely to label the kind.
    """
    hits: list[PathHit] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip().strip('"')  # -csv/quoted output is tolerated
        if not line:
            continue
        if kind_filter in ("dir", "file"):
            kind = kind_filter
        else:
            last_segment = line.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            kind = "dir" if "." not in last_segment else "file"
        hits.append(PathHit(path=line, kind=kind, size=None, modified=None))
    return hits


class EverythingProvider:
    """LocalSearchProvider backed by Everything's ES.exe command-line client.

    Gracefully degrades on any error: timeout, missing binary, non-zero exit,
    or OS error all result in ``available()`` returning ``False`` and
    ``search()`` returning ``[]``.  Callers should check ``available()`` before
    presenting results so the UI can surface a ``degraded`` flag.
    """

    # Cache the version-probe result for the lifetime of this instance so
    # ``available()`` can be called cheaply in hot paths.
    _available: bool | None

    # Sentinel: version probe has not run yet.
    _NOT_PROBED: ClassVar[object] = object()

    def __init__(self) -> None:
        self._available = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """Return True iff ES.exe is configured, the file exists, and responds."""
        if self._available is not None:
            return self._available
        self._available = self._probe()
        return self._available

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        kind: str = "any",
        whole_name: bool = False,
    ) -> list[PathHit]:
        """Search using ES.exe; return ``[]`` on any failure (graceful degrade)."""
        if not self.available():
            return []
        es = _es_path()
        if not es:
            return []
        # ES switches (verified against ES 1.1.0.27): /ad = folders only,
        # /a-d = files only, -n = max results, wfn:<text> = whole-filename
        # match.
        args: list[str] = [es, "-n", str(limit)]
        if kind == "dir":
            args.append("/ad")
        elif kind == "file":
            args.append("/a-d")
        if whole_name:
            # One argv: the phrase must stay inside the wfn: function.
            args.append(f'wfn:"{query}"' if " " in query else f"wfn:{query}")
        else:
            # Each TERM must be its own argv item. A multi-word query passed as
            # ONE argv gets quoted by Windows arg-joining, so ES sees a single
            # literal phrase and returns 0 hits for every multi-word query
            # (verified against ES 1.1.0.27: `path:projects jsonl` split into
            # two argv items -> hits; the same string as one argv -> 0).
            # User-quoted phrases ("a b") are kept as one term so ES still
            # treats them as a phrase.
            args.extend(_TERM_SPLIT_RE.findall(query) or [query])
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=_timeout(),
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            _LOG.warning("EverythingProvider: ES.exe timed out for query %r", query)
            self._available = False
            return []
        except FileNotFoundError:
            _LOG.warning("EverythingProvider: ES.exe not found at %r", es)
            self._available = False
            return []
        except OSError as exc:
            _LOG.warning("EverythingProvider: OS error running ES.exe: %s", exc)
            self._available = False
            return []

        if result.returncode != 0:
            _LOG.warning(
                "EverythingProvider: ES.exe exited %d for query %r; stderr=%r",
                result.returncode,
                query,
                result.stderr[:200] if result.stderr else "",
            )
            return []

        return _parse_es_output(result.stdout, kind)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _probe(self) -> bool:
        """Run ``es -version`` to verify the binary is usable."""
        es = _es_path()
        if not es:
            return False
        if not os.path.isfile(es):
            _LOG.debug("EverythingProvider: ES.exe path does not exist: %r", es)
            return False
        try:
            result = subprocess.run(
                [es, "-version"],
                capture_output=True,
                text=True,
                timeout=_timeout(),
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            _LOG.warning("EverythingProvider: version probe timed out")
            return False
        except FileNotFoundError:
            _LOG.warning("EverythingProvider: ES.exe not found during probe: %r", es)
            return False
        except OSError as exc:
            _LOG.warning("EverythingProvider: OS error during probe: %s", exc)
            return False

        ok = result.returncode == 0
        if not ok:
            _LOG.warning(
                "EverythingProvider: version probe failed (exit %d)", result.returncode
            )
        return ok
