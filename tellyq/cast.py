"""Cast transport. Commands run on the caller; receiver events use a thread-safe queue."""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from queue import Empty, Queue
from threading import Event, Lock
from time import monotonic
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

import pychromecast
from pychromecast.controllers import BaseController
from pychromecast.controllers.receiver import ReceiverController
from pychromecast.controllers.youtube import YouTubeController
from pychromecast.error import RequestFailed, RequestTimeout
from pychromecast.models import CastInfo
from pychromecast.response_handler import WaitResponse
from pychromecast.socket_client import ConnectionStatus, ConnectionStatusListener
from zeroconf import Zeroconf

from .cast_messages import media_observation as media_observation
from .cast_messages import normalize_message
from .domain.values import (
    CommandAction,
    CommandOutcome,
    ControlDiagnostic,
    ControlReason,
    ControlResponse,
    ControlStage,
    PlaybackScope,
)
from .models import Device, Observation

if TYPE_CHECKING:
    from .youtube_metadata import YouTubeMetadataProbe

MEDIA = "urn:x-cast:com.google.cast.media"
RECEIVER = "urn:x-cast:com.google.cast.receiver"
VIDEO_ID = "FozIp7Va7dY"


def now() -> str:
    return datetime.now(UTC).isoformat()


def device_dict(info: CastInfo) -> Device:
    return {
        "uuid": str(info.uuid),
        "name": info.friendly_name,
        "model": info.model_name,
        "type": info.cast_type,
    }


@contextmanager
def discovery(seconds: float = 12) -> Iterator[tuple[pychromecast.CastBrowser, Zeroconf]]:
    zconf = Zeroconf()
    browser = pychromecast.CastBrowser(pychromecast.SimpleCastListener(lambda *_: None), zconf)
    try:
        browser.start_discovery()
        Event().wait(seconds)
        yield browser, zconf
    finally:
        browser.stop_discovery()
        zconf.close()


class HasUUID(Protocol):
    @property
    def uuid(self) -> UUID: ...


def select_device[T: HasUUID](devices: Iterable[T], device_id: str) -> T:
    """An exact UUID is required even when only one receiver is discovered."""
    wanted = UUID(device_id)
    matches = [device for device in devices if device.uuid == wanted]
    if len(matches) != 1:
        raise ValueError("The selected device was not uniquely discovered; run discover again.")
    return matches[0]


class Observer(BaseController, ConnectionStatusListener):
    """Listener with single-use correlation for our receiver status polls."""

    def __init__(
        self,
        namespace: str,
        events: Queue[Observation],
        *,
        metadata_probe: YouTubeMetadataProbe | None = None,
    ) -> None:
        super().__init__(namespace, target_platform=namespace == RECEIVER)
        self.events = events
        self._status_lock = Lock()
        self._status_request: dict[str, object] | None = None
        self._status_deadline = 0.0
        self._metadata_probe = metadata_probe

    def request_status(self, receiver: ReceiverController, deadline: float) -> None:
        """Poll receiver status, superseding any earlier unanswered request.

        PyChromecast 14.0.10 adds requestId to this same dict before sendall.
        Keep it available before sending so an immediate reply can correlate.
        This is the same GET_STATUS as ReceiverController.update_status, with
        the assigned ID retained here instead of an unbounded callback entry.
        """
        with self._status_lock:
            request: dict[str, object] = {"type": "GET_STATUS"}
            self._status_request = request
            self._status_deadline = deadline
        # Never hold the observation lock across transport I/O. The assigned ID
        # is written before bytes are sent, so even an immediate reply sees it.
        try:
            receiver.send_message(request)
        except Exception:
            with self._status_lock:
                if self._status_request is request:
                    self._status_request = None
            raise

    def cancel_status_request(self) -> None:
        """Retire evidence at the observation-window / connection boundary."""
        with self._status_lock:
            self._status_request = None

    def channel_disconnected(self) -> None:
        self.cancel_status_request()

    def new_connection_status(self, status: ConnectionStatus) -> None:
        # Platform observers do not receive app channel_disconnected callbacks
        # reliably. Socket transitions also reset PyChromecast's request counter.
        with self._status_lock:
            self._status_request = None
            if status.status in {"LOST", "DISCONNECTED", "CONNECTING"}:
                self.events.put(
                    {
                        "kind": "error",
                        "type": "CONNECTION_RESET",
                        "reason": status.status,
                        "code": None,
                        "observed_at": now(),
                        "monotonic": monotonic(),
                    }
                )

    def _matching_request_id(self, data: object, received_at: float) -> int | None:
        pending = self._status_request
        if pending is None:
            return None
        if received_at >= self._status_deadline:
            self._status_request = None
            return None
        if not isinstance(data, dict) or data.get("type") != "RECEIVER_STATUS":
            return None
        request_id = pending.get("requestId")
        reply_id = data.get("requestId")
        if (
            type(request_id) is not int
            or request_id <= 0
            or type(reply_id) is not int
            or reply_id != request_id
        ):
            return None
        self._status_request = None
        return request_id

    def receive_message(self, _message: object, _data: object) -> bool:
        with self._status_lock:
            received_at = monotonic()
            observed_at = now()
            if self._metadata_probe is not None:
                self._metadata_probe.receive(_data, observed_at=observed_at, monotonic=received_at)
            request_id = self._matching_request_id(_data, received_at)
            value: Observation | None = normalize_message(
                _data, receiver_status_request_id=request_id
            )
            if value is None:
                return False
            if value["kind"] == "receiver" and value.get("app_id") is None and request_id is None:
                # Even explicit [] can be a delayed reply to a pre-stop poll.
                value = {
                    "kind": "error",
                    "type": "INVALID_RECEIVER_STATUS",
                    "reason": "UNCORRELATED_APP_ABSENCE",
                    "code": None,
                }
            # Ownership transfer and retirement share the lock: no old poll can
            # enqueue a newly timestamped idle result after its window closes.
            self.events.put({"observed_at": observed_at, "monotonic": received_at, **value})
            return True


class Connection:
    def __init__(
        self,
        info: CastInfo,
        zconf: Zeroconf,
        *,
        metadata_probe: YouTubeMetadataProbe | None = None,
    ) -> None:
        self.events: Queue[Observation] = Queue()
        self.cast = pychromecast.get_chromecast_from_cast_info(
            info, zconf, tries=2, retry_wait=1, timeout=8
        )
        self.cast.register_handler(Observer(MEDIA, self.events, metadata_probe=metadata_probe))
        self.receiver_observer = Observer(RECEIVER, self.events)
        self.cast.register_handler(self.receiver_observer)
        self.cast.register_connection_listener(self.receiver_observer)
        self.youtube = YouTubeController(timeout=10)
        self.cast.register_handler(self.youtube)

    def control(
        self, scope: PlaybackScope, playback_id: str, action: CommandAction
    ) -> ControlResponse:
        """Pin all remote identities instead of following a mutable current session."""
        from .cast_backend import video_id

        receiver = self.cast.status
        media = self.cast.media_controller.status
        reason = None
        if action not in {CommandAction.PAUSE, CommandAction.RESUME}:
            reason = ControlReason.ACTION_UNSUPPORTED
        elif receiver is None:
            reason = ControlReason.OWNERSHIP_UNVERIFIED
        elif receiver.app_id != scope.application_id or receiver.session_id != scope.session_id:
            reason = ControlReason.IDENTITY_CHANGED
        elif not receiver.transport_id:
            reason = ControlReason.DESTINATION_UNAVAILABLE
        elif (
            str(media.media_session_id) != playback_id
            or video_id(media.content_id) != scope.request.content.content_id
        ):
            reason = ControlReason.IDENTITY_CHANGED
        elif not media.supports_pause:
            reason = ControlReason.CAPABILITY_UNAVAILABLE
        elif media.player_state != ("PLAYING" if action == CommandAction.PAUSE else "PAUSED"):
            reason = ControlReason.STATE_MISMATCH
        if reason is not None:
            return ControlResponse(
                CommandOutcome.REJECTED, ControlDiagnostic(ControlStage.TRANSPORT_GUARD, reason)
            )
        assert receiver is not None and receiver.transport_id
        try:
            media_id = int(playback_id)
        except ValueError:
            return ControlResponse(
                CommandOutcome.REJECTED,
                ControlDiagnostic(ControlStage.TRANSPORT_GUARD, ControlReason.INVALID_MEDIA_ID),
            )
        response = WaitResponse(10, action.value)
        try:
            self.cast.socket_client.send_message(
                receiver.transport_id,
                MEDIA,
                {
                    "type": "PAUSE" if action == CommandAction.PAUSE else "PLAY",
                    "sessionId": scope.session_id,
                    "mediaSessionId": media_id,
                },
                callback_function=response.callback,
            )
            response.wait_response()
        except (Exception, KeyboardInterrupt) as exc:
            reason = (
                ControlReason.RESPONSE_TIMEOUT
                if isinstance(exc, RequestTimeout)
                else ControlReason.MESSAGE_NOT_SENT
                if isinstance(exc, RequestFailed)
                else ControlReason.TRANSPORT_EXCEPTION
            )
            return ControlResponse(
                CommandOutcome.UNKNOWN,
                ControlDiagnostic(ControlStage.TRANSPORT, reason),
            )
        reply = response.response
        reply_type = reply.get("type") if isinstance(reply, dict) else None
        reason = (
            {
                "MEDIA_STATUS": ControlReason.MEDIA_STATUS,
                "INVALID_REQUEST": ControlReason.INVALID_REQUEST,
                "INVALID_PLAYER_STATE": ControlReason.INVALID_PLAYER_STATE,
            }.get(reply_type)
            if isinstance(reply_type, str)
            else None
        )
        if reason is not None:
            return ControlResponse(
                CommandOutcome.ACCEPTED
                if reason == ControlReason.MEDIA_STATUS
                else CommandOutcome.REJECTED,
                ControlDiagnostic(ControlStage.RECEIVER, reason),
            )
        return ControlResponse(
            CommandOutcome.UNKNOWN,
            ControlDiagnostic(ControlStage.TRANSPORT, ControlReason.RESPONSE_UNKNOWN),
        )

    def drain(self) -> list[Observation]:
        result: list[Observation] = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except Empty:
                return result

    def observe(self, seconds: float) -> list[Observation]:
        end = monotonic() + seconds
        try:
            while monotonic() < end:
                self.receiver_observer.request_status(
                    self.cast.socket_client.receiver_controller, min(end, monotonic() + 2)
                )
                if self.cast.media_controller.is_active:
                    self.cast.media_controller.update_status()
                Event().wait(min(2, max(0, end - monotonic())))
        finally:
            self.receiver_observer.cancel_status_request()
        return self.drain()


@contextmanager
def connect(
    device_id: str,
    discovery_seconds: float = 12,
    *,
    metadata_probe: YouTubeMetadataProbe | None = None,
) -> Iterator[tuple[Connection, Device]]:
    with discovery(discovery_seconds) as (browser, zconf):
        info = select_device(list(browser.devices.values()), device_id)
        connection = (
            Connection(info, zconf)
            if metadata_probe is None
            else Connection(info, zconf, metadata_probe=metadata_probe)
        )
        try:
            connection.cast.wait(timeout=15)
            yield connection, device_dict(info)
        finally:
            connection.receiver_observer.cancel_status_request()
            if connection.cast.socket_client.is_alive():
                connection.cast.disconnect(timeout=10)
