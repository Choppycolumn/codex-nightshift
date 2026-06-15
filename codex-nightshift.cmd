@echo off
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
python -m codex_nightshift %*
exit /b %ERRORLEVEL%
