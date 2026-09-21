"""Synthetic metadata privacy/structure regressions; never receiver acceptance evidence."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from queue import Queue
from runpy import run_path

import pytest

from tellyq.cast_backend import CastBackend, open_backend
from tellyq.domain.values import PlaybackTarget
from tellyq.models import YouTubeMetadataRecord
from tellyq.youtube_metadata import MetadataJournal, YouTubeMetadataProbe, project_metadata
from tests.playback_support import FakeClock, FakeTransport

CONTENT = "abcdefghijk"
TARGET = PlaybackTarget("synthetic", "cast")


def message(**changes):
    return {
        "type": "MEDIA_STATUS",
        "status": [
            {
                "playerState": "PLAYING",
                "mediaSessionId": 17,
                "currentTime": 4,
                "media": {"contentId": CONTENT, "duration": 10, "customData": {}},
                "customData": {},
                **changes,
            }
        ],
    }


def project(data, **kwargs):
    return project_metadata(
        data,
        requested_content=CONTENT,
        sequence=1,
        observed_at="2026-09-21T12:00:00+00:00",
        monotonic=100,
        **kwargs,
    )


def test_empty_custom_data_is_distinct_from_absent_and_invalid():
    sample, schema = project(message())
    assert schema is None
    assert sample["custom_data"]["status"]["shape"] == "valid"
    assert sample["custom_data"]["status"]["entries"] == 0
    assert sample["custom_data"]["status"]["types"]["object"] == 1
    assert sample["custom_data"]["extended_media"]["shape"] == "unavailable"
    data = message(media={"contentId": CONTENT}, customData=None)
    sample, _ = project(data)
    assert sample["custom_data"]["media"]["shape"] == "absent"
    assert sample["custom_data"]["status"]["shape"] == "null"
    sample, _ = project(message(customData=[True, "PRIVATE"]))
    assert sample["custom_data"]["status"]["shape"] == "invalid"
    assert sample["custom_data"]["status"]["types"]["string"] == 1


def test_projection_never_serializes_arbitrary_names_or_scalar_values():
    data = message(
        customData={
            "privateTitle": "PRIVATE_TITLE",
            "https://private.invalid/token": "SECRET_VALUE",
            "nested": {"duration": 9999, "playingAd": False, "odd": ["OTHER_SECRET"]},
        }
    )
    sample, _ = project(data)
    wire = json.dumps(sample)
    for secret in ("privateTitle", "private.invalid", "SECRET", "nested", "playingAd", "9999"):
        assert secret not in wire
    assert sample["duration"] == 10
    assert sample["position"] == 4
    assert sample["ad_break"] is None
    assert sample["content_relation"] == "requested"
    assert CONTENT not in wire


def test_private_schema_opt_in_has_names_and_types_but_never_values():
    sample, schema = project(
        message(
            customData={
                "candidate": {"duration": 987654, "isAd": False},
                "AUTHtoken": "SECRET",
                "https://private": "SECRET",
                "a" * 49: "SECRET",
            }
        ),
        private_schema=True,
    )
    assert schema is not None
    private = json.dumps(schema)
    assert "candidate" in private and "duration" in private and "isAd" in private
    for private_value in ("987654", "SECRET", "AUTHtoken", "https://private", "a" * 49):
        assert private_value not in private
    assert "[redacted-key]" in private
    assert "candidate" not in json.dumps(sample)


def test_missing_terminal_fields_are_not_filled_from_previous_sample():
    probe = YouTubeMetadataProbe(CONTENT)
    probe.receive(message(customData={"playerState": 1081}), observed_at="first", monotonic=1)
    probe.receive(
        message(playerState="IDLE", idleReason="FINISHED", media=None),
        observed_at="second",
        monotonic=2,
    )
    pending, dropped = probe.drain()
    terminal = pending[-1][0]
    assert terminal["media_session_relation"] == "same"
    assert terminal["content_relation"] == "unknown"
    assert terminal["duration"] is None
    assert terminal["ad_break"] is None
    assert pending[0][0]["custom_player_state"] == 1081
    assert terminal["custom_player_state"] is None
    assert terminal["custom_player_state_shape"] == "absent"
    assert dropped == 0
    probe.receive(message(media={"contentId": "new-title"}), observed_at="third", monotonic=3)
    assert probe.drain()[0][0][0]["content_relation"] == "other"


@pytest.mark.parametrize("code", [1081, 0, -1, 987654, -(2**31), 2**31 - 1])
def test_reviewed_player_state_scalar_is_opaque_and_does_not_establish_ads(code):
    sample, _ = project(message(customData={"playerState": code, "isAd": False}))
    assert sample["custom_player_state"] == code
    assert sample["custom_player_state_shape"] == "valid"
    assert sample["player_state"] == "PLAYING"
    assert sample["ad_break"] is None


@pytest.mark.parametrize(
    "code", [True, False, 1081.0, "1081", 2**31, -(2**31) - 1, [], {}, float("nan")]
)
def test_reviewed_player_state_rejects_coercion_and_out_of_range_values(code):
    sample, _ = project(message(customData={"playerState": code}))
    assert sample["custom_player_state"] is None
    assert sample["custom_player_state_shape"] == "invalid"
    json.dumps(sample, allow_nan=False)


def test_reviewed_player_state_has_exact_current_status_path_only():
    sample, _ = project(message(customData={"playerState": None}))
    assert sample["custom_player_state"] is None
    assert sample["custom_player_state_shape"] == "null"
    sample, _ = project(message(customData=None))
    assert sample["custom_player_state_shape"] == "unavailable"
    data = message(
        media={
            "customData": {"playerState": 1081, "currentIndex": 999999, "listId": "PRIVATE_LIST"}
        },
        customData={"nested": {"playerState": 1081}},
    )
    sample, _ = project(data)
    assert sample["custom_player_state"] is None
    assert sample["custom_player_state_shape"] == "absent"
    for value in ("1081", "999999", "PRIVATE_LIST", "currentIndex", "listId"):
        assert value not in json.dumps(sample)


@pytest.mark.parametrize("value", [True, float("inf"), float("nan"), -1, "10"])
def test_invalid_duration_does_not_become_numeric_context(value):
    sample, _ = project(message(media={"duration": value}))
    assert sample["duration"] is None
    json.dumps(sample, allow_nan=False)


def test_projection_limits_deep_wide_and_cyclic_unknown_data():
    root = {}
    root["cycle"] = root
    root["wide"] = list(range(1000))
    deep = {}
    for _ in range(100):
        deep = {"next": deep}
    root["deep"] = deep
    sample, schema = project(message(customData=root), private_schema=True)
    summary = sample["custom_data"]["status"]
    assert summary["inspected_nodes"] <= 128
    assert summary["maximum_depth"] <= 6
    assert summary["truncated"] is True
    assert schema is not None and len(schema["fields"]["status"]) <= 128
    json.dumps(sample, allow_nan=False)


def test_probe_queue_reports_drops_and_missing_session_clears_comparison():
    probe = YouTubeMetadataProbe(CONTENT)
    probe.receive({"type": "OTHER", "SECRET": "SECRET"}, observed_at="now", monotonic=0)
    for sequence in range(300):
        probe.receive(message(), observed_at="now", monotonic=sequence)
    pending, dropped = probe.drain()
    assert len(pending) == 256
    assert dropped == 44
    assert pending[0][0]["sequence"] == 45
    probe.receive(message(mediaSessionId=None), observed_at="now", monotonic=301)
    probe.receive(message(), observed_at="now", monotonic=302)
    records, _ = probe.drain()
    assert records[0][0]["media_session_relation"] == "unknown"
    assert records[1][0]["media_session_relation"] == "first"


def test_metadata_hook_keeps_normalized_observations_unchanged():
    from tellyq.cast import MEDIA, Observer

    plain, inspected = Queue(), Queue()
    probe = YouTubeMetadataProbe(CONTENT)
    data = message(customData={"privateValue": "SECRET"})
    Observer(MEDIA, plain).receive_message(None, data)
    Observer(MEDIA, inspected, metadata_probe=probe).receive_message(None, data)
    one, two = plain.get_nowait(), inspected.get_nowait()
    for record in (one, two):
        del record["monotonic"]
        del record["observed_at"]
    assert one == two
    assert "privateValue" not in json.dumps(two)
    assert len(probe.drain()[0]) == 1


def test_open_backend_rejects_metadata_hook_on_mutable_adapter_before_connection(monkeypatch):
    from tellyq import cast

    def forbidden(*args, **kwargs):
        pytest.fail("Connection must not occur")

    monkeypatch.setattr(cast, "connect", forbidden)
    with (
        pytest.raises(ValueError, match="observation-only"),
        open_backend(TARGET, FakeClock(), 2, metadata_probe=YouTubeMetadataProbe(CONTENT)),
    ):
        pytest.fail("Mutable backend yielded")


def test_read_only_open_backend_passes_probe_without_control(monkeypatch):
    from tellyq import cast

    probe = YouTubeMetadataProbe(CONTENT)

    @contextmanager
    def connection(device, *, metadata_probe):
        assert device == "synthetic" and metadata_probe is probe
        yield object(), {"uuid": "synthetic", "name": "Synthetic"}

    monkeypatch.setattr(cast, "connect", connection)
    with open_backend(TARGET, FakeClock(), 2, read_only=True, metadata_probe=probe) as (backend, _):
        assert backend.read_only is True


def test_journals_are_private_exclusive_runtime_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "runtime" / "metadata.jsonl"
    record: YouTubeMetadataRecord = {
        "schema_version": 1,
        "kind": "begin",
        "observed_at": "now",
        "read_only": True,
        "completion_authorized": False,
    }
    with MetadataJournal(path) as journal:
        journal.write(record)
        assert os.stat(path).st_mode & 0o777 == 0o600
        with pytest.raises(FileExistsError):
            MetadataJournal(path)
    assert json.loads(path.read_text()) == record
    with pytest.raises(ValueError, match="runtime"):
        MetadataJournal(tmp_path / "public.jsonl")
    (tmp_path / "runtime" / "escaped").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="runtime"):
        MetadataJournal(tmp_path / "runtime" / "escaped" / "public.jsonl")


def capture_script():
    return run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/capture_youtube_metadata.py")
    )


@pytest.mark.parametrize("failure", [None, KeyboardInterrupt(), OSError("SECRET NETWORK")])
def test_capture_is_ready_before_poll_and_preserves_end_marker(
    tmp_path, monkeypatch, capsys, failure
):
    monkeypatch.chdir(tmp_path)
    probe, clock = YouTubeMetadataProbe(CONTENT, private_schema=True), FakeClock()

    class Transport(FakeTransport):
        def observe(self, seconds):
            assert '"capturing": true' in capsys.readouterr().err
            probe.receive(
                message(customData={"candidate": "SECRET"}),
                observed_at="now",
                monotonic=clock.tick(),
            )
            clock.tick(3)
            if failure is not None:
                raise failure
            return []

    backend = CastBackend(Transport(clock), TARGET, clock, read_only=True)
    safe, private = Path("runtime/safe.jsonl"), Path("runtime/private.jsonl")
    with MetadataJournal(safe) as journal, MetadataJournal(private) as private_journal:
        end = capture_script()["capture"](
            backend,
            target=TARGET,
            probe=probe,
            clock=clock,
            journal=journal,
            private_journal=private_journal,
            seconds=2,
        )
    assert end["stop_reason"] == (
        "deadline"
        if failure is None
        else "interrupted"
        if isinstance(failure, KeyboardInterrupt)
        else "backend_error"
    )
    assert end["samples"] == 1
    assert "candidate" not in safe.read_text()
    assert "candidate" in private.read_text()
    assert "SECRET" not in safe.read_text() + private.read_text() + capsys.readouterr().out
    assert json.loads(safe.read_text().splitlines()[-1])["kind"] == "end"


def test_cli_rejects_invalid_inputs_without_opening_a_receiver(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = capture_script()["main"]
    base = [
        "--device",
        "00000000-0000-0000-0000-000000000000",
        "--content",
        CONTENT,
        "--output",
        "runtime/test.jsonl",
    ]
    for seconds in ("nan", "inf", "0", "14401"):
        assert main([*base, "--seconds", seconds]) == 1
    assert not Path("runtime/test.jsonl").exists()
    assert "00000000" not in capsys.readouterr().out
