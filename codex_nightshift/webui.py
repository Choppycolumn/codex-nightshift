from __future__ import annotations

import json
import mimetypes
import os
import sys
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import load_config
from .state import (
    adopt_thread,
    load_registry,
    remove_thread,
    set_thread_prompt,
    set_thread_schedule,
    set_thread_strict,
)
from .transcripts import analyze_session, latest_quota, list_recent_sessions, resolve_session

WEB_ROOT = Path(__file__).resolve().parent / "web"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _reset_text(timestamp: int | None) -> str:
    if timestamp is None:
        return "未知"
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%m-%d %H:%M")


def build_state() -> dict[str, Any]:
    config = load_config()
    gui = config["gui"]
    idle_min = float(config["watch"]["idle_min"])
    registry = load_registry()["threads"]
    quota = latest_quota()
    sessions = []
    for session in list_recent_sessions(
        days=float(gui["session_days"]), limit=int(gui["session_limit"])
    ):
        status = analyze_session(session, idle_min)
        adopted = registry.get(session.session_id)
        sessions.append(
            {
                "id": session.session_id,
                "short_id": session.session_id[:8],
                "title": session.title,
                "cwd": session.cwd,
                "project": Path(session.cwd).name or session.cwd,
                "last_active": session.last_active.astimezone().strftime("%m-%d %H:%M"),
                "state": status.state,
                "reason": status.reason,
                "confidence": status.confidence,
                "auto": adopted is not None,
                "strict": bool(adopted and adopted.get("strict")),
                "resume_prompt": (adopted or {}).get("resume_prompt", ""),
                "scheduled_command": (adopted or {}).get("scheduled_command", {}),
                "last_result": (adopted or {}).get("last_result", ""),
            }
        )
    quota_data = None
    if quota:
        quota_data = {
            "primary": {
                "used": quota.primary.used_percent,
                "reset": _reset_text(quota.primary.resets_at),
            },
            "secondary": {
                "used": quota.secondary.used_percent,
                "reset": _reset_text(quota.secondary.resets_at),
            },
        }
    from .runner import _lock_owner_alive

    return {
        "quota": quota_data,
        "sessions": sessions,
        "adopted_count": len(registry),
        "watcher_running": _lock_owner_alive(),
        "data_dir": str(Path.home() / ".codex-nightshift"),
    }


def set_session_auto(session_id: str, enabled: bool, strict: bool = False) -> dict[str, Any]:
    if enabled:
        session = resolve_session(session_id)
        adopt_thread(session.session_id, session.transcript, session.cwd, session.title, strict)
        return {"ok": True, "message": "已加入自动续跑", "auto": True, "strict": strict}
    removed = remove_thread(session_id)
    if removed is None:
        return {"ok": True, "message": "保持不执行", "auto": False, "strict": False}
    return {"ok": True, "message": "已设为不执行", "auto": False, "strict": False}


def set_session_mode(session_id: str, strict: bool) -> dict[str, Any]:
    if not set_thread_strict(session_id, strict):
        return {"ok": False, "message": "请先开启自动续跑"}
    return {"ok": True, "message": "执行模式已更新", "strict": strict}


def set_session_prompt(session_id: str, prompt: str) -> dict[str, Any]:
    prompt = prompt.strip()
    if len(prompt) > 4000:
        return {"ok": False, "message": "第一句指令不能超过 4000 个字符"}
    if not set_thread_prompt(session_id, prompt):
        return {"ok": False, "message": "请先开启自动续跑"}
    message = "已保存恢复后的第一句指令" if prompt else "已恢复使用默认续跑指令"
    return {"ok": True, "message": message, "resume_prompt": prompt}


def set_session_schedule(
    session_id: str, enabled: bool, run_at: str, repeat: str, prompt: str
) -> dict[str, Any]:
    run_at = run_at.strip()
    repeat = repeat.strip() or "once"
    prompt = prompt.strip()
    if repeat not in {"once", "hourly", "daily"}:
        return {"ok": False, "message": "未知重复模式"}
    if len(prompt) > 4000:
        return {"ok": False, "message": "计划命令不能超过 4000 个字符"}
    if enabled:
        if not run_at:
            return {"ok": False, "message": "请设置下达时间"}
        if not prompt:
            return {"ok": False, "message": "请填写要下达的命令"}
        try:
            datetime.fromisoformat(run_at)
        except ValueError:
            return {"ok": False, "message": "时间格式无效"}
    schedule = {
        "enabled": enabled,
        "run_at": run_at,
        "repeat": repeat,
        "prompt": prompt,
    }
    if not set_thread_schedule(session_id, schedule):
        return {"ok": False, "message": "请先开启自动续跑"}
    message = "计划命令已保存" if enabled else "计划命令已关闭"
    return {"ok": True, "message": message, "scheduled_command": schedule}


def background_action(action: str) -> dict[str, Any]:
    if sys.platform != "win32":
        return {"ok": False, "message": "后台任务管理目前仅支持 Windows"}
    from . import schedule_win

    actions = {
        "install_start": lambda: schedule_win.install(start_now=True),
        "start": schedule_win.start,
        "stop": schedule_win.stop,
        "remove": schedule_win.remove,
    }
    handler = actions.get(action)
    if handler is None:
        return {"ok": False, "message": "未知后台操作"}
    ok, message = handler()
    return {"ok": ok, "message": message}


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexNightshiftGUI/0.3"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _host_allowed(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        return host in ALLOWED_HOSTS

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.hostname in {"127.0.0.1", "localhost", "::1"}

    def _json(self, data: dict[str, Any], status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict[str, Any]:
        length = min(int(self.headers.get("Content-Length", "0")), 32 * 1024)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        if not self._host_allowed():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(build_state())
            return
        if path == "/api/background/status":
            if sys.platform != "win32":
                self._json({"ok": False, "message": "仅支持 Windows"})
                return
            from .schedule_win import status

            ok, message = status()
            self._json({"ok": ok, "message": message})
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        if not self._host_allowed() or not self._origin_allowed():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        try:
            data = self._read_json()
            path = urlparse(self.path).path
            if path == "/api/session/auto":
                result = set_session_auto(
                    str(data.get("session_id", "")),
                    bool(data.get("enabled")),
                    bool(data.get("strict")),
                )
            elif path == "/api/session/mode":
                result = set_session_mode(
                    str(data.get("session_id", "")), bool(data.get("strict"))
                )
            elif path == "/api/session/prompt":
                result = set_session_prompt(
                    str(data.get("session_id", "")), str(data.get("prompt", ""))
                )
            elif path == "/api/session/schedule":
                result = set_session_schedule(
                    str(data.get("session_id", "")),
                    bool(data.get("enabled")),
                    str(data.get("run_at", "")),
                    str(data.get("repeat", "once")),
                    str(data.get("prompt", "")),
                )
            elif path == "/api/background":
                result = background_action(str(data.get("action", "")))
            else:
                self._json({"ok": False, "message": "接口不存在"}, 404)
                return
            self._json(result, 200 if result.get("ok") else 400)
        except (json.JSONDecodeError, LookupError, OSError, ValueError) as exc:
            self._json({"ok": False, "message": str(exc)}, 400)

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            target = WEB_ROOT / "index.html"
        payload = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(payload)


def run_gui(port: int = 0, open_browser: bool = True) -> None:
    config = load_config()
    port = port or int(config["gui"]["port"])
    url = f"http://127.0.0.1:{port}/"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        if open_browser:
            webbrowser.open(url)
        print(f"GUI may already be running: {url}")
        return
    print(f"Codex Nightshift GUI: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
