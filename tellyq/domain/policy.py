"""Deterministic evidence policy. Callers provide every event and time input."""

from dataclasses import replace

from .values import (
    CommandAction,
    CommandOutcome,
    CommandReceipt,
    CompletionAttribution,
    CompletionEvidence,
    ContentPhase,
    DisplayEvidence,
    EvidencePolicy,
    EvidenceReason,
    IdentityUpdate,
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


def request_stop(snapshot: SessionSnapshot, *, boundary: float | None = None) -> SessionSnapshot:
    """Cancel natural advancement before sending any remote stop effect."""
    if boundary is not None:
        require_instant(boundary, "stop boundary")
    return replace(
        snapshot,
        revision=snapshot.revision + 1,
        stop_requested=True,
        stop_boundary=boundary if boundary is not None else snapshot.stop_boundary,
        completion_anchor=None,
        has_confirmed_playback=False,
        evidence=PlaybackEvidence(natural_completion_confirmed=snapshot.completion is not None),
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
    """Expire current telemetry, retaining a previously witnessed historical completion."""
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
        state=PlayerState.ENDED if snapshot.completion is not None else PlayerState.UNKNOWN,
        evidence=PlaybackEvidence(
            natural_completion_confirmed=snapshot.completion is not None,
            reason=EvidenceReason.STALE,
        ),
        progress_anchor=None,
        completion_anchor=None,
        has_confirmed_playback=False,
    )


def invalidate_history(snapshot: SessionSnapshot) -> SessionSnapshot:
    """Retire the attribution chain at an explicit missing-observation boundary."""
    return replace(
        snapshot,
        revision=snapshot.revision + 1,
        progress_anchor=None,
        completion_anchor=None,
        has_confirmed_playback=False,
        release_boundary=None,
        release_confirmed=False,
        state=PlayerState.ENDED if snapshot.completion is not None else PlayerState.UNKNOWN,
        evidence=PlaybackEvidence(
            natural_completion_confirmed=snapshot.completion is not None,
            reason=EvidenceReason.IDENTITY_UNKNOWN,
        ),
    )


def _lost(snapshot: SessionSnapshot, observation: PlaybackObservation) -> SessionSnapshot:
    return replace(
        snapshot,
        revision=snapshot.revision + 1,
        latest=observation,
        state=PlayerState.UNKNOWN,
        progress_anchor=None,
        completion_anchor=None,
        has_confirmed_playback=False,
        ownership_lost=True,
        release_boundary=None,
        release_confirmed=False,
        evidence=PlaybackEvidence(
            natural_completion_confirmed=snapshot.completion is not None,
            reason=EvidenceReason.SESSION_REPLACED,
        ),
    )


def _qualified(observation: PlaybackObservation) -> bool:
    """Provider contracts stay in adapters; the core checks their generic consistency."""
    evidence = observation.provider_evidence
    phases = {
        PlayerState.PLAYING: ContentPhase.PLAYING,
        PlayerState.PAUSED: ContentPhase.PAUSED,
        PlayerState.BUFFERING: ContentPhase.BUFFERING,
        PlayerState.IDLE: ContentPhase.FINISHED,
    }
    return (
        evidence is not None
        and phases.get(observation.state) == evidence.phase
        and observation.ad_active is False
        and (
            observation.state != PlayerState.IDLE or observation.idle_reason == IdleReason.FINISHED
        )
    )


def _history_matches(
    snapshot: SessionSnapshot, observation: PlaybackObservation, policy: EvidencePolicy
) -> bool:
    anchor = snapshot.completion_anchor
    return (
        anchor is not None
        and snapshot.has_confirmed_playback
        and anchor.playback_id is not None
        and anchor.playback_id == observation.playback_id
        and anchor.session_id == observation.session_id == snapshot.scope.session_id
        and anchor.application_id == observation.application_id == snapshot.scope.application_id
        and 0 < observation.monotonic - anchor.monotonic <= policy.max_age_seconds
        and anchor.sequence < observation.sequence
    )


def _incremental_identity(
    snapshot: SessionSnapshot, observation: PlaybackObservation, policy: EvidencePolicy
) -> bool:
    anchor = snapshot.completion_anchor
    return (
        observation.content is None
        and observation.identity_update == IdentityUpdate.OMITTED
        and _qualified(observation)
        and _history_matches(snapshot, observation, policy)
        and anchor is not None
        and anchor.content == snapshot.scope.request.content
        and anchor.identity_update == IdentityUpdate.EXPLICIT
        and anchor.provider_evidence is not None
        and observation.provider_evidence is not None
        and anchor.provider_evidence.source == observation.provider_evidence.source
    )


def _finish(
    snapshot: SessionSnapshot, observation: PlaybackObservation, *, history: bool
) -> SessionSnapshot:
    anchor = snapshot.completion_anchor
    assert anchor is not None
    completion = CompletionEvidence(
        observed_at=observation.observed_at,
        monotonic=observation.monotonic,
        sequence=observation.sequence,
        identity_sequence=anchor.sequence if history else observation.sequence,
        attribution=CompletionAttribution.ORDERED_HISTORY
        if history
        else CompletionAttribution.EXPLICIT,
        source=observation.provider_evidence.source
        if observation.provider_evidence is not None
        else observation.source,
    )
    return replace(
        snapshot,
        revision=snapshot.revision + 1,
        latest=observation,
        state=PlayerState.ENDED,
        progress_anchor=None,
        completion_anchor=None,
        completion=completion,
        has_confirmed_playback=False,
        evidence=PlaybackEvidence(
            identity_confirmed=not history,
            natural_completion_confirmed=True,
            reason=EvidenceReason.NATURAL_COMPLETION,
        ),
    )


def observe(
    snapshot: SessionSnapshot,
    observation: PlaybackObservation,
    *,
    now: float,
    policy: EvidencePolicy = _DEFAULT_POLICY,
) -> SessionSnapshot:
    """Consume ordered fresh samples; never turn cached content into observed identity.

    A provider-qualified incremental terminal can use a recent exact-content
    witness. Unknown media, ads, gaps, media replacement and stop intent invalidate
    that chain. Completion is a single historical fact; ownership can still be lost.
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
    if observation.sequence <= snapshot.last_sequence or (
        snapshot.last_monotonic is not None and observation.monotonic <= snapshot.last_monotonic
    ):
        return snapshot
    if snapshot.last_sequence >= 0 and observation.sequence > snapshot.last_sequence + 1:
        snapshot = replace(
            snapshot,
            progress_anchor=None,
            completion_anchor=None,
            has_confirmed_playback=False,
            release_boundary=None,
            release_confirmed=False,
        )
    snapshot = replace(
        snapshot, last_sequence=observation.sequence, last_monotonic=observation.monotonic
    )
    if observation.connection_reset:
        return _lost(snapshot, observation)
    if snapshot.release_boundary is not None and (
        observation.ad_active is True
        or observation.state in {PlayerState.PLAYING, PlayerState.PAUSED}
    ):
        snapshot = replace(snapshot, release_boundary=None, release_confirmed=False)
    if observation.session_active is False:
        if (
            snapshot.release_boundary is not None
            and observation.monotonic > snapshot.release_boundary
            and snapshot.completion is not None
            and not snapshot.stop_requested
            and snapshot.receipt is not None
            and snapshot.receipt.action == CommandAction.RELEASE
            and snapshot.receipt.outcome == CommandOutcome.ACCEPTED
        ):
            return replace(
                snapshot,
                revision=snapshot.revision + 1,
                latest=observation,
                state=PlayerState.ENDED,
                release_confirmed=True,
                evidence=PlaybackEvidence(
                    natural_completion_confirmed=True, reason=EvidenceReason.NATURAL_COMPLETION
                ),
            )
        if (
            snapshot.stop_requested
            and snapshot.stop_boundary is not None
            and observation.monotonic > snapshot.stop_boundary
        ):
            return replace(
                snapshot,
                revision=snapshot.revision + 1,
                latest=observation,
                state=PlayerState.STOPPED,
                progress_anchor=None,
                completion_anchor=None,
                evidence=PlaybackEvidence(
                    natural_completion_confirmed=snapshot.completion is not None,
                    reason=EvidenceReason.STOP_OBSERVED,
                ),
            )
        return _lost(snapshot, observation)
    if (
        (
            observation.application_id is not None
            and scope.application_id is not None
            and observation.application_id != scope.application_id
        )
        or (observation.session_id is not None and observation.session_id != scope.session_id)
        or (observation.content is not None and observation.content != scope.request.content)
    ):
        return _lost(snapshot, observation)
    if observation.session_active is True and observation.session_id == scope.session_id:
        # Matching control-plane status supplies ownership, not new media evidence.
        return replace(snapshot, revision=snapshot.revision + 1)
    if snapshot.completion is not None:
        # Later idle/buffering messages do not revoke or repeat a historical end.
        return replace(
            snapshot,
            revision=snapshot.revision + 1,
            latest=observation,
            state=PlayerState.ENDED,
            evidence=PlaybackEvidence(
                natural_completion_confirmed=True, reason=EvidenceReason.NATURAL_COMPLETION
            ),
        )
    history = _incremental_identity(snapshot, observation, policy)
    if observation.session_id is None or (observation.content is None and not history):
        return _unconfirmed(snapshot, observation, EvidenceReason.IDENTITY_UNKNOWN)
    if observation.ad_active is not False:
        reason = EvidenceReason.AD_ACTIVE if observation.ad_active else EvidenceReason.AD_UNKNOWN
        return _unconfirmed(snapshot, observation, reason, identity=observation.content is not None)
    if observation.provider_evidence is not None and (
        not _qualified(observation) or observation.identity_update == IdentityUpdate.UNKNOWN
    ):
        return _unconfirmed(snapshot, observation, EvidenceReason.IDENTITY_UNKNOWN)
    anchor = snapshot.completion_anchor
    same_playback = anchor is not None and anchor.playback_id == observation.playback_id
    backwards = (
        anchor is not None
        and anchor.position is not None
        and observation.position is not None
        and observation.position < anchor.position
    )
    previous_source = anchor.provider_evidence if anchor is not None else None
    current_source = observation.provider_evidence
    same_source = (previous_source is None and current_source is None) or (
        previous_source is not None
        and current_source is not None
        and previous_source.source == current_source.source
    )
    if not same_playback or not same_source or backwards:
        snapshot = replace(
            snapshot, has_confirmed_playback=False, completion_anchor=None, progress_anchor=None
        )
    if observation.state == PlayerState.IDLE:
        if snapshot.stop_requested and observation.idle_reason == IdleReason.CANCELED:
            return replace(
                snapshot,
                revision=snapshot.revision + 1,
                latest=observation,
                state=PlayerState.STOPPED,
                progress_anchor=None,
                completion_anchor=None,
                evidence=PlaybackEvidence(
                    identity_confirmed=True, reason=EvidenceReason.STOP_OBSERVED
                ),
            )
        if (
            observation.idle_reason == IdleReason.FINISHED
            and snapshot.has_confirmed_playback
            and snapshot.completion_anchor is not None
            and not snapshot.stop_requested
        ):
            return _finish(snapshot, observation, history=history)
    if history:
        # An incremental nonterminal preserves attribution briefly, but neither
        # supplies fresh identity nor moves the last exact-content anchor forward.
        return _unconfirmed(
            snapshot, observation, EvidenceReason.IDENTITY_UNKNOWN, retain_completion=True
        )
    if observation.state != PlayerState.PLAYING:
        retain = observation.state in {PlayerState.PAUSED, PlayerState.BUFFERING}
        if retain:
            snapshot = replace(snapshot, completion_anchor=observation)
        return _unconfirmed(
            snapshot,
            observation,
            EvidenceReason.NOT_PLAYING,
            identity=True,
            retain_completion=retain,
        )
    if observation.position is None:
        return _unconfirmed(snapshot, observation, EvidenceReason.POSITION_UNKNOWN, identity=True)
    progress = snapshot.progress_anchor
    confirmed = (
        progress is not None
        and progress.position is not None
        and progress.playback_id == observation.playback_id
        and now - progress.monotonic <= policy.max_age_seconds
        and observation.monotonic - progress.monotonic >= policy.min_progress_interval_seconds
        and observation.position > progress.position
    )
    if (
        progress is None
        or progress.position is None
        or progress.playback_id != observation.playback_id
        or confirmed
        or observation.position < progress.position
        or observation.monotonic - progress.monotonic >= policy.max_age_seconds
    ):
        progress = observation
    return replace(
        snapshot,
        revision=snapshot.revision + 1,
        latest=observation,
        progress_anchor=progress,
        completion_anchor=observation,
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
    retain_completion: bool = False,
) -> SessionSnapshot:
    return replace(
        snapshot,
        revision=snapshot.revision + 1,
        latest=observation,
        state=observation.state if identity else PlayerState.UNKNOWN,
        progress_anchor=None,
        completion_anchor=snapshot.completion_anchor if retain_completion else None,
        has_confirmed_playback=snapshot.has_confirmed_playback if retain_completion else False,
        evidence=PlaybackEvidence(identity_confirmed=identity, reason=reason),
    )
