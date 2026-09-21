"""Pure queue decisions; neither durable history nor a decision performs device I/O."""

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from .queue import (
    AttemptOrigin,
    ExecutionState,
    NativeReservationState,
    QueueAttempt,
    QueueIntent,
    QueueMode,
    QueueSnapshot,
)
from .values import (
    CommandAction,
    CompletionAttribution,
    ContentPhase,
    EvidencePolicy,
    IdleReason,
    PlaybackObservation,
    PlayerState,
    SessionSnapshot,
    require_instant,
)


class QueueDisposition(StrEnum):
    HOLD = "hold"
    READY = "ready"
    COMPLETE = "complete"


class QueueReason(StrEnum):
    CANCELED = "canceled"
    INVALID_QUEUE = "invalid_queue"
    AUTHORITY_REQUIRED = "authority_required"
    SCOPE_MISMATCH = "scope_mismatch"
    NEEDS_ATTENTION = "needs_attention"
    COMMAND_UNRESOLVED = "command_unresolved"
    SESSION_REQUIRED = "session_required"
    AWAITING_COMPLETION = "awaiting_completion"
    INVALID_COMPLETION = "invalid_completion"
    COMPLETION_OBSERVED = "completion_observed"
    SKIP_REQUESTED = "skip_requested"
    RECEIVER_REPLACED = "receiver_replaced"
    RECEIVER_UNCONFIRMED = "receiver_unconfirmed"
    NEXT_ITEM = "next_item"
    EXHAUSTED = "exhausted"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    NATIVE_SUCCESSOR_REQUIRED = "native_successor_required"


@dataclass(frozen=True, slots=True)
class QueueAuthority:
    """Caller-created owner scope, never recovered from a saved monotonic timestamp.

    The owner supplies its current queue generation, live connection generation and
    acquisition boundary. This value is authorization, not receiver evidence.
    """

    queue_id: str
    generation: int
    connection_generation: str
    started_monotonic: float

    def __post_init__(self) -> None:
        if not self.queue_id or not self.connection_generation:
            raise ValueError("authority requires queue and connection identities")
        if not _counter(self.generation):
            raise ValueError("authority generation must be a non-negative integer")
        require_instant(self.started_monotonic, "authority boundary")


@dataclass(frozen=True, slots=True)
class SkipIntent:
    """An explicit caller intent; skipping records no claim about the remote effect."""

    intent_id: str
    attempt_id: str
    item_id: str

    def __post_init__(self) -> None:
        if not self.intent_id or not self.attempt_id or not self.item_id:
            raise ValueError("skip requires intent, attempt and item identities")


@dataclass(frozen=True, slots=True)
class AttemptSettlement:
    attempt_id: str
    item_id: str
    outcome: QueueIntent
    decision_id: str


@dataclass(frozen=True, slots=True)
class QueueDecision:
    """At most one local settlement or next-item proposal, with optimistic guards.

    A settlement always holds advancement until it is persisted and the queue is
    reduced again. READY still requires a dispatch-time authority/cancellation check.
    """

    queue_id: str
    expected_revision: int
    generation: int
    disposition: QueueDisposition
    reason: QueueReason
    settlement: AttemptSettlement | None = None
    next_item_id: str | None = None


_POLICY = EvidencePolicy()
_SETTLED = {QueueIntent.FINISHED, QueueIntent.SKIPPED}


def _counter(value: int) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _valid_queue(queue: QueueSnapshot) -> bool:
    """Reject inconsistent hand-built snapshots as well as miscorrelated inputs."""
    items = {item.item_id: item for item in queue.items}
    attempts = {attempt.attempt_id: attempt for attempt in queue.attempts}
    if (
        not queue.queue_id
        or not queue.items
        or not _counter(queue.revision)
        or not _counter(queue.generation)
        or len(items) != len(queue.items)
        or len(attempts) != len(queue.attempts)
        or len({attempt.item_id for attempt in queue.attempts}) != len(queue.attempts)
        or len({command.command_id for command in queue.commands}) != len(queue.commands)
        or any(command.attempt_id not in attempts for command in queue.commands)
        or any(
            not _counter(command.generation) or command.generation > queue.generation
            for command in queue.commands
        )
    ):
        return False
    current = [item.item_id for item in queue.items if item.intent == QueueIntent.CURRENT]
    if len(current) > 1 or (current and current[0] != queue.current_item_id):
        return False
    if queue.attempts:
        if queue.current_item_id != queue.attempts[-1].item_id:
            return False
    elif queue.current_item_id is not None or current:
        return False
    pending_seen = False
    for item in queue.items:
        if item.intent == QueueIntent.PENDING:
            pending_seen = True
        elif pending_seen:
            return False
    return all(
        attempt.item_id in items
        and attempt.intent == items[attempt.item_id].intent
        and attempt.intent != QueueIntent.PENDING
        and (attempt.intent != QueueIntent.CURRENT or attempt.decision_id is None)
        and (attempt.intent not in _SETTLED or bool(attempt.decision_id))
        and _valid_attempt_origin(queue, attempt)
        for attempt in queue.attempts
    )


def _valid_attempt_origin(queue: QueueSnapshot, attempt: QueueAttempt) -> bool:
    starts = sum(
        command.attempt_id == attempt.attempt_id and command.action == CommandAction.START
        for command in queue.commands
    )
    if attempt.origin == AttemptOrigin.LOCAL_START:
        return starts == 1 and attempt.reservation_id is None
    return (
        queue.mode == QueueMode.NATIVE
        and attempt.origin == AttemptOrigin.NATIVE_OBSERVED
        and starts == 0
        and any(
            reservation.reservation_id == attempt.reservation_id
            and reservation.successor_attempt_id == attempt.attempt_id
            and reservation.successor_item_id == attempt.item_id
            and reservation.state
            in {NativeReservationState.ADOPTED, NativeReservationState.SESSION_EXIT_OBSERVED}
            and reservation.adoption_decision_id is not None
            for reservation in queue.reservations
        )
    )


def _session_matches(
    queue: QueueSnapshot, session: SessionSnapshot, authority: QueueAuthority, now: float
) -> bool:
    if not queue.attempts:
        return False
    attempt = queue.attempts[-1]
    scope = session.scope
    request = scope.request
    item = next(item for item in queue.items if item.item_id == attempt.item_id)
    return (
        request.target == queue.target
        and request.queue_item_id == attempt.item_id
        and request.attempt_id == attempt.attempt_id
        and request.content == item.content
        and scope.connection_generation == authority.connection_generation
        and authority.started_monotonic <= scope.started_monotonic <= now
        and (
            queue.mode == QueueMode.NATIVE
            or all(
                command.generation == queue.generation
                for command in queue.commands
                if command.attempt_id == attempt.attempt_id
            )
        )
    )


def _valid_completion(session: SessionSnapshot, now: float) -> bool:
    completion = session.completion
    if completion is None:
        return False
    try:
        require_instant(completion.monotonic, "completion time")
    except ValueError:
        return False
    return (
        session.evidence.natural_completion_confirmed
        and bool(completion.source)
        and completion.observed_at.tzinfo is not None
        and completion.observed_at.utcoffset() is not None
        and _counter(completion.sequence)
        and _counter(completion.identity_sequence)
        and completion.identity_sequence <= completion.sequence <= session.last_sequence
        and session.last_monotonic is not None
        and session.scope.started_monotonic < completion.monotonic <= session.last_monotonic <= now
        and (
            (
                completion.attribution == CompletionAttribution.EXPLICIT
                and completion.identity_sequence == completion.sequence
            )
            or (
                completion.attribution == CompletionAttribution.ORDERED_HISTORY
                and completion.identity_sequence < completion.sequence
            )
        )
    )


def _decision_id(queue: QueueSnapshot, session: SessionSnapshot, skip: SkipIntent | None) -> str:
    scope = session.scope
    request = scope.request
    parts = [queue.queue_id, request.attempt_id, request.queue_item_id]
    if skip is not None:
        parts += ["skip", skip.intent_id]
    else:
        completion = session.completion
        assert completion is not None
        parts += [
            "finish",
            scope.connection_generation,
            scope.session_id,
            completion.source,
            completion.observed_at.isoformat(),
            repr(completion.monotonic),
            str(completion.sequence),
            str(completion.identity_sequence),
            completion.attribution.value,
        ]
    # Length prefixes make arbitrary opaque identifiers unambiguous without a wire codec.
    material = "".join(f"{len(part)}:{part}" for part in parts)
    return "queue-decision-v1-" + sha256(material.encode()).hexdigest()


def _receiver_ready(
    queue: QueueSnapshot,
    session: SessionSnapshot | None,
    authority: QueueAuthority,
    receiver: PlaybackObservation | None,
    now: float,
    policy: EvidencePolicy,
) -> bool:
    latest = session.latest if session is not None else None
    if receiver is not None:
        # An older idle packet must not override a newer media/receiver observation.
        if session is not None and (
            (session.last_monotonic is not None and receiver.monotonic < session.last_monotonic)
            or receiver.sequence < session.last_sequence
            or (
                receiver != session.latest
                and (
                    receiver.sequence == session.last_sequence
                    or receiver.monotonic == session.last_monotonic
                )
            )
        ):
            return False
        latest = receiver
    if (
        latest is None
        or latest.target != queue.target
        or latest.connection_generation != authority.connection_generation
        or latest.monotonic <= authority.started_monotonic
        or not 0 <= now - latest.monotonic <= policy.max_age_seconds
        or latest.connection_reset
        or (
            session is not None
            and (
                latest.sequence < session.last_sequence
                or (
                    session.last_monotonic is not None and latest.monotonic < session.last_monotonic
                )
            )
        )
    ):
        return False
    if latest.session_active is False:
        return True
    if session is None or session.completion is None or latest != session.latest:
        return False
    completion = session.completion
    provider = latest.provider_evidence
    return (
        _valid_completion(session, now)
        and latest.sequence == session.last_sequence == completion.sequence
        and latest.monotonic == session.last_monotonic == completion.monotonic
        and latest.session_id == session.scope.session_id
        and latest.application_id == session.scope.application_id
        and latest.content in (None, session.scope.request.content)
        and latest.state == PlayerState.IDLE
        and latest.idle_reason == IdleReason.FINISHED
        and latest.ad_active is False
        and provider is not None
        and provider.phase == ContentPhase.FINISHED
        and provider.source == completion.source
    )


def decide_queue(
    queue: QueueSnapshot,
    session: SessionSnapshot | None,
    *,
    now: float,
    authority: QueueAuthority | None,
    receiver: PlaybackObservation | None = None,
    skip: SkipIntent | None = None,
    policy: EvidencePolicy = _POLICY,
) -> QueueDecision:
    """Propose one local transition, using caller-supplied live evidence and authority.

    The caller must first feed every ordered event to playback policy. ``receiver``
    may supply a newer explicit idle baseline; it cannot erase known replacement.
    Persist a settlement, reload, and reduce again before preparing a start intent.
    """
    require_instant(now, "current time")

    def hold(reason: QueueReason, settlement: AttemptSettlement | None = None) -> QueueDecision:
        return QueueDecision(
            queue.queue_id,
            queue.revision,
            queue.generation,
            QueueDisposition.HOLD,
            reason,
            settlement=settlement,
        )

    if queue.cancellation_requested or (session is not None and session.stop_requested):
        return hold(QueueReason.CANCELED)
    if not _valid_queue(queue):
        return hold(QueueReason.INVALID_QUEUE)
    if any(command.action == CommandAction.STOP for command in queue.commands):
        return hold(QueueReason.CANCELED)
    if authority is None:
        return hold(QueueReason.AUTHORITY_REQUIRED)
    if (
        authority.queue_id != queue.queue_id
        or authority.generation != queue.generation
        or authority.started_monotonic > now
    ):
        return hold(QueueReason.SCOPE_MISMATCH)
    if queue.mode != QueueMode.NATIVE and any(
        item.intent in {QueueIntent.NEEDS_ATTENTION, QueueIntent.STOPPED} for item in queue.items
    ):
        return hold(QueueReason.NEEDS_ATTENTION)
    if any(
        command.state
        in {ExecutionState.PENDING, ExecutionState.DISPATCHED, ExecutionState.UNCERTAIN}
        for command in queue.commands
    ):
        return hold(QueueReason.COMMAND_UNRESOLVED)
    if session is not None and not _session_matches(queue, session, authority, now):
        return hold(QueueReason.SCOPE_MISMATCH)
    if queue.attempts and session is None:
        return hold(QueueReason.SESSION_REQUIRED)
    if not queue.attempts and any(item.intent != QueueIntent.PENDING for item in queue.items):
        return hold(QueueReason.NEEDS_ATTENTION)
    attempt: QueueAttempt | None = queue.attempts[-1] if queue.attempts else None
    if skip is not None and (
        attempt is None or skip.attempt_id != attempt.attempt_id or skip.item_id != attempt.item_id
    ):
        return hold(QueueReason.SCOPE_MISMATCH)
    if attempt is not None and attempt.intent == QueueIntent.CURRENT:
        assert session is not None
        if skip is None and not _valid_completion(session, now):
            return hold(
                QueueReason.INVALID_COMPLETION
                if session.completion is not None
                else QueueReason.AWAITING_COMPLETION
            )
        if any(
            command.attempt_id == attempt.attempt_id
            and command.action == CommandAction.START
            and command.state != ExecutionState.ACKNOWLEDGED
            for command in queue.commands
        ):
            return hold(QueueReason.COMMAND_UNRESOLVED)
        outcome = QueueIntent.SKIPPED if skip is not None else QueueIntent.FINISHED
        settlement = AttemptSettlement(
            attempt.attempt_id, attempt.item_id, outcome, _decision_id(queue, session, skip)
        )
        reason = QueueReason.SKIP_REQUESTED if skip is not None else QueueReason.COMPLETION_OBSERVED
        if session.ownership_lost:
            reason = QueueReason.RECEIVER_REPLACED
        return hold(reason, settlement)
    if queue.reconciliation_required:
        return hold(QueueReason.RECONCILIATION_REQUIRED)
    if queue.mode == QueueMode.NATIVE and queue.attempts:
        # Native successors are adopted from reserved observations. Never fall
        # back to a second START, even if the receiver appears idle after a gap.
        return hold(QueueReason.NATIVE_SUCCESSOR_REQUIRED)
    if session is not None and session.ownership_lost:
        return hold(QueueReason.RECEIVER_REPLACED)
    if attempt is not None and attempt.intent == QueueIntent.FINISHED:
        assert session is not None
        if not _valid_completion(session, now) or attempt.decision_id != _decision_id(
            queue, session, None
        ):
            return hold(QueueReason.INVALID_COMPLETION)
    if any(item.intent not in {*_SETTLED, QueueIntent.PENDING} for item in queue.items):
        return hold(QueueReason.NEEDS_ATTENTION)
    pending = next((item for item in queue.items if item.intent == QueueIntent.PENDING), None)
    if pending is None:
        return QueueDecision(
            queue.queue_id,
            queue.revision,
            queue.generation,
            QueueDisposition.COMPLETE,
            QueueReason.EXHAUSTED,
        )
    if not _receiver_ready(queue, session, authority, receiver, now, policy):
        return hold(QueueReason.RECEIVER_UNCONFIRMED)
    return QueueDecision(
        queue.queue_id,
        queue.revision,
        queue.generation,
        QueueDisposition.READY,
        QueueReason.NEXT_ITEM,
        next_item_id=pending.item_id,
    )
