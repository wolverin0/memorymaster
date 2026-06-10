# RUNBOOK — Supervised WAL TRUNCATE checkpoint

**Why**: the live `memorymaster.db-wal` reached 1.44 GB against a 3.47 GB DB
(verified 2026-06-09). Passive auto-checkpoint is permanently starved because
~12 reader/writer processes keep read marks open, so the WAL never resets.
A large WAL slows every reader, bloats recovery time after a crash, and is a
standing input to the corruption class seen on 2026-06-05
(`idx_verbatim_session` btree corruption — salvage script:
`scripts/recover_db_indexcorrupt.py`).

This is a **one-time supervised procedure** for the P1 rollout (spec §2.5 /
§5 Day 0). After it, the steward integrity phase runs
`wal_checkpoint(TRUNCATE)` every cycle automatically.

**Never** run this against the DB while you are unsure which processes hold
it open. A TRUNCATE checkpoint that cannot win the lock is harmless (it
reports `busy=1`), but the point of the supervised run is to actually win.

## Procedure

1. **Quiesce writers** (the more you stop, the more likely TRUNCATE wins):
   - Close or idle all Claude Code panes (each per-pane MCP server holds a
     handle; idle panes don't hold write locks but active ones might).
   - Confirm no steward cycle is running (`schtasks /query` for the 6 h task,
     or just wait for it to finish).
   - Confirm the 15-min hermes delta sync is not mid-run.
   - Optional check for open handles:
     `handle.exe memorymaster.db` (Sysinternals) or
     `Get-Process | Where-Object {$_.Modules.FileName -match "memorymaster"}`.

2. **Record the before state** (read-only):

   ```powershell
   Get-Item "G:\_OneDrive\OneDrive\Desktop\Py Apps\memorymaster\memorymaster.db*" |
     Select-Object Name, Length
   ```

3. **Run the checkpoint** on a dedicated connection:

   ```powershell
   python -c "import sqlite3; c = sqlite3.connect(r'G:\_OneDrive\OneDrive\Desktop\Py Apps\memorymaster\memorymaster.db'); c.execute('PRAGMA busy_timeout = 30000'); print(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()); c.close()"
   ```

   Output is `(busy, log_frames, checkpointed_frames)`:
   - `(0, N, N)` — success: all frames checkpointed, WAL truncated to 0.
   - `(1, ...)` — a reader/writer blocked it. Quiesce harder (step 1) and
     retry. Do NOT kill processes holding the DB with `taskkill /F` to force
     this; just retry later.

4. **Verify the after state**: `memorymaster.db-wal` should now be 0 bytes
   (or a few pages). Re-run the step-2 listing and record both readings in
   `PROGRAM-LOG.md`.

5. **Sanity check** (read-only, ~2 s on the live DB):

   ```powershell
   python -c "import sqlite3; c = sqlite3.connect(r'file:G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster/memorymaster.db?mode=ro', uri=True); print(c.execute('PRAGMA quick_check').fetchall()); c.close()"
   ```

   Expect `[('ok',)]`. Anything else: stop, do not write, snapshot first
   (`memorymaster/snapshot.py` backup API), then investigate.

## VM crontab audit checklist (incident evidence — F6)

The legacy `scripts/openclaw-sync.sh` scp-uploaded a merged DB **over the
live file** while writers held it open. The script now hard-exits with a
RETIRED guard, but the VM cron entry must be removed and the 2026-06-05
incident window audited:

- [ ] SSH to the Hermes/OpenClaw VM.
- [ ] `crontab -l` — look for the `*/15 * * * * .../openclaw-sync.sh` line;
      remove it (`crontab -e`).
- [ ] `grep -n "2026-06-05" /var/log/memorymaster-sync.log` — record whether
      a sync ran (especially an upload, "Upload OK") in the hours before the
      corruption was detected. Attach findings to the incident postmortem.
- [ ] Confirm `/opt/memorymaster/scripts/openclaw-sync.sh` on the VM is the
      guarded version (pull latest, or copy the guard in) — a stale unguarded
      copy on the VM defeats the repo-side fix.
- [ ] Confirm the hermes delta sync (the replacement) is the only remaining
      cross-machine sync path.
