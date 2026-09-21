import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from tellyq.controller import current_state, execute
from tellyq.models import Observation
from tellyq.state import command_lock, load_queue, save


class FakeConnection:
    def __init__(self):
        self.playing = False
        self.stopped = False
        self.other_session = False
        self.youtube = SimpleNamespace(play_video=Mock(side_effect=self.play))
        self.cast = SimpleNamespace(quit_app=Mock(side_effect=self.stop))

    def play(self, _video):
        self.playing = True

    def stop(self, **_kwargs):
        self.stopped = True

    def observe(self, _seconds: float) -> list[Observation]:
        t = monotonic()
        receiver: Observation = {
            "kind": "receiver",
            "monotonic": t,
            "app_name": "Backdrop" if self.stopped else "YouTube",
            "app_id": "backdrop" if self.stopped else "youtube",
            "app_session_id": "other" if self.other_session else "session",
        }
        events: list[Observation] = [receiver]
        if self.playing and not self.stopped:
            for index in (0, 2):
                events.append(
                    {
                        "kind": "media",
                        "monotonic": t + index,
                        "content_id": "FozIp7Va7dY",
                        "player_state": "PLAYING",
                        "media_session_id": 1,
                        "position": 10 + index,
                    }
                )
        return events


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.device_id = str(uuid4())
        self.connection = FakeConnection()

        @contextmanager
        def fake_connect(device_id):
            self.assertEqual(device_id, self.device_id)
            yield self.connection, {"uuid": device_id, "name": "Test receiver"}

        self.patcher = patch("tellyq.controller.connect", fake_connect)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def queue_and_start(self):
        report, code = execute("queue", self.runtime, self.device_id)
        self.assertEqual(code, 0)
        self.connection.youtube.play_video.assert_not_called()
        report, code = execute("start", self.runtime)
        self.assertEqual(code, 0)
        self.assertEqual(report["state"], "playing")
        return report

    def test_queue_start_status_stop_and_persistence(self):
        self.queue_and_start()
        for _ in range(2):
            report, code = execute("status", self.runtime)
            self.assertEqual(code, 0)
            self.assertEqual(report["commands"], [])
        self.connection.youtube.play_video.assert_called_once()
        report, code = execute("stop", self.runtime)
        self.assertEqual(code, 0)
        self.assertTrue(report["stop_confirmed"])
        self.assertEqual(load_queue(self.runtime)["items"][0]["state"], "stopped")

    def test_duplicate_start_does_not_restart_program(self):
        self.queue_and_start()
        _report, code = execute("start", self.runtime)
        self.assertEqual(code, 1)
        self.connection.youtube.play_video.assert_called_once()
        self.assertEqual(load_queue(self.runtime)["items"][0]["state"], "playing")

    def test_stop_refuses_a_replacement_session(self):
        self.queue_and_start()
        self.connection.other_session = True
        _report, code = execute("stop", self.runtime)
        self.assertEqual(code, 1)
        self.connection.cast.quit_app.assert_not_called()

    def test_command_return_without_evidence_stays_unconfirmed(self):
        execute("queue", self.runtime, self.device_id)
        self.connection.youtube.play_video.side_effect = None
        report, code = execute("start", self.runtime)
        self.assertEqual(code, 0)
        self.assertTrue(report["commands"][0]["returned"])
        self.assertEqual(report["state"], "unconfirmed")

    def test_uncertain_network_failure_is_not_playback_success(self):
        execute("queue", self.runtime, self.device_id)
        self.connection.youtube.play_video.side_effect = TimeoutError("timed out")
        report, code = execute("start", self.runtime)
        self.assertEqual(code, 1)
        self.assertEqual(report["state"], "unconfirmed")
        self.assertEqual(load_queue(self.runtime)["items"][0]["state"], "unconfirmed")

    def test_runtime_duration_never_means_finished(self):
        events: list[Observation] = [
            {
                "kind": "media",
                "content_id": "FozIp7Va7dY",
                "player_state": "BUFFERING",
                "position": 1623,
                "duration": 1623,
            }
        ]
        self.assertEqual(
            current_state(events, {"receiver_playback_confirmed": False}), "unconfirmed"
        )
        events[0].update(player_state="IDLE", idle_reason="FINISHED")
        self.assertEqual(current_state(events, {"receiver_playback_confirmed": False}), "finished")

    def test_app_exit_invalidates_old_playing_state(self):
        events: list[Observation] = [
            {"kind": "media", "content_id": "FozIp7Va7dY", "player_state": "PLAYING"},
            {"kind": "receiver", "app_name": "Backdrop"},
        ]
        self.assertEqual(
            current_state(events, {"receiver_playback_confirmed": True}), "unconfirmed"
        )

    def test_status_persists_uncertainty_without_restarting(self):
        self.queue_and_start()
        self.connection.playing = False
        _report, code = execute("status", self.runtime)
        self.assertEqual(code, 0)
        self.assertEqual(load_queue(self.runtime)["items"][0]["state"], "unconfirmed")
        self.connection.youtube.play_video.assert_called_once()

    def test_process_lock_rejects_overlapping_commands(self):
        with (
            command_lock(self.runtime),
            self.assertRaises(RuntimeError),
            command_lock(self.runtime),
        ):
            self.fail("second command must not enter")

    def test_saved_state_is_private_and_no_temporary_file_remains(self):
        path = self.runtime / "state.json"
        save(path, {"value": 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.runtime.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
