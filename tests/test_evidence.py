import unittest
from types import SimpleNamespace
from uuid import uuid4

from tellyq.cast import media_observation, select_device
from tellyq.evidence import playback_evidence, video_id


def sample(position, time, **changes):
    return {
        "kind": "media",
        "content_id": "FozIp7Va7dY",
        "player_state": "PLAYING",
        "position": position,
        "monotonic": time,
        "media_session_id": 1,
        **changes,
    }


class EvidenceTests(unittest.TestCase):
    def test_app_launch_does_not_confirm_playback(self):
        result = playback_evidence([{"kind": "receiver", "app_name": "YouTube"}], "FozIp7Va7dY")
        self.assertFalse(result["receiver_playback_confirmed"])

    def test_missing_sample_time_cannot_confirm_progress(self):
        second = sample(7, 3)
        del second["monotonic"]
        result = playback_evidence([sample(5, 1), second], "FozIp7Va7dY")
        self.assertFalse(result["receiver_playback_confirmed"])

    def test_requires_identity_and_two_advancing_observations(self):
        self.assertTrue(
            playback_evidence([sample(5, 1), sample(7, 3)], "FozIp7Va7dY")[
                "receiver_playback_confirmed"
            ]
        )
        for events in (
            [sample(5, 1)],
            [sample(5, 1), sample(5, 3)],
            [sample(5, 1), sample(7, 3, content_id="another-video")],
            [sample(5, 1), sample(7, 3, media_session_id=2)],
            [sample(5, 1), sample(7, 3, player_state="BUFFERING")],
            [sample(5, 1), sample(7, 3, ad_break=True)],
        ):
            with self.subTest(events=events):
                self.assertFalse(
                    playback_evidence(events, "FozIp7Va7dY")["receiver_playback_confirmed"]
                )

    def test_missing_receiver_values_remain_unknown(self):
        result = media_observation({"status": [{"playerState": "PLAYING"}]})
        self.assertIsNone(result["content_id"])
        self.assertIsNone(result["position"])
        self.assertIsNone(result["duration"])

    def test_empty_status_does_not_reuse_old_media(self):
        self.assertEqual(media_observation({"status": []}), {"kind": "media", "empty_status": True})

    def test_youtube_url_identity_is_exact(self):
        self.assertEqual(video_id("https://www.youtube.com/watch?v=FozIp7Va7dY"), "FozIp7Va7dY")
        self.assertIsNone(video_id("https://example.com/watch?v=FozIp7Va7dY"))

    def test_never_selects_first_device_as_fallback(self):
        target = SimpleNamespace(uuid=uuid4())
        other = SimpleNamespace(uuid=uuid4())
        self.assertIs(select_device([other, target], str(target.uuid)), target)
        with self.assertRaises(ValueError):
            select_device([other], str(target.uuid))


if __name__ == "__main__":
    unittest.main()
