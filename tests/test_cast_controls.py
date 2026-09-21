"""Pinned PyChromecast transport tests with synthetic identities, no sockets."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pychromecast.controllers.media import MediaStatus
from pychromecast.controllers.receiver import CastStatus

from tellyq.cast import MEDIA, Connection
from tellyq.domain.values import (
    CommandAction,
    ContentRef,
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
    assert connection.control(scope, "17", CommandAction.PAUSE) is True
    assert sent == [
        ("transport-a", MEDIA, {"type": "PAUSE", "sessionId": "app-session", "mediaSessionId": 17})
    ]
    connection.cast.media_controller.status.player_state = "PAUSED"
    assert connection.control(scope, "17", CommandAction.RESUME) is True
    assert sent[-1][2]["type"] == "PLAY"


@pytest.mark.parametrize(
    "field,value",
    [
        ("media_session_id", 18),
        ("content_id", "video-b"),
        ("supported_media_commands", 0),
        ("player_state", "IDLE"),
    ],
)
def test_changed_cached_media_refuses_before_send(control, field, value):
    connection, scope, sent = control
    setattr(connection.cast.media_controller.status, field, value)
    assert connection.control(scope, "17", CommandAction.PAUSE) is False
    assert sent == []


@pytest.mark.parametrize(
    "reply,result",
    [
        ({"type": "INVALID_REQUEST"}, False),
        ({"type": "INVALID_PLAYER_STATE"}, False),
        ({"type": "unrecognized"}, None),
        (None, None),
    ],
)
def test_transport_distinguishes_rejection_and_unknown_response(control, reply, result):
    connection, scope, _ = control

    def send(*_, callback_function):
        callback_function(True, reply)

    connection.cast.socket_client.send_message = send
    assert connection.control(scope, "17", CommandAction.PAUSE) is result


def test_transport_failure_is_not_swallowed(control):
    connection, scope, _ = control
    connection.cast.socket_client.send_message = Mock(side_effect=OSError("private"))
    with pytest.raises(OSError):
        connection.control(scope, "17", CommandAction.PAUSE)


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
    assert connection.control(scope, "17", CommandAction.PAUSE) is True
    assert client._request_callbacks == {}
