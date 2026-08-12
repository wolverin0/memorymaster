"""Read-only Hermes CLI status command for the MemoryMaster provider."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import ProviderConfig
from .installer import install_plugin
from .outbox import DurableOutbox


def _status(args) -> None:
    home = Path(getattr(args, "hermes_home", "") or Path.home() / ".hermes")
    config = ProviderConfig.load(home)
    result = {
        "configured": bool(config.endpoint and config.token),
        "endpoint": config.endpoint,
        "outbox": str(config.outbox_path),
        "replica_read_only": bool(config.replica_db_path),
    }
    if config.outbox_path.exists():
        outbox = DurableOutbox(
            config.outbox_path,
            max_pending=config.max_pending,
            max_pending_bytes=config.max_pending_bytes,
        )
        try:
            result["queue"] = outbox.counts()
        finally:
            outbox.close()
    print(json.dumps(result, indent=2, sort_keys=True))


def register_cli(subparser) -> None:
    """Register ``hermes memorymaster status`` without mutating provider state."""
    subcommands = subparser.add_subparsers(dest="memorymaster_command")
    status = subcommands.add_parser("status", help="Show config and durable outbox health")
    status.add_argument("--hermes-home", default="")
    status.set_defaults(func=_status)


def _install(args) -> None:
    home = Path(args.hermes_home or Path.home() / ".hermes")
    result = install_plugin(home, apply=args.apply, force=args.force)
    print(json.dumps(result, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install or inspect Hermes MemoryMaster")
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="Preview/install Hermes provider shim")
    install.add_argument("--hermes-home", default="")
    install.add_argument("--apply", action="store_true")
    install.add_argument("--force", action="store_true")
    install.set_defaults(func=_install)
    status = commands.add_parser("status", help="Show provider and outbox health")
    status.add_argument("--hermes-home", default="")
    status.set_defaults(func=_status)
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
