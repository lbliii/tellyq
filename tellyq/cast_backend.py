"""Cast adapter: wire parsing, callback correlation and bounded device effects."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from math import isfinite
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from .domain.native_queue import (
    NativeQueueAction,
    NativeQueueCapabilities,
    NativeQueueDiagnostic,
    NativeQueueReason,
    NativeQueueReceipt,
    NativeQueueRequest,
    NativeQueueResponse,
)
from .domain.ports import Clock
from .domain.values import (
    CapabilityEvidence,
    CommandAction,
    CommandOutcome,
    CommandReceipt,
    ContentPhase,
    ContentRef,
    ControlDiagnostic,
    ControlReason,
    ControlResponse,
    ControlStage,
    ErrorCode,
    IdentityUpdate,
    IdleReason,
    PlaybackCapabilities,
    PlaybackError,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    Support,
)
from .models import Device, Observation
from .youtube_state import YOUTUBE_APPLICATION_ID, interpret_player_state

if TYPE_CHECKING:
    from .cast import Connection
    from .youtube_metadata import YouTubeMetadataProbe


@runtime_checkable
class PendingObservationTransport(Protocol):
    """Optional nonblocking access to already normalized callbacks after interrupted I/O."""

    def drain_pending(self) -> list[Observation]: ...


@runtime_checkable
class ObservationUntilTransport(Protocol):
    """Deliver drained batches once, retaining the ordinary polling deadline.

    ready receives newly consumed batches, not cumulative history. Every event
    in the returned list must have been delivered to ready exactly once, in
    order, including a final drain after retiring receiver correlation.
    """

    def observe_until(
        self, seconds: float, ready: Callable[[list[Observation]], bool]
    ) -> list[Observation]: ...


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
class CastControls(Protocol):
    def control(
        self, scope: PlaybackScope, playback_id: str, action: CommandAction
    ) -> ControlResponse | bool | None: ...


@runtime_checkable
class CastNativeQueueTransport(Protocol):
    """Optional one-mutation route, requiring an already initialized session."""

    def native_queue(self, request: NativeQueueRequest) -> NativeQueueResponse: ...


class CastBackend:
    def __init__(
        self,
        transport: CastTransport,
        target: PlaybackTarget,
        clock: Clock,
        seconds: float = 30,
        *,
        read_only: bool = False,
        retain_raw: bool = True,
    ) -> None:
        self.transport = transport
        self.target = target
        self.clock = clock
        self.seconds = seconds
        self._read_only = read_only
        self._retain_raw = retain_raw
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

    def native_queue_capabilities(self, target: PlaybackTarget) -> NativeQueueCapabilities:
        """Advertise callable routes only; neither route implies live queue proof."""
        self._target(target)
        if not self._allows_effects() or not isinstance(self.transport, CastNativeQueueTransport):
            return NativeQueueCapabilities()
        return NativeQueueCapabilities(
            play_next=CapabilityEvidence(
                Support.ADVERTISED,
                "Optional guarded YouTube play_next route; not queue membership or playback proof",
                self.clock.utcnow(),
            ),
            clear=CapabilityEvidence(
                Support.ADVERTISED,
                "Optional guarded YouTube clear_playlist route; not empty-queue or stop proof",
                self.clock.utcnow(),
            ),
        )

    def _native_queue_guard(self, request: NativeQueueRequest) -> NativeQueueReason | None:
        scope = request.scope
        receiver, media = self._receiver, self._media
        now = self.clock.monotonic()
        if not self._allows_effects():
            return NativeQueueReason.READ_ONLY
        if not isinstance(self.transport, CastNativeQueueTransport):
            return NativeQueueReason.CAPABILITY_UNAVAILABLE
        if (
            scope.request.target.route != "cast"
            or scope.request.content.provider != "youtube"
            or scope.application_id != YOUTUBE_APPLICATION_ID
            or (request.successor is not None and request.successor.provider != "youtube")
        ):
            return NativeQueueReason.PROVIDER_MISMATCH
        if scope.connection_generation != self.generation:
            return NativeQueueReason.CONNECTION_CHANGED
        if receiver is None or media is None or media.identity_update != IdentityUpdate.EXPLICIT:
            return NativeQueueReason.MEDIA_UNAVAILABLE
        if media.connection_generation != self.generation:
            return NativeQueueReason.CONNECTION_CHANGED
        if (
            receiver.get("app_id") != scope.application_id
            or receiver.get("app_session_id") != scope.session_id
            or receiver.get("app_name") != "YouTube"
            or media.application_id != scope.application_id
            or media.session_id != scope.session_id
            or media.content != scope.request.content
            or media.playback_id != request.playback_id
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
            return NativeQueueReason.IDENTITY_CHANGED
        if (
            not scope.started_monotonic <= media.monotonic <= now
            or not 0 <= now - media.monotonic <= 5
            or not 0 <= now - receiver.get("monotonic", float("-inf")) <= 5
        ):
            return NativeQueueReason.STALE_OBSERVATION
        if request.action == NativeQueueAction.PLAY_NEXT:
            if media.state not in {PlayerState.PLAYING, PlayerState.PAUSED}:
                return NativeQueueReason.STATE_MISMATCH
            if media.ad_active is not False:
                return NativeQueueReason.AD_STATE_UNQUALIFIED
        return None

    def native_queue(self, request: NativeQueueRequest) -> NativeQueueReceipt:
        """Dispatch once. The caller journals intent and limits approved successors.

        No local deduplication substitutes for durability. Native staged work can
        continue during disconnect or a local hold; reconnect must reconcile it.
        This method neither consumes callbacks nor changes playback evidence.
        """
        self._target(request.scope.request.target)
        reason = self._native_queue_guard(request)
        if reason is not None:
            response = NativeQueueResponse(
                CommandOutcome.REJECTED, NativeQueueDiagnostic(ControlStage.BACKEND, reason)
            )
        else:
            assert isinstance(self.transport, CastNativeQueueTransport)
            try:
                response = self.transport.native_queue(request)
            except Exception, KeyboardInterrupt:
                response = NativeQueueResponse(
                    CommandOutcome.UNKNOWN,
                    NativeQueueDiagnostic(
                        ControlStage.TRANSPORT, NativeQueueReason.TRANSPORT_EXCEPTION
                    ),
                )
            if not isinstance(response, NativeQueueResponse):
                response = NativeQueueResponse(
                    CommandOutcome.UNKNOWN,
                    NativeQueueDiagnostic(
                        ControlStage.TRANSPORT, NativeQueueReason.RESPONSE_UNKNOWN
                    ),
                )
        return NativeQueueReceipt(
            request, response.outcome, self.clock.utcnow(), response.diagnostic
        )

    def capabilities(self, target: PlaybackTarget) -> PlaybackCapabilities:
        self._target(target)
        media = self._media
        if (
            not isinstance(self.transport, CastControls)
            or media is None
            or media.content is None
            or not 0 <= self.clock.monotonic() - media.monotonic <= 5
            or media.pause_supported is None
        ):
            return PlaybackCapabilities()
        pause = CapabilityEvidence(
            Support.ADVERTISED if media.pause_supported else Support.UNSUPPORTED,
            "Cast supportedMediaCommands PAUSE bit (receiver advertisement, not live proof)",
            media.observed_at,
        )
        # PLAY is Cast's standard resume command. Restrict this route to a fresh
        # PAUSED session whose receiver also advertises pause, not an app launch.
        resume = (
            CapabilityEvidence(
                pause.support, "Cast PAUSED media with PAUSE support; PLAY route", media.observed_at
            )
            if media.state == PlayerState.PAUSED
            else CapabilityEvidence()
        )
        return PlaybackCapabilities(pause=pause, resume=resume)

    def pause(self, scope: PlaybackScope, playback_id: str) -> CommandReceipt:
        return self._control(scope, playback_id, CommandAction.PAUSE)

    def resume(self, scope: PlaybackScope, playback_id: str) -> CommandReceipt:
        return self._control(scope, playback_id, CommandAction.RESUME)

    def _control_guard(
        self, scope: PlaybackScope, playback_id: str, action: CommandAction
    ) -> ControlReason | None:
        receiver, media = self._receiver, self._media
        now = self.clock.monotonic()
        expected = PlayerState.PLAYING if action == CommandAction.PAUSE else PlayerState.PAUSED
        if not self._allows_effects():
            return ControlReason.READ_ONLY
        if not isinstance(self.transport, CastControls):
            return ControlReason.CAPABILITY_UNAVAILABLE
        if receiver is None or media is None:
            return ControlReason.MEDIA_UNAVAILABLE
        if (
            scope.connection_generation != self.generation
            or media.connection_generation != self.generation
        ):
            return ControlReason.CONNECTION_CHANGED
        if (
            receiver.get("app_id") != scope.application_id
            or receiver.get("app_session_id") != scope.session_id
            or media.application_id != scope.application_id
            or media.session_id != scope.session_id
            or media.content != scope.request.content
            or media.playback_id != playback_id
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
            return ControlReason.IDENTITY_CHANGED
        if media.state != expected:
            return ControlReason.STATE_MISMATCH
        if media.pause_supported is not True:
            return ControlReason.CAPABILITY_UNAVAILABLE
        if (
            not 0 <= now - media.monotonic <= 5
            or not 0 <= now - receiver.get("monotonic", float("-inf")) <= 5
        ):
            return ControlReason.STALE_OBSERVATION
        return None

    def _control(
        self, scope: PlaybackScope, playback_id: str, action: CommandAction
    ) -> CommandReceipt:
        self._target(scope.request.target)
        reason = self._control_guard(scope, playback_id, action)
        if reason is not None:
            return self._control_receipt(
                scope,
                action,
                ControlResponse(
                    CommandOutcome.REJECTED, ControlDiagnostic(ControlStage.BACKEND, reason)
                ),
            )
        assert isinstance(self.transport, CastControls)
        self._window = 6
        try:
            result = self.transport.control(scope, playback_id, action)
        except Exception, KeyboardInterrupt:
            result = ControlResponse(
                CommandOutcome.UNKNOWN,
                ControlDiagnostic(ControlStage.TRANSPORT, ControlReason.TRANSPORT_EXCEPTION),
            )
        if not isinstance(result, ControlResponse):
            # Compatibility with adapters predating provenance: a boolean does
            # not establish whether a refusal was local or came from a receiver.
            result = ControlResponse(
                CommandOutcome.ACCEPTED
                if result is True
                else (CommandOutcome.REJECTED if result is False else CommandOutcome.UNKNOWN),
                ControlDiagnostic(ControlStage.TRANSPORT, ControlReason.LEGACY_RESULT),
            )
        return self._control_receipt(scope, action, result)

    def _control_receipt(
        self, scope: PlaybackScope, action: CommandAction, response: ControlResponse
    ) -> CommandReceipt:
        error = None
        if response.outcome != CommandOutcome.ACCEPTED:
            uncertain = response.outcome == CommandOutcome.UNKNOWN
            error = PlaybackError(
                ErrorCode.COMMAND_OUTCOME_UNKNOWN if uncertain else ErrorCode.CONTROL_REJECTED,
                f"Cast media control {'outcome unknown' if uncertain else 'refused'} at "
                f"{response.diagnostic.stage.value}: {response.diagnostic.reason.value}.",
                scope.request.request_id,
                uncertain,
                "Inspect fresh status before retrying.",
            )
        return self._receipt(scope.request, action, response.outcome, error, response.diagnostic)

    def _receipt(
        self,
        request: PlaybackRequest,
        action: CommandAction,
        outcome: CommandOutcome,
        error: PlaybackError | None = None,
        diagnostic: ControlDiagnostic | None = None,
    ) -> CommandReceipt:
        return CommandReceipt(
            request.request_id,
            request.attempt_id,
            action,
            outcome,
            self.clock.utcnow(),
            error,
            diagnostic=diagnostic,
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
        # Routine status must return before the five-second evidence lifetime.
        # Explicit start/control/stop keep their separate verification budgets.
        self._window = 2
        self._remember_raw(raw)
        return self._normalize(target, raw)

    def observe_until(
        self,
        target: PlaybackTarget,
        ready: Callable[[tuple[PlaybackObservation, ...]], bool],
    ) -> tuple[PlaybackObservation, ...]:
        """Normalize entire drained batches before evaluating application evidence."""
        self._target(target)
        if not isinstance(self.transport, ObservationUntilTransport):
            return self.observe(target)
        result: list[PlaybackObservation] = []
        consumed: list[Observation] = []

        def receive(batch: list[Observation]) -> bool:
            consumed.extend(batch)
            result.extend(self._normalize(target, batch))
            return ready(tuple(result))

        try:
            self.transport.observe_until(self._window, receive)
        except Exception, KeyboardInterrupt:
            if isinstance(self.transport, PendingObservationTransport):
                with suppress(Exception, KeyboardInterrupt):
                    consumed.extend(self.transport.drain_pending())
            raise
        finally:
            self._window = 2
            self._remember_raw(consumed)
        return tuple(result)

    def _normalize(
        self, target: PlaybackTarget, raw: list[Observation]
    ) -> tuple[PlaybackObservation, ...]:
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
                    self._remember_identity(observation)
        return tuple(result)

    def _remember_identity(self, observation: PlaybackObservation) -> None:
        """Retain the exact witnesses needed by any scoped takeover query.

        For each field, a scope's most recent mismatch is either the newest
        value or the newest different value. Two distinct non-null values per
        field therefore preserve every boundary query with at most six events.
        A return to the original identity cannot erase its takeover witness.
        """
        candidates = [*self._identity_history, observation]
        kept: set[int] = set()
        for field in ("session_id", "application_id", "content"):
            values: list[object] = []
            for index in range(len(candidates) - 1, -1, -1):
                value = getattr(candidates[index], field)
                if value is not None and value not in values:
                    values.append(value)
                    kept.add(index)
                    if len(values) == 2:
                        break
        self._identity_history = [candidates[index] for index in sorted(kept)]

    def _remember_raw(self, raw: list[Observation]) -> None:
        if self.read_only or not self._retain_raw:
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
        provider = (
            interpret_player_state(
                event,
                application_id=receiver.get("app_id")
                if correlated and receiver is not None and media_id is not None
                else None,
            )
            if "custom_player_state_shape" in event
            else None
        )
        ad_active = event.get("ad_break")
        if provider is not None:
            if provider.phase == ContentPhase.AD:
                ad_active = True
            elif provider.phase != ContentPhase.UNKNOWN:
                ad_active = False
            elif ad_active is not True:
                ad_active = None
        identity_update = IdentityUpdate.UNKNOWN
        if content is not None and (
            (event.get("wire_media") == "valid" and event.get("wire_media_content_id") == "valid")
            or (
                event.get("wire_media") == "absent"
                and event.get("wire_extended_status") == "valid"
                and event.get("wire_extended_media") == "valid"
                and event.get("wire_extended_content_id") == "valid"
            )
        ):
            identity_update = IdentityUpdate.EXPLICIT
        elif event.get("wire_media") == event.get("wire_extended_status") == "absent":
            identity_update = IdentityUpdate.OMITTED
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
            ad_active=ad_active,
            identity_update=identity_update,
            provider_evidence=provider,
            pause_supported=event.get("pause_supported"),
            idle_reason=reasons.get(event.get("idle_reason") or ""),
            source="cast_media",
        )
        if self._media is not None and observation.monotonic <= self._media.monotonic:
            return None
        # Missing identity/support must retire cached permission to control.
        self._media = observation
        return observation


class _Transport:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def observe(self, seconds: float) -> list[Observation]:
        return self.connection.observe(seconds)

    def observe_until(
        self, seconds: float, ready: Callable[[list[Observation]], bool]
    ) -> list[Observation]:
        return self.connection.observe_until(seconds, ready)

    def drain_pending(self) -> list[Observation]:
        return self.connection.drain()

    def play(self, content_id: str) -> None:
        self.connection.youtube.play_video(content_id)

    def quit(self) -> None:
        self.connection.cast.quit_app(timeout=10)

    def control(
        self, scope: PlaybackScope, playback_id: str, action: CommandAction
    ) -> ControlResponse:
        return self.connection.control(scope, playback_id, action)

    def _native_queue_guard(self, request: NativeQueueRequest) -> NativeQueueReason | None:
        """Recheck mutable library identity without polling or initializing anything."""
        scope = request.scope
        cast = self.connection.cast
        receiver, media = cast.status, cast.media_controller.status
        if (
            scope.request.target.route != "cast"
            or scope.request.content.provider != "youtube"
            or scope.application_id != YOUTUBE_APPLICATION_ID
            or (request.successor is not None and request.successor.provider != "youtube")
        ):
            return NativeQueueReason.PROVIDER_MISMATCH
        if not cast.socket_client.is_connected or cast.socket_client.is_stopped:
            return NativeQueueReason.CONNECTION_CHANGED
        if (
            str(cast.uuid) != scope.request.target.device_id
            or receiver is None
            or receiver.app_id != scope.application_id
            or receiver.session_id != scope.session_id
            or not receiver.transport_id
            or type(media.media_session_id) is not int
            or str(media.media_session_id) != request.playback_id
            or video_id(media.content_id) != scope.request.content.content_id
        ):
            return NativeQueueReason.IDENTITY_CHANGED
        if request.action == NativeQueueAction.PLAY_NEXT and media.player_state not in {
            "PLAYING",
            "PAUSED",
        }:
            return NativeQueueReason.STATE_MISMATCH
        youtube = self.connection.youtube
        # These pinned-library readiness fields are inspected only as a local
        # guard and never returned/logged. Without them the public queue methods
        # can call update_screen_id(), which may launch YouTube implicitly.
        session = youtube._session
        if (
            not youtube._screen_id
            or session is None
            or session._screen_id != youtube._screen_id
            or not session.in_session
        ):
            return NativeQueueReason.SESSION_UNINITIALIZED
        return None

    def native_queue(self, request: NativeQueueRequest) -> NativeQueueResponse:
        """Thin forwarding to pinned methods, not an atomic session-pinned command.

        PyChromecast bounds individual HTTP calls; casttube rebinds its lounge
        session internally. Receiver changes during that I/O cannot be excluded.
        Recheck before and after, report uncertainty on change, and never retry.
        """
        reason = self._native_queue_guard(request)
        if reason is not None:
            return NativeQueueResponse(
                CommandOutcome.REJECTED,
                NativeQueueDiagnostic(ControlStage.TRANSPORT_GUARD, reason),
            )
        try:
            if request.action == NativeQueueAction.PLAY_NEXT:
                assert request.successor is not None
                self.connection.youtube.play_next(request.successor.content_id)
            else:
                self.connection.youtube.clear_playlist()
            reason = self._native_queue_guard(request)
        except Exception, KeyboardInterrupt:
            reason = NativeQueueReason.TRANSPORT_EXCEPTION
        return NativeQueueResponse(
            CommandOutcome.ACCEPTED if reason is None else CommandOutcome.UNKNOWN,
            NativeQueueDiagnostic(
                ControlStage.TRANSPORT, reason or NativeQueueReason.COMMAND_RETURNED
            ),
        )


@contextmanager
def open_backend(
    target: PlaybackTarget,
    clock: Clock,
    seconds: float,
    *,
    read_only: bool = False,
    retain_raw: bool = True,
    metadata_probe: YouTubeMetadataProbe | None = None,
) -> Iterator[tuple[CastBackend, Device]]:
    if metadata_probe is not None and not read_only:
        raise ValueError("Metadata inspection requires an observation-only backend.")
    from .cast import connect

    connection_context = (
        connect(target.device_id)
        if metadata_probe is None
        else connect(target.device_id, metadata_probe=metadata_probe)
    )
    with connection_context as (connection, device):
        yield (
            CastBackend(
                _Transport(connection),
                target,
                clock,
                seconds,
                read_only=read_only,
                retain_raw=retain_raw,
            ),
            device,
        )


def discover_devices() -> list[Device]:
    from .cast import device_dict, discovery

    with discovery() as (browser, _):
        return sorted(
            (device_dict(info) for info in list(browser.devices.values())),
            key=lambda device: device["name"] or "",
        )
