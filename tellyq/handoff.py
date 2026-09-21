"""Process-local release authority and bounded explanations; never completion evidence."""

from dataclasses import dataclass, replace
from enum import StrEnum

from .domain.queue_policy import QueueReason
from .domain.values import (
    ContentPhase,
    IdentityUpdate,
    IdleReason,
    PlaybackObservation,
    PlayerState,
    SessionSnapshot,
)


class HandoffStage(StrEnum):
    QUEUE = "queue"
    RELEASE = "release"
    SUCCESSOR = "successor"


class HandoffDisposition(StrEnum):
    HOLD = "hold"
    READY = "ready"
    COMPLETE = "complete"


class HandoffReason(StrEnum):
    AWAITING_START = "awaiting_start"
    AWAITING_COMPLETION = "awaiting_completion"
    ATTEMPT_NOT_FINISHED = "attempt_not_finished"
    CANCELED = "canceled"
    SESSION_REQUIRED = "session_required"
    OWNERSHIP_LOST = "ownership_lost"
    STOP_REQUESTED = "stop_requested"
    AUTHORITY_REQUIRED = "authority_required"
    AUTHORITY_BLOCKED = "authority_blocked"
    COMPLETION_REQUIRED = "completion_required"
    COMPLETION_MISMATCH = "completion_mismatch"
    TERMINAL_STALE = "terminal_stale"
    TERMINAL_UNQUALIFIED = "terminal_unqualified"
    SEQUENCE_GAP = "sequence_gap"
    OBSERVATION_GAP = "observation_gap"
    CLOCK_DISCONTINUITY = "clock_discontinuity"
    TARGET_CHANGED = "target_changed"
    CONNECTION_CHANGED = "connection_changed"
    CONNECTION_RESET = "connection_reset"
    AD_ACTIVE = "ad_active"
    CONTENT_CHANGED = "content_changed"
    SESSION_CHANGED = "session_changed"
    APPLICATION_CHANGED = "application_changed"
    MEDIA_CHANGED = "media_changed"
    PLAYBACK_RESTARTED = "playback_restarted"
    PROVIDER_ACTIVE = "provider_active"
    POSITION_REWOUND = "position_rewound"
    RECEIVER_MISSING = "receiver_missing"
    MEDIA_MISSING = "media_missing"
    RECEIVER_INACTIVE = "receiver_inactive"
    RECEIVER_IDENTITY_MISMATCH = "receiver_identity_mismatch"
    RECEIVER_STALE = "receiver_stale"
    MEDIA_IDENTITY_MISSING = "media_identity_missing"
    MEDIA_IDENTITY_MISMATCH = "media_identity_mismatch"
    MEDIA_STALE = "media_stale"
    MEDIA_STATE_UNQUALIFIED = "media_state_unqualified"
    PROVIDER_PHASE_UNQUALIFIED = "provider_phase_unqualified"
    RELEASE_ELIGIBLE = "release_eligible"
    RELEASE_REJECTED = "release_rejected"
    RELEASE_UNRESOLVED = "release_unresolved"
    AWAITING_RELEASE_IDLE = "awaiting_release_idle"
    AWAITING_FRESH_IDLE = "awaiting_fresh_idle"
    QUEUE_POLICY_HOLD = "queue_policy_hold"
    SUCCESSOR_ELIGIBLE = "successor_eligible"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class HandoffEvidence:
    """Fixed safe projection; relative timing and comparisons replace private identities.

    Ages are measured when the explanation is captured, not when a client reads it.
    First-veto tracking has no current clock; its age is explicitly unavailable.
    Provider numeric codes are absent from normalized observations and not inferred.
    """

    sequence: int
    age_seconds: float | None
    since_terminal_seconds: float | None
    sequence_delta: int | None
    observation_delta_seconds: float | None
    terminal_sequence: int | None
    terminal_position: float | None
    state: PlayerState
    idle_reason: IdleReason | None
    position: float | None
    ad_active: bool | None
    provider_phase: ContentPhase | None
    identity_update: IdentityUpdate
    session_active: bool | None
    connection_reset: bool
    target_matches: bool
    connection_matches: bool
    content_matches: bool | None
    session_matches: bool | None
    application_matches: bool | None
    media_matches: bool | None


@dataclass(frozen=True, slots=True)
class HandoffVeto:
    reason: HandoffReason
    evidence: HandoffEvidence


@dataclass(frozen=True, slots=True)
class HandoffDiagnostic:
    """Eligibility at one stage, never evidence that a command was dispatched."""

    stage: HandoffStage
    disposition: HandoffDisposition
    reason: HandoffReason
    evidence: HandoffEvidence | None = None
    first_veto: HandoffVeto | None = None
    queue_reason: QueueReason | None = None


@dataclass(frozen=True, slots=True)
class ReleaseAuthority:
    terminal: PlaybackObservation
    last_sequence: int
    last_monotonic: float
    blocked: bool = False
    first_veto: HandoffVeto | None = None


def handoff_evidence(
    snapshot: SessionSnapshot,
    event: PlaybackObservation | None,
    authority: ReleaseAuthority | None,
    *,
    now: float | None,
) -> HandoffEvidence | None:
    if event is None:
        return None
    scope = snapshot.scope
    terminal = authority.terminal if authority else None
    return HandoffEvidence(
        sequence=event.sequence,
        age_seconds=now - event.monotonic if now is not None else None,
        since_terminal_seconds=event.monotonic - terminal.monotonic if terminal else None,
        sequence_delta=event.sequence - authority.last_sequence if authority else None,
        observation_delta_seconds=event.monotonic - authority.last_monotonic if authority else None,
        terminal_sequence=terminal.sequence if terminal else None,
        terminal_position=terminal.position if terminal else None,
        state=event.state,
        idle_reason=event.idle_reason,
        position=event.position,
        ad_active=event.ad_active,
        provider_phase=event.provider_evidence.phase if event.provider_evidence else None,
        identity_update=event.identity_update,
        session_active=event.session_active,
        connection_reset=event.connection_reset,
        target_matches=event.target == scope.request.target,
        connection_matches=event.connection_generation == scope.connection_generation,
        content_matches=event.content == scope.request.content
        if event.content is not None
        else None,
        session_matches=event.session_id == scope.session_id
        if event.session_id is not None
        else None,
        application_matches=event.application_id == scope.application_id
        if event.application_id is not None
        else None,
        media_matches=event.playback_id == terminal.playback_id
        if terminal is not None and event.playback_id is not None
        else None,
    )


def _veto_reason(
    snapshot: SessionSnapshot, event: PlaybackObservation, authority: ReleaseAuthority
) -> HandoffReason | None:
    """First matching guard, in the original veto order. No guard is relaxed."""
    terminal, scope = authority.terminal, snapshot.scope
    checks = (
        (event.sequence != authority.last_sequence + 1, HandoffReason.SEQUENCE_GAP),
        (event.monotonic <= authority.last_monotonic, HandoffReason.CLOCK_DISCONTINUITY),
        (event.monotonic - authority.last_monotonic > 5, HandoffReason.OBSERVATION_GAP),
        (event.target != scope.request.target, HandoffReason.TARGET_CHANGED),
        (
            event.connection_generation != scope.connection_generation,
            HandoffReason.CONNECTION_CHANGED,
        ),
        (event.connection_reset, HandoffReason.CONNECTION_RESET),
        (event.ad_active is True, HandoffReason.AD_ACTIVE),
        (
            event.content is not None and event.content != scope.request.content,
            HandoffReason.CONTENT_CHANGED,
        ),
        (
            event.session_id is not None and event.session_id != scope.session_id,
            HandoffReason.SESSION_CHANGED,
        ),
        (
            event.application_id is not None and event.application_id != scope.application_id,
            HandoffReason.APPLICATION_CHANGED,
        ),
        (
            event.playback_id is not None and event.playback_id != terminal.playback_id,
            HandoffReason.MEDIA_CHANGED,
        ),
        (
            event.state in {PlayerState.PLAYING, PlayerState.PAUSED},
            HandoffReason.PLAYBACK_RESTARTED,
        ),
        (
            event.provider_evidence is not None
            and event.provider_evidence.phase
            in {ContentPhase.AD, ContentPhase.PLAYING, ContentPhase.PAUSED},
            HandoffReason.PROVIDER_ACTIVE,
        ),
        (
            event.position is not None
            and terminal.position is not None
            and event.position < terminal.position,
            HandoffReason.POSITION_REWOUND,
        ),
    )
    return next((reason for failed, reason in checks if failed), None)


def track_release(
    snapshot: SessionSnapshot,
    events: tuple[PlaybackObservation, ...],
    authority: ReleaseAuthority | None,
) -> ReleaseAuthority | None:
    """Retain the terminal and first veto permanently, even after harmless later events."""
    witness = snapshot.completion
    if witness is None:
        return None
    if authority is None:
        terminal = next(
            (
                event
                for event in events
                if event.sequence == witness.sequence and event.monotonic == witness.monotonic
            ),
            None,
        )
        if terminal is None:
            return None
        authority = ReleaseAuthority(terminal, terminal.sequence, terminal.monotonic)
    for event in events:
        if (
            event.sequence <= authority.terminal.sequence
            or event.sequence <= authority.last_sequence
        ):
            continue  # The same returned batch may be passed through two application layers.
        veto = authority.first_veto
        reason = _veto_reason(snapshot, event, authority)
        if reason is not None and veto is None:
            evidence = handoff_evidence(snapshot, event, authority, now=None)
            assert evidence is not None
            veto = HandoffVeto(reason, evidence)
        authority = replace(
            authority,
            last_sequence=event.sequence,
            last_monotonic=event.monotonic,
            blocked=authority.blocked or reason is not None,
            first_veto=veto,
        )
    return authority


def release_decision(
    snapshot: SessionSnapshot,
    authority: ReleaseAuthority | None,
    events: tuple[PlaybackObservation, ...],
    *,
    now: float,
) -> HandoffDiagnostic:
    """Explain the existing fresh-identity release policy without inventing evidence."""

    def hold(reason: HandoffReason, event: PlaybackObservation | None = None) -> HandoffDiagnostic:
        return HandoffDiagnostic(
            HandoffStage.RELEASE,
            HandoffDisposition.HOLD,
            reason,
            handoff_evidence(snapshot, event, authority, now=now),
            authority.first_veto if authority else None,
        )

    if authority is None:
        return hold(HandoffReason.AUTHORITY_REQUIRED)
    if authority.blocked:
        return hold(HandoffReason.AUTHORITY_BLOCKED, events[-1] if events else None)
    witness = snapshot.completion
    if witness is None:
        return hold(HandoffReason.COMPLETION_REQUIRED)
    terminal, scope = authority.terminal, snapshot.scope
    if snapshot.ownership_lost:
        return hold(HandoffReason.OWNERSHIP_LOST, snapshot.latest)
    if snapshot.stop_requested:
        return hold(HandoffReason.STOP_REQUESTED, snapshot.latest)
    if not 0 <= now - terminal.monotonic <= 5:
        return hold(HandoffReason.TERMINAL_STALE, terminal)
    if terminal.sequence != witness.sequence or terminal.monotonic != witness.monotonic:
        return hold(HandoffReason.COMPLETION_MISMATCH, terminal)
    if (
        terminal.playback_id is None
        or terminal.ad_active is not False
        or terminal.state != PlayerState.IDLE
        or terminal.idle_reason != IdleReason.FINISHED
        or terminal.target != scope.request.target
        or terminal.connection_generation != scope.connection_generation
        or terminal.session_id != scope.session_id
        or terminal.application_id != scope.application_id
        or terminal.content not in (None, scope.request.content)
    ):
        return hold(HandoffReason.TERMINAL_UNQUALIFIED, terminal)
    receiver = next((event for event in reversed(events) if event.session_active is not None), None)
    media = next((event for event in reversed(events) if event.session_active is None), None)
    if receiver is None:
        return hold(HandoffReason.RECEIVER_MISSING)
    if media is None:
        return hold(HandoffReason.MEDIA_MISSING)
    if receiver.session_active is not True:
        return hold(HandoffReason.RECEIVER_INACTIVE, receiver)
    if (
        receiver.session_id != scope.session_id
        or receiver.application_id != scope.application_id
        or receiver.target != scope.request.target
        or receiver.connection_generation != scope.connection_generation
    ):
        return hold(HandoffReason.RECEIVER_IDENTITY_MISMATCH, receiver)
    if not 0 <= now - receiver.monotonic <= 5:
        return hold(HandoffReason.RECEIVER_STALE, receiver)
    if not 0 <= now - media.monotonic <= 5:
        return hold(HandoffReason.MEDIA_STALE, media)
    if media.content is None or media.identity_update != IdentityUpdate.EXPLICIT:
        return hold(HandoffReason.MEDIA_IDENTITY_MISSING, media)
    if (
        media.content != scope.request.content
        or media.session_id != scope.session_id
        or media.application_id != scope.application_id
        or media.playback_id != terminal.playback_id
        or media.target != scope.request.target
        or media.connection_generation != scope.connection_generation
    ):
        return hold(HandoffReason.MEDIA_IDENTITY_MISMATCH, media)
    if media.ad_active is True:
        return hold(HandoffReason.AD_ACTIVE, media)
    if media.state not in {PlayerState.IDLE, PlayerState.BUFFERING} or (
        media.state == PlayerState.IDLE and media.idle_reason != IdleReason.FINISHED
    ):
        return hold(HandoffReason.MEDIA_STATE_UNQUALIFIED, media)
    if media.provider_evidence is not None and media.provider_evidence.phase not in {
        ContentPhase.UNKNOWN,
        ContentPhase.BUFFERING,
        ContentPhase.FINISHED,
    }:
        return hold(HandoffReason.PROVIDER_PHASE_UNQUALIFIED, media)
    return HandoffDiagnostic(
        HandoffStage.RELEASE,
        HandoffDisposition.READY,
        HandoffReason.RELEASE_ELIGIBLE,
        handoff_evidence(snapshot, media, authority, now=now),
    )


def may_release(
    snapshot: SessionSnapshot,
    authority: ReleaseAuthority | None,
    events: tuple[PlaybackObservation, ...],
    *,
    now: float,
) -> bool:
    """Compatibility predicate: eligibility is still checked immediately before effects."""
    return (
        release_decision(snapshot, authority, events, now=now).disposition
        == HandoffDisposition.READY
    )
