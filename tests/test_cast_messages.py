"""Offline contract tests; every protocol example and identity is synthetic."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pychromecast.controllers.receiver import ReceiverController
from pychromecast.generated.cast_channel_pb2 import CastMessage
from pychromecast.socket_client import ConnectionStatus, NetworkAddress, SocketClient

from tellyq.cast import MEDIA, RECEIVER, Connection, Observer
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
        assert event == case.get("expected_callback", case["expected"]), case["name"]
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


@pytest.fixture
def idle_fixture():
    value = json.loads((FIXTURES / "idle_receiver.json").read_text())
    assert value["provenance"] == (
        "synthetic; hand-authored from reported field names and Open Screen source; no device capture"
    )
    return value


def test_idle_receiver_requires_request_correlation(idle_fixture):
    message = idle_fixture["message"]
    passive = normalize_message(message)
    assert passive is not None and passive["kind"] == "error"
    assert normalize_message(message, receiver_status_request_id=41) == idle_fixture["expected"]
    for request_id in (None, False, True, -1, 0, 40, 42):
        event = normalize_message(message, receiver_status_request_id=request_id)
        assert event is not None
        assert event["kind"] == "error"
        assert "app_id" not in event


@pytest.mark.parametrize("request_id", [None, False, True, 41.0, "41", 0, -1, 42])
def test_idle_receiver_invalid_response_id_stays_unknown(idle_fixture, request_id):
    message = idle_fixture["message"]
    message["requestId"] = request_id
    event = normalize_message(message, receiver_status_request_id=41)
    assert event is not None
    assert event["kind"] == "error"
    assert "app_id" not in event


@pytest.mark.parametrize(
    "status",
    [
        None,
        {},
        {"isActiveInput": True, "isStandBy": False},
        {"userEq": {}},
        {"volume": {"level": 0.4, "muted": False}},
        {"userEq": [], "volume": {"level": 0.4, "muted": False}},
        {"userEq": {}, "volume": {}},
        {"userEq": {}, "volume": {"level": 0.4}},
        {"userEq": {}, "volume": {"level": True, "muted": False}},
        {"userEq": {}, "volume": {"level": float("inf"), "muted": False}},
        {"userEq": {}, "volume": {"level": 1.1, "muted": False}},
        {"userEq": {}, "volume": {"level": -0.1, "muted": False}},
        {"userEq": {}, "volume": {"level": 0.4, "muted": 0}},
    ],
)
def test_solicited_partial_or_malformed_idle_receiver_stays_unknown(idle_fixture, status):
    message = idle_fixture["message"]
    message["status"] = status
    event = normalize_message(message, receiver_status_request_id=41)
    assert event is not None
    assert event["kind"] == "error"
    assert "app_id" not in event


@pytest.mark.parametrize(
    ("field", "value"),
    [("applications", None), ("applications", {}), ("isActiveInput", 1), ("isStandBy", None)],
)
def test_solicited_idle_does_not_hide_invalid_explicit_fields(idle_fixture, field, value):
    message = idle_fixture["message"]
    message["status"][field] = value
    event = normalize_message(message, receiver_status_request_id=41)
    assert event is not None and event["kind"] == "error"


def test_openscreen_idle_shape_without_optional_input_fields(idle_fixture):
    message = idle_fixture["message"]
    del message["status"]["isActiveInput"]
    del message["status"]["isStandBy"]
    event = normalize_message(message, receiver_status_request_id=41)
    assert event is not None
    assert event["kind"] == "receiver"
    assert event["app_id"] is None
    assert event["active_input"] is None
    assert event["standby"] is None


@pytest.fixture
def status_transport(monkeypatch):
    events: Queue[Observation] = Queue()
    observer = Observer(RECEIVER, events)
    clock = SimpleNamespace(time=100.0)
    monkeypatch.setattr("tellyq.cast.monotonic", lambda: clock.time)
    requests = []

    def send(request):
        request["requestId"] = 41 + len(requests)
        requests.append(request)

    receiver = Mock(spec=ReceiverController)
    receiver.send_message.side_effect = send
    return SimpleNamespace(
        events=events, observer=observer, receiver=receiver, requests=requests, clock=clock
    )


def test_poll_response_can_arrive_before_send_returns(status_transport, idle_fixture):
    transport = status_transport

    def send(request):
        request["requestId"] = 41
        # A different callback thread must not deadlock on the sending thread's
        # observation lock. The response can beat send_message's return.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(
                transport.observer.receive_message, None, idle_fixture["message"]
            ).result(timeout=2)

    transport.receiver.send_message.side_effect = send
    transport.observer.request_status(transport.receiver, deadline=102)
    event = transport.events.get_nowait()
    assert event.pop("monotonic") == 100
    assert isinstance(event.pop("observed_at"), str)
    assert event == idle_fixture["expected"]
    assert transport.events.empty()
    # A duplicate of the consumed response cannot provide another idle proof.
    transport.observer.receive_message(None, idle_fixture["message"])
    assert transport.events.get_nowait()["kind"] == "error"


@pytest.mark.parametrize(
    "retirement", ["expiry", "cancel", "disconnect", "socket-transition", "supersede"]
)
def test_retired_poll_cannot_turn_late_response_into_idle_proof(
    status_transport, idle_fixture, retirement
):
    transport = status_transport
    transport.observer.request_status(transport.receiver, deadline=102)
    if retirement == "expiry":
        transport.clock.time = 102
    elif retirement == "cancel":
        transport.observer.cancel_status_request()
    elif retirement == "disconnect":
        transport.observer.channel_disconnected()
    elif retirement == "socket-transition":
        transport.observer.new_connection_status(ConnectionStatus("LOST", None, None))
        assert transport.events.get_nowait()["type"] == "CONNECTION_RESET"
    else:
        transport.observer.request_status(transport.receiver, deadline=102)
    transport.observer.receive_message(None, idle_fixture["message"])
    assert transport.events.get_nowait()["kind"] == "error"
    if retirement == "supersede":
        idle_fixture["message"]["requestId"] = 42
        transport.observer.receive_message(None, idle_fixture["message"])
        assert transport.events.get_nowait()["kind"] == "receiver"


@pytest.mark.parametrize("state", ["LOST", "DISCONNECTED", "CONNECTING", "CONNECTED"])
def test_connection_transitions_retire_poll_and_publish_only_scalar_reset_evidence(
    status_transport, idle_fixture, state
):
    transport = status_transport
    transport.observer.request_status(transport.receiver, deadline=102)
    transport.observer.new_connection_status(
        ConnectionStatus(state, NetworkAddress("synthetic.invalid", 8009), None)
    )
    if state != "CONNECTED":
        event = transport.events.get_nowait()
        assert isinstance(event.pop("observed_at"), str)
        assert event == {
            "kind": "error",
            "type": "CONNECTION_RESET",
            "reason": state,
            "code": None,
            "monotonic": 100,
        }
        assert "synthetic.invalid" not in json.dumps(event)
    assert transport.events.empty()
    transport.observer.receive_message(None, idle_fixture["message"])
    assert transport.events.get_nowait()["kind"] == "error"


def test_wrong_request_id_and_unrelated_message_do_not_consume_poll(status_transport, idle_fixture):
    transport = status_transport
    transport.observer.request_status(transport.receiver, deadline=102)
    transport.observer.receive_message(None, {"type": "MEDIA_STATUS", "status": []})
    assert transport.events.get_nowait()["kind"] == "media"
    for request_id in (40, True, 41.0, "41"):
        transport.observer.receive_message(
            None, {**idle_fixture["message"], "requestId": request_id}
        )
        assert transport.events.get_nowait()["kind"] == "error"
    transport.observer.receive_message(None, idle_fixture["message"])
    assert transport.events.get_nowait()["kind"] == "receiver"


@pytest.mark.parametrize("retirement", ["none", "expiry", "cancel", "supersede"])
def test_explicit_empty_application_list_requires_current_poll_at_transport(
    status_transport, retirement
):
    transport = status_transport
    transport.observer.request_status(transport.receiver, deadline=102)
    if retirement == "expiry":
        transport.clock.time = 102
    elif retirement == "cancel":
        transport.observer.cancel_status_request()
    elif retirement == "supersede":
        transport.observer.request_status(transport.receiver, deadline=102)
    transport.observer.receive_message(
        None, {"type": "RECEIVER_STATUS", "requestId": 41, "status": {"applications": []}}
    )
    event = transport.events.get_nowait()
    if retirement == "none":
        assert event["kind"] == "receiver"
        assert event["app_id"] is None
    else:
        assert event["kind"] == "error"
        assert event["reason"] == "UNCORRELATED_APP_ABSENCE"


def test_unsolicited_explicit_empty_applications_cannot_prove_stop(status_transport):
    transport = status_transport
    transport.observer.receive_message(
        None, {"type": "RECEIVER_STATUS", "status": {"applications": []}}
    )
    event = transport.events.get_nowait()
    assert event["kind"] == "error"
    assert event["reason"] == "UNCORRELATED_APP_ABSENCE"


def test_correlated_partial_reply_consumes_poll_without_reusing_old_identity(status_transport):
    transport = status_transport
    transport.observer.request_status(transport.receiver, deadline=102)
    transport.observer.receive_message(None, {"type": "RECEIVER_STATUS", "requestId": 41})
    assert transport.events.get_nowait()["kind"] == "error"
    assert transport.observer._status_request is None


def test_poll_send_failure_retires_assigned_request(status_transport, idle_fixture):
    transport = status_transport

    def fail(request):
        request["requestId"] = 41
        raise OSError("synthetic send failure")

    transport.receiver.send_message.side_effect = fail
    with pytest.raises(OSError, match="synthetic send failure"):
        transport.observer.request_status(transport.receiver, deadline=102)
    transport.observer.receive_message(None, idle_fixture["message"])
    assert transport.events.get_nowait()["kind"] == "error"


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("explicit_empty", [False, True])
def test_observe_retires_baseline_poll_before_next_window(
    monkeypatch, status_transport, idle_fixture, fails, explicit_empty
):
    transport = status_transport
    connection = Connection.__new__(Connection)
    connection.events = transport.events
    connection.receiver_observer = transport.observer
    cast = Mock()
    cast.socket_client.receiver_controller = transport.receiver
    cast.media_controller.is_active = True
    connection.cast = cast

    def wait(_seconds):
        transport.clock.time = 102
        if fails:
            raise OSError("synthetic observation failure")

    monkeypatch.setattr("tellyq.cast.Event", lambda: SimpleNamespace(wait=wait))
    if fails:
        with pytest.raises(OSError, match="synthetic observation failure"):
            connection.observe(2)
    else:
        assert connection.observe(2) == []
    cast.media_controller.update_status.assert_called_once_with()
    transport.clock.time = 103
    if explicit_empty:
        idle_fixture["message"]["status"] = {"applications": []}
    transport.observer.receive_message(None, idle_fixture["message"])
    assert connection.drain()[0]["kind"] == "error"


def test_pinned_pychromecast_send_path_injects_id_before_socket_write(
    status_transport, idle_fixture
):
    """Exercise real 14.0.10 send methods with only socket I/O mocked."""
    transport = status_transport
    client = Mock(spec=SocketClient)
    client._gen_request_id.return_value = 41
    client.stop = Mock()
    client.stop.is_set.return_value = False
    client.connecting = False
    client._force_recon = False
    client.source_id = "synthetic-sender"
    client.logger = Mock()
    client.fn = "Synthetic receiver"
    client.host = "synthetic.invalid"
    client.port = 8009
    client.socket = Mock()
    client.send_message.side_effect = lambda *args, **kwargs: SocketClient.send_message(
        client, *args, **kwargs
    )
    client.send_platform_message.side_effect = lambda *args, **kwargs: (
        SocketClient.send_platform_message(client, *args, **kwargs)
    )

    def socket_write(packet):
        wire = CastMessage.FromString(packet[4:])
        assert wire.namespace == RECEIVER
        assert json.loads(wire.payload_utf8) == {"type": "GET_STATUS", "requestId": 41}
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(
                transport.observer.receive_message, wire, idle_fixture["message"]
            ).result(timeout=2)

    client.socket.sendall.side_effect = socket_write
    receiver = ReceiverController()
    client.receiver_controller = receiver
    receiver.registered(client)
    transport.observer.request_status(receiver, deadline=102)
    event = transport.events.get_nowait()
    assert event["kind"] == "receiver"
    assert event["app_id"] is None
    assert transport.events.empty()


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


def test_wire_shapes_distinguish_omission_null_invalid_and_unused_fallback():
    event = media_observation(
        {
            "status": [
                {
                    "media": {"contentId": 42, "breaks": [], "breakClips": None},
                    "extendedStatus": {"media": {"contentId": "unused-private-content"}},
                    "breakStatus": {"breakId": None, "currentBreakTime": True},
                    "currentItemId": 5,
                    "loadingItemId": False,
                    "preloadedItemId": None,
                },
                {"media": {"contentId": "unselected-private-content"}},
            ]
        }
    )
    assert event["wire_status_count"] == 2
    assert event["wire_media"] == "valid"
    assert event["wire_media_content_id"] == "invalid"
    assert event["wire_extended_content_id"] == "valid"
    assert event["wire_media_breaks"] == "valid"
    assert event["wire_media_break_clips"] == "null"
    assert event["wire_break_status"] == "valid"
    assert event["wire_break_id"] == "null"
    assert event["wire_break_clip_id"] == "absent"
    assert event["wire_break_time"] == "invalid"
    assert event["wire_current_item_id"] == "valid"
    assert event["wire_loading_item_id"] == "invalid"
    assert event["wire_preloaded_item_id"] == "null"
    assert event["content_id"] is None  # Diagnostics cannot change identity policy.
    assert event["ad_break"] is None
    assert "private-content" not in json.dumps(event)


def test_wire_shapes_are_current_message_only_and_never_copy_custom_payload():
    events: Queue[Observation] = Queue()
    observer = Observer(MEDIA, events)
    observer.receive_message(
        None,
        {
            "type": "MEDIA_STATUS",
            "status": [
                {
                    "customData": {"SECRET-token": "SECRET-credential"},
                    "media": {"contentId": "synthetic", "customData": {"SECRET": "SECRET"}},
                    "breakStatus": {"breakClipId": "SECRET-ad-id"},
                }
            ],
        },
    )
    first = events.get_nowait()
    assert first["wire_status_custom_data"] == "valid"
    assert first["wire_media_custom_data"] == "valid"
    assert first["wire_break_clip_id"] == "valid" and first["ad_break"] is True
    assert "SECRET" not in json.dumps(first)
    observer.receive_message(
        None,
        {"type": "MEDIA_STATUS", "status": [{"playerState": "IDLE", "idleReason": "FINISHED"}]},
    )
    terminal = events.get_nowait()
    assert terminal["wire_media"] == "absent"
    assert terminal["wire_media_content_id"] == "unavailable"
    assert terminal["wire_break_status"] == "absent"
    assert terminal["wire_break_clip_id"] == "unavailable"
    assert terminal["wire_status_custom_data"] == "absent"
    assert terminal["content_id"] is None and terminal["ad_break"] is None


@pytest.mark.parametrize("value,shape", [(None, "null"), ([], "invalid"), ({}, "valid")])
def test_wire_break_container_shape_does_not_establish_inactive_ads(value, shape):
    event = media_observation({"status": [{"breakStatus": value}]})
    assert event["wire_break_status"] == shape
    assert event["ad_break"] is None


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
