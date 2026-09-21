"""Passive observation of exactly authorized native YouTube playback.

Callers validate the durable queue/reservation and cancellation state before
constructing these trackers. The helpers perform no I/O, persistence or commands;
even verified playback does not authorize another enqueue. Reconnect constructors
accept a new process-local boundary/generation, never restored progress anchors.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum

from .domain import policy
from .domain.values import (
    CompletionEvidence,
    ContentRef,
    EvidencePolicy,
    EvidenceReason,
    IdentityUpdate,
    PlaybackEvidence,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    SessionSnapshot,
    require_instant,
)
from .youtube_state import YOUTUBE_APPLICATION_ID

_DEFAULT_POLICY = EvidencePolicy()


class NativeObservationRefusal(StrEnum):
    CONNECTION_CHANGED = "connection_changed"
    SESSION_CHANGED = "session_changed"
    SESSION_EXITED = "session_exited"
    UNEXPECTED_CONTENT = "unexpected_content"


def _validate_identity(request: PlaybackRequest, application_id: str, session_id: str) -> None:
    if (
        request.target.route != "cast"
        or request.content.provider != "youtube"
        or application_id != YOUTUBE_APPLICATION_ID
        or not session_id
    ):
        raise ValueError("native observation requires an exact YouTube Cast application/session")


@dataclass(frozen=True, slots=True)
class NativeSuccessorAuthorization:
    """Declared durable intent, not fresh ownership or successful enqueue evidence.

    The caller maps a validated reservation to these exact requests. This value
    alone neither verifies a queue generation nor grants permission to dispatch.
    """

    operation_id: str
    predecessor: PlaybackRequest
    successor: PlaybackRequest
    application_id: str
    session_id: str

    def __post_init__(self) -> None:
        _validate_identity(self.predecessor, self.application_id, self.session_id)
        _validate_identity(self.successor, self.application_id, self.session_id)
        if (
            not self.operation_id
            or self.predecessor.target != self.successor.target
            or self.predecessor.attempt_id == self.successor.attempt_id
            or self.predecessor.queue_item_id == self.successor.queue_item_id
            or self.predecessor.content.content_id == self.successor.content.content_id
        ):
            raise ValueError(
                "native successor requires a distinct exact item/attempt on the same target"
            )


class NativePlaybackTracker:
    """Fresh passive scope for a declared current item, including status-only reopen.

    An exact explicit media observation following a fresh matching receiver witness
    establishes the scope. Its receipt remains None. Readiness is playback proof at
    the last supplied `now`, so callers must refresh with observe((), now=...) when
    time advances. A contradiction stays refused until explicit reconciliation
    creates another tracker; a later return to the expected content cannot undo it.
    """

    def __init__(
        self,
        request: PlaybackRequest,
        *,
        application_id: str,
        session_id: str,
        connection_generation: str,
        started_monotonic: float,
        evidence_policy: EvidencePolicy = _DEFAULT_POLICY,
    ) -> None:
        _validate_identity(request, application_id, session_id)
        require_instant(started_monotonic, "fresh observation boundary")
        if not connection_generation:
            raise ValueError("current connection generation is required")
        self.request = request
        self.application_id, self.session_id = application_id, session_id
        self.connection_generation, self.started_monotonic = (
            connection_generation,
            started_monotonic,
        )
        self.evidence_policy = evidence_policy
        self.snapshot: SessionSnapshot | None = None
        self.refusal: NativeObservationRefusal | None = None
        self._receiver: PlaybackObservation | None = None
        self._last_sequence = -1
        self._last_monotonic = started_monotonic
        self._now = started_monotonic

    @property
    def ready(self) -> bool:
        snapshot = self.snapshot
        return (
            self.refusal is None
            and self._receiver is not None
            and 0 <= self._now - self._receiver.monotonic <= self.evidence_policy.max_age_seconds
            and snapshot is not None
            and not snapshot.ownership_lost
            and snapshot.evidence.receiver_playback_confirmed
        )

    def _refresh(self, now: float) -> None:
        require_instant(now, "current time")
        if now < self._now:
            raise ValueError("current time cannot precede the previous observation window")
        self._now = now
        if self.snapshot is not None:
            self.snapshot = policy.refresh(self.snapshot, now=now, policy=self.evidence_policy)

    def _refuse(self, reason: NativeObservationRefusal, event: PlaybackObservation) -> None:
        self.refusal = reason
        self._receiver = None
        if self.snapshot is not None:
            self.snapshot = replace(
                policy.invalidate_history(self.snapshot),
                ownership_lost=True,
                latest=event,
                evidence=PlaybackEvidence(
                    natural_completion_confirmed=self.snapshot.completion is not None,
                    reason=EvidenceReason.SESSION_REPLACED,
                ),
            )

    def _observe(
        self, event: PlaybackObservation, *, now: float, waiting_content: ContentRef | None = None
    ) -> bool:
        """Return whether the event is in this ordered current-generation window."""
        if (
            self.refusal is not None
            or event.target != self.request.target
            or event.monotonic <= self.started_monotonic
            or not 0 <= now - event.monotonic <= self.evidence_policy.max_age_seconds
            or event.sequence <= self._last_sequence
            or event.monotonic <= self._last_monotonic
        ):
            return False
        boundary = self._last_monotonic
        if self._last_sequence >= 0 and event.sequence > self._last_sequence + 1:
            self._receiver = None
            if self.snapshot is not None:
                self.snapshot = policy.invalidate_history(self.snapshot)
        self._last_sequence, self._last_monotonic = event.sequence, event.monotonic
        if event.connection_reset or event.connection_generation != self.connection_generation:
            self._refuse(NativeObservationRefusal.CONNECTION_CHANGED, event)
            return True
        if event.session_active is False:
            self._refuse(NativeObservationRefusal.SESSION_EXITED, event)
            return True
        if (event.application_id is not None and event.application_id != self.application_id) or (
            event.session_id is not None and event.session_id != self.session_id
        ):
            self._refuse(NativeObservationRefusal.SESSION_CHANGED, event)
            return True
        if (
            event.content is not None
            and event.content != self.request.content
            and not (self.snapshot is None and event.content == waiting_content)
        ):
            self._refuse(NativeObservationRefusal.UNEXPECTED_CONTENT, event)
            return True
        exact_session = (
            event.application_id == self.application_id and event.session_id == self.session_id
        )
        if event.session_active is True:
            self._receiver = event if exact_session else None
            if self.snapshot is not None:
                self.snapshot = (
                    policy.observe(self.snapshot, event, now=now, policy=self.evidence_policy)
                    if exact_session
                    else policy.invalidate_history(self.snapshot)
                )
            return True
        receiver = self._receiver
        if (
            not exact_session
            or not event.playback_id
            or receiver is None
            or not 0 <= event.monotonic - receiver.monotonic <= self.evidence_policy.max_age_seconds
        ):
            if not exact_session:
                self._receiver = None
            if self.snapshot is not None:
                self.snapshot = policy.invalidate_history(self.snapshot)
            return True
        if event.content is not None and event.identity_update != IdentityUpdate.EXPLICIT:
            if self.snapshot is not None:
                self.snapshot = policy.invalidate_history(self.snapshot)
            return True
        if self.snapshot is None:
            if event.content != self.request.content or event.playback_id is None:
                return True
            self.snapshot = SessionSnapshot(
                PlaybackScope(
                    self.request,
                    self.session_id,
                    self.connection_generation,
                    boundary,
                    self.application_id,
                )
            )
        self.snapshot = policy.observe(self.snapshot, event, now=now, policy=self.evidence_policy)
        return True

    def observe(
        self, events: Iterable[PlaybackObservation], *, now: float
    ) -> SessionSnapshot | None:
        self._refresh(now)
        for event in events:
            self._observe(event, now=now)
        self._refresh(now)
        return self.snapshot


class NativeSuccessorTracker:
    """Track declared A → B passively, preserving A's independently observed finish.

    B may qualify without an observed A finish; that does not fabricate completion
    or authorize further staging. Live callers provide A's pre-batch snapshot.
    Reconnect callers use reconnect(), which cannot restore live evidence from A.
    """

    def __init__(
        self,
        authorization: NativeSuccessorAuthorization,
        *,
        connection_generation: str,
        started_monotonic: float,
        predecessor: SessionSnapshot | None = None,
        evidence_policy: EvidencePolicy = _DEFAULT_POLICY,
    ) -> None:
        self.authorization = authorization
        self._tracker = NativePlaybackTracker(
            authorization.successor,
            application_id=authorization.application_id,
            session_id=authorization.session_id,
            connection_generation=connection_generation,
            started_monotonic=started_monotonic,
            evidence_policy=evidence_policy,
        )
        if predecessor is not None and (
            predecessor.scope.request != authorization.predecessor
            or predecessor.scope.application_id != authorization.application_id
            or predecessor.scope.session_id != authorization.session_id
            or predecessor.scope.connection_generation != connection_generation
            or predecessor.scope.started_monotonic > started_monotonic
            or (
                predecessor.last_monotonic is not None
                and predecessor.last_monotonic > started_monotonic
            )
            or predecessor.ownership_lost
            or predecessor.stop_requested
        ):
            raise ValueError("live predecessor snapshot must match the current authorized scope")
        self.predecessor = predecessor or SessionSnapshot(
            PlaybackScope(
                authorization.predecessor,
                authorization.session_id,
                connection_generation,
                started_monotonic,
                authorization.application_id,
            )
        )
        if predecessor is not None:
            self._tracker._last_sequence = predecessor.last_sequence

    @classmethod
    def reconnect(
        cls,
        authorization: NativeSuccessorAuthorization,
        *,
        connection_generation: str,
        started_monotonic: float,
        predecessor_completion: CompletionEvidence | None = None,
        evidence_policy: EvidencePolicy = _DEFAULT_POLICY,
    ) -> NativeSuccessorTracker:
        """Start fresh; optional completion is historical and never a progress anchor."""
        result = cls(
            authorization,
            connection_generation=connection_generation,
            started_monotonic=started_monotonic,
            evidence_policy=evidence_policy,
        )
        result.predecessor = replace(
            result.predecessor,
            completion=predecessor_completion,
            evidence=PlaybackEvidence(
                natural_completion_confirmed=predecessor_completion is not None
            ),
        )
        return result

    @property
    def successor(self) -> SessionSnapshot | None:
        return self._tracker.snapshot

    @property
    def ready(self) -> bool:
        return self._tracker.ready

    @property
    def refusal(self) -> NativeObservationRefusal | None:
        return self._tracker.refusal

    def observe(
        self, events: Iterable[PlaybackObservation], *, now: float
    ) -> SessionSnapshot | None:
        self._tracker._refresh(now)
        for event in events:
            if self._tracker._observe(
                event, now=now, waiting_content=self.authorization.predecessor.content
            ):
                self.predecessor = policy.observe(
                    self.predecessor, event, now=now, policy=self._tracker.evidence_policy
                )
                if self.refusal is not None and not self.predecessor.ownership_lost:
                    self.predecessor = replace(
                        policy.invalidate_history(self.predecessor),
                        ownership_lost=True,
                        evidence=PlaybackEvidence(
                            natural_completion_confirmed=self.predecessor.completion is not None,
                            reason=EvidenceReason.SESSION_REPLACED,
                        ),
                    )
        self.predecessor = policy.refresh(
            self.predecessor, now=now, policy=self._tracker.evidence_policy
        )
        self._tracker._refresh(now)
        return self.successor
