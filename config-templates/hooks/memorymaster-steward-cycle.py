"""Scheduled task: run MemoryMaster steward cycle + Obsidian vault export."""
import os, sys

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.insert(0, PROJECT_ROOT)
os.environ["MEMORYMASTER_DEFAULT_DB"] = DB_PATH
os.chdir(PROJECT_ROOT)

try:
    from memorymaster.service import MemoryService
    from pathlib import Path

    svc = MemoryService(db_target=DB_PATH, workspace_root=Path(PROJECT_ROOT))
    result = svc.run_cycle()
    print(f"[MemoryMaster] steward cycle: {result}")
except Exception as e:
    print(f"[MemoryMaster] steward error: {e}", file=sys.stderr)

# Export to Obsidian vault
try:
    from memorymaster.vault_exporter import export_vault
    vault_path = os.path.join(PROJECT_ROOT, "obsidian-vault")
    stats = export_vault(DB_PATH, vault_path)
    print(f"[MemoryMaster] vault export: {stats}")
except Exception as e:
    print(f"[MemoryMaster] vault export error: {e}", file=sys.stderr)
