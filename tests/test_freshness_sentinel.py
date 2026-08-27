"""Regresion T-0241: el sentinel avisa SOLO cuando el canonico diverge.

Ejercita la decision con repos git REALES (origin + clon), no con mocks del
propio check: diverge (clon >=1 commits detras) => exit 1 + linea con el conteo
en el rollup del dia; al dia => exit 0 y NINGUN archivo de aviso — un aviso que
sale siempre no es aviso (criterios 2, 3 y 4 de la tarjeta).
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import freshness_sentinel  # noqa: E402


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _make_origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(tmp_path, "init", "-b", "main", str(origin))
    _git(origin, "config", "user.email", "t@t")
    _git(origin, "config", "user.name", "t")
    (origin / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(origin, "add", "a.txt")
    _git(origin, "commit", "-m", "c1")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    return origin, clone


def test_divergence_appends_notice_with_count(tmp_path):
    origin, clone = _make_origin_and_clone(tmp_path)
    (origin / "a.txt").write_text("v2\n", encoding="utf-8")
    _git(origin, "commit", "-am", "c2")  # clon queda 1 detras

    rollups = tmp_path / "rollups"
    rc = freshness_sentinel.main(["--target", str(clone), "--rollup-dir", str(rollups)])
    assert rc == 1
    today = datetime.now().strftime("%Y-%m-%d")
    text = (rollups / f"{today}.md").read_text(encoding="utf-8")
    assert "freshness-sentinel" in text
    assert "1 commit(s) detras de origin/main" in text


def test_up_to_date_is_silent(tmp_path):
    _, clone = _make_origin_and_clone(tmp_path)
    rollups = tmp_path / "rollups"
    rc = freshness_sentinel.main(["--target", str(clone), "--rollup-dir", str(rollups)])
    assert rc == 0
    assert not rollups.exists(), "al dia NO debe escribirse ningun aviso"


def test_diverge_then_catch_up_goes_quiet(tmp_path):
    origin, clone = _make_origin_and_clone(tmp_path)
    (origin / "a.txt").write_text("v2\n", encoding="utf-8")
    _git(origin, "commit", "-am", "c2")
    rollups = tmp_path / "rollups"
    assert freshness_sentinel.main(["--target", str(clone), "--rollup-dir", str(rollups)]) == 1

    _git(clone, "pull", "--quiet", "origin", "main")
    today = datetime.now().strftime("%Y-%m-%d")
    before = (rollups / f"{today}.md").read_text(encoding="utf-8")
    assert freshness_sentinel.main(["--target", str(clone), "--rollup-dir", str(rollups)]) == 0
    after = (rollups / f"{today}.md").read_text(encoding="utf-8")
    assert before == after, "una corrida verde no debe agregar lineas"
