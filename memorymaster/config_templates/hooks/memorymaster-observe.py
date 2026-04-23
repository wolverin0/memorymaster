#!/usr/bin/env python3
"""Post-session hook: extract decisions and preferences from the session.

Installed as Stop hook. Reads the session summary and ingests important
observations into memorymaster for future recall.
"""

import json
import subprocess
import sys


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    # The Stop hook receives session context
    # Extract any important text to observe
    summary = data.get("summary", "") or data.get("stopReason", "")
    if not summary:
        sys.exit(0)

    try:
        subprocess.run(
            ["memorymaster", "observe", "--text", summary[:2000], "--source", "session-end"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
