import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("CODEX_HOME", tempfile.mkdtemp(prefix="cn_codex_"))
os.environ.setdefault("CODEX_NIGHTSHIFT_HOME", tempfile.mkdtemp(prefix="cn_data_"))

from codex_nightshift.transcripts import (  # noqa: E402
    analyze_session,
    latest_quota,
    read_session_info,
    resolve_session,
)


def _event(ts: datetime, payload_type: str, **payload):
    return {
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "type": "event_msg",
        "payload": {"type": payload_type, **payload},
    }


def _token(ts: datetime, primary=20, secondary=5, p_reset=None, s_reset=None):
    return _event(
        ts,
        "token_count",
        rate_limits={
            "primary": {
                "used_percent": primary,
                "window_minutes": 300,
                "resets_at": p_reset or int(time.time()) + 3600,
            },
            "secondary": {
                "used_percent": secondary,
                "window_minutes": 10080,
                "resets_at": s_reset or int(time.time()) + 86400,
            },
        },
    )


def _write(root: Path, session_id: str, events: list[dict], age_min=30) -> Path:
    folder = root / "2026" / "06" / "15"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-test-{session_id}.jsonl"
    meta = {
        "timestamp": events[0]["timestamp"],
        "type": "session_meta",
        "payload": {"id": session_id, "cwd": str(root / "project")},
    }
    path.write_text("\n".join(json.dumps(row) for row in [meta, *events]) + "\n")
    old = time.time() - age_min * 60
    os.utime(path, (old, old))
    return path


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cn_sessions_"))
        self.now = datetime.now(timezone.utc)

    def _status(self, events, age_min=30):
        path = _write(self.root, "session-1", events, age_min)
        return analyze_session(read_session_info(path), idle_min_threshold=5, now=self.now)

    def test_completed_turn_is_not_resumable(self):
        status = self._status(
            [
                _event(self.now - timedelta(hours=1), "task_started", turn_id="t1"),
                _event(self.now - timedelta(minutes=50), "task_complete", turn_id="t1"),
            ]
        )
        self.assertEqual(status.state, "complete")

    def test_aborted_turn_is_stopped_and_not_resumable(self):
        status = self._status(
            [
                _event(self.now - timedelta(hours=1), "task_started", turn_id="t1"),
                _event(self.now - timedelta(minutes=50), "turn_aborted", turn_id="t1"),
            ]
        )
        self.assertEqual(status.state, "stopped")
        self.assertFalse(status.resumable)

    def test_recent_unfinished_turn_is_active(self):
        status = self._status(
            [_event(self.now - timedelta(minutes=1), "task_started", turn_id="t1")],
            age_min=0,
        )
        self.assertEqual(status.state, "active")

    def test_idle_unfinished_turn_is_medium_confidence(self):
        status = self._status(
            [_event(self.now - timedelta(hours=1), "task_started", turn_id="t1")]
        )
        self.assertTrue(status.resumable)
        self.assertEqual(status.confidence, "medium")

    def test_exhausted_quota_is_high_confidence(self):
        status = self._status(
            [
                _event(self.now - timedelta(hours=1), "task_started", turn_id="t1"),
                _token(self.now - timedelta(minutes=50), primary=100),
            ]
        )
        self.assertTrue(status.resumable)
        self.assertEqual(status.reason, "quota exhausted")
        self.assertEqual(status.confidence, "high")

    def test_expired_exhausted_snapshot_remains_high_confidence(self):
        status = self._status(
            [
                _event(self.now - timedelta(hours=2), "task_started", turn_id="t1"),
                _token(
                    self.now - timedelta(hours=1),
                    primary=100,
                    p_reset=int(time.time()) - 60,
                ),
            ]
        )
        self.assertEqual(status.confidence, "high")
        self.assertIsNone(status.quota.blocked_until())

    def test_exhausted_snapshot_before_current_turn_is_not_high_confidence(self):
        status = self._status(
            [
                _token(self.now - timedelta(hours=2), primary=100),
                _event(self.now - timedelta(hours=1), "task_started", turn_id="t2"),
            ]
        )
        self.assertEqual(status.confidence, "medium")
        self.assertEqual(status.reason, "unfinished turn became idle")

    def test_latest_quota_across_sessions(self):
        _write(self.root, "old", [_token(self.now - timedelta(hours=2), primary=10)])
        _write(self.root, "new", [_token(self.now - timedelta(hours=1), primary=40)])
        quota = latest_quota(self.root)
        self.assertEqual(quota.primary.used_percent, 40)

    def test_resolve_unique_prefix_and_last(self):
        index = self.root / "index.jsonl"
        first = _write(self.root, "abc-111", [_event(self.now, "task_complete")], age_min=20)
        second = _write(self.root, "def-222", [_event(self.now, "task_complete")], age_min=10)
        index.write_text("")
        self.assertEqual(resolve_session("abc", root=self.root, index_path=index).transcript, first)
        self.assertEqual(resolve_session("last", root=self.root, index_path=index).transcript, second)


if __name__ == "__main__":
    unittest.main()
