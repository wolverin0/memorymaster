"""GitNexus → MemoryMaster bridge.

Runs `npx gitnexus analyze` on a project, then ingests architectural
facts as claims into memorymaster.

Usage:
    python scripts/gitnexus_to_claims.py --project argentina-sales-hub
    python scripts/gitnexus_to_claims.py --all
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService

PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT", "."))
DB_PATH = PROJECTS_ROOT / "memorymaster" / "memorymaster.db"


def _run_gitnexus(project_dir: Path, command: str, *args: str) -> dict | None:
    """Run a gitnexus CLI command and return parsed JSON, or None on failure."""
    cmd = ["npx", "-y", "gitnexus", command, *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(project_dir),
            timeout=120,
        )
        if result.returncode != 0:
            return None
        # gitnexus outputs JSON to stdout
        output = result.stdout.strip()
        if not output:
            return None
        return json.loads(output)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def _analyze_project(project_dir: Path) -> dict | None:
    """Run gitnexus analyze and capture summary stats."""
    result = subprocess.run(
        ["npx", "-y", "gitnexus", "analyze"],
        capture_output=True,
        text=True,
        cwd=str(project_dir),
        timeout=300,
    )
    # Parse the summary line: "3,102 nodes | 8,689 edges | 201 clusters | 249 flows"
    for line in result.stdout.split("\n") + result.stderr.split("\n"):
        if "nodes" in line and "edges" in line:
            parts = line.strip().split("|")
            stats = {}
            for part in parts:
                part = part.strip()
                for key in ("nodes", "edges", "clusters", "flows"):
                    if key in part:
                        num = part.replace(key, "").strip().replace(",", "")
                        try:
                            stats[key] = int(num)
                        except ValueError:
                            pass
            if stats:
                return stats
    return None


def ingest_project(svc: MemoryService, project_name: str, project_dir: Path) -> dict:
    """Analyze a project with GitNexus and ingest architectural claims."""
    stats = {"analyzed": False, "claims_ingested": 0, "errors": 0}
    scope = f"project:{project_name}"

    # Step 1: Analyze
    print(f"  Analyzing {project_name}...")
    analysis = _analyze_project(project_dir)
    if not analysis:
        print(f"  SKIP: gitnexus analyze failed for {project_name}")
        return stats

    stats["analyzed"] = True
    nodes = analysis.get("nodes", 0)
    edges = analysis.get("edges", 0)
    clusters = analysis.get("clusters", 0)
    flows = analysis.get("flows", 0)

    # Step 2: Ingest summary claim
    try:
        svc.ingest(
            text=f"{project_name} codebase structure: {nodes} code symbols, {edges} relationships, {clusters} functional clusters, {flows} execution flows (analyzed by GitNexus).",
            citations=[CitationInput(source=f"gitnexus:analyze:{project_name}")],
            claim_type="fact",
            scope=scope,
            confidence=0.9,
            idempotency_key=f"gitnexus-summary-{project_name}",
        )
        stats["claims_ingested"] += 1
    except Exception as e:
        print(f"  ERROR ingesting summary: {e}")
        stats["errors"] += 1

    # Step 3: Query for key symbols and ingest
    query_result = _run_gitnexus(project_dir, "query", "main entry points and core modules")
    if query_result:
        definitions = query_result.get("definitions", [])[:10]
        for defn in definitions:
            name = defn.get("name", "")
            file_path = defn.get("filePath", "")
            start_line = defn.get("startLine", "")
            if not name or not file_path:
                continue
            try:
                svc.ingest(
                    text=f"{project_name}: key symbol '{name}' defined in {file_path}:{start_line}",
                    citations=[CitationInput(
                        source=f"gitnexus:query:{project_name}",
                        locator=f"{file_path}:{start_line}",
                    )],
                    claim_type="fact",
                    scope=scope,
                    confidence=0.8,
                    idempotency_key=f"gitnexus-symbol-{project_name}-{name}-{file_path}",
                )
                stats["claims_ingested"] += 1
            except Exception:
                stats["errors"] += 1

    print(f"  Done: {stats['claims_ingested']} claims, {stats['errors']} errors")
    return stats


def main():
    parser = argparse.ArgumentParser(description="Ingest GitNexus analysis into memorymaster")
    parser.add_argument("--project", help="Single project name")
    parser.add_argument("--all", action="store_true", help="Analyze all projects")
    parser.add_argument("--db", default=str(DB_PATH), help="memorymaster DB path")
    args = parser.parse_args()

    if not args.project and not args.all:
        parser.error("Specify --project <name> or --all")

    svc = MemoryService(db_target=args.db, workspace_root=PROJECTS_ROOT / "memorymaster")

    projects = []
    if args.all:
        for d in sorted(PROJECTS_ROOT.iterdir()):
            if not d.is_dir():
                continue
            # Skip non-project dirs
            if d.name.startswith(".") or d.name == "memoryking":
                continue
            if (d / "package.json").exists() or (d / "pyproject.toml").exists() or (d / "src").exists():
                projects.append(d.name)
    else:
        projects = [args.project]

    total_claims = 0
    total_errors = 0

    for name in projects:
        project_dir = PROJECTS_ROOT / name
        if not project_dir.is_dir():
            print(f"SKIP: {name} not found at {project_dir}")
            continue
        result = ingest_project(svc, name, project_dir)
        total_claims += result["claims_ingested"]
        total_errors += result["errors"]

    print(f"\nTotal: {total_claims} claims ingested, {total_errors} errors across {len(projects)} projects")


if __name__ == "__main__":
    main()
