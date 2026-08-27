@echo off
rem Wrapper para schtasks (T-0241): schtasks trata argumentos finales como
rem switches propios, asi que la task apunta a este .cmd sin argumentos.
python "%~dp0freshness_sentinel.py"
exit /b %errorlevel%
