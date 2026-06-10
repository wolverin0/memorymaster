@echo off
REM ============================================================================
REM  C4 FINISHER - run this AFTER closing every Claude/Codex pane that uses
REM  MemoryMaster (so no mcp_server / hook holds memorymaster.db open).
REM  Double-click it, or run from a plain cmd/PowerShell that is NOT a
REM  memorymaster-connected agent session.
REM
REM  It swaps the verified-repaired DB in for the corrupt one.
REM  - corrupt DB is preserved as memorymaster.db.corrupt-2026-06-05
REM  - repaired DB (quick_check=ok, 84,583 claims, all other tables 100%) -> live
REM ============================================================================
cd /d "G:\_OneDrive\OneDrive\Desktop\Py Apps\memorymaster"

echo Killing any lingering memorymaster MCP servers...
for /f "tokens=2" %%P in ('tasklist /fi "imagename eq python.exe" /v /fo list ^| findstr /i "PID:"') do rem (no-op; use wmic below)
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'memorymaster' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
timeout /t 2 /nobreak >nul

if not exist memorymaster_repaired.db (
  echo ERROR: memorymaster_repaired.db not found. Aborting.
  pause & exit /b 1
)

echo Verifying repaired artifact...
python -c "import sqlite3,sys; r=sqlite3.connect('memorymaster_repaired.db').execute('PRAGMA quick_check').fetchone()[0]; print('quick_check:',r); sys.exit(0 if r=='ok' else 1)"
if errorlevel 1 ( echo ERROR: artifact quick_check not ok. Aborting. & pause & exit /b 1 )

echo Backing up corrupt DB (if not already) and swapping...
if not exist memorymaster.db.corrupt-2026-06-05 ( ren memorymaster.db memorymaster.db.corrupt-2026-06-05 ) else ( del /f memorymaster.db )
if exist memorymaster.db-wal del /f memorymaster.db-wal
if exist memorymaster.db-shm del /f memorymaster.db-shm
ren memorymaster_repaired.db memorymaster.db

echo Final verify on live DB:
python -c "import sqlite3; c=sqlite3.connect('memorymaster.db'); print('quick_check:',c.execute('PRAGMA quick_check').fetchone()[0]); print('claims:',c.execute('SELECT count(*) FROM claims').fetchone()[0]); print('verbatim:',c.execute('SELECT count(*) FROM verbatim_memories').fetchone()[0])"

echo.
echo DONE. Corrupt DB preserved at memorymaster.db.corrupt-2026-06-05
echo Reopen your panes / run /mcp reconnect to restart MCP servers on the clean DB.
pause
