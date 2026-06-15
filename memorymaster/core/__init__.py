"""Core domain: models, service facade, lifecycle, config, security, providers.

P2 restructure subpackage. Highest fan-in modules in the package (models 38,
security 14, service 12) — every other subpackage imports from here, so core
moved last (Phase 7) and keeps permanent shims at the old root paths for
externally installed hooks and scripts.
"""
