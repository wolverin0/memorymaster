# Installing the Windows side of the Hermes delta sync

`windows-hermes-sync.ps1` runs twice daily via Windows Task Scheduler. It is
the **only** process that writes the Windows MemoryMaster DB (Option 1 — see
`docs/integration/hermes-agent-brief.md`).

## What it does each run

1. **Inbound** — if `delta-exchange\hermes-delta.db` exists, merge it into the
   Windows MemoryMaster DB (`merge-db`, idempotent).
2. **Outbound** — export the Windows delta to `delta-exchange\windows-delta.db`
   for Hermes to consume on its next run.

## One-time install

Open a normal (non-admin) PowerShell. A per-user scheduled task does not need
admin rights. Run:

```powershell
$ps1 = "G:\_OneDrive\OneDrive\Desktop\Py Apps\memorymaster\scripts\windows-hermes-sync.ps1"
$action = "powershell -NoProfile -ExecutionPolicy Bypass -File \"$ps1\""

schtasks /create /tn "MemoryMaster-HermesSync-AM" /sc daily /st 04:00 /tr $action /f
schtasks /create /tn "MemoryMaster-HermesSync-PM" /sc daily /st 16:00 /tr $action /f
```

Times 04:00 / 16:00 are deliberately one hour after Hermes's 03:00 / 15:00
cron runs, so a fresh `hermes-delta.db` is already on the share when Windows
picks it up.

## Verify

```powershell
schtasks /query /tn "MemoryMaster-HermesSync-AM" /v /fo list
schtasks /query /tn "MemoryMaster-HermesSync-PM" /v /fo list
```

Run it once by hand to confirm it works end-to-end:

```powershell
schtasks /run /tn "MemoryMaster-HermesSync-AM"
# or directly:
powershell -NoProfile -ExecutionPolicy Bypass -File "G:\_OneDrive\OneDrive\Desktop\Py Apps\memorymaster\scripts\windows-hermes-sync.ps1"
```

Expected output ends with `DONE windows delta sync`, and
`delta-exchange\windows-delta.db` + `delta-exchange\.windows.watermark`
should appear.

## Uninstall

```powershell
schtasks /delete /tn "MemoryMaster-HermesSync-AM" /f
schtasks /delete /tn "MemoryMaster-HermesSync-PM" /f
```

## Environment overrides (optional)

- `MEMORYMASTER_DB` — path to the Windows MemoryMaster DB (default: the G:\ repo copy)
- `MEMORYMASTER_BIN` — CLI invocation (default: `python -m memorymaster`)

To pass these to the scheduled task, set them as **system** environment
variables (the task runs in a fresh environment), or wrap the `.ps1` call in
a small `.cmd` that sets them first.

## Why this and not a single round-trip script

The Windows DB is ~2.5 GB. SQLite's file-locking semantics are unreliable
over SMB/CIFS — a write across the mount can corrupt the DB. So:

- Windows writes only its own local DB (this script).
- Hermes writes only its own local DB.
- The only things that cross the network are KB-sized delta files, copied
  with plain `cp` (never opened as SQLite over the mount).

See `docs/integration/hermes-agent-brief.md` and the header of
`hermes-sync.sh` for the full rationale.
