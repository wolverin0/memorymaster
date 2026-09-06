<!-- doc-head: useful-memory milestone executable acceptance and measured evidence -->
# Useful memory, reliable delivery
Covers: recall installation, honest evaluation and source-level Dreaming coverage.
Read when: implementing or accepting this milestone; ROADMAP.md remains authority.
<!-- /doc-head -->

- [x] G1: Recall delivery guards survive installation and preserve custom hooks.
  CHECK: python -m pytest tests/test_recall_delivery.py tests/test_recall_hook_installation.py -q
  EVIDENCE: The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set
- [x] G2: CI evaluates real fixtures and cannot hide evaluation failure.
  CHECK: python -m pytest tests/test_ci_evaluation_contract.py tests/test_qrels_regression.py tests/test_public_demo.py -q
  EVIDENCE: The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set
- [x] G3: Dreaming measures omitted useful facts and cannot accept duplicate or synthetic human labels.
  CHECK: python -m pytest tests/test_dreaming_evaluation.py tests/test_dreaming_sampling.py -q
  EVIDENCE: The event loop scope for asynchronous fixtures will default to the fixture caching scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope. Set
- [x] G4: Existing Dreaming source population sampled read-only, including zero-output captures; no precision fabricated.
  EVIDENCE: 188 captures; 118 zero-output, 10 candidates/no actions, 60 with actions; 15 sampled; artifacts/useful-memory/source-sample.json; quality remains unknown.
- [x] G5: Focused integration, lint, package and release metadata verified; implementation recorded in sole roadmap.
  EVIDENCE: 32 focused plus 83 integration and 12 release-truth tests passed; 94% scoped coverage; Ruff clean; clean-installed 4.8.8 imports pass; ROADMAP.md and implementation ledger updated.
