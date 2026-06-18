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
        self.assertEqual(command[-3:], ["resume", "session-1", "-"])

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
            custom = runner._effective_prompt(config, "custom prompt")
            fallback = runner._effective_prompt(config, "  ")
        finally:
            runner.find_codex_cmd = original
        self.assertEqual(custom, "custom prompt")
        self.assertEqual(fallback, "default prompt")

    def test_retry_time_is_parsed_from_limit_message(self):
        now = datetime(2026, 6, 18, 10, 55, tzinfo=timezone.utc).timestamp()
        retry_at = runner._retry_at_from_output(
            "You've hit your usage limit. Try again at 2:14 PM.", now=now
        )
        parsed = datetime.fromtimestamp(retry_at).astimezone()
        self.assertEqual((parsed.hour, parsed.minute), (14, 14))

    def test_long_prompt_is_sent_over_stdin(self):
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
            "codex_cmd": "codex",
            "resume": {
                "sandbox": "workspace-write",
                "prompt": "default prompt",
                "extra_args": [],
                "timeout_min": 1,
            },
        }
        prompt = "x" * 100_000
        captured = {}
        originals = {
            "find_codex_cmd": runner.find_codex_cmd,
            "run": runner.subprocess.run,
        }
        runner.find_codex_cmd = lambda config: "codex"

        def fake_run(command, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        runner.subprocess.run = fake_run
        try:
            result = runner.resume_session(session, config=config, prompt=prompt)
        finally:
            for attr, value in originals.items():
                setattr(runner, attr, value)
            transcript.unlink(missing_ok=True)
        self.assertTrue(result.success)
        self.assertEqual(result.command[-1], "-")
        self.assertEqual(captured["input"], prompt)
        if os.name == "nt":
            self.assertNotEqual(captured["creationflags"], 0)

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

    def test_failed_resume_pauses_thread_instead_of_retrying(self):
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
                }
            }
        }
        session = SessionInfo(
            "session-1",
            transcript,
            str(transcript.parent),
            "test",
            datetime.now(timezone.utc),
        )
        status = SimpleNamespace(
            resumable=True,
            confidence="high",
            reason="turn ended without a final answer",
            session=session,
            fingerprint="fp",
            quota=None,
        )
        updates = {}
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
        runner.resume_session = lambda *args, **kwargs: runner.RunResult(
            False, False, 1, transcript.with_suffix(".log"), ["codex", "resume"]
        )
        runner.update_thread = lambda session_id, **kwargs: updates.update(kwargs)
        try:
            resumed, _ = runner.run_cycle()
        finally:
            for attr, value in originals.items():
                setattr(runner, attr, value)
            transcript.unlink(missing_ok=True)
        self.assertEqual(resumed, 1)
        self.assertFalse(updates["enabled"])
        self.assertEqual(updates["last_result"], "failed")
        self.assertIn("exit code 1", updates["pause_reason"])

    def test_limit_result_waits_until_reported_retry_time(self):
        descriptor, name = tempfile.mkstemp(prefix="cn_transcript_", suffix=".jsonl")
        os.close(descriptor)
        transcript = Path(name)
        retry_at = datetime.now(timezone.utc).timestamp() + 3600
        config = {
            "watch": {
                "max_resumes_per_cycle": 1,
                "idle_min": 5,
                "retry_backoff_sec": 0,
            },
            "resume": {"resume_incomplete": True},
        }
        entry = {
            "session_id": "session-1",
            "transcript": str(transcript),
            "cwd": str(transcript.parent),
            "title": "test",
            "enabled": True,
        }
        registry = {"threads": {"session-1": entry}}
        session = SessionInfo(
            "session-1",
            transcript,
            str(transcript.parent),
            "test",
            datetime.now(timezone.utc),
        )
        status = SimpleNamespace(
            resumable=True,
            confidence="high",
            reason="quota exhausted",
            session=session,
            fingerprint="fp",
            quota=None,
        )
        updates = {}
        attempts = []
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
        runner.resume_session = lambda *args, **kwargs: (
            attempts.append(1)
            or runner.RunResult(
                False,
                True,
                1,
                transcript.with_suffix(".log"),
                ["codex", "resume"],
                retry_at,
            )
        )
        runner.update_thread = lambda session_id, **kwargs: updates.update(kwargs)
        try:
            first, _ = runner.run_cycle()
            entry.update(updates)
            second, blocked = runner.run_cycle()
        finally:
            for attr, value in originals.items():
                setattr(runner, attr, value)
            transcript.unlink(missing_ok=True)
        self.assertEqual((first, second), (1, 0))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(updates["retry_after"], retry_at)
        self.assertEqual(blocked, retry_at)

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
