from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


PLUGIN_SRC = Path(__file__).parents[1] / "integrations" / "hermes-memorymaster" / "src"
if str(PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SRC))

from hermes_memorymaster.config import ProviderConfig  # noqa: E402


def test_provider_config_defaults_to_live_injection_timeout(tmp_path: Path) -> None:
    config = ProviderConfig.load(tmp_path)
    assert config.request_timeout_seconds == 0.35
    assert config.delivery_timeout_seconds == 5.0


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://memory.invalid/mcp",
        "http://user:password@memory.invalid/mcp",
        "http://memory.invalid/mcp?token=secret",
        "http://memory.invalid/mcp#fragment",
    ],
)
def test_provider_config_rejects_secret_bearing_or_unsupported_urls(
    tmp_path: Path, endpoint: str
) -> None:
    (tmp_path / "memorymaster-provider.json").write_text(
        json.dumps({"endpoint": endpoint}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="endpoint"):
        ProviderConfig.load(tmp_path)
