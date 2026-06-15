from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import LOG_DIR, find_codex_cmd, load_config
from .state import load_registry, update_thread
from .transcripts import SessionInfo, analyze_session, latest_quota

LIMIT_MARKERS = (
    "usage limit",
    "rate limit",
    "limit reached",
    "try again at",
    "weekly limit",
)
WATCH_LOCK = LOG_DIR.parent / "watch.lock"


def _log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    with (LOG_DIR / "watch.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


@dataclass
class RunResult:
    success: bool
    hit_limit: bool
    returncode: int
    log_path: Path
    command: list[str]


def build_resume_command(
    session_id: str, config: dict[str, Any], prompt: str = ""
) -> list[str]:
    resume = config["resume"]
    effective_prompt = prompt.strip() or resume["prompt"]
    command = [
        find_codex_cmd(config),
        "exec",
        "--json",
        "--sandbox",
        resume["sandbox"],
        "--skip-git-repo-check",
    ]
    command.extend(str(arg) for arg in resume.get("extra_args", []))
    command.extend(["resume", session_id, effective_prompt])
    return command


def resume_session(
    session: SessionInfo,
    config: dict[str, Any] | None = None,
    dry_run: bool = False,
    prompt: str = "",
) -> RunResult:
    config = config or load_config()
    command = build_resume_command(session.session_id, config, prompt)
    log_path = LOG_DIR / f"resume-{session.session_id[:8]}-{int(time.time())}.log"
    if dry_run:
        return RunResult(True, False, 0, log_path, command)

    cwd = session.cwd if Path(session.cwd).is_dir() else str(Path.home())
    timeout = int(config["resume"]["timeout_min"]) * 60
    _log(f"resuming {session.session_id[:8]} in {cwd}")
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        output = (
            f"command: {json.dumps(command)}\n"
            f"exit: {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
        log_path.write_text(output, encoding="utf-8")
        blob = (result.stdout + result.stderr).lower()
        hit_limit = any(marker in blob for marker in LIMIT_MARKERS)
        return RunResult(result.returncode == 0 and not hit_limit, hit_limit, result.returncode, log_path, command)
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(f"timeout after {timeout}s\n{exc}", encoding="utf-8")
        return RunResult(False, False, 124, log_path, command)
    except OSError as exc:
        log_path.write_text(f"launch error: {exc}\n", encoding="utf-8")
        return RunResult(False, False, 127, log_path, command)


def _session_from_entry(entry: dict[str, Any]) -> SessionInfo:
    path = Path(entry["transcript"])
    mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    return SessionInfo(
        session_id=entry["session_id"],
        transcript=path,
        cwd=entry.get("cwd") or str(Path.home()),
        title=entry.get("title") or f"(untitled) {entry['session_id'][:8]}",
        last_active=mtime,
    )


def run_cycle(dry_run: bool = False) -> tuple[int, int | None]:
    config = load_config()
    registry = load_registry()
    watch = config["watch"]
    resumed = 0
    global_quota = latest_quota()
    blocked_until = global_quota.blocked_until() if global_quota else None

    for session_id, entry in registry["threads"].items():
        if resumed >= int(watch["max_resumes_per_cycle"]):
            break
        if not entry.get("enabled", True):
            continue
        path = Path(entry.get("transcript", ""))
        if not path.exists():
            _log(f"skipping {session_id[:8]}: transcript is missing")
            continue

        status = analyze_session(_session_from_entry(entry), float(watch["idle_min"]))
        if not status.resumable:
            continue
        if entry.get("strict") and status.confidence != "high":
            continue
        if status.confidence != "high" and not config["resume"]["resume_incomplete"]:
            continue
        if (
            entry.get("last_fingerprint") == status.fingerprint
            and entry.get("last_result") == "success"
        ):
            continue

        quota = global_quota or status.quota
        wait_until = quota.blocked_until() if quota else None
        if wait_until:
            blocked_until = max(blocked_until or 0, wait_until)
            continue

        last_attempt = float(entry.get("last_attempt_at", 0))
        if time.time() - last_attempt < float(watch["retry_backoff_sec"]):
            continue

        result = resume_session(
            status.session,
            config=config,
            dry_run=dry_run,
            prompt=str(entry.get("resume_prompt", "")),
        )
        resumed += 1
        if dry_run:
            _log("command: " + subprocess.list2cmdline(result.command))
        else:
            update_thread(
                session_id,
                last_attempt_at=time.time(),
                last_fingerprint=status.fingerprint,
                last_result="success" if result.success else "limit" if result.hit_limit else "failed",
                last_log=str(result.log_path),
            )
        _log(
            f"{'would resume' if dry_run else 'resume finished'} {session_id[:8]}: "
            f"{status.reason}; result={'dry-run' if dry_run else result.returncode}"
        )
    return resumed, blocked_until


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        try:
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                return False
            return exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_started_at(pid: int) -> float | None:
    if os.name != "nt":
        return None
    import ctypes

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information, False, pid
    )
    if not handle:
        return None
    creation = ctypes.c_ulonglong()
    exit_time = ctypes.c_ulonglong()
    kernel = ctypes.c_ulonglong()
    user = ctypes.c_ulonglong()
    try:
        ok = ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        return creation.value / 10_000_000 - 11_644_473_600
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _read_watch_lock() -> tuple[int, float | None]:
    try:
        raw = WATCH_LOCK.read_text(encoding="ascii").strip()
        data = json.loads(raw)
        if isinstance(data, dict):
            return int(data["pid"]), float(data.get("started_at", 0)) or None
        return int(data), None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return 0, None


def _lock_owner_alive() -> bool:
    pid, started_at = _read_watch_lock()
    if not pid or not _pid_is_alive(pid):
        return False
    if os.name != "nt":
        return True
    actual = _process_started_at(pid)
    return (
        started_at is not None
        and actual is not None
        and abs(actual - started_at) < 2
    )


def stop_lock_owner() -> bool:
    pid, _ = _read_watch_lock()
    if not pid or not _lock_owner_alive():
        WATCH_LOCK.unlink(missing_ok=True)
        return False
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
        )
    else:
        os.kill(pid, 15)
    for _ in range(25):
        if not _pid_is_alive(pid):
            break
        time.sleep(0.2)
    if not _pid_is_alive(pid):
        WATCH_LOCK.unlink(missing_ok=True)
    return True


def _acquire_watch_lock() -> None:
    WATCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    if WATCH_LOCK.exists():
        existing_pid, _ = _read_watch_lock()
        if existing_pid and _lock_owner_alive():
            raise RuntimeError(f"another watcher is already running (pid {existing_pid})")
        WATCH_LOCK.unlink(missing_ok=True)
    try:
        descriptor = os.open(WATCH_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError("another watcher acquired the lock") from exc
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "started_at": _process_started_at(os.getpid()) or time.time(),
                }
            )
        )


def watch_forever(poll_sec: float = 0, dry_run: bool = False, once: bool = False) -> None:
    config = load_config()
    poll = poll_sec or float(config["watch"]["poll_interval_sec"])
    grace = float(config["watch"]["reset_grace_sec"])
    _acquire_watch_lock()
    try:
        _log(f"watch started (poll={poll:.0f}s, dry_run={dry_run})")
        while True:
            resumed, blocked_until = run_cycle(dry_run=dry_run)
            if once:
                return
            sleep_for = poll
            if blocked_until:
                sleep_for = min(poll, max(1, blocked_until - time.time() + grace))
            if resumed == 0:
                _log(f"no resumes; sleeping {sleep_for:.0f}s")
            time.sleep(sleep_for)
    finally:
        WATCH_LOCK.unlink(missing_ok=True)
