"""External-integration bridges: dream bridge, DB sync, Atlas inbox, media, connectors.

P2 restructure subpackage. Modules here are pure leaves (fan-in <= 3) that
bridge MemoryMaster to external systems (Claude Auto Dream, OpenClaw/Hermes
sync, Atlas API, WhatsApp imports, media providers).
"""
