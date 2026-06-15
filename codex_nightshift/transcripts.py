from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .config import CODEX_HOME

TAIL_BYTES = 256 * 1024


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _iter_jsonl(path: Path) -> Iterator[dict]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _tail_jsonl(path: Path, max_bytes: int = TAIL_BYTES) -> list[dict]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@dataclass
class RateWindow:
    name: str
    used_percent: float | None
    window_minutes: int | None
    resets_at: int | None

    @property
    def exhausted(self) -> bool:
        return self.used_percent is not None and self.used_percent >= 100

    def seconds_until_reset(self, now: float | None = None) -> float | None:
        if self.resets_at is None:
            return None
        return self.resets_at - (now or time.time())


@dataclass
class QuotaSnapshot:
    timestamp: datetime
    primary: RateWindow
    secondary: RateWindow

    def blocked_until(self, now: float | None = None) -> int | None:
        current = now or time.time()
        resets = [
            window.resets_at
            for window in (self.primary, self.secondary)
            if window.exhausted
            and window.resets_at is not None
            and window.resets_at > current
        ]
        return max(resets) if resets else None


@dataclass
class SessionInfo:
    session_id: str
    transcript: Path
    cwd: str
    title: str
    last_active: datetime


@dataclass
class SessionStatus:
    session: SessionInfo
    state: str
    reason: str
    confidence: str
    fingerprint: str
    idle_min: float
    quota: QuotaSnapshot | None

    @property
    def resumable(self) -> bool:
        return self.state == "interrupted"


def _rate_window(name: str, raw: dict | None) -> RateWindow:
    raw = raw or {}
    return RateWindow(
        name=name,
        used_percent=raw.get("used_percent"),
        window_minutes=raw.get("window_minutes"),
        resets_at=raw.get("resets_at"),
    )


def quota_from_event(event: dict) -> QuotaSnapshot | None:
    if event.get("type") != "event_msg":
        return None
    payload = event.get("payload") or {}
    if payload.get("type") != "token_count" or not payload.get("rate_limits"):
        return None
    timestamp = _parse_time(event.get("timestamp"))
    if timestamp is None:
        return None
    limits = payload["rate_limits"]
    return QuotaSnapshot(
        timestamp=timestamp,
        primary=_rate_window("5h", limits.get("primary")),
        secondary=_rate_window("weekly", limits.get("secondary")),
    )


def session_paths(root: Path | None = None) -> list[Path]:
    sessions = root or CODEX_HOME / "sessions"
    if not sessions.exists():
        return []
    paths = list(sessions.rglob("rollout-*.jsonl"))
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return paths


def load_titles(index_path: Path | None = None) -> dict[str, str]:
    path = index_path or CODEX_HOME / "session_index.jsonl"
    titles: dict[str, str] = {}
    for event in _iter_jsonl(path):
        session_id = event.get("id")
        title = event.get("thread_name")
        if session_id and title:
            titles[session_id] = title
    return titles


def read_session_info(path: Path, titles: dict[str, str] | None = None) -> SessionInfo:
    session_id = path.stem.rsplit("-", 1)[-1]
    cwd = str(Path.home())
    for event in _iter_jsonl(path):
        if event.get("type") == "session_meta":
            payload = event.get("payload") or {}
            session_id = payload.get("id") or session_id
            cwd = payload.get("cwd") or cwd
            break
    mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    title = (titles or {}).get(session_id, f"(untitled) {session_id[:8]}")
    return SessionInfo(session_id, path, cwd, title, mtime)


def list_recent_sessions(
    days: float = 7,
    limit: int = 30,
    root: Path | None = None,
    index_path: Path | None = None,
) -> list[SessionInfo]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    titles = load_titles(index_path)
    out = []
    for path in session_paths(root):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if mtime < cutoff:
            continue
        out.append(read_session_info(path, titles))
        if len(out) >= limit:
            break
    return out


def resolve_session(
    query: str,
    days: float = 30,
    root: Path | None = None,
    index_path: Path | None = None,
) -> SessionInfo:
    sessions = list_recent_sessions(days=days, limit=500, root=root, index_path=index_path)
    if query == "last":
        if not sessions:
            raise LookupError("no recent Codex sessions found")
        return sessions[0]
    matches = [
        session
        for session in sessions
        if session.session_id == query or session.session_id.startswith(query)
    ]
    if not matches:
        raise LookupError(f"no session matching '{query}'")
    if len(matches) > 1:
        raise LookupError(f"session prefix '{query}' is ambiguous")
    return matches[0]


def analyze_session(
    session: SessionInfo,
    idle_min_threshold: float = 5,
    now: datetime | None = None,
) -> SessionStatus:
    now = now or datetime.now(timezone.utc)
    last_start: tuple[datetime, str] | None = None
    last_terminal: tuple[datetime, str, bool] | None = None
    quota: QuotaSnapshot | None = None

    for event in _iter_jsonl(session.transcript):
        timestamp = _parse_time(event.get("timestamp"))
        if timestamp is None:
            continue
        candidate = quota_from_event(event)
        if candidate is not None:
            quota = candidate
        if event.get("type") != "event_msg":
            continue
        payload = event.get("payload") or {}
        event_type = payload.get("type")
        if event_type == "task_started":
            last_start = (timestamp, payload.get("turn_id") or timestamp.isoformat())
        elif event_type == "task_complete":
            has_final_answer = bool(str(payload.get("last_agent_message") or "").strip())
            last_terminal = (timestamp, event_type, has_final_answer)
        elif event_type == "turn_aborted":
            last_terminal = (timestamp, event_type, False)

    try:
        mtime = datetime.fromtimestamp(session.transcript.stat().st_mtime, timezone.utc)
    except OSError:
        mtime = session.last_active
    idle_min = max(0, (now - mtime).total_seconds() / 60)

    if last_start is None:
        return SessionStatus(session, "complete", "last turn completed", "high", "", idle_min, quota)
    if last_terminal is not None and last_terminal[0] >= last_start[0]:
        if last_terminal[1] == "turn_aborted":
            return SessionStatus(
                session, "stopped", "last turn was aborted", "high", "", idle_min, quota
            )
        if last_terminal[2]:
            return SessionStatus(
                session, "complete", "last turn returned a final answer", "high", "", idle_min, quota
            )
        fingerprint = f"{last_start[1]}@{last_start[0].timestamp():.3f}"
        return SessionStatus(
            session,
            "interrupted",
            "turn ended without a final answer",
            "high",
            fingerprint,
            idle_min,
            quota,
        )
    fingerprint = f"{last_start[1]}@{last_start[0].timestamp():.3f}"
    if idle_min < idle_min_threshold:
        return SessionStatus(session, "active", "transcript is still changing", "high", fingerprint, idle_min, quota)
    quota_matches_turn = quota is not None and quota.timestamp >= last_start[0]
    if quota_matches_turn and (quota.primary.exhausted or quota.secondary.exhausted):
        return SessionStatus(session, "interrupted", "quota exhausted", "high", fingerprint, idle_min, quota)
    return SessionStatus(
        session,
        "interrupted",
        "unfinished turn became idle",
        "medium",
        fingerprint,
        idle_min,
        quota,
    )


def latest_quota(root: Path | None = None, max_files: int = 50) -> QuotaSnapshot | None:
    latest: QuotaSnapshot | None = None
    for path in session_paths(root)[:max_files]:
        for event in _tail_jsonl(path):
            candidate = quota_from_event(event)
            if candidate and (latest is None or candidate.timestamp > latest.timestamp):
                latest = candidate
    return latest
