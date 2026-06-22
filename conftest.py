"""Root conftest — make the repo root importable during the test session.

A handful of tests (e.g. ``tests/test_connectors.py``) import from the
top-level ``scripts/`` directory, which is intentionally NOT shipped in the
wheel (``pyproject`` scopes ``packages.find`` to ``memorymaster*`` so we don't
pollute site-packages). Under an editable CI install (``pip install -e .``)
that scoping means the repo root is no longer on ``sys.path``, and
``from scripts import ...`` raises ``ModuleNotFoundError`` during collection —
turning the whole CI run red even though every test itself is fine.

Inserting the repo root here (pytest loads the rootdir conftest before any
collection) restores import access to ``scripts/`` for tests without changing
what the published package ships. Idempotent and harmless when the root is
already present.
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
