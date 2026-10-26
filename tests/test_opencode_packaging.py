"""Plugin templates must survive Python package installation."""
from pathlib import Path
import tomllib

from setuptools import Distribution


def test_packaged_data_includes_both_plugins_and_runtime():
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as stream:
        metadata = tomllib.load(stream)
    dist = Distribution({"packages": ["memorymaster"],
                         "package_data": metadata["tool"]["setuptools"]["package-data"]})
    builder = dist.get_command_obj("build_py")
    builder.ensure_finalized()
    builder.get_data_files()
    packaged = {str(Path(source, name)).replace("\\", "/")
                for _package, source, _destination, names in builder.data_files for name in names}
    assert {"memorymaster/config_templates/opencode/" + name for name in (
        "plugin-v1.mjs", "plugin-v2.mjs", "runtime.mjs",
    )} <= packaged
