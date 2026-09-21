"""Opt-in receiver-managed successors for the foreground playback owner."""

from dataclasses import replace
from uuid import uuid4

from .application import PlaybackResult
from .domain.native_queue import (
    NativeQueueAction,
    NativeQueueBackend,
    NativeQueueReceipt,
    NativeQueueRequest,
)
from .domain.queue import (
    ExecutionState,
    NativeReservation,
    NativeReservationState,
    QueueIntent,
    QueueMode,
    QueueSnapshot,
)
from .domain.queue_policy import QueueAuthority, decide_queue
from .domain.values import (
    CommandAction,
    PlaybackObservation,
    PlaybackRequest,
    PlayerState,
    SessionSnapshot,
    Support,
)
from .handoff import HandoffDiagnostic, HandoffDisposition, HandoffReason, HandoffStage
from .native_observation import (
    NativePlaybackTracker,
    NativeSuccessorAuthorization,
    NativeSuccessorTracker,
)
from .native_queue_execution import (
    NativeDispatch,
    NativeExecutionRequest,
    NativeOutcomePersistenceError,
    NativeQueueExecutor,
)
from .playback_task import PlaybackTask
from .runner import Cancellation, RunnerCommand, TaskView

_SENT = {ExecutionState.DISPATCHED, ExecutionState.ACKNOWLEDGED, ExecutionState.UNCERTAIN}


class NativePlaybackTask(PlaybackTask):
    """Reuse initial launch and guarded controls; successors never issue START.

    All methods run on the existing owner thread. A reopened owner only observes
    previously authorized work and retains a durable hold against further staging.
    """

    def _recover_queue(self, queue: QueueSnapshot) -> None:
        if queue.mode != QueueMode.NATIVE:
            raise ValueError("Native playback requires an explicitly native queue")
        self._recovering = bool(queue.attempts)
        self._observed_generation: str | None = None
        self._current_tracker: NativePlaybackTracker | None = None
        self._successor_tracker: NativeSuccessorTracker | None = None
        self._tracked_reservation: str | None = None
        self._pending_stop: tuple[RunnerCommand, str, float] | None = None
        self._native_executor = NativeQueueExecutor(self.queue_store, self.clock)
        self.queue_store.recover(self.queue_id, expected_revision=queue.revision)

    def _outstanding(self, queue: QueueSnapshot) -> NativeReservation | None:
        return next(
            (r for r in queue.reservations if r.state == NativeReservationState.RESERVED), None
        )

    def _sent(self, queue: QueueSnapshot, reservation: NativeReservation) -> bool:
        return any(
            operation.reservation_id == reservation.reservation_id
            and operation.action == NativeQueueAction.PLAY_NEXT
            and operation.state in _SENT
            for operation in queue.native_operations
        )

    def _request(self, queue: QueueSnapshot, attempt_id: str, item_id: str) -> PlaybackRequest:
        item = next(item for item in queue.items if item.item_id == item_id)
        return PlaybackRequest(
            "observe-" + attempt_id, attempt_id, item_id, item.content, self.target
        )

    def _hold(self) -> None:
        queue = self._load()
        if not queue.reconciliation_required:
            self.queue_store.hold_for_reconciliation(
                self.queue_id, expected_revision=queue.revision
            )

    def _hold_after_failure(self) -> None:
        self._hold()

    def _trackers(self, events: tuple[PlaybackObservation, ...]) -> None:
        fresh = [
            event
            for event in events
            if event.target == self.target
            and event.monotonic > self._boundary
            and 0 <= self.clock.monotonic() - event.monotonic <= 5
        ]
        if not fresh:
            return
        generation = fresh[-1].connection_generation
        if self._observed_generation is not None and (
            generation != self._observed_generation or any(e.connection_reset for e in fresh)
        ):
            self._hold()
            self._recovering = True
            self._boundary = self.clock.monotonic()
            self._current_tracker = self._successor_tracker = None
            self._tracked_reservation = None
            self._observed_generation = None
            self._playback = None
            self._pending_stop = None
            return
        if any(event.connection_reset for event in fresh):
            # A startup reset is not a receiver identity witness. The next
            # observation window must establish the new connection explicitly.
            self._boundary = self.clock.monotonic()
            return
        self._observed_generation = generation
        queue = self._load()
        if self._authority is None or self._authority.connection_generation != generation:
            self._authority = QueueAuthority(
                self.queue_id, queue.generation, generation, self._boundary
            )
        if (
            self._recovering
            and self._playback is None
            and self._current_tracker is None
            and queue.attempts
        ):
            attempt = queue.attempts[-1]
            saved = self._app.store.load(attempt.attempt_id)
            reservation = next(
                (r for r in queue.reservations if r.successor_attempt_id == attempt.attempt_id),
                None,
            )
            session = (
                reservation.session_id
                if reservation
                else (saved.scope.session_id if saved else None)
            )
            application = (
                reservation.application_id
                if reservation
                else (saved.scope.application_id if saved else None)
            )
            if session and application:
                self._current_tracker = NativePlaybackTracker(
                    self._request(queue, attempt.attempt_id, attempt.item_id),
                    application_id=application,
                    session_id=session,
                    connection_generation=generation,
                    started_monotonic=self._boundary,
                )
        reservation = self._outstanding(queue)
        if (
            reservation is not None
            and self._sent(queue, reservation)
            and self._tracked_reservation != reservation.reservation_id
        ):
            predecessor = next(
                a for a in queue.attempts if a.attempt_id == reservation.predecessor_attempt_id
            )
            owned = self._playback
            request = (
                owned.scope.request
                if owned is not None and owned.scope.request.attempt_id == predecessor.attempt_id
                else self._request(queue, predecessor.attempt_id, predecessor.item_id)
            )
            authorization = NativeSuccessorAuthorization(
                reservation.reservation_id,
                request,
                self._request(
                    queue, reservation.successor_attempt_id, reservation.successor_item_id
                ),
                reservation.application_id,
                reservation.session_id,
            )
            # Live staging constructs its tracker before the next observation.
            # A tracker created here is necessarily a fresh reconciliation view.
            self._successor_tracker = NativeSuccessorTracker.reconnect(
                authorization,
                connection_generation=generation,
                started_monotonic=self._boundary,
            )
            self._tracked_reservation = reservation.reservation_id

    def _accept(self, result: PlaybackResult) -> None:
        # Reduce the complete command baseline/result before any later queue
        # changes. Persistence/adoption waits until the command journal commits.
        if self._successor_tracker is not None:
            self._successor_tracker.observe(result.observations, now=self.clock.monotonic())
        if self._current_tracker is not None:
            self._current_tracker.observe(result.observations, now=self.clock.monotonic())
        super()._accept(result)

    def _save_passive(self, snapshot: SessionSnapshot) -> None:
        saved = self._app.store.load(snapshot.scope.request.attempt_id)
        snapshot = (
            replace(snapshot, revision=max(snapshot.revision, saved.revision + 1))
            if saved
            else snapshot
        )
        self._app.store.save(snapshot, expected_revision=saved.revision if saved else None)
        self._playback = snapshot
        self._release = None
        self._release_refusal = None

    def _settle(self) -> None:
        if self._playback is None:
            return
        queue = self._load()
        decision = decide_queue(
            queue, self._playback, now=self.clock.monotonic(), authority=self._authority
        )
        if decision.settlement is not None:
            change = decision.settlement
            self.queue_store.settle_attempt(
                self.queue_id,
                change.attempt_id,
                change.outcome,
                decision_id=change.decision_id,
                expected_revision=decision.expected_revision,
            )

    def _reconcile_native(self) -> None:
        current = self._current_tracker
        transition = self._successor_tracker
        if current is not None and current.ready and self._playback is None:
            assert current.snapshot is not None
            self._save_passive(current.snapshot)
            self._current_tracker = None
        self._settle()
        queue = self._load()
        reservation = self._outstanding(queue)
        if transition is not None and transition.ready and reservation is not None:
            successor = transition.successor
            assert successor is not None
            self.queue_store.adopt_native_successor(
                self.queue_id,
                reservation.reservation_id,
                observed=successor,
                now=self.clock.monotonic(),
                at=self.clock.utcnow(),
                decision_id="adopt-" + reservation.reservation_id,
                expected_revision=queue.revision,
                expected_generation=queue.generation,
            )
            self._save_passive(successor)
            self._successor_tracker = self._current_tracker = None
            self._tracked_reservation = None
        elif (
            transition is not None
            and transition.refusal is not None
            and not queue.cancellation_requested
        ):
            self._hold()

    def _observe_native(self) -> None:
        events = self._backend.observe(self.target)
        self._trackers(events)
        self._accept(PlaybackResult(events))
        self._reconcile_native()

    def _native_allowed(self, dispatch: NativeDispatch, cancellation: Cancellation) -> bool:
        queue = self._load()
        owned = self._playback
        return (
            queue.revision == dispatch.snapshot.revision
            and queue.generation == dispatch.snapshot.generation
            and owned is not None
            and not owned.ownership_lost
            and dispatch.request.scope == owned.scope
            and (
                dispatch.request.action == NativeQueueAction.CLEAR
                or not (
                    cancellation.requested
                    or queue.cancellation_requested
                    or queue.reconciliation_required
                    or self._recovering
                )
            )
        )

    def _stage(self, cancellation: Cancellation) -> None:
        queue, owned = self._load(), self._playback
        if (
            self._recovering
            or queue.reconciliation_required
            or self._cancel(cancellation)
            or owned is None
            or owned.ownership_lost
            or owned.stop_requested
            or not owned.evidence.receiver_playback_confirmed
            or owned.latest is None
            or owned.latest.playback_id is None
            or self._outstanding(queue) is not None
            or not queue.attempts
            or queue.attempts[-1].intent != QueueIntent.CURRENT
        ):
            return
        item = next((item for item in queue.items if item.intent == QueueIntent.PENDING), None)
        if item is None:
            return
        backend = self._backend.backend
        if not isinstance(backend, NativeQueueBackend) or backend.native_queue_capabilities(
            self.target
        ).play_next.support not in {Support.ADVERTISED, Support.VERIFIED}:
            self._hold()
            return
        reservation_id, attempt_id = str(uuid4()), str(uuid4())
        request = NativeQueueRequest(
            "enqueue-" + reservation_id,
            NativeQueueAction.PLAY_NEXT,
            owned.scope,
            owned.latest.playback_id,
            item.content,
        )
        # Include callbacks queued before or during the blocking library call.
        # Moving this boundary past dispatch could erase an intervening takeover.
        observation_boundary = owned.last_monotonic or owned.scope.started_monotonic

        def invoke(dispatch: NativeDispatch) -> NativeQueueReceipt:
            # Final guard follows durable dispatch; the adapter checks fresh
            # receiver scope once more immediately before the library operation.
            if not self._native_allowed(dispatch, cancellation):
                raise ValueError("Native dispatch authority changed")
            return backend.native_queue(dispatch.request)

        result = self._native_executor.execute(
            NativeExecutionRequest(
                self.queue_id,
                reservation_id,
                request,
                queue.revision,
                queue.generation,
                successor_item_id=item.item_id,
                successor_attempt_id=attempt_id,
            ),
            invoke,
            may_effect=lambda: not cancellation.requested,
        )
        if result.operation.state not in _SENT:
            self._hold()
            return
        authorization = NativeSuccessorAuthorization(
            reservation_id,
            owned.scope.request,
            self._request(result.snapshot, attempt_id, item.item_id),
            result.reservation.application_id,
            result.reservation.session_id,
        )
        self._successor_tracker = NativeSuccessorTracker(
            authorization,
            connection_generation=owned.scope.connection_generation,
            started_monotonic=observation_boundary,
            predecessor=owned,
        )
        self._tracked_reservation = reservation_id

    def _clear(self, cancellation: Cancellation) -> None:
        queue, owned = self._load(), self._playback
        reservation = self._outstanding(queue)
        backend = self._backend.backend
        if (
            reservation is None
            or owned is None
            or owned.ownership_lost
            or owned.latest is None
            or owned.latest.playback_id is None
            or not isinstance(backend, NativeQueueBackend)
        ):
            return
        request = NativeQueueRequest(
            "clear-" + reservation.reservation_id,
            NativeQueueAction.CLEAR,
            owned.scope,
            owned.latest.playback_id,
        )

        def invoke(dispatch: NativeDispatch) -> NativeQueueReceipt:
            if not self._native_allowed(dispatch, cancellation):
                raise ValueError("Native clear authority changed")
            return backend.native_queue(dispatch.request)

        try:
            self._native_executor.execute(
                NativeExecutionRequest(
                    self.queue_id,
                    reservation.reservation_id,
                    request,
                    queue.revision,
                    queue.generation,
                ),
                invoke,
            )
        except NativeOutcomePersistenceError:
            # Cancellation was committed first. Unknown clearing cannot prevent
            # the separately journaled, freshly scoped operator STOP below.
            self._hold()

    def _record_exit(self) -> None:
        owned = self._playback
        if (
            owned is None
            or owned.ownership_lost
            or not (owned.state == PlayerState.STOPPED or owned.release_confirmed)
        ):
            return
        latest = owned.latest
        boundary = owned.stop_boundary if owned.stop_requested else owned.release_boundary
        if (
            latest is None
            or latest.session_active is not False
            or latest.target != self.target
            or latest.connection_generation != owned.scope.connection_generation
            or boundary is None
            or latest.monotonic <= boundary
            or not 0 <= self.clock.monotonic() - latest.monotonic <= 5
        ):
            # A canceled media item can be STOPPED while YouTube remains active.
            # Only a correlated receiver exit supports this stronger journal fact.
            return
        queue = self._load()
        for reservation in queue.reservations:
            if (
                reservation.state != NativeReservationState.SESSION_EXIT_OBSERVED
                and reservation.application_id == owned.scope.application_id
                and reservation.session_id == owned.scope.session_id
            ):
                queue = self.queue_store.record_native_session_exit(
                    self.queue_id,
                    reservation.reservation_id,
                    at=self.clock.utcnow(),
                    decision_id="exit-" + reservation.reservation_id,
                    expected_revision=queue.revision,
                    expected_generation=queue.generation,
                )

    def handle(self, command: RunnerCommand, cancellation: Cancellation) -> TaskView:
        self._check_thread()
        self._cancellation = cancellation
        if command.action == CommandAction.STOP:
            queue = self._load()
            if not queue.cancellation_requested:
                self.queue_store.cancel_queue(self.queue_id, expected_revision=queue.revision)
            self._observe_native()
            self._clear(cancellation)
            queue = self._load()
            reservation = self._outstanding(queue)
            transition = self._successor_tracker
            if (
                reservation is not None
                and transition is not None
                and transition.refusal is None
                and (self._playback is None or self._playback.ownership_lost)
                and not any(c.command_id == command.command_id for c in queue.commands)
            ):
                # The explicit STOP has not been dispatched. Keep it only for
                # this authorized handoff and this owner, for at most ten seconds.
                # A crash/reconnect never restores this process-local intent.
                if self._pending_stop is None:
                    self._pending_stop = (
                        command,
                        reservation.reservation_id,
                        self.clock.monotonic() + 10,
                    )
                return self._view()
            self._pending_stop = None
        super().handle(command, cancellation)
        self._reconcile_native()
        self._record_exit()
        return self._view()

    def step(self, cancellation: Cancellation) -> TaskView:
        self._check_thread()
        self._cancel(cancellation)
        if not self._started and not self._recovering:
            return self._view()
        self._observe_native()
        self._finish_pending_stop(cancellation)
        self._record_exit()
        self._stage(cancellation)
        return self._view()

    def _finish_pending_stop(self, cancellation: Cancellation) -> None:
        pending = self._pending_stop
        if pending is None:
            return
        command, reservation_id, deadline = pending
        queue = self._load()
        reservation = next(r for r in queue.reservations if r.reservation_id == reservation_id)
        if (
            self.clock.monotonic() > deadline
            or any(c.command_id == command.command_id for c in queue.commands)
            or (self._successor_tracker is not None and self._successor_tracker.refusal is not None)
        ):
            self._pending_stop = None
            return
        owned = self._playback
        if (
            owned is not None
            and not owned.ownership_lost
            and (
                (
                    reservation.state == NativeReservationState.ADOPTED
                    and owned.scope.request.attempt_id == reservation.successor_attempt_id
                )
                or (
                    reservation.state == NativeReservationState.RESERVED
                    and owned.scope.request.attempt_id == reservation.predecessor_attempt_id
                )
            )
            and owned.evidence.receiver_playback_confirmed
        ):
            self._pending_stop = None
            self._clear(cancellation)
            super().handle(command, cancellation)
            self._reconcile_native()

    def _handoff(self, queue: QueueSnapshot) -> HandoffDiagnostic:
        if self._pending_stop is not None:
            reason = HandoffReason.STOP_AWAITING_NATIVE_OBSERVATION
        elif queue.cancellation_requested:
            reason = HandoffReason.CANCELED
        elif queue.reconciliation_required:
            reason = HandoffReason.NATIVE_RECONCILIATION_REQUIRED
        elif self._successor_tracker is not None and self._successor_tracker.refusal is None:
            reason = HandoffReason.NATIVE_SUCCESSOR_PENDING
        elif not self._started and not self._recovering:
            reason = HandoffReason.AWAITING_START
        elif all(item.intent == QueueIntent.FINISHED for item in queue.items):
            return HandoffDiagnostic(
                HandoffStage.SUCCESSOR, HandoffDisposition.COMPLETE, HandoffReason.EXHAUSTED
            )
        elif self._playback is not None and self._playback.ownership_lost:
            reason = HandoffReason.OWNERSHIP_LOST
        else:
            reason = HandoffReason.AWAITING_COMPLETION
        return HandoffDiagnostic(HandoffStage.SUCCESSOR, HandoffDisposition.HOLD, reason)
