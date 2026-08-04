# Supply-chain security checks
# Covers: the fail-closed local release gate for secrets, dependencies, SBOMs, and images.
# Key terms: Gitleaks, pip-audit, CycloneDX, Docker Scout, immutable image, Docker config.
# Read when: preparing release evidence or changing scanner policy and authentication boundaries.
# Authentication: Docker config is scoped only to the Docker Scout subprocess.
# Privacy: scanner streams and Docker configuration paths are excluded from persisted reports.
# Updated: 2026-08-04 after authenticated immutable-image validation exposed the sterile-config boundary.

MemoryMaster's local release gate combines five fail-closed checks:

1. Gitleaks scans the complete Git history with built-in rules. Previously
   reviewed synthetic test-fixture findings are admitted only by exact
   commit/path/rule/line fingerprints; path, rule, and wildcard suppressions
   are rejected.
2. `pip-audit` audits the trusted project in strict mode against the explicit
   OSV vulnerability service.
3. A second dependency audit covers the personal/local minimal release extras:
   `mcp` and `security`. Qdrant is an optional semantic profile with a separate
   runtime evidence gate.
4. The CycloneDX validator binds the SBOM's root component and SHA-256 hash to
   the exact `memorymaster` wheel and its wheel metadata.
5. Docker Scout scans at most three already-local immutable `sha256:<image-id>`
   targets for high and critical findings, including base-image findings.

The runner discards scanner stdout/stderr and emits only fixed check results
plus safe evidence hashes: repository commit, release-wheel SHA-256, SBOM
SHA-256, immutable image IDs, native-tool hashes, and Python/`pip-audit`
versions. Missing tools, unavailable evidence, timeouts, nonzero exits,
mutable image tags, and invalid or mismatched SBOMs all fail the gate.

At execution time, the runner resolves the operator's Docker configuration
directory before creating its sterile scanner environment. It passes that
directory as Docker CLI's global `--config` argument and as `DOCKER_CONFIG` for
that Docker Scout subprocess only. Docker Scout's CLI plugin requires the
environment variable even when the parent Docker command received the global
flag. On Windows, the sterile environment retains only the standard program
directory variables needed to discover Docker Desktop's bundled Scout plugin.
No other `DOCKER_*` variable is inherited, no other scanner receives the
configuration path, the configuration is never copied, and the ordinary user
`PATH` remains excluded. Absolute paths stay redacted from command-plan/report
serialization. A missing, symlink-escaped, or repository-local `config.json`
fails preflight. Use `--docker-config` to select a non-default configuration
directory.

## Inspect the command plan without execution

This mode performs no scanner, network, registry, Docker, or artifact read:

```powershell
$Version = python -c "import importlib.metadata as m; print(m.version('memorymaster'))"
python scripts/run_supply_chain_checks.py `
  --release-artifact artifacts/memorymaster-$Version-py3-none-any.whl `
  --sbom artifacts/memorymaster-$Version.cdx.json `
  --local-image sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa `
  --command-plan
```

`--dry-run` is an alias. The displayed executable and policy placeholders are
resolved only during execution.

## Prepare release evidence

Build the exact wheel and generate a CycloneDX JSON SBOM with an approved local
generator. The SBOM must place the release root at `metadata.component`, use
the exact `memorymaster` name/version/PyPI purl, and include the wheel's SHA-256
under `metadata.component.hashes`. Dependency-only SBOMs that omit the release
root are rejected.

Build the image locally, then capture its immutable image ID:

```powershell
docker build --pull=false --tag memorymaster:phase1 .
$imageId = docker image inspect --format '{{.Id}}' memorymaster:phase1
```

Run the gate from the same checkout that owns the runner:

```powershell
$Version = python -c "import importlib.metadata as m; print(m.version('memorymaster'))"
python scripts/run_supply_chain_checks.py `
  --release-artifact artifacts/memorymaster-$Version-py3-none-any.whl `
  --sbom artifacts/memorymaster-$Version.cdx.json `
  --local-image $imageId
```

Repeat `--local-image` for approved Qdrant and Ollama images, up to the
three-image bound. Tags, registry URLs, and mutable references are rejected.

## Isolation and policy

The runner resolves Gitleaks, Git, and Docker to absolute non-repository files,
records their hashes, and runs every child from a sterile temporary directory
with a minimal environment. Ambient `GIT_*`, `GITLEAKS_*`, `PIP_AUDIT_*`,
Docker, proxy, certificate, credential, and Python-path variables do not reach
the scanners. Python tools run with `-I`; the validator path comes from the
trusted runner location rather than `--repo-root`.

Gitleaks uses a temporary config that extends its built-in defaults, a
validated copy of `.gitleaks-reviewed-fingerprints`,
`--ignore-gitleaks-allow`, full-history `--log-opts=--all`, and full redaction.
The review file accepts only exact fingerprints under `tests/`; duplicates,
path traversal, non-test paths, wildcards, and malformed entries fail closed.
Repository `.gitleaks.toml`, `.gitleaksignore`, and environment overrides are
ignored. `pip-audit` is pinned to the `osv` service and a temporary pip
configuration. Scanner streams go to `DEVNULL`, per-command timeouts and a
one-hour global deadline apply, and the Docker build context is an exact
allowlist of Dockerfile inputs.

## Verification state

On 2026-07-30, Gitleaks 8.21.2 scanned all 860 commits and reported zero
unreviewed findings after applying 44 exact reviewed fingerprints. The two
fingerprints added after the 2026-07-14 review are synthetic redaction-test
values in `tests/test_dreaming_surfaces.py` and
`tests/test_dreaming_capture.py`; both commits precede the vNext baseline.
All reviewed findings remain test-only, with no application, configuration,
workflow, documentation, or user-data finding. No credential rotation or
history rewrite is indicated by that classification.

An isolated install of `.[mcp,security]` produced 36 dependencies and passed a
strict OSV audit with zero known vulnerabilities. Qdrant was intentionally
excluded because it is not part of the default personal/local package profile.

The repository still cannot truthfully close these optional/container checks
by inspection:

- Approved hashes/versions for native scanners and an approved release SBOM
  generator require operator review.
- Qdrant and Ollama require approved local images with recorded immutable IDs;
  Docker Scout and the local daemon must be available.
- Approved immutable commit SHAs for third-party CI actions remain separate
  release-pipeline work.

Retain aggregate/fixed results and evidence hashes. Never put raw secret-scan
findings or credentials into general logs or repository artifacts. This
document defines the gate; it does not claim external scans passed.
