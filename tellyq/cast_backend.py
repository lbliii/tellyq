"""Cast adapter: wire parsing, callback correlation and bounded device effects."""

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from math import isfinite
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from .domain.ports import Clock
from .domain.values import (
    CommandAction,
    CommandOutcome,
    CommandReceipt,
    ContentRef,
    ErrorCode,
    IdleReason,
    PlaybackCapabilities,
    PlaybackError,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
)
from .models import Device, Observation

if TYPE_CHECKING:
    from .cast import Connection


def video_id(content_id: str | None) -> str | None:
    """Decode YouTube wire identifiers at this provider boundary only."""
    if not content_id:
        return None
    if "://" not in content_id:
        return content_id
    try:
        url = urlparse(content_id)
        if url.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            values = parse_qs(url.query).get("v")
            return values[0] if values else None
        if url.hostname == "youtu.be":
            return url.path.strip("/") or None
    except ValueError:
        return None
    return None


class CastTransport(Protocol):
    def observe(self, seconds: float) -> list[Observation]: ...
    def play(self, content_id: str) -> None: ...
    def quit(self) -> None: ...


@runtime_checkable
class PendingObservationTransport(Protocol):
    """Optional nonblocking access to already normalized callbacks after interrupted I/O."""

    def drain_pending(self) -> list[Observation]: ...


class CastBackend:
    def __init__(
        self,
        transport: CastTransport,
        target: PlaybackTarget,
        clock: Clock,
        seconds: float = 30,
        *,
        read_only: bool = False,
    ) -> None:
        self.transport = transport
        self.target = target
        self.clock = clock
        self.seconds = seconds
        self._read_only = read_only
        self.generation = str(uuid4())
        self.raw_observations: list[Observation] = []
        self._sequence = 0
        self._last_event_time = float("-inf")
        self._window = 2.0
        self._receiver: Observation | None = None
        self._media: PlaybackObservation | None = None
        self._identity_history: list[PlaybackObservation] = []

    def _target(self, target: PlaybackTarget) -> None:
        if target != self.target:
            raise ValueError("The backend is bound to a different target.")

    @property
    def read_only(self) -> bool:
        return self._read_only

    def _allows_effects(self) -> bool:
        return not self.read_only

    def capabilities(self, target: PlaybackTarget) -> PlaybackCapabilities:
        self._target(target)
        # The transport supports commands, but this is not verified hardware capability.
        # In particular, Cast does not supply a verified no-ad or natural-end signal.
        return PlaybackCapabilities()

    def _receipt(
        self,
        request: PlaybackRequest,
        action: CommandAction,
        outcome: CommandOutcome,
        error: PlaybackError | None = None,
    ) -> CommandReceipt:
        return CommandReceipt(
            request.request_id, request.attempt_id, action, outcome, self.clock.utcnow(), error
        )

    def start(self, request: PlaybackRequest) -> CommandReceipt:
        self._target(request.target)
        if not self._allows_effects():
            return self._receipt(request, CommandAction.START, CommandOutcome.REJECTED)
        if request.content.provider != "youtube":
            return self._receipt(request, CommandAction.START, CommandOutcome.REJECTED)
        self._window = self.seconds
        try:
            self.transport.play(request.content.content_id)
        except Exception, KeyboardInterrupt:
            error = PlaybackError(
                ErrorCode.COMMAND_OUTCOME_UNKNOWN,
                "Cast start outcome is unknown.",
                request.request_id,
                True,
                "Inspect fresh status before retrying.",
            )
            return self._receipt(request, CommandAction.START, CommandOutcome.UNKNOWN, error)
        return self._receipt(request, CommandAction.START, CommandOutcome.ACCEPTED)

    def stop(self, scope: PlaybackScope) -> CommandReceipt:
        self._target(scope.request.target)
        if not self._allows_effects():
            return self._receipt(scope.request, CommandAction.STOP, CommandOutcome.REJECTED)
        receiver = self._receiver
        now = self.clock.monotonic()
        if (
            scope.connection_generation != self.generation
            or receiver is None
            or receiver.get("app_id") != scope.application_id
            or receiver.get("app_session_id") != scope.session_id
            or not 0 <= now - receiver.get("monotonic", float("-inf")) <= 5
            or any(
                event.monotonic > scope.started_monotonic
                and (
                    (event.session_id is not None and event.session_id != scope.session_id)
                    or (
                        event.application_id is not None
                        and event.application_id != scope.application_id
                    )
                    or (event.content is not None and event.content != scope.request.content)
                )
                for event in self._identity_history
            )
        ):
            return self._receipt(scope.request, CommandAction.STOP, CommandOutcome.REJECTED)
        self._window = 6
        try:
            self.transport.quit()
        except Exception, KeyboardInterrupt:
            error = PlaybackError(
                ErrorCode.COMMAND_OUTCOME_UNKNOWN,
                "Cast stop outcome is unknown.",
                scope.request.request_id,
                True,
                "Inspect fresh status before retrying.",
            )
            return self._receipt(scope.request, CommandAction.STOP, CommandOutcome.UNKNOWN, error)
        return self._receipt(scope.request, CommandAction.STOP, CommandOutcome.ACCEPTED)

    def observe(self, target: PlaybackTarget) -> tuple[PlaybackObservation, ...]:
        self._target(target)
        try:
            raw = self.transport.observe(self._window)
        except Exception, KeyboardInterrupt:
            if isinstance(self.transport, PendingObservationTransport):
                # Preserve the original failure if even the local drain fails.
                with suppress(Exception, KeyboardInterrupt):
                    self._remember_raw(self.transport.drain_pending())
            raise
        self._window = 6
        self._remember_raw(raw)
        result: list[PlaybackObservation] = []
        for event in raw:
            instant = event.get("monotonic")
            if (
                isinstance(instant, bool)
                or not isinstance(instant, (int, float))
                or not isfinite(instant)
            ):
                continue
            if not self.clock.monotonic() - instant >= 0:
                continue
            if instant <= self._last_event_time:
                continue
            self._last_event_time = instant
            self._sequence += 1
            observed_at = self.clock.utcnow()
            stamp = event.get("observed_at")
            if stamp is not None:
                try:
                    parsed = datetime.fromisoformat(stamp)
                    if parsed.tzinfo is not None:
                        observed_at = parsed
                except ValueError:
                    pass
            base = PlaybackObservation(
                target,
                self.generation,
                self._sequence,
                observed_at,
                instant,
            )
            observation = self._translate(base, event)
            if observation is not None:
                result.append(observation)
                if not self.read_only:
                    self._identity_history.append(observation)
        return tuple(result)

    def _remember_raw(self, raw: list[Observation]) -> None:
        if self.read_only:
            self.raw_observations = [event.copy() for event in raw]
        else:
            self.raw_observations.extend(event.copy() for event in raw)

    def observe_window(
        self, target: PlaybackTarget, seconds: float
    ) -> tuple[PlaybackObservation, ...]:
        """Observe a short diagnostic window without retaining effect ownership history."""
        if not self.read_only:
            raise ValueError("Bounded capture requires an observation-only backend.")
        if isinstance(seconds, bool) or not isfinite(seconds) or not 0 < seconds <= 2:
            raise ValueError("Capture windows must be positive and at most two seconds.")
        self._window = seconds
        return self.observe(target)

    def drain_raw_observations(self) -> list[Observation]:
        """Transfer normalized wire records; these are not original protocol frames."""
        result, self.raw_observations = self.raw_observations, []
        return result

    def _translate(
        self, base: PlaybackObservation, event: Observation
    ) -> PlaybackObservation | None:
        from dataclasses import replace

        if event.get("type") == "CONNECTION_RESET":
            self.generation = str(uuid4())
            self._receiver = None
            self._media = None
            self._identity_history = []
            return replace(base, connection_reset=True)
        if event["kind"] == "receiver":
            previous = self._receiver
            if previous is not None and base.monotonic <= previous.get("monotonic", 0):
                return None
            self._receiver = event.copy()
            if previous is None or (previous.get("app_id"), previous.get("app_session_id")) != (
                event.get("app_id"),
                event.get("app_session_id"),
            ):
                self._media = None
            return replace(
                base,
                session_id=event.get("app_session_id"),
                application_id=event.get("app_id"),
                session_active=event.get("app_id") is not None,
                source="receiver_status",
            )
        if event["kind"] != "media":
            if event.get("type") == "INVALID_RECEIVER_STATUS":
                self._receiver = None
            return base
        receiver = self._receiver
        correlated = (
            receiver is not None
            and 0 <= base.monotonic - receiver.get("monotonic", float("-inf")) <= 5
        )
        content_id = video_id(event.get("content_id"))
        media_id = event.get("media_session_id")
        content = (
            ContentRef("youtube", content_id, title=event.get("title"))
            if content_id
            and correlated
            and receiver is not None
            and receiver.get("app_name") == "YouTube"
            and media_id is not None
            else None
        )
        states = {
            "PLAYING": PlayerState.PLAYING,
            "PAUSED": PlayerState.PAUSED,
            "BUFFERING": PlayerState.BUFFERING,
            "IDLE": PlayerState.IDLE,
        }
        reasons = {
            "FINISHED": IdleReason.FINISHED,
            "CANCELLED": IdleReason.CANCELED,
            "CANCELED": IdleReason.CANCELED,
            "ERROR": IdleReason.ERROR,
        }
        observation = replace(
            base,
            session_id=receiver.get("app_session_id")
            if correlated and receiver is not None
            else None,
            application_id=receiver.get("app_id") if correlated and receiver is not None else None,
            content=content,
            playback_id=str(media_id) if media_id is not None else None,
            state=states.get(event.get("player_state") or "", PlayerState.UNKNOWN),
            position=event.get("position"),
            duration=event.get("duration"),
            ad_active=event.get("ad_break"),
            idle_reason=reasons.get(event.get("idle_reason") or ""),
            source="cast_media",
        )
        if self._media is not None and observation.monotonic <= self._media.monotonic:
            return None
        if observation.content is not None:
            self._media = observation
        return observation


class _Transport:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def observe(self, seconds: float) -> list[Observation]:
        return self.connection.observe(seconds)

    def drain_pending(self) -> list[Observation]:
        return self.connection.drain()

    def play(self, content_id: str) -> None:
        self.connection.youtube.play_video(content_id)

    def quit(self) -> None:
        self.connection.cast.quit_app(timeout=10)


@contextmanager
def open_backend(
    target: PlaybackTarget,
    clock: Clock,
    seconds: float,
    *,
    read_only: bool = False,
) -> Iterator[tuple[CastBackend, Device]]:
    from .cast import connect

    with connect(target.device_id) as (connection, device):
        yield (
            CastBackend(_Transport(connection), target, clock, seconds, read_only=read_only),
            device,
        )


def discover_devices() -> list[Device]:
    from .cast import device_dict, discovery

    with discovery() as (browser, _):
        return sorted(
            (device_dict(info) for info in list(browser.devices.values())),
            key=lambda device: device["name"] or "",
        )
