from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import zipfile
from collections.abc import Mapping, Sequence
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path


MAX_SBOM_BYTES = 32 * 1024 * 1024
MAX_JSON_DIGITS = 1024
MAX_JSON_DEPTH = 128
MAX_JSON_NODES = 100_000
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_WHEEL_ENTRIES = 10_000
MAX_METADATA_BYTES = 1024 * 1024
SUPPORTED_SPEC_VERSIONS = frozenset({"1.4", "1.5", "1.6"})
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ValueError("invalid arguments")


def _bounded_int(value: str) -> int:
    if len(value.lstrip("-")) > MAX_JSON_DIGITS:
        raise ValueError("integer is too large")
    return int(value)


def _bounded_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("float is not finite")
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _document_shape_bounded(document: object) -> bool:
    pending = [(document, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            return False
        if type(value) is dict:
            pending.extend((item, depth + 1) for pair in value.items() for item in pair)
        elif type(value) is list:
            pending.extend((item, depth + 1) for item in value)
        elif type(value) is float and not math.isfinite(value):
            return False
        elif value is not None and type(value) not in {str, int, float, bool}:
            return False
    return True


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _component_hash(component: Mapping[str, object]) -> str | None:
    hashes = component.get("hashes")
    if not isinstance(hashes, list):
        return None
    for item in hashes:
        if not isinstance(item, Mapping):
            continue
        algorithm = item.get("alg")
        content = item.get("content")
        if (
            isinstance(algorithm, str)
            and algorithm.casefold().replace("-", "") == "sha256"
            and isinstance(content, str)
            and _SHA256_RE.fullmatch(content)
        ):
            return content.casefold()
    return None


def _project_component(document: Mapping[str, object]) -> Mapping[str, object] | None:
    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    component = metadata.get("component")
    return component if isinstance(component, Mapping) else None


def _component_matches(
    component: Mapping[str, object] | None,
    expected_name: str,
    expected_version: str,
) -> bool:
    if component is None:
        return False
    expected_name_folded = _canonical_name(expected_name)
    expected_purl = f"pkg:pypi/{expected_name_folded}@{expected_version}"
    component_type = component.get("type")
    name = component.get("name")
    purl = component.get("purl")
    return (
        isinstance(component_type, str)
        and component_type in {"application", "library"}
        and isinstance(name, str)
        and _canonical_name(name) == expected_name_folded
        and component.get("version") == expected_version
        and isinstance(purl, str)
        and purl.casefold() == expected_purl
    )


def validate_sbom_document(
    document: object,
    *,
    expected_name: str,
    expected_version: str,
    expected_artifact_sha256: str | None = None,
) -> tuple[str, ...]:
    if not isinstance(document, Mapping):
        return ("document_not_object",)
    if not isinstance(expected_name, str) or not isinstance(expected_version, str):
        return ("invalid_expected_identity",)
    errors: list[str] = []
    if document.get("bomFormat") != "CycloneDX":
        errors.append("invalid_bom_format")
    spec_version = document.get("specVersion")
    if not isinstance(spec_version, str) or spec_version not in SUPPORTED_SPEC_VERSIONS:
        errors.append("invalid_spec_version")
    expected_schemas = {
        f"http://cyclonedx.org/schema/bom-{spec_version}.schema.json",
        f"https://cyclonedx.org/schema/bom-{spec_version}.schema.json",
    }
    schema = document.get("$schema")
    if not isinstance(schema, str) or schema not in expected_schemas:
        errors.append("invalid_schema")
    components = document.get("components")
    if not isinstance(components, list) or not components:
        errors.append("components_missing")
    elif any(not isinstance(item, Mapping) or not isinstance(item.get("type"), str) for item in components):
        errors.append("components_invalid")
    component = _project_component(document)
    if not _component_matches(component, expected_name, expected_version):
        errors.append("project_component_missing")
    else:
        component_hash = _component_hash(component)
        if component_hash is None:
            errors.append("artifact_hash_missing")
        elif expected_artifact_sha256 is not None and component_hash != expected_artifact_sha256.casefold():
            errors.append("artifact_hash_mismatch")
    return tuple(errors)


def _load_document(path: Path) -> tuple[object | None, str | None]:
    try:
        with path.open("rb") as handle:
            payload = handle.read(MAX_SBOM_BYTES + 1)
    except (AttributeError, OSError):
        return None, "file_unavailable"
    if len(payload) > MAX_SBOM_BYTES:
        return None, "file_too_large"
    try:
        document = json.loads(
            payload.decode("utf-8"),
            parse_int=_bounded_int,
            parse_float=_bounded_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        return None, "invalid_json"
    if not _document_shape_bounded(document):
        return None, "document_too_complex"
    return document, None


def _wheel_identity_and_sha256(path: Path) -> tuple[str, str, str] | None:
    if path.suffix.casefold() != ".whl":
        return None
    try:
        with path.open("rb") as handle:
            file_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > MAX_ARTIFACT_BYTES:
                return None
            digest = hashlib.sha256()
            bytes_read = 0
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                bytes_read += len(chunk)
                if bytes_read > MAX_ARTIFACT_BYTES:
                    return None
                digest.update(chunk)
            handle.seek(0)
            with zipfile.ZipFile(handle) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_WHEEL_ENTRIES:
                    return None
                metadata_infos = [
                    info
                    for info in infos
                    if info.filename.endswith(".dist-info/METADATA")
                    and len(info.filename.split("/")) == 2
                    and info.file_size <= MAX_METADATA_BYTES
                ]
                if len(metadata_infos) != 1:
                    return None
                with archive.open(metadata_infos[0]) as metadata_handle:
                    payload = metadata_handle.read(MAX_METADATA_BYTES + 1)
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile):
        return None
    if len(payload) > MAX_METADATA_BYTES:
        return None
    metadata = BytesParser(policy=compat32).parsebytes(payload)
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not isinstance(name, str) or not isinstance(version, str):
        return None
    return _canonical_name(name.strip()), version.strip(), digest.hexdigest()


def validate_sbom_file(
    path: Path,
    *,
    expected_name: str,
    expected_version: str,
    artifact_path: Path | None = None,
) -> tuple[str, ...]:
    if not isinstance(expected_name, str) or not isinstance(expected_version, str):
        return ("invalid_expected_identity",)
    document, load_error = _load_document(path)
    if load_error is not None:
        return (load_error,)
    if artifact_path is None:
        return validate_sbom_document(
            document,
            expected_name=expected_name,
            expected_version=expected_version,
        )
    artifact = _wheel_identity_and_sha256(artifact_path)
    if artifact is None:
        return ("artifact_invalid",)
    artifact_name, artifact_version, artifact_sha256 = artifact
    if artifact_name != _canonical_name(expected_name) or artifact_version != expected_version:
        return ("artifact_identity_mismatch",)
    return validate_sbom_document(
        document,
        expected_name=expected_name,
        expected_version=expected_version,
        expected_artifact_sha256=artifact_sha256,
    )


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description="Validate a release CycloneDX SBOM.")
    parser.add_argument("--sbom", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--expected-name", required=True)
    parser.add_argument("--expected-version", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except ValueError:
        print(json.dumps({"ok": False, "errors": ["invalid_arguments"]}))
        return 2
    errors = validate_sbom_file(
        args.sbom,
        expected_name=args.expected_name,
        expected_version=args.expected_version,
        artifact_path=args.artifact,
    )
    print(json.dumps({"ok": not errors, "errors": list(errors)}))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
