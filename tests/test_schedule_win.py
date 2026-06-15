import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("CODEX_NIGHTSHIFT_HOME", tempfile.mkdtemp(prefix="cn_data_"))

from codex_nightshift import schedule_win  # noqa: E402
from codex_nightshift import runner  # noqa: E402


class ScheduleWindowsTests(unittest.TestCase):
    def test_install_registers_logon_task(self):
        original_run = schedule_win._run_ps
        original_name = schedule_win.os.name
        captured = {}

        def fake_run(command):
            captured["command"] = command
            return SimpleNamespace(returncode=0, stdout="installed", stderr="")

        schedule_win._run_ps = fake_run
        try:
            if original_name != "nt":
                schedule_win.os.name = "nt"
            ok, _ = schedule_win.install(start_now=True)
        finally:
            schedule_win._run_ps = original_run
            schedule_win.os.name = original_name
        self.assertTrue(ok)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", captured["command"])
        self.assertIn("Start-ScheduledTask", captured["command"])
        self.assertIn("-m codex_nightshift watch", captured["command"])
        self.assertIn("-WorkingDirectory", captured["command"])

    def test_cleanup_removes_dead_process_lock(self):
        original_lock = runner.WATCH_LOCK
        original_alive = runner._lock_owner_alive
        lock = Path(tempfile.mkdtemp(prefix="cn_lock_")) / "watch.lock"
        lock.write_text("123", encoding="ascii")
        runner.WATCH_LOCK = lock
        runner._lock_owner_alive = lambda: False
        try:
            schedule_win._cleanup_stale_watch_lock(wait_sec=0)
        finally:
            runner.WATCH_LOCK = original_lock
            runner._lock_owner_alive = original_alive
        self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
