from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import CONFIG_PATH, DATA_DIR, ensure_dirs, find_codex_cmd, load_config, save_config
from .state import adopt_thread, load_registry, remove_thread
from .transcripts import (
    SessionInfo,
    analyze_session,
    latest_quota,
    list_recent_sessions,
    resolve_session,
)


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _fmt_reset(timestamp: int | None) -> str:
    if timestamp is None:
        return "unknown"
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _print_quota() -> None:
    quota = latest_quota()
    if quota is None:
        print("quota: unavailable (no token_count rate-limit records found)")
        return
    for window in (quota.primary, quota.secondary):
        used = "?" if window.used_percent is None else f"{window.used_percent:.0f}%"
        print(f"{window.name:7} used={used:>4}  resets={_fmt_reset(window.resets_at)}")


def _status_for_entry(entry: dict, idle_min: float):
    path = Path(entry["transcript"])
    if not path.exists():
        return None
    session = SessionInfo(
        entry["session_id"],
        path,
        entry.get("cwd", str(Path.home())),
        entry.get("title", ""),
        datetime.fromtimestamp(path.stat().st_mtime).astimezone(),
    )
    return analyze_session(session, idle_min)


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    _print_quota()
    registry = load_registry()
    print(f"adopted: {len(registry['threads'])}")
    for entry in registry["threads"].values():
        status = _status_for_entry(entry, float(config["watch"]["idle_min"]))
        state = status.state if status else "missing"
        reason = status.reason if status else "transcript missing"
        strict = " strict" if entry.get("strict") else ""
        print(f"  {entry['session_id'][:8]}  {state}{strict}  {entry.get('title', '')[:60]}")
        print(f"            {reason}")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    config = load_config()
    adopted = load_registry()["threads"]
    for session in list_recent_sessions(days=args.days, limit=args.limit):
        status = analyze_session(session, float(config["watch"]["idle_min"]))
        flag = "*" if session.session_id in adopted else " "
        local = session.last_active.astimezone().strftime("%m-%d %H:%M")
        print(f"{flag} {session.session_id[:12]}  {local}  {status.state:11}  {session.title[:60]}")
        if args.verbose:
            print(f"    id:  {session.session_id}")
            print(f"    cwd: {session.cwd}")
            print(f"    why: {status.reason}")
    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    query = "last" if args.last else args.session
    if not query:
        print("error: provide a session id/prefix or use --last", file=sys.stderr)
        return 2
    try:
        session = resolve_session(query)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    adopt_thread(session.session_id, session.transcript, session.cwd, session.title, args.strict)
    print(f"adopted {session.session_id}")
    print(f"  title:  {session.title}")
    print(f"  mode:   {'limit-only' if args.strict else 'limit + idle unfinished turns'}")
    print("  start:  codex-nightshift watch --dry-run")
    return 0


def cmd_unadopt(args: argparse.Namespace) -> int:
    removed = remove_thread(args.session)
    if removed is None:
        print(f"error: no unique adopted thread matching '{args.session}'", file=sys.stderr)
        return 1
    print(f"unadopted {removed}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    config = load_config()
    registry = load_registry()
    found = 0
    for entry in registry["threads"].values():
        status = _status_for_entry(entry, float(config["watch"]["idle_min"]))
        if status and status.resumable:
            found += 1
            print(f"{entry['session_id']}  {status.confidence}  {status.reason}")
    if not found:
        print("no adopted interrupted sessions")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from .runner import resume_session, run_cycle

    if not args.session:
        resumed, blocked = run_cycle(dry_run=args.dry_run)
        print(f"processed {resumed} adopted session(s)")
        if blocked:
            print(f"quota blocked until {_fmt_reset(blocked)}")
        return 0
    try:
        session = resolve_session(args.session)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    result = resume_session(session, dry_run=args.dry_run)
    print("command: " + subprocess.list2cmdline(result.command))
    if not args.dry_run:
        print(f"exit: {result.returncode}; log: {result.log_path}")
    return 0 if result.success else 1


def cmd_watch(args: argparse.Namespace) -> int:
    from .runner import watch_forever

    try:
        watch_forever(args.poll, dry_run=args.dry_run, once=args.once)
    except KeyboardInterrupt:
        print("\nwatch stopped")
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config = load_config()
    if not CONFIG_PATH.exists():
        save_config(config)
        print(f"created {CONFIG_PATH}")
    print(f"config: {CONFIG_PATH}")
    print(f"data:   {DATA_DIR}")
    print(json.dumps(config, indent=2, ensure_ascii=False))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"data dir:   {DATA_DIR}")
    print(f"config:     {CONFIG_PATH}")
    try:
        command = find_codex_cmd(load_config())
        print(f"codex cmd:  {command}")
        result = subprocess.run(
            [command, "--version"], capture_output=True, text=True, timeout=15
        )
        print(f"codex test: exit {result.returncode} {(result.stdout or result.stderr).strip()}")
        return 0 if result.returncode == 0 else 1
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"codex test: FAILED: {exc}")
        return 1


def cmd_background(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        print("error: background task management is currently Windows-only", file=sys.stderr)
        return 1
    from . import schedule_win

    actions = {
        "status": schedule_win.status,
        "install": lambda: schedule_win.install(start_now=args.start_now),
        "remove": schedule_win.remove,
        "start": schedule_win.start,
        "stop": schedule_win.stop,
    }
    ok, message = actions[args.action]()
    print(message)
    return 0 if ok else 1


def cmd_gui(args: argparse.Namespace) -> int:
    from .webui import run_gui

    run_gui(port=args.port, open_browser=not args.no_open)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-nightshift",
        description="Adopt ordinary Codex sessions and resume interrupted work.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    command = sub.add_parser("status", help="show quota and adopted sessions")
    command.set_defaults(func=cmd_status)

    command = sub.add_parser("sessions", help="list recent ordinary Codex sessions")
    command.add_argument("--days", type=float, default=7)
    command.add_argument("--limit", type=int, default=30)
    command.add_argument("--verbose", action="store_true")
    command.set_defaults(func=cmd_sessions)

    command = sub.add_parser("adopt", help="allow nightshift to resume a session")
    command.add_argument("session", nargs="?", default="")
    command.add_argument("--last", action="store_true")
    command.add_argument("--strict", action="store_true", help="resume only when quota exhaustion is explicit")
    command.set_defaults(func=cmd_adopt)

    command = sub.add_parser("unadopt", help="stop managing an adopted session")
    command.add_argument("session")
    command.set_defaults(func=cmd_unadopt)

    command = sub.add_parser("scan", help="show adopted interrupted sessions")
    command.set_defaults(func=cmd_scan)

    command = sub.add_parser("resume", help="resume a session now, or process adopted sessions")
    command.add_argument("session", nargs="?", default="")
    command.add_argument("--dry-run", action="store_true")
    command.set_defaults(func=cmd_resume)

    command = sub.add_parser("watch", help="watch adopted sessions and resume them")
    command.add_argument("--poll", type=float, default=0)
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--once", action="store_true")
    command.set_defaults(func=cmd_watch)

    command = sub.add_parser("config", help="create and show configuration")
    command.set_defaults(func=cmd_config)

    command = sub.add_parser("doctor", help="verify the standalone Codex CLI")
    command.set_defaults(func=cmd_doctor)

    command = sub.add_parser("background", help="manage the Windows background watcher")
    command.add_argument(
        "action",
        nargs="?",
        choices=["status", "install", "remove", "start", "stop"],
        default="status",
    )
    command.add_argument(
        "--start-now",
        action="store_true",
        help="start the watcher immediately after installing it",
    )
    command.set_defaults(func=cmd_background)

    command = sub.add_parser("gui", help="open the graphical task control panel")
    command.add_argument("--port", type=int, default=0)
    command.add_argument("--no-open", action="store_true")
    command.set_defaults(func=cmd_gui)
    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    ensure_dirs()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
