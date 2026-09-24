"""Governed TypeSafe Jev decisions with an append-only decision ledger.

The package is inert by default (``MEMORYMASTER_JEV_MODE`` defaults to ``off``).
Importing it never imports an HTTP client, opens a database or reads a key; those
happen only inside :func:`memorymaster.decisions.engine.decide` for an enabled
surface.  SQLite stays authoritative: Jev only orders/labels IDs that code has
already authorized, and every live action is reversible or a proposal.

Modules: ``config`` (env), ``credentials`` (key lookup), ``egress`` (single
redactor), ``transport`` (in-process, stdlib ``http.client``), ``questions`` (versioned registry),
``ledger`` (sidecar SQLite), ``policy`` (exploration/breaker/budget), ``engine``
(orchestration), ``outcomes`` (joiners), ``metrics`` and ``export``.
"""
from __future__ import annotations

__all__ = ["SURFACES"]

from memorymaster.decisions.config import SURFACES
