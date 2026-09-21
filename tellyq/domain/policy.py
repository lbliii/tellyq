"""Deterministic evidence policy. Callers provide every event and time input."""

from dataclasses import replace

from .values import (
    CommandAction,
    CommandReceipt,
    DisplayEvidence,
    EvidencePolicy,
    EvidenceReason,
    IdleReason,
    PlaybackEvidence,
    PlaybackObservation,
    PlayerState,
    SessionSnapshot,
    require_instant,
)

_DEFAULT_POLICY = EvidencePolicy()


def record_receipt(snapshot: SessionSnapshot, receipt: CommandReceipt) -> SessionSnapshot:
    """Record command outcome without turning it into observed playback state."""
    request = snapshot.scope.request
    if receipt.request_id != request.request_id or receipt.attempt_id != request.attempt_id:
        raise ValueError("receipt does not belong to this request and attempt")
    result = request_stop(snapshot) if receipt.action == CommandAction.STOP else snapshot
    return replace(result, revision=result.revision + 1, receipt=receipt)


def request_stop(snapshot: SessionSnapshot) -> SessionSnapshot:
    """Cancel natural advancement before sending any remote stop effect."""
    return replace(
        snapshot,
        revision=snapshot.revision + 1,
        stop_requested=True,
        evidence=replace(snapshot.evidence, natural_completion_confirmed=False),
    )


def record_display(snapshot: SessionSnapshot, display: DisplayEvidence) -> SessionSnapshot:
    """Store an explicitly historical report; it never confirms receiver progress."""
    request = snapshot.scope.request
    if display.target != request.target or display.content != request.content:
        raise ValueError("display evidence does not belong to this content and target")
    return replace(snapshot, revision=snapshot.revision + 1, display=display)


def refresh(
    snapshot: SessionSnapshot, *, now: float, policy: EvidencePolicy = _DEFAULT_POLICY
) -> SessionSnapshot:
    """Expire current telemetry, including when no callback arrives."""
    require_instant(now, "current time")
    latest = snapshot.latest
    if latest is None or snapshot.ownership_lost:
        return snapshot
    if now < latest.monotonic:
        raise ValueError("current time precedes the latest observation")
    if now - latest.monotonic <= policy.max_age_seconds:
        return snapshot
    if snapshot.evidence.reason == EvidenceReason.STALE:
        return snapshot
    return replace(
        snapshot,
        revision=snapshot.revision + 1,
        state=PlayerState.UNKNOWN,
        evidence=PlaybackEvidence(reason=EvidenceReason.STALE),
        progress_anchor=None,
        has_confirmed_playback=False,
    )


def observe(
    snapshot: SessionSnapshot,
    observation: PlaybackObservation,
    *,
    now: float,
    policy: EvidencePolicy = _DEFAULT_POLICY,
) -> SessionSnapshot:
    """Consume a fresh ordered sample from one explicitly owned connection scope.

    Foreign, old-generation, pre-start, stale, future, duplicate and reordered
    observations are discarded. They cannot extend the current evidence lifetime.
    A fresh known takeover is sticky: callers must reconcile and create a new
    scope, rather than allowing delayed packets to reclaim the receiver.
    """
    snapshot = refresh(snapshot, now=now, policy=policy)
    scope = snapshot.scope
    if (
        snapshot.ownership_lost
        or observation.target != scope.request.target
        or observation.connection_generation != scope.connection_generation
        or observation.monotonic <= scope.started_monotonic
        or not 0 <= now - observation.monotonic <= policy.max_age_seconds
    ):
        return snapshot
    latest = snapshot.latest
    if latest is not None and (
        observation.sequence <= latest.sequence or observation.monotonic <= latest.monotonic
    ):
        return snapshot
    if (observation.session_id is not None and observation.session_id != scope.session_id) or (
        observation.content is not None and observation.content != scope.request.content
    ):
        return replace(
            snapshot,
            revision=snapshot.revision + 1,
            latest=observation,
            state=PlayerState.UNKNOWN,
            evidence=PlaybackEvidence(reason=EvidenceReason.SESSION_REPLACED),
            progress_anchor=None,
            has_confirmed_playback=False,
            ownership_lost=True,
        )
    if observation.session_id is None or observation.content is None:
        return _unconfirmed(snapshot, observation, EvidenceReason.IDENTITY_UNKNOWN)
    if observation.ad_active is not False:
        reason = EvidenceReason.AD_ACTIVE if observation.ad_active else EvidenceReason.AD_UNKNOWN
        return _unconfirmed(snapshot, observation, reason, identity=True)
    if observation.state == PlayerState.IDLE:
        if snapshot.stop_requested and observation.idle_reason == IdleReason.CANCELED:
            return replace(
                snapshot,
                revision=snapshot.revision + 1,
                latest=observation,
                state=PlayerState.STOPPED,
                progress_anchor=None,
                evidence=PlaybackEvidence(
                    identity_confirmed=True, reason=EvidenceReason.STOP_OBSERVED
                ),
            )
        if (
            observation.idle_reason == IdleReason.FINISHED
            and snapshot.has_confirmed_playback
            and not snapshot.stop_requested
        ):
            return replace(
                snapshot,
                revision=snapshot.revision + 1,
                latest=observation,
                state=PlayerState.ENDED,
                progress_anchor=None,
                evidence=PlaybackEvidence(
                    identity_confirmed=True,
                    natural_completion_confirmed=True,
                    reason=EvidenceReason.NATURAL_COMPLETION,
                ),
            )
    if observation.state != PlayerState.PLAYING:
        return _unconfirmed(snapshot, observation, EvidenceReason.NOT_PLAYING, identity=True)
    if observation.position is None:
        return _unconfirmed(snapshot, observation, EvidenceReason.POSITION_UNKNOWN, identity=True)
    anchor = snapshot.progress_anchor
    confirmed = (
        anchor is not None
        and anchor.position is not None
        and now - anchor.monotonic <= policy.max_age_seconds
        and observation.monotonic - anchor.monotonic >= policy.min_progress_interval_seconds
        and observation.position > anchor.position
    )
    # Keep the first close sample as an anchor so frequent callbacks can still
    # establish a full interval. A seek backwards or an expired anchor resets it.
    if (
        anchor is None
        or anchor.position is None
        or confirmed
        or observation.position < anchor.position
        or observation.monotonic - anchor.monotonic >= policy.max_age_seconds
    ):
        anchor = observation
    return replace(
        snapshot,
        revision=snapshot.revision + 1,
        latest=observation,
        progress_anchor=anchor,
        state=PlayerState.PLAYING,
        has_confirmed_playback=snapshot.has_confirmed_playback or confirmed,
        evidence=PlaybackEvidence(
            identity_confirmed=True,
            receiver_playback_confirmed=confirmed,
            reason=EvidenceReason.PROGRESS_CONFIRMED
            if confirmed
            else EvidenceReason.AWAITING_PROGRESS,
        ),
    )


def _unconfirmed(
    snapshot: SessionSnapshot,
    observation: PlaybackObservation,
    reason: EvidenceReason,
    *,
    identity: bool = False,
) -> SessionSnapshot:
    return replace(
        snapshot,
        revision=snapshot.revision + 1,
        latest=observation,
        state=observation.state if identity else PlayerState.UNKNOWN,
        progress_anchor=None,
        evidence=PlaybackEvidence(identity_confirmed=identity, reason=reason),
    )
