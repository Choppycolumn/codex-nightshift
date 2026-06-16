import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("CODEX_HOME", tempfile.mkdtemp(prefix="cn_codex_"))
os.environ.setdefault("CODEX_NIGHTSHIFT_HOME", tempfile.mkdtemp(prefix="cn_data_"))

from codex_nightshift import runner  # noqa: E402
from codex_nightshift.transcripts import SessionInfo  # noqa: E402


class RunnerTests(unittest.TestCase):
    def test_resume_command_is_noninteractive_and_workspace_scoped(self):
        config = {
            "codex_cmd": "codex",
            "resume": {
                "sandbox": "workspace-write",
                "prompt": "continue",
                "extra_args": [],
            },
        }
        original = runner.find_codex_cmd
        runner.find_codex_cmd = lambda config: "codex"
        try:
            command = runner.build_resume_command("session-1", config)
        finally:
            runner.find_codex_cmd = original
        self.assertEqual(command[:3], ["codex", "exec", "--json"])
        self.assertIn("workspace-write", command)
        self.assertEqual(command[-3:], ["resume", "session-1", "continue"])

    def test_task_prompt_overrides_default_and_blank_falls_back(self):
        config = {
            "codex_cmd": "codex",
            "resume": {
                "sandbox": "workspace-write",
                "prompt": "default prompt",
                "extra_args": [],
            },
        }
        original = runner.find_codex_cmd
        runner.find_codex_cmd = lambda config: "codex"
        try:
            custom = runner.build_resume_command("session-1", config, "custom prompt")
            fallback = runner.build_resume_command("session-1", config, "  ")
        finally:
            runner.find_codex_cmd = original
        self.assertEqual(custom[-1], "custom prompt")
        self.assertEqual(fallback[-1], "default prompt")

    def test_dry_run_does_not_update_adopted_state(self):
        descriptor, name = tempfile.mkstemp(prefix="cn_transcript_", suffix=".jsonl")
        os.close(descriptor)
        transcript = Path(name)
        session = SessionInfo(
            "session-1",
            transcript,
            str(transcript.parent),
            "test",
            datetime.now(timezone.utc),
        )
        config = {
            "watch": {
                "max_resumes_per_cycle": 1,
                "idle_min": 5,
                "retry_backoff_sec": 300,
            },
            "resume": {"resume_incomplete": True},
        }
        registry = {
            "threads": {
                "session-1": {
                    "session_id": "session-1",
                    "transcript": str(transcript),
                    "cwd": str(transcript.parent),
                    "title": "test",
                    "enabled": True,
                    "resume_prompt": "Run targeted tests before continuing.",
                }
            }
        }
        status = SimpleNamespace(
            resumable=True,
            confidence="high",
            reason="quota exhausted",
            session=session,
            fingerprint="fp",
            quota=None,
        )
        originals = {
            "load_config": runner.load_config,
            "load_registry": runner.load_registry,
            "latest_quota": runner.latest_quota,
            "analyze_session": runner.analyze_session,
            "resume_session": runner.resume_session,
            "update_thread": runner.update_thread,
        }
        runner.load_config = lambda: config
        runner.load_registry = lambda: registry
        runner.latest_quota = lambda: None
        runner.analyze_session = lambda *args, **kwargs: status
        prompts = []
        runner.resume_session = lambda *args, **kwargs: (
            prompts.append(kwargs.get("prompt"))
            or runner.RunResult(
                True, False, 0, transcript.with_suffix(".log"), ["codex", "resume"]
            )
        )
        runner.update_thread = lambda *args, **kwargs: self.fail("dry run mutated state")
        try:
            resumed, _ = runner.run_cycle(dry_run=True)
        finally:
            for name, value in originals.items():
                setattr(runner, name, value)
            transcript.unlink(missing_ok=True)
        self.assertEqual(resumed, 1)
        self.assertEqual(prompts, ["Run targeted tests before continuing."])

    def test_watch_lock_rejects_second_watcher(self):
        original = runner.WATCH_LOCK
        runner.WATCH_LOCK = Path(tempfile.mkdtemp(prefix="cn_lock_")) / "watch.lock"
        try:
            runner._acquire_watch_lock()
            with self.assertRaises(RuntimeError):
                runner._acquire_watch_lock()
        finally:
            runner.WATCH_LOCK.unlink(missing_ok=True)
            runner.WATCH_LOCK = original

    def test_stale_legacy_lock_does_not_block_watcher(self):
        original_lock = runner.WATCH_LOCK
        original_alive = runner._pid_is_alive
        lock = Path(tempfile.mkdtemp(prefix="cn_lock_")) / "watch.lock"
        lock.write_text(str(os.getpid()), encoding="ascii")
        runner.WATCH_LOCK = lock
        runner._pid_is_alive = lambda pid: True
        try:
            runner._acquire_watch_lock()
            data = lock.read_text(encoding="ascii")
        finally:
            lock.unlink(missing_ok=True)
            runner.WATCH_LOCK = original_lock
            runner._pid_is_alive = original_alive
        self.assertIn("started_at", data)

    def test_successful_fingerprint_is_not_resumed_twice(self):
        descriptor, name = tempfile.mkstemp(prefix="cn_transcript_", suffix=".jsonl")
        os.close(descriptor)
        transcript = Path(name)
        config = {
            "watch": {
                "max_resumes_per_cycle": 1,
                "idle_min": 5,
                "retry_backoff_sec": 0,
            },
            "resume": {"resume_incomplete": True},
        }
        registry = {
            "threads": {
                "session-1": {
                    "session_id": "session-1",
                    "transcript": str(transcript),
                    "cwd": str(transcript.parent),
                    "title": "test",
                    "enabled": True,
                    "last_fingerprint": "fp",
                    "last_result": "success",
                }
            }
        }
        status = SimpleNamespace(
            resumable=True,
            confidence="high",
            reason="quota exhausted",
            fingerprint="fp",
            quota=None,
        )
        originals = {
            "load_config": runner.load_config,
            "load_registry": runner.load_registry,
            "latest_quota": runner.latest_quota,
            "analyze_session": runner.analyze_session,
            "resume_session": runner.resume_session,
        }
        runner.load_config = lambda: config
        runner.load_registry = lambda: registry
        runner.latest_quota = lambda: None
        runner.analyze_session = lambda *args, **kwargs: status
        runner.resume_session = lambda *args, **kwargs: self.fail("duplicate resume")
        try:
            resumed, _ = runner.run_cycle()
        finally:
            for attr, value in originals.items():
                setattr(runner, attr, value)
            transcript.unlink(missing_ok=True)
        self.assertEqual(resumed, 0)

    def test_due_scheduled_command_runs_even_when_session_is_complete(self):
        descriptor, name = tempfile.mkstemp(prefix="cn_transcript_", suffix=".jsonl")
        os.close(descriptor)
        transcript = Path(name)
        config = {
            "watch": {
                "max_resumes_per_cycle": 1,
                "idle_min": 5,
                "retry_backoff_sec": 0,
            },
            "resume": {"resume_incomplete": True},
        }
        registry = {
            "threads": {
                "session-1": {
                    "session_id": "session-1",
                    "transcript": str(transcript),
                    "cwd": str(transcript.parent),
                    "title": "test",
                    "enabled": True,
                    "scheduled_command": {
                        "enabled": True,
                        "run_at": "2000-01-01T00:00",
                        "repeat": "once",
                        "prompt": "Check CI and report back.",
                    },
                }
            }
        }
        updates = {}
        prompts = []
        originals = {
            "load_config": runner.load_config,
            "load_registry": runner.load_registry,
            "latest_quota": runner.latest_quota,
            "analyze_session": runner.analyze_session,
            "resume_session": runner.resume_session,
            "update_thread": runner.update_thread,
        }
        runner.load_config = lambda: config
        runner.load_registry = lambda: registry
        runner.latest_quota = lambda: None
        runner.analyze_session = lambda *args, **kwargs: self.fail("schedule waited for interruption")
        runner.resume_session = lambda *args, **kwargs: (
            prompts.append(kwargs.get("prompt"))
            or runner.RunResult(
                True, False, 0, transcript.with_suffix(".log"), ["codex", "resume"]
            )
        )
        runner.update_thread = lambda session_id, **kwargs: updates.update(kwargs)
        try:
            resumed, _ = runner.run_cycle()
        finally:
            for attr, value in originals.items():
                setattr(runner, attr, value)
            transcript.unlink(missing_ok=True)
        self.assertEqual(resumed, 1)
        self.assertEqual(prompts, ["Check CI and report back."])
        self.assertFalse(updates["scheduled_command"]["enabled"])
        self.assertEqual(updates["last_result"], "scheduled-success")


if __name__ == "__main__":
    unittest.main()
