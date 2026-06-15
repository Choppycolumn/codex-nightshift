from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
DATA_DIR = Path(
    os.environ.get("CODEX_NIGHTSHIFT_HOME", Path.home() / ".codex-nightshift")
)
CONFIG_PATH = DATA_DIR / "config.json"
REGISTRY_PATH = DATA_DIR / "adopted.json"
LOG_DIR = DATA_DIR / "logs"

DEFAULTS: dict[str, Any] = {
    "codex_cmd": "",
    "watch": {
        "poll_interval_sec": 60,
        "idle_min": 5,
        "lookback_days": 14,
        "retry_backoff_sec": 300,
        "reset_grace_sec": 20,
        "max_resumes_per_cycle": 1,
    },
    "resume": {
        "prompt": (
            "The previous turn was interrupted. Continue from where you left off, "
            "verify the current repository state, and finish the remaining work."
        ),
        "sandbox": "workspace-write",
        "timeout_min": 240,
        "resume_incomplete": True,
        "extra_args": [],
    },
    "gui": {
        "port": 8765,
        "session_days": 14,
        "session_limit": 100,
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return DEFAULTS
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return _merge(DEFAULTS, raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: could not read {CONFIG_PATH}: {exc}", file=sys.stderr)
        return DEFAULTS


def save_config(config: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def ensure_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def find_codex_cmd(config: dict[str, Any] | None = None) -> str:
    configured = (config or {}).get("codex_cmd")
    if configured:
        return str(configured)

    candidates: list[str] = []
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA", ""))
        candidates.extend(
            str(appdata / "npm" / name) for name in ("codex.cmd", "codex.exe")
        )
        local_bin = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        if local_bin.exists():
            managed = sorted(
                local_bin.glob("*/codex.exe"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            candidates.extend(str(path) for path in managed)
    found = shutil.which("codex")
    if found:
        candidates.append(found)

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise FileNotFoundError(
        "Codex CLI was not found. Install the standalone CLI or set codex_cmd in "
        f"{CONFIG_PATH}."
    )
