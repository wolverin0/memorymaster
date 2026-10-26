"""Disposable Python hook contract probe, launched only by hooks doctor.

This is a cooperative test harness, not a security sandbox for untrusted code.
Audit guards prevent accidental live SQLite/network/worker access by our hooks.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import runpy
import sys


def main() -> int:
    request = json.loads(sys.argv[1])
    root = Path(request["root"]).resolve()
    violations = []
    script = Path(request["args"][0]).resolve() if request["args"][0] != "-m" else None
    package = Path(__file__).resolve().parents[1]
    runtime_roots = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}

    def inside(value):
        try:
            return Path(os.fsdecode(value)).resolve().is_relative_to(root)
        except (TypeError, ValueError):
            return False

    def audit(event, args):
        denied = False
        if event == "sqlite3.connect":
            value = str(args[0])
            if value.startswith("file:"):
                from urllib.parse import unquote

                value = unquote(value[5:].split("?", 1)[0])
            denied = value != ":memory:" and not inside(value)
        elif event == "open":
            flags = args[2]
            if not isinstance(args[0], int) and not inside(args[0]):
                target = Path(os.fsdecode(args[0])).resolve()
                writing = isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
                source = target == script or (target.is_relative_to(package) and target.suffix in {".py", ".pyc", ".sql"})
                runtime = any(target.is_relative_to(prefix) for prefix in runtime_roots)
                denied = writing or not (source or runtime)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename"}:
            denied = not inside(args[0]) or (event == "os.rename" and not inside(args[1]))
        elif event in {"socket.connect", "socket.bind", "subprocess.Popen", "os.system", "os.exec", "os.spawn"}:
            denied = True
        if denied:
            if event == "open" and not writing and target.name == ".git":
                # Recall also checks parent project scopes. Model enclosing
                # repositories as absent; never read the enclosing checkout.
                raise FileNotFoundError("no enclosing repository in doctor fixture")
            violations.append(event)
            raise PermissionError("doctor fixture isolation: " + event)

    sys.addaudithook(audit)
    # Stop Git-aware scope resolution at this fixture instead of discovering
    # the checkout that happens to contain a test's temporary directory.
    (root / ".git").mkdir()
    # Full disposable schema, one public confirmed memory, real recall core.
    from memorymaster.core import lifecycle
    from memorymaster.core.models import CitationInput
    from memorymaster.core.service import MemoryService

    service = MemoryService(root / "memorymaster.db", workspace_root=root)
    service.init_db()
    claim = service.store.create_claim(
        text="MemoryMaster doctor fixture recall uses deterministic cited context.",
        citations=[CitationInput(source="test://hooks-doctor")], scope="project:fixture",
    )
    lifecycle.transition_claim(service.store, claim.id, "confirmed", reason="fixture", event_type="validator")
    args = request["args"]
    exit_code = 0
    try:
        if args[0] == "-m":
            sys.argv = args[1:]
            runpy.run_module(args[1], run_name="__main__")
        else:
            path = Path(args[0])
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            # Installed templates bind the DB via an absolute bootstrap root.
            # Redirect only that bootstrap; all recall/capture logic is original.
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id in {"PROJECT_ROOT", "MM_ROOT"}
                    for target in node.targets
                ):
                    node.value = (ast.Call(func=ast.Name(id="Path", ctx=ast.Load()),
                                           args=[ast.Constant(str(root))], keywords=[])
                                  if isinstance(node.value, ast.Call) else ast.Constant(str(root)))
            sys.argv = args
            namespace = {"__name__": "__main__", "__file__": str(path)}
            exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), namespace)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1 if exc.code else 0
    finally:
        # Hooks often swallow errors by design. Surface isolation failures anyway.
        if violations:
            print("doctor: blocked external access: " + ",".join(sorted(set(violations))), file=sys.stderr)
            exit_code = 4
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
