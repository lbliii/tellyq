"""Optional native queue contracts: dispatch is never membership/playback evidence."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pychromecast.controllers.media import MediaStatus
from pychromecast.controllers.receiver import CastStatus
from pychromecast.controllers.youtube import YouTubeController

from tellyq.cast import Connection
from tellyq.cast_backend import CastBackend, _Transport
from tellyq.domain.native_queue import (
    NativeQueueAction,
    NativeQueueBackend,
    NativeQueueCapabilities,
    NativeQueueDiagnostic,
    NativeQueueReason,
    NativeQueueReceipt,
    NativeQueueRequest,
    NativeQueueResponse,
)
from tellyq.domain.values import (
    CommandOutcome,
    ContentRef,
    ControlStage,
    IdentityUpdate,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    Support,
)
from tellyq.youtube_state import YOUTUBE_APPLICATION_ID
from tests.playback_support import FakeClock, FakeTransport


class NativeTransport(FakeTransport):
    def __init__(self, clock):
        super().__init__(clock)
        self.playing = True
        self.operations = []
        self.observations = 0

    def observe(self, seconds):
        self.observations += 1
        events = super().observe(seconds)
        for event in events:
            if event["kind"] == "receiver":
                event["app_id"] = YOUTUBE_APPLICATION_ID
            else:
                event["wire_media"] = "valid"
                event["wire_media_content_id"] = "valid"
        return events

    def native_queue(self, request):
        self.operations.append(request)
        return NativeQueueResponse(
            CommandOutcome.ACCEPTED,
            NativeQueueDiagnostic(ControlStage.TRANSPORT, NativeQueueReason.COMMAND_RETURNED),
        )


@pytest.fixture
def native():
    clock = FakeClock()
    target = PlaybackTarget("synthetic-device", "cast")
    playback = PlaybackRequest(
        "request", "attempt", "item-a", ContentRef("youtube", "FozIp7Va7dY"), target
    )
    transport = NativeTransport(clock)
    backend = CastBackend(transport, target, clock)
    scope = PlaybackScope(playback, "session", backend.generation, 100, YOUTUBE_APPLICATION_ID)
    request = NativeQueueRequest(
        "native-operation",
        NativeQueueAction.PLAY_NEXT,
        scope,
        "1",
        ContentRef("youtube", "video-b"),
    )
    backend.observe(target)
    return clock, transport, backend, request


def clear_request(request):
    return replace(request, action=NativeQueueAction.CLEAR, successor=None)


def test_request_and_receipt_are_immutable_and_explicit(native):
    clock, _, _, request = native
    with pytest.raises(FrozenInstanceError):
        request.__setattr__("successor", ContentRef("youtube", "other"))
    receipt = NativeQueueReceipt(
        request,
        CommandOutcome.ACCEPTED,
        clock.utcnow(),
        NativeQueueDiagnostic(ControlStage.TRANSPORT, NativeQueueReason.COMMAND_RETURNED),
    )
    assert receipt.request.operation_id == "native-operation"
    assert receipt.request.scope.request.attempt_id == "attempt"
    assert receipt.request.successor == ContentRef("youtube", "video-b")
    with pytest.raises(ValueError, match="timezone"):
        replace(receipt, recorded_at=datetime(2026, 1, 1))


@pytest.mark.parametrize(
    "change",
    [
        {"operation_id": ""},
        {"playback_id": ""},
        {"action": "unrecognized"},
        {"successor": None},
        {"action": NativeQueueAction.CLEAR},
    ],
)
def test_incomplete_native_intent_is_invalid(native, change):
    with pytest.raises(ValueError):
        replace(native[3], **change)


def test_capabilities_only_advertise_optional_routes_without_side_effects(native):
    _, transport, backend, request = native
    assert isinstance(backend, NativeQueueBackend)
    capabilities = backend.native_queue_capabilities(request.scope.request.target)
    assert capabilities.play_next.support == capabilities.clear.support == Support.ADVERTISED
    assert "not queue membership" in capabilities.play_next.source
    assert "not empty-queue" in capabilities.clear.source
    assert not transport.operations and not transport.plays and transport.quits == 0
    assert transport.observations == 1
    backend._read_only = True
    assert (
        backend.native_queue_capabilities(request.scope.request.target) == NativeQueueCapabilities()
    )


@pytest.mark.parametrize("action", list(NativeQueueAction))
def test_optional_port_does_not_expand_required_transport(native, action):
    clock, _, backend, request = native
    backend.transport = FakeTransport(clock)
    request = request if action == NativeQueueAction.PLAY_NEXT else clear_request(request)
    assert (
        backend.native_queue_capabilities(request.scope.request.target) == NativeQueueCapabilities()
    )
    receipt = backend.native_queue(request)
    assert receipt.outcome == CommandOutcome.REJECTED
    assert receipt.diagnostic.reason == NativeQueueReason.CAPABILITY_UNAVAILABLE


@pytest.mark.parametrize("action", list(NativeQueueAction))
def test_success_is_one_dispatch_and_does_not_observe_or_change_playback_evidence(native, action):
    _, transport, backend, request = native
    request = request if action == NativeQueueAction.PLAY_NEXT else clear_request(request)
    before = backend._media
    receipt = backend.native_queue(request)
    assert receipt.outcome == CommandOutcome.ACCEPTED
    assert receipt.request == request
    assert receipt.diagnostic.stage == ControlStage.TRANSPORT
    assert transport.operations == [request]
    assert backend._media is before
    assert transport.observations == 1
    assert not transport.plays and transport.quits == 0


@pytest.mark.parametrize("action", list(NativeQueueAction))
@pytest.mark.parametrize(
    "change,reason",
    [
        ("read_only", NativeQueueReason.READ_ONLY),
        ("generation", NativeQueueReason.CONNECTION_CHANGED),
        ("scope_application", NativeQueueReason.PROVIDER_MISMATCH),
        ("provider", NativeQueueReason.PROVIDER_MISMATCH),
        ("playback", NativeQueueReason.IDENTITY_CHANGED),
        ("session", NativeQueueReason.IDENTITY_CHANGED),
        ("content", NativeQueueReason.IDENTITY_CHANGED),
        ("media_generation", NativeQueueReason.CONNECTION_CHANGED),
        ("no_media", NativeQueueReason.MEDIA_UNAVAILABLE),
        ("no_receiver", NativeQueueReason.MEDIA_UNAVAILABLE),
        ("omitted_identity", NativeQueueReason.MEDIA_UNAVAILABLE),
        ("unknown_identity", NativeQueueReason.MEDIA_UNAVAILABLE),
        ("stale", NativeQueueReason.STALE_OBSERVATION),
        ("future_boundary", NativeQueueReason.STALE_OBSERVATION),
    ],
)
def test_backend_ownership_refusals_never_dispatch(native, action, change, reason):
    clock, transport, backend, request = native
    request = request if action == NativeQueueAction.PLAY_NEXT else clear_request(request)
    assert backend._media is not None
    if change == "read_only":
        backend._read_only = True
    elif change == "generation":
        request = replace(request, scope=replace(request.scope, connection_generation="old"))
    elif change == "scope_application":
        request = replace(request, scope=replace(request.scope, application_id="other"))
    elif change == "provider":
        playback = replace(request.scope.request, content=ContentRef("other", "video-a"))
        request = replace(request, scope=replace(request.scope, request=playback))
    elif change == "playback":
        request = replace(request, playback_id="other")
    elif change == "session":
        backend._media = replace(backend._media, session_id="other")
    elif change == "content":
        backend._media = replace(backend._media, content=ContentRef("youtube", "other"))
    elif change == "media_generation":
        backend._media = replace(backend._media, connection_generation="other")
    elif change == "no_media":
        backend._media = None
    elif change == "no_receiver":
        backend._receiver = None
    elif change in {"omitted_identity", "unknown_identity"}:
        backend._media = replace(
            backend._media,
            identity_update=IdentityUpdate.OMITTED
            if change == "omitted_identity"
            else IdentityUpdate.UNKNOWN,
        )
    elif change == "stale":
        clock.tick(6)
    else:
        request = replace(
            request, scope=replace(request.scope, started_monotonic=clock.instant + 1)
        )
    receipt = backend.native_queue(request)
    assert receipt.outcome == CommandOutcome.REJECTED
    assert receipt.diagnostic == NativeQueueDiagnostic(ControlStage.BACKEND, reason)
    assert not transport.operations


def test_successor_requires_compatible_provider(native):
    _, transport, backend, request = native
    request = replace(request, successor=ContentRef("other", "video-b"))
    assert backend.native_queue(request).diagnostic.reason == NativeQueueReason.PROVIDER_MISMATCH
    assert not transport.operations


def test_target_mismatch_is_rejected_before_transport(native):
    _, transport, backend, request = native
    playback = replace(request.scope.request, target=PlaybackTarget("other", "cast"))
    request = replace(request, scope=replace(request.scope, request=playback))
    with pytest.raises(ValueError, match="different target"):
        backend.native_queue(request)
    assert not transport.operations


@pytest.mark.parametrize("state", list(PlayerState)[:5])
@pytest.mark.parametrize("ad_active", [False, True, None])
def test_enqueue_requires_playing_or_paused_and_known_inactive_ads(native, state, ad_active):
    _, transport, backend, request = native
    assert backend._media is not None
    backend._media = replace(backend._media, state=state, ad_active=ad_active)
    allowed = state in {PlayerState.PLAYING, PlayerState.PAUSED} and ad_active is False
    receipt = backend.native_queue(request)
    assert receipt.outcome == (CommandOutcome.ACCEPTED if allowed else CommandOutcome.REJECTED)
    assert bool(transport.operations) == allowed


@pytest.mark.parametrize("action", list(NativeQueueAction))
def test_identity_return_after_takeover_does_not_restore_queue_ownership(native, action):
    _, transport, backend, request = native
    request = request if action == NativeQueueAction.PLAY_NEXT else clear_request(request)
    transport.content = "someone-else"
    backend.observe(request.scope.request.target)
    transport.content = request.scope.request.content.content_id
    backend.observe(request.scope.request.target)
    receipt = backend.native_queue(request)
    assert receipt.outcome == CommandOutcome.REJECTED
    assert receipt.diagnostic.reason == NativeQueueReason.IDENTITY_CHANGED
    assert not transport.operations


@pytest.mark.parametrize("action", list(NativeQueueAction))
def test_connection_reset_retires_queue_permission(native, action):
    clock, transport, backend, request = native
    request = request if action == NativeQueueAction.PLAY_NEXT else clear_request(request)
    backend._normalize(
        request.scope.request.target,
        [{"kind": "error", "type": "CONNECTION_RESET", "monotonic": clock.tick()}],
    )
    backend.observe(request.scope.request.target)
    assert backend.native_queue(request).outcome == CommandOutcome.REJECTED
    assert not transport.operations


@pytest.mark.parametrize("failure", [TimeoutError("private token"), KeyboardInterrupt()])
def test_exception_is_unknown_without_retry_or_private_details(native, failure):
    _, transport, backend, request = native
    mutation = Mock(side_effect=failure)
    transport.native_queue = mutation
    receipt = backend.native_queue(request)
    assert receipt.outcome == CommandOutcome.UNKNOWN
    assert receipt.diagnostic.reason == NativeQueueReason.TRANSPORT_EXCEPTION
    assert "private" not in str(receipt)
    mutation.assert_called_once_with(request)


@pytest.mark.parametrize("outcome", list(CommandOutcome))
def test_transport_provenance_is_preserved(native, outcome):
    _, transport, backend, request = native
    diagnostic = NativeQueueDiagnostic(
        ControlStage.TRANSPORT_GUARD, NativeQueueReason.IDENTITY_CHANGED
    )
    transport.native_queue = Mock(return_value=NativeQueueResponse(outcome, diagnostic))
    receipt = backend.native_queue(request)
    assert receipt.outcome == outcome and receipt.diagnostic == diagnostic


def test_unrecognized_transport_response_stays_unknown(native):
    _, transport, backend, request = native
    transport.native_queue = Mock(return_value=True)
    receipt = backend.native_queue(request)
    assert receipt.outcome == CommandOutcome.UNKNOWN
    assert receipt.diagnostic.reason == NativeQueueReason.RESPONSE_UNKNOWN


@pytest.fixture
def pinned(native):
    _, _, _, request = native
    media = MediaStatus()
    media.media_session_id = 1
    media.content_id = request.scope.request.content.content_id
    media.player_state = "PLAYING"
    receiver = CastStatus(
        False,
        False,
        0.5,
        False,
        YOUTUBE_APPLICATION_ID,
        "YouTube",
        [],
        "session",
        "transport",
        "Playing",
        None,
        "attenuation",
    )
    youtube = YouTubeController(timeout=10)
    youtube._screen_id = "synthetic-screen"
    youtube._session = Mock(
        _screen_id="synthetic-screen", in_session=True, play_next=Mock(), clear_playlist=Mock()
    )
    youtube.update_screen_id = Mock(side_effect=AssertionError("must never initialize or launch"))
    connection = object.__new__(Connection)
    connection.cast = Mock(
        uuid="synthetic-device",
        status=receiver,
        media_controller=SimpleNamespace(status=media),
        socket_client=SimpleNamespace(is_connected=True, is_stopped=False),
    )
    connection.youtube = youtube
    return connection, _Transport(connection), request


@pytest.mark.parametrize("action", list(NativeQueueAction))
def test_pinned_controller_forwards_exact_successor_or_clear_without_launch(pinned, action):
    connection, transport, request = pinned
    request = request if action == NativeQueueAction.PLAY_NEXT else clear_request(request)
    response = transport.native_queue(request)
    assert response.outcome == CommandOutcome.ACCEPTED
    assert response.diagnostic.reason == NativeQueueReason.COMMAND_RETURNED
    session = connection.youtube._session
    if action == NativeQueueAction.PLAY_NEXT:
        session.play_next.assert_called_once_with("video-b")
        session.clear_playlist.assert_not_called()
    else:
        session.clear_playlist.assert_called_once_with()
        session.play_next.assert_not_called()
    connection.youtube.update_screen_id.assert_not_called()
    connection.cast.assert_not_called()


@pytest.mark.parametrize("action", list(NativeQueueAction))
@pytest.mark.parametrize(
    "change,reason",
    [
        ("device", NativeQueueReason.IDENTITY_CHANGED),
        ("app", NativeQueueReason.IDENTITY_CHANGED),
        ("session", NativeQueueReason.IDENTITY_CHANGED),
        ("media", NativeQueueReason.IDENTITY_CHANGED),
        ("missing_media", NativeQueueReason.IDENTITY_CHANGED),
        ("content", NativeQueueReason.IDENTITY_CHANGED),
        ("disconnected", NativeQueueReason.CONNECTION_CHANGED),
        ("stopped", NativeQueueReason.CONNECTION_CHANGED),
        ("uninitialized", NativeQueueReason.SESSION_UNINITIALIZED),
        ("no_screen", NativeQueueReason.SESSION_UNINITIALIZED),
        ("screen_changed", NativeQueueReason.SESSION_UNINITIALIZED),
        ("unbound", NativeQueueReason.SESSION_UNINITIALIZED),
    ],
)
def test_pinned_mutable_cache_is_rechecked_before_any_effect(pinned, action, change, reason):
    connection, transport, request = pinned
    request = request if action == NativeQueueAction.PLAY_NEXT else clear_request(request)
    session = connection.youtube._session
    if change == "device":
        connection.cast.uuid = "replacement"
    elif change == "app":
        connection.cast.status = replace(connection.cast.status, app_id="replacement")
    elif change == "session":
        connection.cast.status = replace(connection.cast.status, session_id="replacement")
    elif change == "media":
        connection.cast.media_controller.status.media_session_id = 2
    elif change == "missing_media":
        connection.cast.media_controller.status.media_session_id = None
    elif change == "content":
        connection.cast.media_controller.status.content_id = "replacement"
    elif change == "disconnected":
        connection.cast.socket_client.is_connected = False
    elif change == "stopped":
        connection.cast.socket_client.is_stopped = True
    elif change == "uninitialized":
        connection.youtube._session = None
    elif change == "no_screen":
        connection.youtube._screen_id = None
    elif change == "screen_changed":
        connection.youtube._screen_id = "replacement"
    else:
        session.in_session = False
    response = transport.native_queue(request)
    assert response.outcome == CommandOutcome.REJECTED
    assert response.diagnostic == NativeQueueDiagnostic(ControlStage.TRANSPORT_GUARD, reason)
    session.play_next.assert_not_called()
    session.clear_playlist.assert_not_called()
    connection.youtube.update_screen_id.assert_not_called()


@pytest.mark.parametrize("action", list(NativeQueueAction))
def test_receiver_change_during_native_io_is_unknown_after_single_dispatch(pinned, action):
    connection, transport, request = pinned
    request = request if action == NativeQueueAction.PLAY_NEXT else clear_request(request)

    def replace_receiver(*_):
        connection.cast.status = replace(connection.cast.status, session_id="replacement")

    mutation = (
        connection.youtube._session.play_next
        if action == NativeQueueAction.PLAY_NEXT
        else connection.youtube._session.clear_playlist
    )
    mutation.side_effect = replace_receiver
    response = transport.native_queue(request)
    assert response.outcome == CommandOutcome.UNKNOWN
    assert response.diagnostic == NativeQueueDiagnostic(
        ControlStage.TRANSPORT, NativeQueueReason.IDENTITY_CHANGED
    )
    mutation.assert_called_once()


@pytest.mark.parametrize("failure", [TimeoutError("private pairing value"), KeyboardInterrupt()])
def test_pinned_native_failure_is_unknown_and_never_retried(pinned, failure):
    connection, transport, request = pinned
    mutation = connection.youtube._session.play_next
    mutation.side_effect = failure
    response = transport.native_queue(request)
    assert response.outcome == CommandOutcome.UNKNOWN
    assert response.diagnostic.reason == NativeQueueReason.TRANSPORT_EXCEPTION
    assert "private" not in str(response)
    mutation.assert_called_once()
