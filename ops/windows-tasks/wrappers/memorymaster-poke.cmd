@echo off
setlocal
rem Orca first (panes moved from WezTerm to Orca; review 2026-09-23 F-08), WezTerm poke as fallback.
set "PY={PYTHON}"
set "ORCA_POKE={HOME}\.memorymaster\checkpoints\orca_poke.py"
set "POKE={PYAPPS}\wezbridge\scripts\poke-pane.cjs"
set "LOG={HOME}\.memorymaster\checkpoints\feature-checkpoint.log"
if "%~1"=="" (echo %DATE% %TIME% memorymaster-poke FAIL: missing message file>>"%LOG%" & exit /b 2)
if not exist "%~1" (echo %DATE% %TIME% memorymaster-poke FAIL: missing %~1>>"%LOG%" & exit /b 2)
"%PY%" -I "%ORCA_POKE%" "%~1"
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" exit /b 0
node "%POKE%" --project memorymaster --tab-title memorymaster --file "%~1" >>"%LOG%" 2>&1
set "RC2=%ERRORLEVEL%"
if not "%RC2%"=="0" echo %DATE% %TIME% memorymaster-poke exit=orca:%RC% wezterm:%RC2%>>"%LOG%"
exit /b %RC2%
