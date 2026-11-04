@echo off
REM MemoryMaster weekly digest — "Tu semana" to Telegram. 3.1-flash-lite follows the
REM length + date-filter instructions far better than 2.5-flash-lite.
set "MM_MODEL=gemini-3.1-flash-lite"
"{PYTHON}" "{CODE}\scripts\atlas_weekly_digest.py"
