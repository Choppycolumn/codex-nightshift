import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("CODEX_NIGHTSHIFT_HOME", tempfile.mkdtemp(prefix="cn_data_"))

from codex_nightshift import webui  # noqa: E402


class WebUiTests(unittest.TestCase):
    def test_build_state_marks_selected_sessions(self):
        session = SimpleNamespace(
            session_id="session-1",
            title="Fix API",
            cwd="C:/project/api",
            last_active=datetime.now(timezone.utc),
        )
        status = SimpleNamespace(
            state="interrupted",
            reason="quota exhausted",
            confidence="high",
        )
        quota = SimpleNamespace(
            primary=SimpleNamespace(used_percent=75, resets_at=2_000_000_000),
            secondary=SimpleNamespace(used_percent=20, resets_at=2_000_100_000),
        )
        originals = {
            "load_config": webui.load_config,
            "load_registry": webui.load_registry,
            "latest_quota": webui.latest_quota,
            "list_recent_sessions": webui.list_recent_sessions,
            "analyze_session": webui.analyze_session,
        }
        webui.load_config = lambda: {
            "gui": {"session_days": 14, "session_limit": 100},
            "watch": {"idle_min": 5},
        }
        webui.load_registry = lambda: {
            "threads": {
                "session-1": {
                    "strict": True,
                    "resume_prompt": "Run the tests first.",
                    "scheduled_command": {
                        "enabled": True,
                        "run_at": "2026-06-16T23:00",
                        "repeat": "daily",
                        "prompt": "Send a nightly report.",
                    },
                    "last_result": "success",
                }
            }
        }
        webui.latest_quota = lambda: quota
        webui.list_recent_sessions = lambda **kwargs: [session]
        webui.analyze_session = lambda *args: status
        try:
            state = webui.build_state()
        finally:
            for name, value in originals.items():
                setattr(webui, name, value)
        self.assertEqual(state["adopted_count"], 1)
        self.assertTrue(state["sessions"][0]["auto"])
        self.assertTrue(state["sessions"][0]["strict"])
        self.assertEqual(state["sessions"][0]["resume_prompt"], "Run the tests first.")
        self.assertEqual(
            state["sessions"][0]["scheduled_command"]["prompt"],
            "Send a nightly report.",
        )
        self.assertEqual(state["quota"]["primary"]["used"], 75)

    def test_auto_toggle_adopts_or_removes(self):
        session = SimpleNamespace(
            session_id="session-1",
            transcript=Path("session.jsonl"),
            cwd="C:/project",
            title="Task",
        )
        calls = []
        originals = {
            "resolve_session": webui.resolve_session,
            "adopt_thread": webui.adopt_thread,
            "remove_thread": webui.remove_thread,
        }
        webui.resolve_session = lambda query: session
        webui.adopt_thread = lambda *args: calls.append(("adopt", args))
        webui.remove_thread = lambda query: calls.append(("remove", query)) or query
        try:
            enabled = webui.set_session_auto("session-1", True, True)
            disabled = webui.set_session_auto("session-1", False)
        finally:
            for name, value in originals.items():
                setattr(webui, name, value)
        self.assertTrue(enabled["auto"])
        self.assertFalse(disabled["auto"])
        self.assertEqual(calls[0][0], "adopt")
        self.assertEqual(calls[1], ("remove", "session-1"))

    def test_prompt_update_requires_adopted_session(self):
        original = webui.set_thread_prompt
        calls = []
        webui.set_thread_prompt = lambda session_id, prompt: calls.append(
            (session_id, prompt)
        ) or session_id == "session-1"
        try:
            saved = webui.set_session_prompt("session-1", "  Run tests.  ")
            cleared = webui.set_session_prompt("session-1", " ")
            missing = webui.set_session_prompt("missing", "Continue")
            too_long = webui.set_session_prompt("session-1", "x" * 4001)
        finally:
            webui.set_thread_prompt = original
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["resume_prompt"], "Run tests.")
        self.assertEqual(cleared["resume_prompt"], "")
        self.assertFalse(missing["ok"])
        self.assertFalse(too_long["ok"])
        self.assertEqual(calls[:2], [("session-1", "Run tests."), ("session-1", "")])

    def test_schedule_update_requires_adopted_session(self):
        original = webui.set_thread_schedule
        calls = []
        webui.set_thread_schedule = lambda session_id, schedule: calls.append(
            (session_id, schedule)
        ) or session_id == "session-1"
        try:
            saved = webui.set_session_schedule(
                "session-1",
                True,
                "2026-06-16T23:30",
                "daily",
                "  Summarize progress.  ",
            )
            disabled = webui.set_session_schedule(
                "session-1", False, "", "once", ""
            )
            missing = webui.set_session_schedule(
                "missing", True, "2026-06-16T23:30", "once", "Run"
            )
            invalid = webui.set_session_schedule(
                "session-1", True, "bad-time", "once", "Run"
            )
        finally:
            webui.set_thread_schedule = original
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["scheduled_command"]["prompt"], "Summarize progress.")
        self.assertEqual(saved["scheduled_command"]["repeat"], "daily")
        self.assertFalse(disabled["scheduled_command"]["enabled"])
        self.assertFalse(missing["ok"])
        self.assertFalse(invalid["ok"])
        self.assertEqual(calls[0][0], "session-1")

    def test_static_assets_exist(self):
        for name in ("index.html", "style.css", "app.js"):
            self.assertTrue((webui.WEB_ROOT / name).is_file())


if __name__ == "__main__":
    unittest.main()
