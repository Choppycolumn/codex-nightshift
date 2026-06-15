from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import DATA_DIR, REGISTRY_PATH


def load_registry() -> dict[str, Any]:
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        if isinstance(data.get("threads"), dict):
            return data
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return {"threads": {}}


def save_registry(registry: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = REGISTRY_PATH.with_suffix(".tmp")
    temp.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temp.replace(REGISTRY_PATH)


def adopt_thread(
    session_id: str, transcript: Path, cwd: str, title: str, strict: bool = False
) -> dict[str, Any]:
    registry = load_registry()
    previous = registry["threads"].get(session_id, {})
    entry = {
        **previous,
        "session_id": session_id,
        "transcript": str(transcript),
        "cwd": cwd,
        "title": title,
        "strict": strict,
        "enabled": True,
        "adopted_at": previous.get("adopted_at", time.time()),
    }
    registry["threads"][session_id] = entry
    save_registry(registry)
    return entry


def remove_thread(query: str) -> str | None:
    registry = load_registry()
    matches = [
        sid for sid in registry["threads"] if sid == query or sid.startswith(query)
    ]
    if len(matches) != 1:
        return None
    session_id = matches[0]
    del registry["threads"][session_id]
    save_registry(registry)
    return session_id


def update_thread(session_id: str, **updates: Any) -> None:
    registry = load_registry()
    entry = registry["threads"].get(session_id)
    if entry is None:
        return
    entry.update(updates)
    registry["threads"][session_id] = entry
    save_registry(registry)


def set_thread_strict(session_id: str, strict: bool) -> bool:
    registry = load_registry()
    entry = registry["threads"].get(session_id)
    if entry is None:
        return False
    entry["strict"] = strict
    registry["threads"][session_id] = entry
    save_registry(registry)
    return True


def set_thread_prompt(session_id: str, prompt: str) -> bool:
    registry = load_registry()
    entry = registry["threads"].get(session_id)
    if entry is None:
        return False
    entry["resume_prompt"] = prompt.strip()
    registry["threads"][session_id] = entry
    save_registry(registry)
    return True
