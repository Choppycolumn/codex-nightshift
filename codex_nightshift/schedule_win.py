from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from .config import DATA_DIR, LOG_DIR

TASK_NAME = "Codex Nightshift Watcher"


def _quote_ps(value: str | Path) -> str:
    return str(value).replace("'", "''")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _run_ps(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def install(start_now: bool = False) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "background task installation is currently Windows-only"
    if start_now:
        from .runner import stop_lock_owner

        stop_lock_owner()
        _cleanup_stale_watch_lock()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    task = _quote_ps(TASK_NAME)
    python = _quote_ps(sys.executable)
    root = _quote_ps(_repo_root())
    start = (
        f"Stop-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue;"
        f"Start-ScheduledTask -TaskName '{task}';"
        if start_now
        else ""
    )
    command = (
        f"$action = New-ScheduledTaskAction -Execute '{python}' "
        f"-Argument '-m codex_nightshift watch' -WorkingDirectory '{root}';"
        "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME;"
        "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-MultipleInstances IgnoreNew -RestartCount 3 "
        "-RestartInterval (New-TimeSpan -Minutes 1);"
        f"Register-ScheduledTask -TaskName '{task}' -Action $action "
        "-Trigger $trigger -Settings $settings -Description "
        "'Resume explicitly adopted Codex sessions after interruptions.' "
        "-Force | Out-Null;"
        f"{start}"
        "Write-Output 'installed'"
    )
    result = _run_ps(command)
    if result.returncode == 0 and "installed" in result.stdout:
        suffix = " and started" if start_now else ""
        return True, f"installed '{TASK_NAME}'{suffix}"
    return False, (result.stderr or result.stdout or "task installation failed").strip()


def remove() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "background task removal is currently Windows-only"
    task = _quote_ps(TASK_NAME)
    command = (
        f"$task = Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue;"
        "if ($task) {"
        "  Stop-ScheduledTask -InputObject $task -ErrorAction SilentlyContinue;"
        "  Unregister-ScheduledTask -InputObject $task -Confirm:$false;"
        "  Write-Output 'removed'"
        "} else { Write-Output 'missing' }"
    )
    result = _run_ps(command)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    from .runner import stop_lock_owner

    stop_lock_owner()
    _cleanup_stale_watch_lock()
    return True, "removed" if "removed" in result.stdout else "not installed"


def start() -> tuple[bool, str]:
    return _simple_action("Start-ScheduledTask", "started")


def stop() -> tuple[bool, str]:
    ok, message = _simple_action("Stop-ScheduledTask", "stopped")
    if ok:
        from .runner import stop_lock_owner

        stop_lock_owner()
        _cleanup_stale_watch_lock()
    return ok, message


def _simple_action(command_name: str, success: str) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "background task management is currently Windows-only"
    task = _quote_ps(TASK_NAME)
    result = _run_ps(
        f"{command_name} -TaskName '{task}' -ErrorAction Stop;"
        f"Write-Output '{success}'"
    )
    if result.returncode == 0 and success in result.stdout:
        return True, success
    return False, (result.stderr or result.stdout or "scheduled task not installed").strip()


def status() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "background task status is currently Windows-only"
    task = _quote_ps(TASK_NAME)
    command = (
        f"$task = Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue;"
        "if (-not $task) { Write-Output 'not installed'; exit 0 };"
        "$info = $task | Get-ScheduledTaskInfo;"
        "Write-Output ('state: ' + $task.State);"
        "Write-Output ('last run: ' + $info.LastRunTime);"
        "Write-Output ('last result: ' + $info.LastTaskResult);"
        "Write-Output ('next run: ' + $info.NextRunTime)"
    )
    result = _run_ps(command)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, result.stdout.strip()


def _cleanup_stale_watch_lock(wait_sec: float = 5) -> None:
    from .runner import WATCH_LOCK, _lock_owner_alive

    deadline = time.time() + wait_sec
    while _lock_owner_alive() and time.time() < deadline:
        time.sleep(0.2)
    if not _lock_owner_alive():
        WATCH_LOCK.unlink(missing_ok=True)
