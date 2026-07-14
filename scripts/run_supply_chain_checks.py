from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


Runner = Callable[..., subprocess.CompletedProcess[str]]
Resolver = Callable[[str], str | None]
TRUSTED_GITLEAKS_CONFIG = 'title = "MemoryMaster mandatory built-in Gitleaks policy"\n\n[extend]\nuseDefault = true\n'
TRUSTED_REPO_ROOT = Path(__file__).resolve().parents[1]
TRUSTED_VALIDATOR_PATH = Path(__file__).with_name("validate_sbom.py").resolve()
TRUSTED_PROJECT_NAME = "memorymaster"
_GITLEAKS_CONFIG_PATH = "<trusted-gitleaks-config>"
_GITLEAKS_IGNORE_PATH = "<reviewed-gitleaks-fingerprints>"
_PIP_REQUIREMENTS_PATH = "<trusted-release-requirements>"
_GITLEAKS_EXECUTABLE = "<approved-gitleaks>"
_DOCKER_EXECUTABLE = "<approved-docker>"
MAX_LOCAL_IMAGES = 3
MAX_PROJECT_FILE_BYTES = 1024 * 1024
MAX_EVIDENCE_FILE_BYTES = 512 * 1024 * 1024
MAX_PLAN_SECONDS = 3600
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
_REF_RE = re.compile(r"refs/[A-Za-z0-9._/-]+\Z")
_RELEASE_EXTRAS = ("mcp", "security")
_GITLEAKS_REVIEW_FILE = ".gitleaks-reviewed-fingerprints"
_GITLEAKS_FINGERPRINT_RE = re.compile(
    r"(?P<commit>[0-9a-f]{40}):(?P<path>tests/[A-Za-z0-9_./-]+):"
    r"(?P<rule>[a-z0-9][a-z0-9-]*):(?P<line>[1-9][0-9]*)\Z"
)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ValueError("invalid arguments")


def _redacted_argv(argv: tuple[str, ...]) -> list[str]:
    redacted: list[str] = []
    for value in argv:
        if value.startswith("local://"):
            redacted.append("local://<redacted-image>")
        elif Path(value).is_absolute():
            redacted.append("<absolute-path>")
        else:
            redacted.append(value)
    return redacted


@dataclass(frozen=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: int

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": _redacted_argv(self.argv),
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    failure_kind: str | None = None
    returncode: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "failure_kind": self.failure_kind,
            "returncode": self.returncode,
        }


@dataclass(frozen=True)
class ExecutionEvidence:
    repository_commit: str
    release_artifact_sha256: str
    sbom_sha256: str
    image_ids: tuple[str, ...]
    tool_sha256: dict[str, str]
    tool_versions: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_commit": self.repository_commit,
            "release_artifact_sha256": self.release_artifact_sha256,
            "sbom_sha256": self.sbom_sha256,
            "image_ids": list(self.image_ids),
            "tool_sha256": dict(self.tool_sha256),
            "tool_versions": dict(self.tool_versions),
        }


@dataclass(frozen=True)
class ExecutionReport:
    ok: bool
    results: tuple[CheckResult, ...]
    evidence: ExecutionEvidence | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.ok,
            "results": [item.to_dict() for item in self.results],
        }
        if self.evidence is not None:
            payload["evidence"] = self.evidence.to_dict()
        return payload


def _load_pyproject(repo_root: Path) -> dict[str, object]:
    path = repo_root / "pyproject.toml"
    try:
        with path.open("rb") as handle:
            payload = handle.read(MAX_PROJECT_FILE_BYTES + 1)
        if len(payload) > MAX_PROJECT_FILE_BYTES:
            raise ValueError
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[import-not-found]
        document = tomllib.loads(payload.decode("utf-8"))
    except (ImportError, OSError, UnicodeError, ValueError):
        raise ValueError("project identity unavailable") from None
    if not isinstance(document, dict):
        raise ValueError("project identity unavailable")
    return document


def _load_project_identity(repo_root: Path) -> tuple[str, str]:
    project = _load_pyproject(repo_root).get("project")
    name = project.get("name") if isinstance(project, dict) else None
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(name, str) or not isinstance(version, str):
        raise ValueError("project identity unavailable")
    normalized_name = re.sub(r"[-_.]+", "-", name.strip()).casefold()
    if normalized_name != TRUSTED_PROJECT_NAME or not version.strip():
        raise ValueError("unexpected project identity")
    if len(version) > 128 or any(char.isspace() for char in version):
        raise ValueError("invalid project version")
    return TRUSTED_PROJECT_NAME, version


def _release_requirements(repo_root: Path) -> tuple[str, ...]:
    project = _load_pyproject(repo_root).get("project")
    if not isinstance(project, dict):
        raise ValueError("project dependencies unavailable")
    dependencies = project.get("dependencies")
    optional = project.get("optional-dependencies")
    if not isinstance(dependencies, list) or not isinstance(optional, dict):
        raise ValueError("project dependencies unavailable")
    requirements = list(dependencies)
    for extra in _RELEASE_EXTRAS:
        values = optional.get(extra)
        if not isinstance(values, list):
            raise ValueError("release extra unavailable")
        requirements.extend(values)
    if any(
        not isinstance(value, str) or not value.strip() or len(value) > 512 or any(char in value for char in "\r\n")
        for value in requirements
    ):
        raise ValueError("invalid release dependency")
    return tuple(dict.fromkeys(value.strip() for value in requirements))


def _reviewed_gitleaks_fingerprints(repo_root: Path) -> str:
    payload = _read_limited(repo_root / _GITLEAKS_REVIEW_FILE, 64 * 1024)
    entries = payload.splitlines()
    if not entries or len(entries) != len(set(entries)):
        raise ValueError("reviewed Gitleaks fingerprints unavailable")
    for entry in entries:
        match = _GITLEAKS_FINGERPRINT_RE.fullmatch(entry)
        if match is None:
            raise ValueError("reviewed Gitleaks fingerprints unavailable")
        path_parts = match.group("path").split("/")
        if any(part in {"", ".", ".."} for part in path_parts):
            raise ValueError("reviewed Gitleaks fingerprints unavailable")
    return "\n".join(entries) + "\n"


def _inside_repo(repo_root: Path, path: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        raise ValueError(f"{label} must stay inside repository") from None
    return resolved


def _local_image_uri(image: str) -> str:
    normalized = image.strip()
    if normalized.startswith("local://"):
        normalized = normalized[len("local://") :]
    if not _IMAGE_ID_RE.fullmatch(normalized):
        raise ValueError("local image must use an immutable sha256 image ID")
    return f"local://{normalized}"


def _image_check_name(index: int) -> str:
    return f"docker_scout_{index}"


def _gitleaks_command(repo_root: Path) -> CommandSpec:
    return CommandSpec(
        "gitleaks_history",
        (
            _GITLEAKS_EXECUTABLE,
            "git",
            "--log-opts=--all",
            "--config",
            _GITLEAKS_CONFIG_PATH,
            "--gitleaks-ignore-path",
            _GITLEAKS_IGNORE_PATH,
            "--ignore-gitleaks-allow",
            "--redact=100",
            "--no-banner",
            "--exit-code",
            "1",
            str(repo_root),
        ),
        300,
    )


def _python_commands(
    repo_root: Path,
    sbom_path: Path,
    artifact_path: Path,
    expected_name: str,
    expected_version: str,
) -> tuple[CommandSpec, ...]:
    return (
        CommandSpec(
            "pip_audit_project",
            (
                sys.executable,
                "-I",
                "-m",
                "pip_audit",
                "--strict",
                "--vulnerability-service",
                "osv",
                "--progress-spinner",
                "off",
                str(repo_root),
            ),
            900,
        ),
        CommandSpec(
            "pip_audit_release_extras",
            (
                sys.executable,
                "-I",
                "-m",
                "pip_audit",
                "--strict",
                "--vulnerability-service",
                "osv",
                "--progress-spinner",
                "off",
                "--requirement",
                _PIP_REQUIREMENTS_PATH,
            ),
            900,
        ),
        CommandSpec(
            "validate_sbom",
            (
                sys.executable,
                "-I",
                str(TRUSTED_VALIDATOR_PATH),
                "--sbom",
                str(sbom_path),
                "--artifact",
                str(artifact_path),
                "--expected-name",
                expected_name,
                "--expected-version",
                expected_version,
            ),
            30,
        ),
    )


def _base_commands(
    repo_root: Path,
    sbom_path: Path,
    artifact_path: Path,
    expected_name: str,
    expected_version: str,
) -> tuple[CommandSpec, ...]:
    return (
        _gitleaks_command(repo_root),
        *_python_commands(repo_root, sbom_path, artifact_path, expected_name, expected_version),
    )


def _image_command(image_uri: str, index: int) -> CommandSpec:
    return CommandSpec(
        _image_check_name(index),
        (
            _DOCKER_EXECUTABLE,
            "scout",
            "cves",
            "--only-severity",
            "high,critical",
            "--exit-code",
            image_uri,
        ),
        600,
    )


def build_command_plan(
    *,
    repo_root: Path,
    sbom_path: Path,
    local_images: Sequence[str],
    expected_name: str | None = None,
    expected_version: str | None = None,
    release_artifact: Path | None = None,
) -> tuple[CommandSpec, ...]:
    root = repo_root.resolve(strict=True)
    output = _inside_repo(root, sbom_path, "SBOM")
    name, version = _load_project_identity(root)
    if expected_name is not None and expected_name != name:
        raise ValueError("expected name does not match project")
    if expected_version is not None and expected_version != version:
        raise ValueError("expected version does not match project")
    artifact = release_artifact or root / f"{name}-{version}-py3-none-any.whl"
    artifact = _inside_repo(root, artifact, "release artifact")
    if isinstance(local_images, (str, bytes)) or len(local_images) > MAX_LOCAL_IMAGES:
        raise ValueError("local image count exceeds limit")
    images = tuple(_local_image_uri(image) for image in local_images)
    if not images:
        raise ValueError("at least one local image is required")
    commands = list(_base_commands(root, output, artifact, name, version))
    commands.extend(_image_command(image, index) for index, image in enumerate(images, start=1))
    return tuple(commands)


def _run_one(
    spec: CommandSpec,
    runner: Runner,
    *,
    environment: dict[str, str],
    working_directory: Path,
    timeout_seconds: float,
) -> CheckResult:
    try:
        completed = runner(
            spec.argv,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            shell=False,
            env=environment,
            cwd=str(working_directory),
        )
    except subprocess.TimeoutExpired:
        return CheckResult(spec.name, "failed", "timeout")
    except (FileNotFoundError, OSError):
        return CheckResult(spec.name, "failed", "tool_unavailable")
    except Exception:
        return CheckResult(spec.name, "failed", "execution_error")
    if completed.returncode != 0:
        return CheckResult(spec.name, "failed", "nonzero_exit", completed.returncode)
    return CheckResult(spec.name, "passed", returncode=0)


def _materialize_policy(plan: Sequence[CommandSpec], directory: Path) -> tuple[CommandSpec, ...]:
    config_path = directory / "gitleaks.toml"
    ignore_path = directory / "gitleaksignore"
    requirements_path = directory / "release-requirements.txt"
    gitleaks = next((spec for spec in plan if spec.name == "gitleaks_history"), None)
    if gitleaks is None:
        raise ValueError("gitleaks command unavailable")
    requirements = _release_requirements(Path(gitleaks.argv[-1]))
    config_path.write_text(TRUSTED_GITLEAKS_CONFIG, encoding="utf-8")
    reviewed_fingerprints = _reviewed_gitleaks_fingerprints(Path(gitleaks.argv[-1]))
    ignore_path.write_text(reviewed_fingerprints, encoding="utf-8")
    requirements_path.write_text("\n".join(requirements) + "\n", encoding="utf-8")
    (directory / "pip.conf").write_text(
        "[global]\ndisable-pip-version-check = true\n",
        encoding="utf-8",
    )
    replacements = {
        _GITLEAKS_CONFIG_PATH: str(config_path),
        _GITLEAKS_IGNORE_PATH: str(ignore_path),
        _PIP_REQUIREMENTS_PATH: str(requirements_path),
    }
    return tuple(replace(spec, argv=tuple(replacements.get(value, value) for value in spec.argv)) for spec in plan)


def _read_limited(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as handle:
            payload = handle.read(limit + 1)
    except OSError:
        raise ValueError("evidence unavailable") from None
    if len(payload) > limit:
        raise ValueError("evidence unavailable")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("evidence unavailable") from None


def _git_directory(repo_root: Path) -> Path:
    marker = repo_root / ".git"
    if marker.is_dir():
        return marker.resolve(strict=True)
    value = _read_limited(marker, 4096).strip()
    if not value.casefold().startswith("gitdir:"):
        raise ValueError("git metadata unavailable")
    raw_path = value.split(":", 1)[1].strip()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = marker.parent / candidate
    try:
        return candidate.resolve(strict=True)
    except OSError:
        raise ValueError("git metadata unavailable") from None


def _packed_ref(common_directory: Path, ref_name: str) -> str | None:
    path = common_directory / "packed-refs"
    if not path.exists():
        return None
    for line in _read_limited(path, 16 * 1024 * 1024).splitlines():
        if not line or line.startswith(("#", "^")):
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1] == ref_name and _COMMIT_RE.fullmatch(parts[0]):
            return parts[0].lower()
    return None


def _repository_commit(repo_root: Path) -> str:
    git_directory = _git_directory(repo_root)
    head = _read_limited(git_directory / "HEAD", 4096).strip()
    if _COMMIT_RE.fullmatch(head):
        return head.lower()
    if not head.startswith("ref: "):
        raise ValueError("git head unavailable")
    ref_name = head[5:]
    if not _REF_RE.fullmatch(ref_name) or ".." in ref_name or "//" in ref_name:
        raise ValueError("git head unavailable")
    common_directory = git_directory
    common_marker = git_directory / "commondir"
    if common_marker.exists():
        common_directory = (git_directory / _read_limited(common_marker, 4096).strip()).resolve(strict=True)
    for root in (git_directory, common_directory):
        ref_path = root / Path(ref_name)
        if ref_path.is_file():
            value = _read_limited(ref_path, 4096).strip()
            if _COMMIT_RE.fullmatch(value):
                return value.lower()
    packed = _packed_ref(common_directory, ref_name)
    if packed is None:
        raise ValueError("git head unavailable")
    return packed


def _sha256_file(path: Path) -> str:
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or resolved.stat().st_size > MAX_EVIDENCE_FILE_BYTES:
            raise ValueError
        digest = hashlib.sha256()
        bytes_read = 0
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                bytes_read += len(chunk)
                if bytes_read > MAX_EVIDENCE_FILE_BYTES:
                    raise ValueError
                digest.update(chunk)
    except (OSError, ValueError):
        raise ValueError("evidence file unavailable") from None
    return digest.hexdigest()


def _plan_context(plan: Sequence[CommandSpec]) -> tuple[Path, Path, Path, tuple[str, ...]]:
    commands = {spec.name: spec for spec in plan}
    try:
        repository = Path(commands["gitleaks_history"].argv[-1]).resolve(strict=True)
        validation = commands["validate_sbom"].argv
        sbom_path = Path(validation[validation.index("--sbom") + 1])
        artifact_path = Path(validation[validation.index("--artifact") + 1])
        image_ids = tuple(
            spec.argv[-1].removeprefix("local://") for spec in plan if spec.name.startswith("docker_scout_")
        )
    except (KeyError, OSError, ValueError):
        raise ValueError("invalid execution plan") from None
    return repository, sbom_path, artifact_path, image_ids


def _trusted_executable(name: str, repo_root: Path, resolver: Resolver) -> Path:
    raw_path = resolver(name)
    if not raw_path:
        raise ValueError("approved tool unavailable")
    try:
        path = Path(raw_path).resolve(strict=True)
    except OSError:
        raise ValueError("approved tool unavailable") from None
    if not path.is_file():
        raise ValueError("approved tool unavailable")
    try:
        path.relative_to(repo_root)
    except ValueError:
        return path
    except OSError:
        raise ValueError("approved tool unavailable") from None
    raise ValueError("repository-local executable refused")


def _materialize_tools(
    plan: Sequence[CommandSpec], repo_root: Path, resolver: Resolver
) -> tuple[tuple[CommandSpec, ...], dict[str, Path]]:
    tools = {
        "gitleaks": _trusted_executable("gitleaks", repo_root, resolver),
        "docker": _trusted_executable("docker", repo_root, resolver),
        "git": _trusted_executable("git", repo_root, resolver),
        "python": Path(sys.executable).resolve(strict=True),
    }
    replacements = {
        _GITLEAKS_EXECUTABLE: str(tools["gitleaks"]),
        _DOCKER_EXECUTABLE: str(tools["docker"]),
    }
    materialized = tuple(
        replace(spec, argv=tuple(replacements.get(value, value) for value in spec.argv)) for spec in plan
    )
    return materialized, tools


def _filtered_path(repo_root: Path, tools: dict[str, Path]) -> str:
    entries = [str(tools[name].parent) for name in ("gitleaks", "docker", "git") if name in tools]
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    if system_root:
        entries.append(str(Path(system_root) / "System32"))
    return os.pathsep.join(dict.fromkeys(entries))


def _sterile_environment(directory: Path, repo_root: Path, tools: dict[str, Path]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    environment.update(
        {
            "PATH": _filtered_path(repo_root, tools),
            "HOME": str(directory),
            "USERPROFILE": str(directory),
            "TEMP": str(directory),
            "TMP": str(directory),
            "TMPDIR": str(directory),
            "NO_COLOR": "1",
            "PIP_CONFIG_FILE": str(directory / "pip.conf"),
        }
    )
    return environment


def _collect_evidence(
    repo_root: Path,
    sbom_path: Path,
    artifact_path: Path,
    image_ids: tuple[str, ...],
    tools: dict[str, Path],
) -> ExecutionEvidence:
    try:
        pip_audit_version = importlib.metadata.version("pip-audit")
    except importlib.metadata.PackageNotFoundError:
        raise ValueError("pip-audit unavailable") from None
    return ExecutionEvidence(
        repository_commit=_repository_commit(repo_root),
        release_artifact_sha256=_sha256_file(artifact_path),
        sbom_sha256=_sha256_file(sbom_path),
        image_ids=image_ids,
        tool_sha256={name: _sha256_file(path) for name, path in tools.items()},
        tool_versions={"pip-audit": pip_audit_version, "python": sys.version.split()[0]},
    )


def _execute(
    plan: Sequence[CommandSpec],
    runner: Runner,
    *,
    environment: dict[str, str],
    working_directory: Path,
) -> ExecutionReport:
    results: list[CheckResult] = []
    deadline = time.monotonic() + MAX_PLAN_SECONDS
    for spec in plan:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append(CheckResult(spec.name, "failed", "global_deadline"))
            return ExecutionReport(False, tuple(results))
        result = _run_one(
            spec,
            runner,
            environment=environment,
            working_directory=working_directory,
            timeout_seconds=min(float(spec.timeout_seconds), remaining),
        )
        results.append(result)
        if result.status != "passed":
            return ExecutionReport(False, tuple(results))
    return ExecutionReport(True, tuple(results))


def execute_plan(
    plan: Sequence[CommandSpec],
    *,
    runner: Runner = subprocess.run,
    resolver: Resolver = shutil.which,
) -> ExecutionReport:
    requires_policy = any(_GITLEAKS_CONFIG_PATH in spec.argv for spec in plan)
    if not requires_policy:
        try:
            with tempfile.TemporaryDirectory(prefix="memorymaster-supply-chain-") as raw_directory:
                directory = Path(raw_directory)
                environment = _sterile_environment(directory, Path.cwd().resolve(), {})
                return _execute(plan, runner, environment=environment, working_directory=directory)
        except OSError:
            return ExecutionReport(False, (CheckResult("execution", "failed", "environment_unavailable"),))
    try:
        with tempfile.TemporaryDirectory(prefix="memorymaster-supply-chain-") as raw_directory:
            directory = Path(raw_directory)
            repo_root, sbom_path, artifact_path, image_ids = _plan_context(plan)
            materialized = _materialize_policy(plan, directory)
            materialized, tools = _materialize_tools(materialized, repo_root, resolver)
            environment = _sterile_environment(directory, repo_root, tools)
            evidence = _collect_evidence(repo_root, sbom_path, artifact_path, image_ids, tools)
            report = _execute(
                materialized,
                runner,
                environment=environment,
                working_directory=directory,
            )
            return replace(report, evidence=evidence)
    except ValueError:
        return ExecutionReport(
            False,
            (CheckResult("supply_chain_preflight", "failed", "evidence_unavailable"),),
        )
    except OSError:
        return ExecutionReport(
            False,
            (CheckResult("gitleaks_history", "failed", "policy_unavailable"),),
        )


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description="Run fail-closed local supply-chain checks.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--sbom", type=Path, default=Path("artifacts/memorymaster.cdx.json"))
    parser.add_argument("--release-artifact", required=True, type=Path)
    parser.add_argument("--local-image", action="append", default=[])
    parser.add_argument("--expected-name")
    parser.add_argument("--expected-version")
    parser.add_argument("--command-plan", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _plan_payload(plan: Sequence[CommandSpec]) -> dict[str, Any]:
    return {"mode": "command-plan", "commands": [item.to_dict() for item in plan]}


def main(argv: Sequence[str] | None = None, *, runner: Runner = subprocess.run) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.repo_root.resolve(strict=True) != TRUSTED_REPO_ROOT:
            raise ValueError("repository root does not match trusted script")
        plan = build_command_plan(
            repo_root=args.repo_root,
            sbom_path=args.sbom,
            local_images=args.local_image,
            expected_name=args.expected_name,
            expected_version=args.expected_version,
            release_artifact=args.release_artifact,
        )
    except (OSError, ValueError):
        print(json.dumps({"ok": False, "failure_kind": "invalid_configuration"}))
        return 2
    if args.command_plan or args.dry_run:
        print(json.dumps(_plan_payload(plan), indent=2))
        return 0
    report = execute_plan(plan, runner=runner)
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
