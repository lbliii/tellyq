"""One-program use cases; transport, persistence and time arrive through ports."""

from collections.abc import Callable
from dataclasses import dataclass, replace

from .domain import policy
from .domain.ports import Clock, PlaybackBackend, PlaybackControls, RevisionConflict, SessionStore
from .domain.values import (
    CommandAction,
    CommandOutcome,
    CommandReceipt,
    ControlDiagnostic,
    ControlReason,
    ControlStage,
    ErrorCode,
    EvidenceReason,
    PlaybackError,
    PlaybackEvidence,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlayerState,
    SessionSnapshot,
    Support,
)


class ControlRefused(ValueError):
    """A safe application guard failure before command intent or dispatch."""

    def __init__(self, reason: ControlReason) -> None:
        self.diagnostic = ControlDiagnostic(ControlStage.APPLICATION, reason)
        super().__init__(f"Media control refused by application: {reason.value}.")


@dataclass(frozen=True, slots=True)
class PlaybackResult:
    observations: tuple[PlaybackObservation, ...]
    snapshot: SessionSnapshot | None = None
    receipt: CommandReceipt | None = None
    reason: EvidenceReason = EvidenceReason.AWAITING_OBSERVATION
    failure: PlaybackError | None = None
    control_observed: bool = False


class PlaybackApplication:
    def __init__(
        self,
        backend: PlaybackBackend,
        store: SessionStore,
        clock: Clock,
        on_receipt: Callable[[CommandReceipt], None] | None = None,
    ) -> None:
        self.backend = backend
        self.store = store
        self.clock = clock
        self.on_receipt = on_receipt
        self.last_result: PlaybackResult | None = None

    def _fresh(self, event: PlaybackObservation, request: PlaybackRequest, boundary: float) -> bool:
        return (
            event.target == request.target
            and event.monotonic > boundary
            and 0 <= self.clock.monotonic() - event.monotonic <= 5
        )

    def _apply(
        self, snapshot: SessionSnapshot, events: tuple[PlaybackObservation, ...]
    ) -> SessionSnapshot:
        for event in events:
            snapshot = policy.observe(snapshot, event, now=self.clock.monotonic())
        return policy.refresh(snapshot, now=self.clock.monotonic())

    def start(self, request: PlaybackRequest) -> PlaybackResult:
        self.last_result = None
        try:
            return self._start(request)
        except Exception, KeyboardInterrupt:
            return self._failed(request)

    def _failed(self, request: PlaybackRequest) -> PlaybackResult:
        if self.last_result is None or self.last_result.receipt is None:
            raise
        error = PlaybackError(
            ErrorCode.COMMAND_OUTCOME_UNKNOWN,
            "Playback was attempted, but observation or persistence failed.",
            request.request_id,
            True,
            "Inspect fresh status before retrying.",
        )
        return replace(self.last_result, failure=error)

    def _dispatch(
        self,
        request: PlaybackRequest,
        action: CommandAction,
        events: tuple[PlaybackObservation, ...],
        effect: Callable[[], CommandReceipt],
    ) -> CommandReceipt:
        requested_at = self.clock.utcnow()
        intent = CommandReceipt(
            request.request_id,
            request.attempt_id,
            action,
            CommandOutcome.UNKNOWN,
            requested_at,
            requested_at=requested_at,
        )
        self.last_result = PlaybackResult(events, receipt=intent)
        receipt = replace(effect(), requested_at=requested_at)
        self.last_result = replace(self.last_result, receipt=receipt)
        if self.on_receipt is not None:
            self.on_receipt(receipt)
        return receipt

    def _start(self, request: PlaybackRequest) -> PlaybackResult:
        if self.store.load(request.attempt_id) is not None:
            raise RevisionConflict("This attempt already exists; inspect status before restarting.")
        baseline = self.backend.observe(request.target)
        boundary = self.clock.monotonic()
        receipt = self._dispatch(
            request, CommandAction.START, baseline, lambda: self.backend.start(request)
        )
        events = self.backend.observe(request.target)
        self.last_result = PlaybackResult(baseline + events, receipt=receipt)
        previous = {event.session_id for event in baseline if event.session_id is not None}
        candidates = [
            event
            for event in events
            if self._fresh(event, request, boundary)
            and event.session_id is not None
            and event.content == request.content
            and (receipt.outcome == CommandOutcome.ACCEPTED or event.session_id not in previous)
        ]
        if receipt.outcome == CommandOutcome.REJECTED or not candidates:
            return PlaybackResult(baseline + events, receipt=receipt)
        candidate = candidates[0]
        assert candidate.session_id is not None
        scope = PlaybackScope(
            request,
            candidate.session_id,
            candidate.connection_generation,
            boundary,
            candidate.application_id,
        )
        initial = SessionSnapshot(scope)
        self.store.save(initial, expected_revision=None)
        snapshot = self._apply(
            policy.record_receipt(initial, receipt),
            tuple(event for event in events if event.sequence >= candidate.sequence),
        )
        self.last_result = PlaybackResult(
            baseline + events, snapshot, receipt, snapshot.evidence.reason
        )
        self.store.save(snapshot, expected_revision=initial.revision)
        return self.last_result

    def _reconcile(
        self,
        request: PlaybackRequest,
        owned: SessionSnapshot,
        boundary: float,
        events: tuple[PlaybackObservation, ...],
    ) -> SessionSnapshot:
        receivers = [
            event
            for event in events
            if self._fresh(event, request, boundary) and event.session_active is not None
        ]
        if not receivers:
            raise ValueError(
                "No fresh receiver ownership evidence; inspect status before retrying."
            )
        latest = receivers[-1]
        if (
            latest.session_active is not True
            or latest.session_id != owned.scope.session_id
            or latest.application_id != owned.scope.application_id
            or owned.scope.request.target != request.target
            or owned.scope.request.content != request.content
        ):
            raise ValueError("The saved session was replaced; refusing to control another session.")
        scope = PlaybackScope(
            request,
            owned.scope.session_id,
            latest.connection_generation,
            boundary,
            owned.scope.application_id,
        )
        initial = (
            replace(owned, scope=replace(scope, started_monotonic=owned.scope.started_monotonic))
            if owned.scope.connection_generation == latest.connection_generation
            else SessionSnapshot(
                scope,
                stop_requested=owned.stop_requested,
                completion=owned.completion,
                evidence=PlaybackEvidence(
                    natural_completion_confirmed=owned.completion is not None
                ),
            )
        )
        snapshot = self._apply(initial, events)
        if snapshot.ownership_lost:
            raise ValueError("Different content or session is active; refusing to control it.")
        return snapshot

    def status(self, request: PlaybackRequest, owned: SessionSnapshot | None) -> PlaybackResult:
        revision = self._revision(owned)
        boundary = self.clock.monotonic()
        events = self.backend.observe(request.target)
        if owned is None:
            return PlaybackResult(events, reason=EvidenceReason.IDENTITY_UNKNOWN)
        try:
            snapshot = self._reconcile(request, owned, boundary, events)
        except ValueError:
            evaluated = self._apply(owned, events)
            if evaluated.completion is None:
                return PlaybackResult(events, reason=EvidenceReason.SESSION_REPLACED)
            # Historical completion survives reconnect/replacement without reviving
            # ownership or current playback evidence. Controls still reconcile strictly.
            snapshot = replace(
                evaluated,
                ownership_lost=True,
                progress_anchor=None,
                completion_anchor=None,
                has_confirmed_playback=False,
                evidence=PlaybackEvidence(
                    natural_completion_confirmed=True, reason=EvidenceReason.SESSION_REPLACED
                ),
            )
            snapshot = self._persist(snapshot, revision)
            return PlaybackResult(events, snapshot, reason=EvidenceReason.SESSION_REPLACED)
        snapshot = self._persist(snapshot, revision)
        return PlaybackResult(events, snapshot, reason=snapshot.evidence.reason)

    def stop(self, request: PlaybackRequest, owned: SessionSnapshot | None) -> PlaybackResult:
        self.last_result = None
        try:
            return self._stop(request, owned)
        except Exception, KeyboardInterrupt:
            return self._failed(request)

    def _stop(self, request: PlaybackRequest, owned: SessionSnapshot | None) -> PlaybackResult:
        if owned is None:
            raise ValueError("No saved playback ownership; refusing to stop another session.")
        revision = self._revision(owned)
        boundary = self.clock.monotonic()
        baseline = self.backend.observe(request.target)
        initial = self._reconcile(request, owned, boundary, baseline)
        snapshot = policy.request_stop(initial, boundary=self.clock.monotonic())
        snapshot = self._persist(snapshot, revision)
        revision = snapshot.revision
        receipt = self._dispatch(
            request, CommandAction.STOP, baseline, lambda: self.backend.stop(snapshot.scope)
        )
        events = self.backend.observe(request.target)
        self.last_result = PlaybackResult(baseline + events, receipt=receipt)
        snapshot = self._apply(policy.record_receipt(snapshot, receipt), events)
        self.last_result = PlaybackResult(
            baseline + events, snapshot, receipt, snapshot.evidence.reason
        )
        self.store.save(snapshot, expected_revision=revision)
        return self.last_result

    def pause(self, request: PlaybackRequest, owned: SessionSnapshot | None) -> PlaybackResult:
        return self._control(request, owned, CommandAction.PAUSE)

    def resume(self, request: PlaybackRequest, owned: SessionSnapshot | None) -> PlaybackResult:
        return self._control(request, owned, CommandAction.RESUME)

    def _control(
        self, request: PlaybackRequest, owned: SessionSnapshot | None, action: CommandAction
    ) -> PlaybackResult:
        self.last_result = None
        try:
            return self._run_control(request, owned, action)
        except Exception, KeyboardInterrupt:
            return self._failed(request)

    def _run_control(
        self, request: PlaybackRequest, owned: SessionSnapshot | None, action: CommandAction
    ) -> PlaybackResult:
        if owned is None or owned.stop_requested or owned.ownership_lost:
            raise ControlRefused(ControlReason.OWNERSHIP_UNVERIFIED)
        revision = self._revision(owned)
        boundary = self.clock.monotonic()
        baseline = self.backend.observe(request.target)
        # A newly opened client may still have a startup reset queued before this
        # command. Fresh ownership must be re-established below; only resets
        # strictly before the boundary may be reconciled this way. Do not use the
        # five-second freshness limit here: any reset during this window blocks.
        if any(
            event.connection_reset
            and event.target == request.target
            and event.monotonic >= boundary
            for event in baseline
        ):
            raise ControlRefused(ControlReason.CONNECTION_CHANGED)
        try:
            snapshot = self._reconcile(request, owned, boundary, baseline)
        except ValueError as exc:
            raise ControlRefused(ControlReason.OWNERSHIP_UNVERIFIED) from exc
        media = snapshot.latest
        expected = PlayerState.PLAYING if action == CommandAction.PAUSE else PlayerState.PAUSED
        if media is None or media.playback_id is None:
            raise ControlRefused(ControlReason.MEDIA_UNAVAILABLE)
        if not self._fresh(media, request, boundary):
            raise ControlRefused(ControlReason.STALE_OBSERVATION)
        if (
            media.content != request.content
            or media.connection_generation != snapshot.scope.connection_generation
            or media.session_id != snapshot.scope.session_id
            or media.application_id != snapshot.scope.application_id
        ):
            raise ControlRefused(ControlReason.IDENTITY_CHANGED)
        if media.state != expected:
            raise ControlRefused(ControlReason.STATE_MISMATCH)
        capabilities = self.backend.capabilities(request.target)
        capability = capabilities.pause if action == CommandAction.PAUSE else capabilities.resume
        if not isinstance(self.backend, PlaybackControls) or capability.support not in {
            Support.ADVERTISED,
            Support.VERIFIED,
        }:
            error = PlaybackError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "This media control is unsupported or its capability is unknown.",
                request.request_id,
                False,
                "Inspect fresh status and receiver support.",
            )
            receipt = CommandReceipt(
                request.request_id,
                request.attempt_id,
                action,
                CommandOutcome.REJECTED,
                self.clock.utcnow(),
                error,
                diagnostic=ControlDiagnostic(
                    ControlStage.APPLICATION, ControlReason.CAPABILITY_UNAVAILABLE
                ),
            )
            return PlaybackResult(baseline, snapshot, receipt, snapshot.evidence.reason)
        backend = self.backend
        playback_id = media.playback_id
        effect = backend.pause if action == CommandAction.PAUSE else backend.resume
        requested_at = self.clock.utcnow()
        intent = CommandReceipt(
            request.request_id,
            request.attempt_id,
            action,
            CommandOutcome.UNKNOWN,
            requested_at,
            requested_at=requested_at,
        )
        # Durable run reports retain intent before any remote effect. The JSON
        # snapshot store deliberately does not resurrect receipts on restart.
        if self.on_receipt is not None:
            self.on_receipt(intent)
        snapshot = self._persist(policy.record_receipt(snapshot, intent), revision)
        revision = snapshot.revision
        command_boundary = self.clock.monotonic()
        receipt = self._dispatch(
            request, action, baseline, lambda: effect(snapshot.scope, playback_id)
        )
        events = self.backend.observe(request.target)
        self.last_result = PlaybackResult(baseline + events, receipt=receipt)
        snapshot = self._apply(policy.record_receipt(snapshot, receipt), events)
        observed = snapshot.latest
        wanted = PlayerState.PAUSED if action == CommandAction.PAUSE else PlayerState.PLAYING
        control_observed = bool(
            receipt.outcome == CommandOutcome.ACCEPTED
            and not snapshot.ownership_lost
            and observed is not None
            and self._fresh(observed, request, command_boundary)
            and observed.content == request.content
            and observed.connection_generation == snapshot.scope.connection_generation
            and observed.session_id == snapshot.scope.session_id
            and observed.application_id == snapshot.scope.application_id
            and snapshot.state == wanted
            and observed.playback_id == playback_id
            and observed.state == wanted
        )
        self.last_result = PlaybackResult(
            baseline + events,
            snapshot,
            receipt,
            snapshot.evidence.reason,
            control_observed=control_observed,
        )
        self.store.save(snapshot, expected_revision=revision)
        return self.last_result

    def _revision(self, owned: SessionSnapshot | None) -> int | None:
        if owned is None:
            return None
        previous = self.store.load(owned.scope.request.attempt_id)
        if previous is not None and previous.revision != owned.revision:
            raise RevisionConflict("Playback changed; reload ownership before observing.")
        return previous.revision if previous is not None else None

    def _persist(self, snapshot: SessionSnapshot, revision: int | None) -> SessionSnapshot:
        if revision is not None:
            snapshot = replace(snapshot, revision=max(snapshot.revision, revision + 1))
        self.store.save(snapshot, expected_revision=revision)
        return snapshot
