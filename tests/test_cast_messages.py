"""Offline contract tests; every protocol example and identity is synthetic."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

import pytest

from tellyq.cast import MEDIA, RECEIVER, Observer
from tellyq.cast_messages import media_observation, normalize_message
from tellyq.models import Observation

FIXTURES = Path(__file__).parent / "fixtures" / "cast"
INVALID_VALUES = (None, True, 5, "unexpected", [], [1], {}, {"unexpected": 1})


def test_synthetic_trace_replays_through_parser_and_callback():
    trace = json.loads((FIXTURES / "replay.json").read_text())
    assert trace["provenance"] == "synthetic; hand-authored; no device capture"
    events: Queue[Observation] = Queue()
    observer = Observer(MEDIA, events)
    for case in trace["cases"]:
        assert normalize_message(case["message"]) == case["expected"], case["name"]
        assert observer.receive_message(None, case["message"]), case["name"]
        event = events.get_nowait()
        assert isinstance(event.pop("observed_at"), str)
        assert isinstance(event.pop("monotonic"), float)
        assert event == case["expected"], case["name"]
    assert events.empty()


@pytest.mark.parametrize("value", INVALID_VALUES)
def test_unknown_message_types_and_containers_are_ignored(value):
    events: Queue[Observation] = Queue()
    observer = Observer(MEDIA, events)
    assert normalize_message(value) is None
    assert normalize_message({"type": value}) is None
    assert observer.receive_message(None, value) is False
    assert observer.receive_message(None, {"type": value}) is False
    assert events.empty()


@pytest.mark.parametrize("value", INVALID_VALUES)
def test_malformed_nested_containers_never_crash_callbacks(value):
    events: Queue[Observation] = Queue()
    observer = Observer(RECEIVER, events)
    messages = [
        {"type": "MEDIA_STATUS", "status": value},
        {"type": "MEDIA_STATUS", "status": [value]},
        {"type": "MEDIA_STATUS", "status": [{"media": value}]},
        {"type": "MEDIA_STATUS", "status": [{"extendedStatus": value}]},
        {"type": "MEDIA_STATUS", "status": [{"media": {"metadata": value}}]},
        {"type": "RECEIVER_STATUS", "status": value},
        {"type": "RECEIVER_STATUS", "status": {"applications": value}},
        {"type": "RECEIVER_STATUS", "status": {"applications": [value]}},
        {"type": "LOAD_FAILED", "reason": value, "detailedErrorCode": value},
    ]
    for message in messages:
        assert observer.receive_message(None, message)
        event = events.get_nowait()
        assert all(
            value is None or isinstance(value, str | float | int) for value in event.values()
        )
        json.dumps(event, allow_nan=False)


@pytest.mark.parametrize(
    "value", [True, False, float("nan"), float("inf"), -float("inf"), -1, "2", [], {}, 10**400]
)
def test_invalid_positions_and_durations_stay_unknown(value):
    event = media_observation({"status": [{"currentTime": value, "media": {"duration": value}}]})
    assert event["position"] is None
    assert event["duration"] is None
    json.dumps(event, allow_nan=False)


@pytest.mark.parametrize("value", [0, 1, 0.25])
def test_finite_nonnegative_times_are_preserved(value):
    event = media_observation({"status": [{"currentTime": value, "media": {"duration": value}}]})
    assert event["position"] == value
    assert event["duration"] == value


def test_scalar_fields_are_validated_without_coercion():
    event = media_observation(
        {
            "status": [
                {
                    "mediaSessionId": True,
                    "playerState": "new-state",
                    "idleReason": "new-reason",
                    "media": {"contentId": 123, "metadata": {"title": ["title"]}},
                }
            ]
        }
    )
    assert event["media_session_id"] is None
    assert event["content_id"] is None
    assert event["title"] is None
    assert event["player_state"] is None
    assert event["idle_reason"] is None
    receiver = normalize_message(
        {
            "type": "RECEIVER_STATUS",
            "status": {
                "applications": [
                    {"appId": "synthetic-app", "displayName": 1, "sessionId": "synthetic-session"}
                ],
                "isActiveInput": 1,
                "isStandBy": "false",
            },
        }
    )
    assert receiver is not None
    assert receiver["app_name"] is None
    assert receiver["active_input"] is None
    assert receiver["standby"] is None


@pytest.mark.parametrize(
    "status",
    [
        None,
        [],
        {},
        {"applications": None},
        {"applications": {}},
        {"applications": [None]},
        {"applications": [{}]},
        {"applications": [{"appId": "synthetic-app", "sessionId": True}]},
    ],
)
def test_invalid_receiver_identity_cannot_masquerade_as_app_exit(status):
    event = normalize_message({"type": "RECEIVER_STATUS", "status": status})
    assert event is not None
    assert event["kind"] == "error"
    assert event["type"] == "INVALID_RECEIVER_STATUS"
    assert "app_id" not in event


def test_valid_empty_applications_still_reports_app_exit():
    event = normalize_message(
        {"type": "RECEIVER_STATUS", "status": {"applications": [], "isActiveInput": False}}
    )
    assert event == {
        "kind": "receiver",
        "app_id": None,
        "app_name": None,
        "app_session_id": None,
        "active_input": False,
        "standby": None,
    }


def test_explicit_empty_media_is_distinct_from_unknown_status():
    assert media_observation({"status": []}) == {"kind": "media", "empty_status": True}
    for data in (None, {}, {"status": None}, {"status": {}}, {"status": [False]}):
        event = media_observation(data)
        assert event["empty_status"] is None
        assert event["content_id"] is None
        assert event["position"] is None


def test_extended_media_is_used_only_when_primary_media_is_absent():
    fallback = {"media": {"contentId": "synthetic-extended", "metadata": {"title": "Synthetic"}}}
    event = media_observation({"status": [{"extendedStatus": fallback}]})
    assert event["content_id"] == "synthetic-extended"
    assert event["title"] == "Synthetic"
    for primary in ({}, [], {"contentId": 42}):
        event = media_observation({"status": [{"media": primary, "extendedStatus": fallback}]})
        assert event["content_id"] is None


@pytest.mark.parametrize(
    "value",
    [None, False, {}, [], {"breakId": []}, {"currentBreakTime": True}, {"whenSkippable": 5}],
)
def test_absent_or_invalid_break_information_is_unknown(value):
    assert media_observation({"status": [{}]})["ad_break"] is None
    assert media_observation({"status": [{"breakStatus": value}]})["ad_break"] is None


@pytest.mark.parametrize(
    "value",
    [
        {"breakId": "synthetic-break"},
        {"breakClipId": "synthetic-clip"},
        {"currentBreakTime": 0},
        {"currentBreakClipTime": 1.5},
    ],
)
def test_valid_current_break_information_reports_ad_activity(value):
    assert media_observation({"status": [{"breakStatus": value}]})["ad_break"] is True


def test_error_output_is_allowlisted_and_does_not_retain_freeform_diagnostics():
    event = normalize_message(
        {
            "type": "LAUNCH_ERROR",
            "reason": "https://example.invalid/?token=synthetic-secret",
            "detailedErrorCode": True,
            "customData": {"token": "synthetic-secret"},
        }
    )
    assert event == {"kind": "error", "type": "LAUNCH_ERROR", "reason": None, "code": None}
    assert "synthetic-secret" not in json.dumps(event)


def test_callback_transfers_owned_snapshots_from_producer_thread():
    events: Queue[Observation] = Queue()
    observer = Observer(MEDIA, events)
    metadata = {"title": "Synthetic original"}
    media = {"contentId": "synthetic-original", "metadata": metadata}
    status = {"media": media, "currentTime": 1}
    message = {"type": "MEDIA_STATUS", "status": [status]}

    def produce():
        assert observer.receive_message(None, message)
        metadata["title"] = "Synthetic replacement"
        media["contentId"] = "synthetic-replacement"
        status["currentTime"] = 2
        assert observer.receive_message(None, message)
        message.clear()

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(produce).result(timeout=2)
    original = events.get_nowait()
    replacement = events.get_nowait()
    assert original["title"] == "Synthetic original"
    assert original["content_id"] == "synthetic-original"
    assert original["position"] == 1
    assert replacement["title"] == "Synthetic replacement"
    assert replacement["position"] == 2
    original["content_id"] = "consumer-owned"
    assert replacement["content_id"] == "synthetic-replacement"


def test_pure_parser_imports_without_cast_dependencies_or_io(tmp_path):
    root = str(Path(__file__).resolve().parents[1])
    source = """
import importlib.abc
import socket
import sys
from pathlib import Path

class DenyCast(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pychromecast', 'zeroconf', 'casttube'}:
            raise AssertionError('Cast dependency imported: ' + fullname)

def denied(*args, **kwargs):
    raise AssertionError('Unexpected network or filesystem mutation')

sys.meta_path.insert(0, DenyCast())
sys.path.insert(0, sys.argv[1])
socket.socket = denied
socket.getaddrinfo = denied
Path.mkdir = denied
from tellyq.cast_messages import normalize_message
assert normalize_message({'type': 'MEDIA_STATUS', 'status': []}) == {
    'kind': 'media', 'empty_status': True,
}
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", source, root],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())
