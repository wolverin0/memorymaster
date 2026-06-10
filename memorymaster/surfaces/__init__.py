"""User-facing surfaces: CLI, MCP server, dashboard, operator, setup, metrics.

P2 restructure subpackage. Zero internal fan-in from the rest of the
package — everything imports INTO surfaces, nothing imports FROM it
(except intra-surfaces helpers like ``cli_helpers``).
"""
