"""Relocated compatibility shim — moved to ``memorymaster.core.session_tracker``.

R7: ``surfaces`` declares zero internal fan-in (see ``surfaces/__init__.py``)
— everything imports INTO surfaces, nothing imports FROM it. ``SessionTracker``
broke that: ``core/service.py`` recall telemetry imported it, making the
package docstring a claim with no enforcement behind it. The module is not a
user-facing surface at all (it is a small SQLite-backed tracker), so it moved
to ``core`` and this alias keeps the surfaces import path working for
``surfaces.mcp_server``, ``surfaces.cli_handlers_curation`` and the deprecated
top-level ``memorymaster.session_tracker`` shim.

``tests/test_extension_boundaries.py`` now enforces the invariant.
"""
import sys as _sys

from memorymaster.core import session_tracker as _new

_sys.modules[__name__] = _new
