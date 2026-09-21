"""One owner-thread composition of playback, durable commands and queue policy."""

from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import replace
from threading import get_ident
from uuid import uuid4

from .application import ControlRefused, PlaybackApplication, PlaybackResult
from .domain import policy
from .domain.ports import Clock, PlaybackBackend, PlaybackControls, SessionStore
from .domain.queue import ExecutionState, QueueIntent, QueueSnapshot, QueueStore
from .domain.queue_policy import QueueAuthority, QueueDisposition, decide_queue
from .domain.values import (
    CommandAction,
    CommandOutcome,
    CommandReceipt,
    PlaybackCapabilities,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    SessionSnapshot,
)
from .handoff import (
    HandoffDiagnostic,
    HandoffDisposition,
    HandoffReason,
    HandoffStage,
    ReleaseAuthority,
    handoff_evidence,
    may_release,
    release_decision,
    track_release,
)
from .queue_execution import QueueCommandRequest, QueueDispatch, QueueExecutor
from .runner import Cancellation, RunnerCommand, TaskView


class _GuardedBackend:
    """The final check runs after application baselines and immediately before effects."""

    def __init__(self, task: PlaybackTask, backend: PlaybackBackend) -> None:
        self.task, self.backend = task, backend

    def capabilities(self, target: PlaybackTarget) -> PlaybackCapabilities:
        return self.backend.capabilities(target)

    def observe(self, target: PlaybackTarget) -> tuple[PlaybackObservation, ...]:
        events = self.backend.observe(target)
        self.task._events = events
        if self.task._playback is not None and self.task._playback.completion is not None:
            self.task._release = track_release(self.task._playback, events, self.task._release)
        return events

    def _refused(self, request: PlaybackRequest, action: CommandAction) -> CommandReceipt:
        return CommandReceipt(
            request.request_id,
            request.attempt_id,
            action,
            CommandOutcome.REJECTED,
            self.task.clock.utcnow(),
        )

    def start(self, request: PlaybackRequest) -> CommandReceipt:
        if (
            not self.task._may_effect(CommandAction.START)
            or not self.task._idle()
            or (
                self.task._handoff_start
                and (
                    self.task._release is None
                    or self.task._release.blocked
                    or self.task._playback is None
                    or self.task._playback.ownership_lost
                    or not self.task._playback.release_confirmed
                )
            )
        ):
            return self._refused(request, CommandAction.START)
        return self.backend.start(request)

    def stop(self, scope: PlaybackScope) -> CommandReceipt:
        dispatch = self.task._dispatch
        action = dispatch.command.action if dispatch is not None else CommandAction.STOP
        allowed = self.task._may_effect(action)
        if action == CommandAction.RELEASE:
            snapshot = self.task._playback
            allowed = (
                allowed
                and snapshot is not None
                and may_release(
                    snapshot, self.task._release, self.task._events, now=self.task.clock.monotonic()
                )
            )
        if not allowed:
            return self._refused(scope.request, CommandAction.STOP)
        return self.backend.stop(scope)

    def pause(self, scope: PlaybackScope, playback_id: str) -> CommandReceipt:
        if not self.task._may_effect(CommandAction.PAUSE) or not isinstance(
            self.backend, PlaybackControls
        ):
            return self._refused(scope.request, CommandAction.PAUSE)
        return self.backend.pause(scope, playback_id)

    def resume(self, scope: PlaybackScope, playback_id: str) -> CommandReceipt:
        if not self.task._may_effect(CommandAction.RESUME) or not isinstance(
            self.backend, PlaybackControls
        ):
            return self._refused(scope.request, CommandAction.RESUME)
        return self.backend.resume(scope, playback_id)


class PlaybackTask:
    """Construct inside SessionRunner's factory; every method stays on that thread.

    The injected backend context owns bounded I/O and resource cleanup. No start
    occurs until an explicit START command. Reopened history is held for recovery.
    """

    def __init__(
        self,
        backend_factory: Callable[[], AbstractContextManager[PlaybackBackend]],
        queue_store: QueueStore,
        session_store: SessionStore,
        clock: Clock,
        *,
        queue_id: str,
        target: PlaybackTarget,
    ) -> None:
        self.clock, self.queue_store = clock, queue_store
        self.queue_id, self.target = queue_id, target
        self._thread = get_ident()
        self._closed = False
        self._boundary = clock.monotonic()
        self._authority: QueueAuthority | None = None
        self._playback: SessionSnapshot | None = None
        self._release: ReleaseAuthority | None = None
        self._release_refusal: HandoffDiagnostic | None = None
        self._events: tuple[PlaybackObservation, ...] = ()
        self._dispatch: QueueDispatch | None = None
        self._cancellation: Cancellation | None = None
        self._started = False
        self._handoff_start = False
        self._executor = QueueExecutor(queue_store, clock)
        queue = self._load()
        if queue.target != target:
            raise ValueError("Queue belongs to another target")
        queue = self._executor.recover(
            queue_id, expected_revision=queue.revision, expected_generation=queue.generation
        )
        # Even an acknowledged release/finished row is history, not resumed authority.
        if queue.attempts and not queue.cancellation_requested:
            queue_store.cancel_queue(queue_id, expected_revision=queue.revision)
        self._resources = ExitStack()
        try:
            backend = self._resources.enter_context(backend_factory())
            self._backend = _GuardedBackend(self, backend)
            self._app = PlaybackApplication(self._backend, session_store, clock)
        except BaseException:
            self._resources.close()
            raise

    def _check_thread(self) -> None:
        if self._closed or get_ident() != self._thread:
            raise RuntimeError("Playback task is closed or used outside its owner thread")

    def _load(self) -> QueueSnapshot:
        queue = self.queue_store.load(self.queue_id)
        if queue is None:
            raise ValueError("Queue does not exist")
        return queue

    def _view(self) -> TaskView:
        queue = self._load()
        return TaskView(self._playback, queue, self._handoff(queue))

    def _handoff(self, queue: QueueSnapshot) -> HandoffDiagnostic:
        owned, authority = self._playback, self._release

        def report(
            stage: HandoffStage,
            reason: HandoffReason,
            disposition: HandoffDisposition = HandoffDisposition.HOLD,
        ) -> HandoffDiagnostic:
            return HandoffDiagnostic(
                stage,
                disposition,
                reason,
                handoff_evidence(
                    owned,
                    self._events[-1] if self._events else None,
                    authority,
                    now=self.clock.monotonic(),
                )
                if owned
                else None,
                authority.first_veto if authority else None,
            )

        if queue.cancellation_requested or (self._cancellation and self._cancellation.requested):
            return report(HandoffStage.QUEUE, HandoffReason.CANCELED)
        if not self._started:
            return report(HandoffStage.QUEUE, HandoffReason.AWAITING_START)
        if owned is None or not queue.attempts:
            return report(HandoffStage.QUEUE, HandoffReason.SESSION_REQUIRED)
        attempt = queue.attempts[-1]
        if owned.ownership_lost and attempt.intent == QueueIntent.CURRENT:
            return report(HandoffStage.QUEUE, HandoffReason.OWNERSHIP_LOST)
        if attempt.intent == QueueIntent.CURRENT:
            return report(HandoffStage.QUEUE, HandoffReason.AWAITING_COMPLETION)
        if attempt.intent != QueueIntent.FINISHED:
            return report(HandoffStage.QUEUE, HandoffReason.ATTEMPT_NOT_FINISHED)
        stage = HandoffStage.SUCCESSOR if owned.release_confirmed else HandoffStage.RELEASE
        if owned.ownership_lost:
            return report(stage, HandoffReason.OWNERSHIP_LOST)
        if authority is None:
            return report(stage, HandoffReason.AUTHORITY_REQUIRED)
        if authority.blocked:
            return report(stage, HandoffReason.AUTHORITY_BLOCKED)
        releases = [
            entry
            for entry in queue.commands
            if entry.attempt_id == attempt.attempt_id and entry.action == CommandAction.RELEASE
        ]
        if not owned.release_confirmed:
            if self._release_refusal is not None:
                return self._release_refusal
            if releases:
                release = releases[-1]
                if release.state == ExecutionState.REJECTED:
                    return report(stage, HandoffReason.RELEASE_REJECTED)
                if release.state != ExecutionState.ACKNOWLEDGED:
                    return report(stage, HandoffReason.RELEASE_UNRESOLVED)
                return report(stage, HandoffReason.AWAITING_RELEASE_IDLE)
            return release_decision(owned, authority, self._events, now=self.clock.monotonic())
        if not releases or releases[-1].state != ExecutionState.ACKNOWLEDGED:
            return report(stage, HandoffReason.RELEASE_UNRESOLVED)
        if not self._idle():
            return report(stage, HandoffReason.AWAITING_FRESH_IDLE)
        decision = decide_queue(
            queue,
            owned,
            now=self.clock.monotonic(),
            authority=self._authority,
            receiver=self._events[-1],
        )
        if decision.disposition == QueueDisposition.READY:
            return report(stage, HandoffReason.SUCCESSOR_ELIGIBLE, HandoffDisposition.READY)
        if decision.disposition == QueueDisposition.COMPLETE:
            return report(stage, HandoffReason.EXHAUSTED, HandoffDisposition.COMPLETE)
        return replace(report(stage, HandoffReason.QUEUE_POLICY_HOLD), queue_reason=decision.reason)

    def _cancel(self, cancellation: Cancellation) -> bool:
        self._cancellation = cancellation
        queue = self._load()
        if cancellation.requested and not queue.cancellation_requested:
            self.queue_store.cancel_queue(self.queue_id, expected_revision=queue.revision)
        return cancellation.requested or queue.cancellation_requested

    def _may_effect(self, action: CommandAction) -> bool:
        self._check_thread()
        dispatch = self._dispatch
        if dispatch is None or dispatch.command.action != action:
            return False
        queue = self._load()
        if (
            queue.revision != dispatch.snapshot.revision
            or queue.generation != dispatch.snapshot.generation
        ):
            return False
        return not (
            action != CommandAction.STOP
            and (
                queue.cancellation_requested
                or self._cancellation is None
                or self._cancellation.requested
            )
        )

    def _idle(self) -> bool:
        if not self._events:
            return False
        last = self._events[-1]
        authority = self._authority
        return (
            authority is not None
            and last.session_active is False
            and last.target == self.target
            and last.connection_generation == authority.connection_generation
            and last.monotonic > self._boundary
            and 0 <= self.clock.monotonic() - last.monotonic <= 5
            and not any(event.connection_reset for event in self._events)
        )

    def _accept(self, result: PlaybackResult) -> None:
        if result.snapshot is not None:
            if (
                self._playback is not None
                and self._playback.scope.request.attempt_id
                != result.snapshot.scope.request.attempt_id
            ):
                self._release = None
                self._release_refusal = None
            self._playback = result.snapshot
        elif self._playback is not None:
            previous = self._playback
            for event in result.observations:
                self._playback = policy.observe(self._playback, event, now=self.clock.monotonic())
            self._playback = policy.refresh(self._playback, now=self.clock.monotonic())
            if self._playback != previous:
                self._app.store.save(self._playback, expected_revision=previous.revision)
        if self._playback is not None:
            self._release = track_release(self._playback, result.observations, self._release)

    def _execute(
        self,
        command_id: str,
        action: CommandAction,
        request: PlaybackRequest,
        operation: Callable[[PlaybackRequest], PlaybackResult],
        cancellation: Cancellation,
    ) -> None:
        queue = self._load()
        request = replace(request, request_id=command_id)
        application_failed = False

        def invoke(dispatch: QueueDispatch) -> CommandReceipt:
            nonlocal application_failed
            self._dispatch = dispatch
            try:
                try:
                    result = operation(request)
                except ControlRefused, ValueError:
                    if self._app.last_result is not None:
                        raise
                    self._accept(PlaybackResult(self._events))
                    if action == CommandAction.RELEASE and self._playback is not None:
                        diagnosis = release_decision(
                            self._playback,
                            self._release,
                            self._events,
                            now=self.clock.monotonic(),
                        )
                        if diagnosis.disposition == HandoffDisposition.HOLD:
                            self._release_refusal = diagnosis
                    return CommandReceipt(
                        command_id,
                        request.attempt_id,
                        action,
                        CommandOutcome.REJECTED,
                        self.clock.utcnow(),
                    )
                self._accept(result)
                application_failed = result.failure is not None
                return result.receipt or CommandReceipt(
                    command_id,
                    request.attempt_id,
                    action,
                    CommandOutcome.UNKNOWN,
                    self.clock.utcnow(),
                )
            finally:
                self._dispatch = None

        result = self._executor.execute(
            QueueCommandRequest(
                self.queue_id,
                request.queue_item_id,
                request.attempt_id,
                command_id,
                action,
                queue.revision,
                queue.generation,
            ),
            invoke,
            may_start=lambda: not cancellation.requested,
        )
        if (
            action == CommandAction.START
            and result.operation_invoked
            and (
                self._playback is None
                or self._playback.scope.request.attempt_id != request.attempt_id
            )
        ):
            current = self._load()
            self.queue_store.settle_attempt(
                self.queue_id,
                request.attempt_id,
                QueueIntent.NEEDS_ATTENTION,
                decision_id="unconfirmed-" + command_id,
                expected_revision=current.revision,
            )
        if application_failed or result.command.state in {
            ExecutionState.UNCERTAIN,
            ExecutionState.DISPATCHED,
        }:
            current = self._load()
            if not current.cancellation_requested:
                self.queue_store.cancel_queue(self.queue_id, expected_revision=current.revision)
        self._cancel(cancellation)

    def _start(self, command: RunnerCommand, cancellation: Cancellation) -> None:
        request = command.request
        if request is None or request.target != self.target:
            raise ValueError("Start requires this queue's target")
        queue = self._load()
        existing = next(
            (entry for entry in queue.commands if entry.command_id == command.command_id), None
        )
        if existing is not None:
            attempt = next(
                entry for entry in queue.attempts if entry.attempt_id == existing.attempt_id
            )
            entry = next(entry for entry in queue.items if entry.item_id == attempt.item_id)
            if (
                existing.action != CommandAction.START
                or existing.attempt_id != request.attempt_id
                or entry.item_id != request.queue_item_id
                or entry.content != request.content
            ):
                raise ValueError("Command identity describes different work")
            return
        if self._cancel(cancellation):
            return
        if self._started:
            return
        self._backend.observe(self.target)
        if not self._events:
            return
        generation = self._events[-1].connection_generation
        self._authority = QueueAuthority(
            self.queue_id, queue.generation, generation, self._boundary
        )
        decision = decide_queue(
            queue,
            None,
            now=self.clock.monotonic(),
            authority=self._authority,
            receiver=self._events[-1],
        )
        item = next(
            (entry for entry in queue.items if entry.item_id == request.queue_item_id), None
        )
        if decision.disposition != QueueDisposition.READY:
            return
        if (
            decision.next_item_id != request.queue_item_id
            or item is None
            or item.content != request.content
        ):
            raise ValueError("Start must select the first pending exact queue item")
        self._started = True
        self._execute(
            command.command_id, CommandAction.START, request, self._app.start, cancellation
        )

    def handle(self, command: RunnerCommand, cancellation: Cancellation) -> TaskView:
        self._check_thread()
        self._cancellation = cancellation
        if command.action == CommandAction.START:
            self._start(command, cancellation)
            return self._view()
        if command.action == CommandAction.RELEASE:
            raise ValueError("Release is an internal completed-attempt operation")
        if command.action == CommandAction.STOP:
            queue = self._load()
            if not queue.cancellation_requested:
                self.queue_store.cancel_queue(self.queue_id, expected_revision=queue.revision)
        elif self._cancel(cancellation):
            return self._view()
        owned = self._playback
        if owned is None or owned.ownership_lost or owned.release_confirmed:
            return self._view()

        def operation(request: PlaybackRequest) -> PlaybackResult:
            if command.action == CommandAction.STOP:
                return self._app.stop(request, owned)
            if command.action == CommandAction.PAUSE:
                return self._app.pause(request, owned)
            return self._app.resume(request, owned)

        self._execute(
            command.command_id, command.action, owned.scope.request, operation, cancellation
        )
        if (
            command.action == CommandAction.STOP
            and self._playback is not None
            and self._playback.state == PlayerState.STOPPED
        ):
            queue = self._load()
            attempt = queue.attempts[-1]
            if attempt.intent == QueueIntent.CURRENT:
                self.queue_store.settle_attempt(
                    self.queue_id,
                    attempt.attempt_id,
                    QueueIntent.STOPPED,
                    decision_id="stopped-" + command.command_id,
                    expected_revision=queue.revision,
                )
        return self._view()

    def step(self, cancellation: Cancellation) -> TaskView:
        self._check_thread()
        canceled = self._cancel(cancellation)
        if not self._started or self._playback is None:
            return self._view()
        queue = self._load()
        attempt = queue.attempts[-1]
        if attempt.intent == QueueIntent.CURRENT or canceled:
            self._accept(self._app.status(self._playback.scope.request, self._playback))
            if self._cancel(cancellation):
                return self._view()
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
            return self._view()
        owned = self._playback
        if owned.ownership_lost or attempt.intent != QueueIntent.FINISHED:
            self._observe_only()
            return self._view()
        releases = [
            entry
            for entry in queue.commands
            if entry.attempt_id == attempt.attempt_id and entry.action == CommandAction.RELEASE
        ]
        if not owned.release_confirmed:
            authority = self._release
            if releases or authority is None or authority.blocked:
                self._observe_only()
                return self._view()
            if not 0 <= self.clock.monotonic() - authority.terminal.monotonic <= 5:
                self._observe_only()
                return self._view()
            self._execute(
                "release-" + attempt.attempt_id,
                CommandAction.RELEASE,
                owned.scope.request,
                lambda request: self._app.release(request, owned, authority),
                cancellation,
            )
            return self._view()
        if not releases or releases[-1].state != ExecutionState.ACKNOWLEDGED:
            self._observe_only()
            return self._view()
        if self._release is None or self._release.blocked:
            self._observe_only()
            return self._view()
        self._backend.observe(self.target)
        self._accept(PlaybackResult(self._events))
        if (
            self._cancel(cancellation)
            or not self._idle()
            or self._release is None
            or self._release.blocked
        ):
            return self._view()
        queue = self._load()
        owned = self._playback
        assert owned is not None
        decision = decide_queue(
            queue,
            owned,
            now=self.clock.monotonic(),
            authority=self._authority,
            receiver=self._events[-1],
        )
        if decision.disposition == QueueDisposition.READY:
            item = next(entry for entry in queue.items if entry.item_id == decision.next_item_id)
            command_id, attempt_id = str(uuid4()), str(uuid4())
            request = PlaybackRequest(
                command_id, attempt_id, item.item_id, item.content, self.target
            )
            self._handoff_start = True
            try:
                self._execute(
                    command_id, CommandAction.START, request, self._app.start, cancellation
                )
            finally:
                self._handoff_start = False
        return self._view()

    def _observe_only(self) -> None:
        self._backend.observe(self.target)
        self._accept(PlaybackResult(self._events))

    def close(self) -> None:
        self._check_thread()
        self._closed = True
        self._resources.close()
