"""User-facing surfaces: CLI, MCP server, dashboard, operator, setup, metrics.

P2 restructure subpackage. Zero internal fan-in from the rest of the
package — everything imports INTO surfaces, nothing imports FROM it
(except intra-surfaces helpers like ``cli_helpers``).

Enforced by ``tests/test_extension_boundaries.py``
(``test_core_layers_do_not_import_user_facing_surfaces``) — not by this
docstring. Lazy/function-local imports count: the guard is an AST sweep.
"""
