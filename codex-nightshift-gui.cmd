@echo off
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
start "" pythonw -m codex_nightshift gui
