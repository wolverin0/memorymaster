"""Shim for backward compat — calls memorymaster.setup_hooks.main().

New users should run `memorymaster-setup` after `pip install memorymaster`.
This shim exists so that `python scripts/setup-hooks.py` still works inside
a cloned repo checkout.
"""
from memorymaster.setup_hooks import main

if __name__ == "__main__":
    main()
