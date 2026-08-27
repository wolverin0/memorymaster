"""Regresion T-0228: tmp-root RELATIVO en release_readiness es flaky en G:\\.

Lo medido en T-0227 (2026-08-23): con el default relativo `.tmp_cases/...` el
script dio falsos NO_GO variables (1 critico una corrida, 5 la siguiente) en el
checkout de G:\\, y el mismo commit dio GO 11/11 midiendo con --tmp-root
ABSOLUTO. Mecanismo: db/inbox/log se pasan como rutas RELATIVAS a ~10
subprocesos; cualquier subcheck que resuelva su cwd distinto (run-operator
recibe ademas --workspace .) apunta a otro archivo y falla de forma
no-determinista. El SQLite flaky no se puede reproducir deterministicamente en
un test; lo que SI se pina es el contrato que lo elimina:

1. el tmp-root EFECTIVO es siempre absoluto, aunque el argumento sea relativo
   (reproducimos exactamente el modo que fallaba: cwd en este repo de G:\\ +
   valor relativo del default);
2. el veredicto DECLARA con que tmp-root midio, en el JSON y en el stdout —
   un NO_GO que no dice con que midio no es auditable.

Contra el codigo de hoy ambas cosas fallan: main() usa Path(args.tmp_root) sin
anclar y el reporte no tiene clave tmp_root.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import release_readiness  # noqa: E402


def _fake_run_cmd(*, name, cmd, critical, timeout_seconds, must_contain=None):
    return release_readiness.CheckResult(
        name=name, critical=critical, status="pass", duration_ms=1, exit_code=0
    )


def _fake_py_check(*, name, critical, fn):
    return release_readiness.CheckResult(
        name=name, critical=critical, status="pass", duration_ms=1
    )


def _run_main(monkeypatch, tmp_path: Path, argv_tail: list[str]) -> tuple[int, dict, str]:
    """Corre main() con los checks stubbeados (rapido y deterministico)."""
    monkeypatch.setattr(release_readiness, "_run_cmd", _fake_run_cmd)
    monkeypatch.setattr(release_readiness, "_run_py_check", _fake_py_check)
    out_json = tmp_path / "report.json"
    monkeypatch.setattr(
        sys, "argv", ["release_readiness.py", "--out-json", str(out_json), *argv_tail]
    )
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = release_readiness.main()
    report = json.loads(out_json.read_text(encoding="utf-8"))
    return rc, report, buf.getvalue()


def test_relative_tmp_root_is_anchored_absolute(monkeypatch, tmp_path):
    """El modo que fallaba en G:\\: argumento relativo => efectivo ABSOLUTO."""
    # cwd = este checkout (G:\ en la maquina del operador), como en la corrida real
    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(repo_root)
    try:
        rc, report, out = _run_main(
            monkeypatch, tmp_path, ["--tmp-root", ".tmp_cases/t0228_regression"]
        )
        assert rc == 0
        declared = report["tmp_root"]  # hoy: KeyError — el reporte no declara
        assert Path(declared).is_absolute(), f"tmp_root no absoluto: {declared}"
        assert Path(declared) == (repo_root / ".tmp_cases" / "t0228_regression").resolve()
    finally:
        import shutil

        shutil.rmtree(repo_root / ".tmp_cases" / "t0228_regression", ignore_errors=True)


def test_verdict_declares_tmp_root_in_json_and_stdout(monkeypatch, tmp_path):
    rc, report, out = _run_main(monkeypatch, tmp_path, ["--tmp-root", str(tmp_path / "abs")])
    assert rc == 0
    declared = report["tmp_root"]
    assert Path(declared).is_absolute()
    stdout_line = json.loads(out.strip().splitlines()[-1])
    assert stdout_line["tmp_root"] == declared, "el veredicto stdout no declara tmp_root"


def test_default_tmp_root_resolves_absolute(monkeypatch, tmp_path):
    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(repo_root)
    rc, report, out = _run_main(monkeypatch, tmp_path, [])
    assert Path(report["tmp_root"]).is_absolute()
