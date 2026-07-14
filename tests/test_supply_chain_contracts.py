from __future__ import annotations

import hashlib
import json
import io
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_supply_chain_checks as supply
from scripts import validate_sbom as sbom


EXPECTED_NAME = "memorymaster"
EXPECTED_VERSION = "4.4.1"
IMAGE_ID = "sha256:" + ("a" * 64)


def _valid_sbom(artifact_sha256: str = "b" * 64) -> dict[str, object]:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "$schema": "https://cyclonedx.org/schema/bom-1.6.schema.json",
        "metadata": {
            "component": {
                "type": "application",
                "name": EXPECTED_NAME,
                "version": EXPECTED_VERSION,
                "purl": f"pkg:pypi/{EXPECTED_NAME}@{EXPECTED_VERSION}",
                "hashes": [{"alg": "SHA-256", "content": artifact_sha256}],
            }
        },
        "components": [
            {
                "type": "application",
                "name": EXPECTED_NAME,
                "version": EXPECTED_VERSION,
                "purl": f"pkg:pypi/{EXPECTED_NAME}@{EXPECTED_VERSION}",
            }
        ],
    }


def _wheel(tmp_path: Path) -> Path:
    path = tmp_path / "memorymaster-4.4.1-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "memorymaster-4.4.1.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: memorymaster\nVersion: 4.4.1\n",
        )
        archive.writestr("memorymaster/__init__.py", "")
    return path


def _prepare_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        (
            f'[project]\nname = "{EXPECTED_NAME}"\nversion = "{EXPECTED_VERSION}"\n'
            'dependencies = ["requests>=2.31", "tenacity>=8.2"]\n'
            "[project.optional-dependencies]\n"
            'mcp = ["mcp>=1.8.1"]\n'
            'qdrant = ["httpx>=0.27"]\n'
            'security = ["cryptography>=42"]\n'
        ),
        encoding="utf-8",
    )
    git_directory = tmp_path / ".git"
    git_directory.mkdir(exist_ok=True)
    (git_directory / "HEAD").write_text("1" * 40, encoding="ascii")
    wheel = _wheel(tmp_path)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (tmp_path / "memorymaster.cdx.json").write_text(json.dumps(_valid_sbom(digest)), encoding="utf-8")
    return wheel


def _resolver(tmp_path: Path) -> supply.Resolver:
    directory = tmp_path.parent / f"{tmp_path.name}-trusted-tools"
    directory.mkdir(exist_ok=True)
    paths: dict[str, str] = {}
    for name in ("gitleaks", "docker", "git"):
        path = directory / f"{name}.bin"
        path.write_bytes(f"trusted-{name}".encode())
        paths[name] = str(path)
    return paths.get


def _plan(tmp_path: Path) -> tuple[supply.CommandSpec, ...]:
    wheel = _prepare_repo(tmp_path)
    return supply.build_command_plan(
        repo_root=tmp_path,
        sbom_path=tmp_path / "memorymaster.cdx.json",
        local_images=[IMAGE_ID],
        expected_name=EXPECTED_NAME,
        expected_version=EXPECTED_VERSION,
        release_artifact=wheel,
    )


def _execute(
    plan: tuple[supply.CommandSpec, ...],
    tmp_path: Path,
    runner: supply.Runner,
) -> supply.ExecutionReport:
    return supply.execute_plan(plan, runner=runner, resolver=_resolver(tmp_path))


def _by_name(plan: tuple[supply.CommandSpec, ...]) -> dict[str, supply.CommandSpec]:
    return {item.name: item for item in plan}


def test_plan_enforces_full_history_redacted_gitleaks_scan(tmp_path: Path) -> None:
    command = _by_name(_plan(tmp_path))["gitleaks_history"]

    assert command.argv[1] == "git"
    assert command.argv[0] != "gitleaks"
    assert "--log-opts=--all" in command.argv
    assert "--redact=100" in command.argv
    assert "--no-git" not in command.argv


def test_gitleaks_policy_cannot_be_suppressed_by_repo_or_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".gitleaks.toml").write_text("[allowlist]\npaths=['.*']\n", encoding="utf-8")
    (tmp_path / ".gitleaksignore").write_text("*:*\n", encoding="utf-8")
    monkeypatch.setenv("GITLEAKS_CONFIG", str(tmp_path / ".gitleaks.toml"))
    monkeypatch.setenv("GITLEAKS_CONFIG_TOML", "[allowlist]\npaths=['.*']")
    observed: dict[str, object] = {}

    def fake_runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--log-opts=--all" in argv:
            observed["argv"] = argv
            observed["env"] = kwargs.get("env")
            if "--config" in argv:
                config_path = Path(argv[argv.index("--config") + 1])
                observed["config"] = config_path.read_text(encoding="utf-8")
            if "--gitleaks-ignore-path" in argv:
                ignore_path = Path(argv[argv.index("--gitleaks-ignore-path") + 1])
                observed["ignore"] = ignore_path.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    report = _execute(_plan(tmp_path), tmp_path, fake_runner)
    argv = observed["argv"]
    environment = observed["env"]

    assert report.ok is True
    assert isinstance(argv, tuple)
    assert "--ignore-gitleaks-allow" in argv
    assert "--config" in argv
    assert "--gitleaks-ignore-path" in argv
    assert observed["config"] == supply.TRUSTED_GITLEAKS_CONFIG
    assert observed["ignore"] == ""
    assert isinstance(environment, dict)
    assert "GITLEAKS_CONFIG" not in environment
    assert "GITLEAKS_CONFIG_TOML" not in environment


def test_scanners_receive_sterile_environment_and_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "PIP_AUDIT_VULNERABILITY_SERVICE",
        "PIP_AUDIT_OSV_URL",
        "DOCKER_HOST",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.setenv(key, "hostile-secret-value")
    observed: list[dict[str, object]] = []

    def fake_runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert _execute(_plan(tmp_path), tmp_path, fake_runner).ok is True
    assert observed
    for kwargs in observed:
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        assert not any(key.startswith(("GIT_", "PIP_AUDIT_", "DOCKER_")) for key in environment)
        assert "AWS_SECRET_ACCESS_KEY" not in environment
        working_directory = Path(str(kwargs["cwd"])).resolve()
        assert working_directory != tmp_path.resolve()
        assert tmp_path.resolve() not in working_directory.parents


def test_native_scanners_are_absolute_and_validator_is_trusted(tmp_path: Path) -> None:
    observed: list[tuple[str, ...]] = []

    def fake_runner(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert _execute(_plan(tmp_path), tmp_path, fake_runner).ok is True
    commands = _by_name(_plan(tmp_path))
    validator = commands["validate_sbom"].argv

    scanner_calls = [argv for argv in observed if "--log-opts=--all" in argv or "scout" in argv]
    assert len(scanner_calls) == 2
    assert all(Path(argv[0]).is_absolute() for argv in scanner_calls)
    assert Path(validator[2]).resolve() == Path(sbom.__file__).resolve()


def test_repository_local_native_scanner_is_refused(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    local_tool = tmp_path / "gitleaks.exe"
    local_tool.write_bytes(b"untrusted")
    trusted = _resolver(tmp_path)

    def resolver(name: str) -> str | None:
        return str(local_tool) if name == "gitleaks" else trusted(name)

    report = supply.execute_plan(
        plan,
        runner=lambda *_args, **_kwargs: pytest.fail("runner called"),
        resolver=resolver,
    )

    assert report.ok is False
    assert report.results[0].status == "failed"


def test_gitleaks_policy_materialization_failure_is_fixed_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise OSError("secret temp path")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", unavailable)

    report = _execute(
        _plan(tmp_path),
        tmp_path,
        lambda *_args, **_kwargs: pytest.fail("runner called"),
    )

    assert report.ok is False
    assert report.results == (supply.CheckResult("gitleaks_history", "failed", "policy_unavailable"),)
    assert "secret temp path" not in json.dumps(report.to_dict())


def test_plan_enforces_strict_cyclonedx_dependency_audit(tmp_path: Path) -> None:
    commands = _by_name(_plan(tmp_path))
    audit = commands["pip_audit_project"].argv
    validation = commands["validate_sbom"].argv

    assert audit[:4] == (sys.executable, "-I", "-m", "pip_audit")
    assert "--strict" in audit
    assert str(tmp_path.resolve()) in audit
    assert validation[0] == sys.executable
    assert validation[validation.index("--expected-name") + 1] == EXPECTED_NAME
    assert validation[validation.index("--expected-version") + 1] == EXPECTED_VERSION


def test_dependency_audit_is_bound_to_project_and_fixed_service(tmp_path: Path) -> None:
    audit = _by_name(_plan(tmp_path))["pip_audit_project"].argv

    assert str(tmp_path.resolve()) in audit
    assert audit[audit.index("--vulnerability-service") + 1] == "pypi"


def test_release_dependency_audit_includes_docker_extras(tmp_path: Path) -> None:
    observed_requirements: list[str] = []

    def fake_runner(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--requirement" in argv:
            path = Path(argv[argv.index("--requirement") + 1])
            observed_requirements.extend(path.read_text(encoding="utf-8").splitlines())
        return subprocess.CompletedProcess(argv, 0)

    report = _execute(_plan(tmp_path), tmp_path, fake_runner)

    assert report.ok is True
    assert {"requests>=2.31", "tenacity>=8.2"}.issubset(observed_requirements)
    assert {"mcp>=1.8.1", "httpx>=0.27", "cryptography>=42"}.issubset(observed_requirements)


def test_project_identity_cannot_be_supplied_by_operator(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "memorymaster"\nversion = "9.9.9"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="does not match"):
        supply.build_command_plan(
            repo_root=tmp_path,
            sbom_path=tmp_path / "sbom.json",
            local_images=[IMAGE_ID],
            expected_name="lookalike",
            expected_version="0.0.0",
        )


def test_python_scanner_and_validator_use_isolated_mode(tmp_path: Path) -> None:
    commands = _by_name(_plan(tmp_path))

    assert commands["pip_audit_project"].argv[:3] == (sys.executable, "-I", "-m")
    assert commands["validate_sbom"].argv[:2] == (sys.executable, "-I")


def test_plan_enforces_local_only_high_and_critical_image_scan(tmp_path: Path) -> None:
    command = next(item for item in _plan(tmp_path) if item.name.startswith("docker_scout_"))

    assert command.argv[1:3] == ("scout", "cves")
    assert command.argv[-1] == f"local://{IMAGE_ID}"
    severities = command.argv[command.argv.index("--only-severity") + 1]
    assert set(severities.split(",")) == {"high", "critical"}
    assert "--exit-code" in command.argv
    assert "--ignore-base" not in command.argv


def test_mutable_image_tag_is_rejected(tmp_path: Path) -> None:
    wheel = _prepare_repo(tmp_path)
    with pytest.raises(ValueError, match="immutable"):
        supply.build_command_plan(
            repo_root=tmp_path,
            sbom_path=tmp_path / "memorymaster.cdx.json",
            local_images=["memorymaster:phase1"],
            release_artifact=wheel,
        )


def test_local_image_count_is_bounded(tmp_path: Path) -> None:
    wheel = _prepare_repo(tmp_path)
    with pytest.raises(ValueError, match="image"):
        supply.build_command_plan(
            repo_root=tmp_path,
            sbom_path=tmp_path / "sbom.json",
            local_images=[f"sha256:{index:064x}" for index in range(16)],
            expected_name=EXPECTED_NAME,
            expected_version=EXPECTED_VERSION,
            release_artifact=wheel,
        )


@pytest.mark.parametrize(
    "image",
    [
        "https://registry.invalid/memorymaster:latest",
        "docker://memorymaster:latest",
        "registry.invalid/memorymaster:latest\n--ignore-base",
        "",
    ],
)
def test_plan_rejects_remote_or_malformed_image_references(tmp_path: Path, image: str) -> None:
    wheel = _prepare_repo(tmp_path)
    with pytest.raises(ValueError, match="local image"):
        supply.build_command_plan(
            repo_root=tmp_path,
            sbom_path=tmp_path / "sbom.json",
            local_images=[image],
            expected_name=EXPECTED_NAME,
            expected_version=EXPECTED_VERSION,
            release_artifact=wheel,
        )


def test_plan_fails_closed_when_no_local_image_is_supplied(tmp_path: Path) -> None:
    wheel = _prepare_repo(tmp_path)
    with pytest.raises(ValueError, match="local image"):
        supply.build_command_plan(
            repo_root=tmp_path,
            sbom_path=tmp_path / "sbom.json",
            local_images=[],
            expected_name=EXPECTED_NAME,
            expected_version=EXPECTED_VERSION,
            release_artifact=wheel,
        )


def test_every_command_has_bounded_timeout_and_argv(tmp_path: Path) -> None:
    for command in _plan(tmp_path):
        assert isinstance(command.argv, tuple)
        assert command.argv
        assert all(isinstance(part, str) and part for part in command.argv)
        assert 0 < command.timeout_seconds <= 900


def test_execute_plan_uses_no_shell_and_never_emits_tool_output(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "secret-from-stdout", "secret-from-stderr")

    report = _execute(_plan(tmp_path), tmp_path, fake_runner)

    assert report.ok is True
    assert len(calls) == len(_plan(tmp_path))
    assert all(kwargs["shell"] is False for _, kwargs in calls)
    assert all(kwargs["stdout"] is subprocess.DEVNULL for _, kwargs in calls)
    assert all(kwargs["stderr"] is subprocess.DEVNULL for _, kwargs in calls)
    assert all("capture_output" not in kwargs for _, kwargs in calls)
    assert all(isinstance(kwargs["timeout"], (int, float)) for _, kwargs in calls)
    encoded = json.dumps(report.to_dict())
    assert "secret-from-stdout" not in encoded
    assert "secret-from-stderr" not in encoded


def test_runner_discards_scanner_streams_instead_of_buffering(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def fake_runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, None, None)

    assert _execute(_plan(tmp_path), tmp_path, fake_runner).ok is True
    assert calls
    assert all(call.get("stdout") is subprocess.DEVNULL for call in calls)
    assert all(call.get("stderr") is subprocess.DEVNULL for call in calls)
    assert all("capture_output" not in call for call in calls)


def test_success_report_contains_bound_evidence(tmp_path: Path) -> None:
    report = _execute(
        _plan(tmp_path),
        tmp_path,
        lambda argv, **_: subprocess.CompletedProcess(argv, 0, "", ""),
    ).to_dict()

    evidence = report["evidence"]
    assert evidence["repository_commit"]
    assert evidence["release_artifact_sha256"]
    assert evidence["sbom_sha256"]
    assert evidence["image_ids"]
    assert evidence["tool_sha256"]


@pytest.mark.parametrize("returncode", [1, 2, 127])
def test_nonzero_tool_exit_fails_closed(returncode: int) -> None:
    spec = supply.CommandSpec("scanner", ("scanner", "safe"), 10)

    def fake_runner(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, "sensitive finding", "sensitive detail")

    report = supply.execute_plan((spec,), runner=fake_runner)

    assert report.ok is False
    assert report.results[0].status == "failed"
    assert report.results[0].failure_kind == "nonzero_exit"
    assert "sensitive" not in json.dumps(report.to_dict())


def test_missing_tool_fails_closed_without_leaking_exception() -> None:
    spec = supply.CommandSpec("scanner", ("missing",), 10)

    def fake_runner(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("missing secret-tool-token")

    report = supply.execute_plan((spec,), runner=fake_runner)

    assert report.ok is False
    assert report.results[0].failure_kind == "tool_unavailable"
    assert "secret-tool-token" not in json.dumps(report.to_dict())


def test_timeout_fails_closed_without_leaking_captured_output() -> None:
    spec = supply.CommandSpec("scanner", ("scanner",), 3)

    def fake_runner(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd=["scanner", "secret-argument"],
            timeout=3,
            output="secret-output",
            stderr="secret-error",
        )

    report = supply.execute_plan((spec,), runner=fake_runner)

    assert report.ok is False
    assert report.results[0].failure_kind == "timeout"
    assert "secret" not in json.dumps(report.to_dict())


def test_unexpected_runner_error_fails_closed_without_leaking_exception() -> None:
    spec = supply.CommandSpec("scanner", ("scanner",), 3)

    def fake_runner(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise RuntimeError("secret-runtime-detail")

    report = supply.execute_plan((spec,), runner=fake_runner)

    assert report.ok is False
    assert report.results[0].failure_kind == "execution_error"
    assert "secret-runtime-detail" not in json.dumps(report.to_dict())


def test_execute_plan_stops_after_first_failure() -> None:
    calls: list[tuple[str, ...]] = []
    plan = (
        supply.CommandSpec("first", ("first",), 10),
        supply.CommandSpec("second", ("second",), 10),
    )

    def fake_runner(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    report = supply.execute_plan(plan, runner=fake_runner)

    assert report.ok is False
    assert calls == [("first",)]
    assert [item.name for item in report.results] == ["first"]


def test_execute_plan_enforces_global_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = (
        supply.CommandSpec("first", ("first",), 10),
        supply.CommandSpec("second", ("second",), 10),
    )
    clock = iter([0.0, 0.0, supply.MAX_PLAN_SECONDS + 1.0])
    monkeypatch.setattr(supply.time, "monotonic", lambda: next(clock))
    calls: list[tuple[str, ...]] = []

    def fake_runner(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    report = supply.execute_plan(plan, runner=fake_runner)

    assert report.ok is False
    assert calls == [("first",)]
    assert report.results[-1].failure_kind == "global_deadline"


def test_cli_refuses_foreign_repository_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    marker = "foreign-secret-marker"
    code = supply.main(
        [
            "--repo-root",
            str(tmp_path),
            "--release-artifact",
            str(tmp_path / marker),
            "--local-image",
            IMAGE_ID,
            "--command-plan",
        ]
    )

    assert code == 2
    assert marker not in capsys.readouterr().out


def test_command_plan_mode_does_not_execute_tools(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def forbidden_runner(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("command-plan mode executed an external tool")

    exit_code = supply.main(
        [
            "--sbom",
            "artifacts/test-sbom.json",
            "--release-artifact",
            "artifacts/test-release.whl",
            "--local-image",
            IMAGE_ID,
            "--command-plan",
        ],
        runner=forbidden_runner,
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["mode"] == "command-plan"
    assert output["commands"]


@pytest.mark.parametrize("flag", ["--command-plan", "--dry-run"])
def test_both_nonexecuting_modes_are_supported(tmp_path: Path, flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = supply.main(
        [
            "--sbom",
            "artifacts/test-sbom.json",
            "--release-artifact",
            "artifacts/test-release.whl",
            "--local-image",
            IMAGE_ID,
            flag,
        ],
        runner=lambda *_args, **_kwargs: pytest.fail("runner called"),
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "command-plan"


def test_valid_cyclonedx_document_passes() -> None:
    errors = sbom.validate_sbom_document(_valid_sbom(), expected_name=EXPECTED_NAME, expected_version=EXPECTED_VERSION)

    assert errors == ()


def test_sbom_rejects_invalid_spec_and_name_version_lookalike() -> None:
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "anything",
        "components": [{"name": EXPECTED_NAME, "version": EXPECTED_VERSION}],
    }

    errors = sbom.validate_sbom_document(document, expected_name=EXPECTED_NAME, expected_version=EXPECTED_VERSION)

    assert "invalid_spec_version" in errors
    assert "project_component_missing" in errors


def test_sbom_reads_at_most_bound_plus_one_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(_valid_sbom()).encode() + (b" " * 128)

    class GrowingPath:
        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_size=1)

        def read_bytes(self) -> bytes:
            return payload

        def open(self, _mode: str) -> io.BytesIO:
            return io.BytesIO(payload)

    monkeypatch.setattr(sbom, "MAX_SBOM_BYTES", 64)

    assert sbom.validate_sbom_file(  # type: ignore[arg-type]
        GrowingPath(), expected_name=EXPECTED_NAME, expected_version=EXPECTED_VERSION
    ) == ("file_too_large",)


def test_sbom_large_integer_fails_closed_without_raising(tmp_path: Path) -> None:
    path = tmp_path / "large-integer.json"
    path.write_text('{"value":' + ("9" * 5000) + "}", encoding="utf-8")

    assert sbom.validate_sbom_file(path, expected_name=EXPECTED_NAME, expected_version=EXPECTED_VERSION) == (
        "invalid_json",
    )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"bomFormat":"CycloneDX","specVersion":"1.6","score":NaN}',
        b'{"components":[],"components":[{"name":"memorymaster"}]}',
    ],
)
def test_sbom_rejects_nonstandard_constants_and_duplicate_keys(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "ambiguous.json"
    path.write_bytes(payload)

    assert sbom.validate_sbom_file(path, expected_name=EXPECTED_NAME, expected_version=EXPECTED_VERSION) == (
        "invalid_json",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document.update(specVersion=[]),
        lambda document: document.update({"$schema": []}),
        lambda document: document["components"][0].update(type=[]),
    ],
)
def test_sbom_unhashable_field_types_fail_closed_without_traceback(mutation: object) -> None:
    document = _valid_sbom()
    mutation(document)  # type: ignore[operator]

    errors = sbom.validate_sbom_document(
        document,
        expected_name=EXPECTED_NAME,
        expected_version=EXPECTED_VERSION,
    )

    assert errors


def test_sbom_must_match_exact_release_wheel_hash(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    sbom_path = tmp_path / "sbom.json"
    sbom_path.write_text(json.dumps(_valid_sbom("0" * 64)), encoding="utf-8")

    errors = sbom.validate_sbom_file(
        sbom_path,
        expected_name=EXPECTED_NAME,
        expected_version=EXPECTED_VERSION,
        artifact_path=wheel,
    )

    assert "artifact_hash_mismatch" in errors
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() not in json.dumps(errors)


def test_valid_sbom_is_bound_to_release_wheel(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    sbom_path = tmp_path / "sbom.json"
    sbom_path.write_text(json.dumps(_valid_sbom(digest)), encoding="utf-8")

    assert (
        sbom.validate_sbom_file(
            sbom_path,
            expected_name=EXPECTED_NAME,
            expected_version=EXPECTED_VERSION,
            artifact_path=wheel,
        )
        == ()
    )


def test_non_wheel_archive_is_rejected_as_release_artifact(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    archive = wheel.with_suffix(".zip")
    wheel.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sbom_path = tmp_path / "sbom.json"
    sbom_path.write_text(json.dumps(_valid_sbom(digest)), encoding="utf-8")

    assert sbom.validate_sbom_file(
        sbom_path,
        expected_name=EXPECTED_NAME,
        expected_version=EXPECTED_VERSION,
        artifact_path=archive,
    ) == ("artifact_invalid",)


def test_secret_shaped_image_is_redacted_from_reports_and_command_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "ghp_" + "7" * 36
    wheel = _prepare_repo(tmp_path)
    with pytest.raises(ValueError, match="immutable"):
        supply.build_command_plan(
            repo_root=tmp_path,
            sbom_path=tmp_path / "memorymaster.cdx.json",
            local_images=[marker],
            release_artifact=wheel,
        )
    exit_code = supply.main(
        [
            "--release-artifact",
            "artifacts/test-release.whl",
            "--local-image",
            marker,
            "--command-plan",
        ]
    )

    assert exit_code == 2
    assert marker not in capsys.readouterr().out


@pytest.mark.parametrize("entrypoint", [supply.main, sbom.main])
def test_cli_parse_errors_redact_raw_arguments(entrypoint: object, capsys: pytest.CaptureFixture[str]) -> None:
    marker = "ghp_" + "8" * 36
    try:
        code = entrypoint(["--unknown", marker])  # type: ignore[operator]
    except SystemExit as exc:
        code = int(exc.code)
    captured = capsys.readouterr()

    assert code == 2
    assert marker not in captured.out + captured.err
    assert json.loads(captured.out)["ok"] is False


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (lambda doc: doc.pop("bomFormat"), "invalid_bom_format"),
        (lambda doc: doc.update(bomFormat="SPDX"), "invalid_bom_format"),
        (lambda doc: doc.pop("specVersion"), "invalid_spec_version"),
        (lambda doc: doc.update(specVersion=""), "invalid_spec_version"),
        (lambda doc: doc.update(components=[]), "components_missing"),
        (
            lambda doc: doc["metadata"]["component"].update(name="lookalike"),
            "project_component_missing",
        ),
        (
            lambda doc: doc["metadata"]["component"].update(version="0.0.0"),
            "project_component_missing",
        ),
    ],
)
def test_invalid_sbom_documents_fail_closed(mutation: object, expected_error: str) -> None:
    document = _valid_sbom()
    mutation(document)  # type: ignore[operator]

    errors = sbom.validate_sbom_document(document, expected_name=EXPECTED_NAME, expected_version=EXPECTED_VERSION)

    assert expected_error in errors


@pytest.mark.parametrize("document", [None, [], "CycloneDX", 7])
def test_non_object_sbom_documents_fail_closed(document: object) -> None:
    assert sbom.validate_sbom_document(document, expected_name=EXPECTED_NAME, expected_version=EXPECTED_VERSION) == (
        "document_not_object",
    )


def test_sbom_cli_does_not_echo_malformed_or_secret_payload(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sbom_path = tmp_path / "secret-name.json"
    sbom_path.write_text('{"token":"do-not-print"', encoding="utf-8")

    exit_code = sbom.main(
        [
            "--sbom",
            str(sbom_path),
            "--expected-name",
            EXPECTED_NAME,
            "--expected-version",
            EXPECTED_VERSION,
        ]
    )

    output = capsys.readouterr().out
    assert exit_code != 0
    assert "do-not-print" not in output
    assert "secret-name" not in output


def test_sbom_cli_fails_closed_on_excessive_json_nesting(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sbom_path = tmp_path / "nested.json"
    sbom_path.write_text('{"nested":' * 2000 + "null" + "}" * 2000, encoding="utf-8")

    exit_code = sbom.main(
        [
            "--sbom",
            str(sbom_path),
            "--expected-name",
            EXPECTED_NAME,
            "--expected-version",
            EXPECTED_VERSION,
        ]
    )

    assert exit_code != 0
    assert json.loads(capsys.readouterr().out)["errors"]


def test_dockerignore_excludes_sensitive_and_generated_context() -> None:
    lines = {
        line.strip()
        for line in Path(".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "**" in lines
    assert "!memorymaster/**" in lines
    assert not any(
        value in lines
        for value in {
            "!.git/**",
            "!.env",
            "!.mcp.json",
            "!*.db",
            "!*.log",
            "!.tmp-*",
            "!.codex/**",
            "!.claude/**",
        }
    )


def test_docker_context_is_an_exact_allowlist_for_dockerfile_inputs() -> None:
    lines = [
        line.strip()
        for line in Path(".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert lines == [
        "**",
        "!Dockerfile",
        "!pyproject.toml",
        "!README.md",
        "!memorymaster/",
        "!memorymaster/**",
    ]
