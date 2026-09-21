"""Pinned PyChromecast transport tests with synthetic identities, no sockets."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pychromecast.controllers.media import MediaStatus
from pychromecast.controllers.receiver import CastStatus

from tellyq.cast import MEDIA, Connection
from tellyq.domain.values import (
    CommandAction,
    CommandOutcome,
    ContentRef,
    ControlReason,
    ControlStage,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
)


@pytest.fixture
def control():
    request = PlaybackRequest(
        "request",
        "attempt",
        "item",
        ContentRef("youtube", "video-a"),
        PlaybackTarget("device", "cast"),
    )
    scope = PlaybackScope(request, "app-session", "connection", 100, "youtube")
    media = MediaStatus()
    media.media_session_id = 17
    media.content_id = "video-a"
    media.player_state = "PLAYING"
    media.supported_media_commands = 1
    status = CastStatus(
        False,
        False,
        0.5,
        False,
        "youtube",
        "YouTube",
        [],
        "app-session",
        "transport-a",
        "Playing",
        None,
        "attenuation",
    )
    sent = []

    def send(destination, namespace, data, *, callback_function):
        sent.append((destination, namespace, dict(data)))
        callback_function(True, {"type": "MEDIA_STATUS"})

    connection = object.__new__(Connection)
    connection.cast = Mock(
        status=status,
        media_controller=SimpleNamespace(status=media),
        socket_client=SimpleNamespace(send_message=send),
    )
    return connection, scope, sent


def test_transport_pins_media_application_and_destination(control):
    connection, scope, sent = control
    assert connection.control(scope, "17", CommandAction.PAUSE).outcome == CommandOutcome.ACCEPTED
    assert sent == [
        ("transport-a", MEDIA, {"type": "PAUSE", "sessionId": "app-session", "mediaSessionId": 17})
    ]
    connection.cast.media_controller.status.player_state = "PAUSED"
    assert connection.control(scope, "17", CommandAction.RESUME).outcome == CommandOutcome.ACCEPTED
    assert sent[-1][2]["type"] == "PLAY"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("media_session_id", 18, ControlReason.IDENTITY_CHANGED),
        ("content_id", "video-b", ControlReason.IDENTITY_CHANGED),
        ("supported_media_commands", 0, ControlReason.CAPABILITY_UNAVAILABLE),
        ("player_state", "IDLE", ControlReason.STATE_MISMATCH),
    ],
)
def test_changed_cached_media_refuses_before_send(control, field, value, reason):
    connection, scope, sent = control
    setattr(connection.cast.media_controller.status, field, value)
    response = connection.control(scope, "17", CommandAction.PAUSE)
    assert response.outcome == CommandOutcome.REJECTED
    assert response.diagnostic.stage == ControlStage.TRANSPORT_GUARD
    assert response.diagnostic.reason == reason
    assert sent == []


@pytest.mark.parametrize(
    "reply,result,stage,reason",
    [
        (
            {"type": "INVALID_REQUEST", "reason": "private wire data"},
            CommandOutcome.REJECTED,
            ControlStage.RECEIVER,
            ControlReason.INVALID_REQUEST,
        ),
        (
            {"type": "INVALID_PLAYER_STATE"},
            CommandOutcome.REJECTED,
            ControlStage.RECEIVER,
            ControlReason.INVALID_PLAYER_STATE,
        ),
        (
            {"type": "unrecognized-private"},
            CommandOutcome.UNKNOWN,
            ControlStage.TRANSPORT,
            ControlReason.RESPONSE_UNKNOWN,
        ),
        (None, CommandOutcome.UNKNOWN, ControlStage.TRANSPORT, ControlReason.RESPONSE_UNKNOWN),
        (
            {"type": ["private"]},
            CommandOutcome.UNKNOWN,
            ControlStage.TRANSPORT,
            ControlReason.RESPONSE_UNKNOWN,
        ),
    ],
)
def test_transport_distinguishes_rejection_and_unknown_response(
    control, reply, result, stage, reason
):
    connection, scope, _ = control

    def send(*_, callback_function):
        callback_function(True, reply)

    connection.cast.socket_client.send_message = send
    response = connection.control(scope, "17", CommandAction.PAUSE)
    assert response.outcome == result
    assert response.diagnostic.stage == stage
    assert response.diagnostic.reason == reason
    assert "private" not in str(response)


def test_transport_failure_is_unknown_with_safe_provenance(control):
    connection, scope, _ = control
    connection.cast.socket_client.send_message = Mock(side_effect=OSError("private"))
    response = connection.control(scope, "17", CommandAction.PAUSE)
    assert response.outcome == CommandOutcome.UNKNOWN
    assert response.diagnostic.stage == ControlStage.TRANSPORT
    assert response.diagnostic.reason == ControlReason.TRANSPORT_EXCEPTION
    assert "private" not in str(response)


@pytest.mark.parametrize(
    "failure,reason",
    [("timeout", ControlReason.RESPONSE_TIMEOUT), ("not_sent", ControlReason.MESSAGE_NOT_SENT)],
)
def test_transport_completion_failures_remain_unknown(control, monkeypatch, failure, reason):
    from pychromecast.error import RequestTimeout

    connection, scope, _ = control
    if failure == "timeout":
        monkeypatch.setattr(
            "tellyq.cast.WaitResponse.wait_response",
            Mock(side_effect=RequestTimeout("private", 10)),
        )
    else:

        def failed(*_, callback_function):
            callback_function(False, {"type": "INVALID_REQUEST", "private": "secret"})

        connection.cast.socket_client.send_message = failed
    response = connection.control(scope, "17", CommandAction.PAUSE)
    assert response.outcome == CommandOutcome.UNKNOWN
    assert response.diagnostic.stage == ControlStage.TRANSPORT
    assert response.diagnostic.reason == reason
    assert "private" not in str(response) and "secret" not in str(response)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("app", ControlReason.IDENTITY_CHANGED),
        ("session", ControlReason.IDENTITY_CHANGED),
        ("destination", ControlReason.DESTINATION_UNAVAILABLE),
        ("receiver", ControlReason.OWNERSHIP_UNVERIFIED),
        ("action", ControlReason.ACTION_UNSUPPORTED),
    ],
)
def test_receiver_cache_guards_have_local_provenance(control, change, reason):
    from dataclasses import replace

    connection, scope, sent = control
    action = CommandAction.PAUSE
    if change == "app":
        connection.cast.status = replace(connection.cast.status, app_id="private replacement")
    elif change == "session":
        connection.cast.status = replace(connection.cast.status, session_id="private replacement")
    elif change == "destination":
        connection.cast.status = replace(connection.cast.status, transport_id="")
    elif change == "receiver":
        connection.cast.status = None
    else:
        action = CommandAction.START
    response = connection.control(scope, "17", action)
    assert response.outcome == CommandOutcome.REJECTED
    assert response.diagnostic.stage == ControlStage.TRANSPORT_GUARD
    assert response.diagnostic.reason == reason
    assert sent == [] and "private" not in str(response)


def test_pinned_library_send_path_keeps_identity_during_callback_race(control):
    """Exercise actual 14.0.10 serialization and callback registration, no sockets."""
    import json
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    from pychromecast.generated.cast_channel_pb2 import CastMessage
    from pychromecast.socket_client import SocketClient

    connection, scope, _ = control
    client = Mock(spec=SocketClient)
    client._gen_request_id.return_value = 41
    client.stop = Mock()
    client.stop.is_set.return_value = False
    client.connecting = False
    client._force_recon = False
    client.source_id = "synthetic-sender"
    client.session_id = "replacement-session"
    client.destination_id = "replacement-transport"
    client.logger = Mock()
    client.fn = "Synthetic receiver"
    client.host = "synthetic.invalid"
    client.port = 8009
    client.socket = Mock()
    client._request_callbacks = {}

    def native_send(*args, **kwargs):
        # Another sender changed PyChromecast's mutable current app before send.
        connection.cast.status = replace(
            connection.cast.status,
            session_id="replacement-session",
            transport_id="replacement-transport",
        )
        return SocketClient.send_message(client, *args, **kwargs)

    client.send_message.side_effect = native_send

    def socket_write(packet):
        wire = CastMessage.FromString(packet[4:])
        assert wire.destination_id == "transport-a"
        assert wire.namespace == MEDIA
        assert json.loads(wire.payload_utf8) == {
            "type": "PAUSE",
            "sessionId": "app-session",
            "mediaSessionId": 17,
            "requestId": 41,
        }
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(
                client._request_callbacks.pop(41), True, {"type": "MEDIA_STATUS"}
            ).result(timeout=2)

    client.socket.sendall.side_effect = socket_write
    connection.cast.socket_client = client
    assert connection.control(scope, "17", CommandAction.PAUSE).outcome == CommandOutcome.ACCEPTED
    assert client._request_callbacks == {}
