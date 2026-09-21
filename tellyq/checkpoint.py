"""One supervised playback attempt, with private chronology and existing policy guards."""

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path
from types import TracebackType

from .application import ControlRefused, PlaybackApplication, PlaybackResult
from .domain.ports import Clock, PlaybackBackend, PlaybackControls, SessionStore
from .domain.values import (
    CommandAction,
    CommandReceipt,
    ControlReason,
    IdleReason,
    PlaybackCapabilities,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    SessionSnapshot,
    Support,
)


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError("Unsupported checkpoint record value.")


@dataclass(frozen=True, slots=True)
class CheckpointOptions:
    seconds: float = 240
    after_terminal: float = 20
    pause_hold: float | None = None

    def __post_init__(self) -> None:
        limits = ((self.seconds, 15, 600), (self.after_terminal, 5, 60))
        if self.pause_hold is not None:
            limits += ((self.pause_hold, 5, 60),)
        if any(
            isinstance(value, bool) or not isfinite(value) or not minimum <= value <= maximum
            for value, minimum, maximum in limits
        ):
            raise ValueError("Checkpoint windows are outside their bounded ranges.")


@dataclass(slots=True)
class CheckpointSummary:
    stop_reason: str = "deadline"
    launch_outcome: str = "not_attempted"
    receiver_playback_observed: bool = False
    natural_completion_observed: bool = False
    terminal_candidates: int = 0
    pause: str = "not_requested"
    resume: str = "not_requested"
    cleanup: str = "not_attempted"
    cleanup_state: str | None = None
    last_state: str | None = None
    last_reason: str | None = None
    error_type: str | None = None
    visual_confirmation: None = None


class CheckpointJournal:
    """Exclusive private output; each ordered record is durable before the next effect."""

    def __init__(self, directory: Path, clock: Clock) -> None:
        if not directory.resolve().is_relative_to(Path("runtime").resolve()):
            raise ValueError("Checkpoint output must be under runtime/.")
        directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.mkdir(mode=0o700)  # Existing attempts are never appended to or overwritten.
        descriptor = os.open(
            directory / "journal.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        self._stream = os.fdopen(descriptor, "w", encoding="utf-8")
        self.clock = clock
        self.sequence = 0

    def write(self, kind: str, **fields: object) -> None:
        record = {
            "schema_version": 1,
            "sequence": self.sequence,
            "recorded_at": self.clock.utcnow().isoformat(),
            "monotonic": self.clock.monotonic(),
            "kind": kind,
            **fields,
        }
        self._stream.write(json.dumps(record, default=_json_value, allow_nan=False) + "\n")
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self.sequence += 1

    def __enter__(self) -> CheckpointJournal:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stream.close()


class _RecordedBackend:
    def __init__(
        self,
        backend: PlaybackBackend,
        journal: CheckpointJournal,
        clock: Clock,
        drain: Callable[[], object],
    ) -> None:
        self.backend, self.journal, self.clock, self.drain = backend, journal, clock, drain
        self.latest: tuple[PlaybackObservation, ...] = ()
        self.phase = "preflight"
        self.launch_boundary: float | None = None
        self.deadline = float("inf")
        self.paused_identity: tuple[str, str | None, str, str | None] | None = None

    def observe(self, target: PlaybackTarget) -> tuple[PlaybackObservation, ...]:
        boundary = self.clock.monotonic()
        try:
            self.latest = self.backend.observe(target)
        except (Exception, KeyboardInterrupt) as exc:
            self.journal.write(
                "observation_error",
                phase=self.phase,
                error_type=type(exc).__name__,
                raw=self.drain(),
            )
            raise
        self.journal.write(
            "observations",
            phase=self.phase,
            boundary=boundary,
            events=self.latest,
            raw=self.drain(),
        )
        return self.latest

    def idle(self, target: PlaybackTarget) -> bool:
        receivers = [
            event
            for event in self.latest
            if event.target == target and event.session_active is not None
        ]
        if not receivers:
            return False
        latest = receivers[-1]
        return (
            latest.session_active is False
            and 0 <= self.clock.monotonic() - latest.monotonic <= 5
            and not any(
                event.monotonic >= latest.monotonic and event.connection_reset
                for event in self.latest
            )
        )

    def capabilities(self, target: PlaybackTarget) -> PlaybackCapabilities:
        return self.backend.capabilities(target)

    def _effect(
        self, action: CommandAction, operation: Callable[[], CommandReceipt]
    ) -> CommandReceipt:
        if action != CommandAction.STOP and self.clock.monotonic() >= self.deadline:
            self.journal.write("command_refused", action=action, reason="deadline")
            raise TimeoutError("Observation budget expired before dispatch.")
        self.journal.write("command_intent", action=action, phase=self.phase, outcome="unknown")
        if action == CommandAction.START:
            self.launch_boundary = self.clock.monotonic()
        try:
            receipt = operation()
        except (Exception, KeyboardInterrupt) as exc:
            self.journal.write("command_error", action=action, error_type=type(exc).__name__)
            raise
        self.journal.write("command_receipt", receipt=receipt, phase=self.phase)
        return receipt

    def start(self, request: PlaybackRequest) -> CommandReceipt:
        # Application.start takes another baseline. Re-check it at the actual effect boundary.
        if not self.idle(request.target):
            raise ValueError("Receiver is active or its idle state is unknown.")
        return self._effect(CommandAction.START, lambda: self.backend.start(request))

    def stop(self, scope: PlaybackScope) -> CommandReceipt:
        return self._effect(CommandAction.STOP, lambda: self.backend.stop(scope))

    def _control(
        self, scope: PlaybackScope, playback_id: str, action: CommandAction
    ) -> CommandReceipt:
        backend = self.backend
        if not isinstance(backend, PlaybackControls):
            raise ValueError("Backend does not offer media controls.")
        effect = backend.pause if action == CommandAction.PAUSE else backend.resume
        return self._effect(action, lambda: effect(scope, playback_id))

    def pause(self, scope: PlaybackScope, playback_id: str) -> CommandReceipt:
        return self._control(scope, playback_id, CommandAction.PAUSE)

    def resume(self, scope: PlaybackScope, playback_id: str) -> CommandReceipt:
        if self.paused_identity != (
            scope.session_id,
            scope.application_id,
            scope.connection_generation,
            playback_id,
        ):
            self.journal.write("command_refused", action="resume", reason="identity_changed")
            raise ControlRefused(ControlReason.IDENTITY_CHANGED)
        return self._control(scope, playback_id, CommandAction.RESUME)


def _discard_raw() -> object:
    return []


def _quiet(_phase: str) -> None:
    pass


class _Attempt:
    def __init__(
        self,
        backend: _RecordedBackend,
        store: SessionStore,
        request: PlaybackRequest,
        options: CheckpointOptions,
        phase: Callable[[str], None],
    ) -> None:
        self.backend, self.request, self.options, self.phase = backend, request, options, phase
        self.clock, self.journal = backend.clock, backend.journal
        self.app = PlaybackApplication(backend, store, self.clock)
        self.owned: SessionSnapshot | None = None
        self.summary = CheckpointSummary(pause="pending" if options.pause_hold else "not_requested")
        self.deadline = self.clock.monotonic() + options.seconds
        self.backend.deadline = self.deadline
        self.post_deadline: float | None = None
        self.terminals: set[tuple[str, int]] = set()
        self.playback_recorded = False
        self.completion_recorded = False
        self.replacement = False

    def result(self, phase: str, result: PlaybackResult) -> None:
        self.journal.write("application_result", phase=phase, result=result)
        if (
            phase == "start"
            and result.receipt is not None
            and self.backend.launch_boundary is not None
        ):
            self.summary.launch_outcome = result.receipt.outcome.value
        snapshot = result.snapshot
        if snapshot is not None:
            self.owned = snapshot
            self.replacement |= snapshot.ownership_lost
            if phase == "cleanup":
                self.summary.cleanup_state = snapshot.state.value
            else:
                self.summary.last_state = snapshot.state.value
                evidence = snapshot.evidence
                self.summary.receiver_playback_observed |= evidence.receiver_playback_confirmed
                self.summary.natural_completion_observed |= evidence.natural_completion_confirmed
                if evidence.natural_completion_confirmed and not self.completion_recorded:
                    self.journal.write("completion_evidence", phase=phase, snapshot=snapshot)
                    self.completion_recorded = True
                    self.post_deadline = self.clock.monotonic() + self.options.after_terminal
                    self.phase("natural_completion_observed")
        elif result.reason.value == "session_replaced":
            self.replacement = True
        if phase != "cleanup":
            self.summary.last_reason = result.reason.value
        boundary = self.backend.launch_boundary
        for event in result.observations:
            identity = (event.connection_generation, event.sequence)
            if (
                phase != "cleanup"
                and boundary is not None
                and event.monotonic > boundary
                and event.idle_reason == IdleReason.FINISHED
                and identity not in self.terminals
            ):
                self.terminals.add(identity)
                self.summary.terminal_candidates += 1
                self.journal.write("terminal_candidate", event=event, completion_inferred=False)
        if self.summary.receiver_playback_observed and not self.playback_recorded:
            self.phase("receiver_playback_observed")
            self.playback_recorded = True

    def call(self, phase: str, operation: Callable[[], PlaybackResult]) -> PlaybackResult:
        self.backend.phase = phase
        self.journal.write("phase", phase=phase)
        self.phase(phase)
        try:
            result = operation()
        except (Exception, KeyboardInterrupt) as exc:
            self.journal.write(
                "application_error",
                phase=phase,
                error_type=type(exc).__name__,
                diagnostic=exc.diagnostic if isinstance(exc, ControlRefused) else None,
            )
            raise
        self.result(phase, result)
        if result.failure is not None:
            raise RuntimeError("Application returned an uncertain operation failure.")
        return result

    def status(self, phase: str = "observe") -> PlaybackResult:
        return self.call(phase, lambda: self.app.status(self.request, self.owned))

    def pause_ready(self) -> bool:
        owned = self.owned
        if owned is None or owned.ownership_lost or self.replacement:
            return False
        media = owned.latest
        return bool(
            media is not None
            and media.content == self.request.content
            and media.state == PlayerState.PLAYING
            and media.session_id == owned.scope.session_id
            and media.application_id == owned.scope.application_id
            and media.connection_generation == owned.scope.connection_generation
            and 0 <= self.clock.monotonic() - media.monotonic <= 5
            and self.backend.capabilities(self.request.target).pause.support
            in {Support.ADVERTISED, Support.VERIFIED}
        )

    def controls(self) -> None:
        self.summary.pause = "attempted"
        try:
            paused = self.call("pause", lambda: self.app.pause(self.request, self.owned))
        except ControlRefused:
            self.summary.pause = "refused"
            return
        self.summary.pause = "observed" if paused.control_observed else "unconfirmed"
        if not paused.control_observed or paused.snapshot is None or paused.snapshot.latest is None:
            self.summary.resume = "refused_unverified_pause"
            return
        scope = paused.snapshot.scope
        self.backend.paused_identity = (
            scope.session_id,
            scope.application_id,
            scope.connection_generation,
            paused.snapshot.latest.playback_id,
        )
        self.phase("pause_observed")
        hold_until = self.clock.monotonic() + (self.options.pause_hold or 0)
        self.journal.write("hold_begin", seconds=self.options.pause_hold)
        while self.clock.monotonic() < min(hold_until, self.deadline):
            self.status("hold")
            if self.replacement:
                break
        self.journal.write("hold_end", completed=self.clock.monotonic() >= hold_until)
        if self.replacement or self.clock.monotonic() >= self.deadline:
            self.summary.resume = "refused_replacement" if self.replacement else "deadline"
            return
        self.summary.resume = "attempted"
        try:
            resumed = self.call("resume", lambda: self.app.resume(self.request, self.owned))
        except ControlRefused:
            self.summary.resume = "refused"
            return
        self.summary.resume = "observed" if resumed.control_observed else "unconfirmed"
        if resumed.control_observed:
            self.phase("resume_observed")

    def cleanup(self) -> None:
        if self.owned is None:
            self.summary.cleanup = "refused_no_ownership"
            self.journal.write("cleanup_refused", reason=self.summary.cleanup)
            return
        if self.replacement:
            self.summary.cleanup = "refused_replacement"
            self.journal.write("cleanup_refused", reason=self.summary.cleanup)
            return
        try:
            # stop obtains fresh receiver/content evidence again before its guarded dispatch.
            result = self.call("cleanup", lambda: self.app.stop(self.request, self.owned))
            self.summary.cleanup = (
                "observed" if result.reason.value == "stop_observed" else "unconfirmed"
            )
        except (Exception, KeyboardInterrupt) as exc:
            self.summary.cleanup = "refused" if isinstance(exc, ValueError) else "error"
            self.journal.write("cleanup_error", error_type=type(exc).__name__)

    def run(self) -> CheckpointSummary:
        self.journal.write("begin", request=self.request, options=self.options)
        try:
            self.backend.observe(self.request.target)
            if not self.backend.idle(self.request.target):
                self.summary.stop_reason = "preflight_refused"
                return self.summary
            if self.clock.monotonic() >= self.deadline:
                return self.summary
            launched = self.call("start", lambda: self.app.start(self.request))
            if launched.receipt is not None:
                self.summary.launch_outcome = launched.receipt.outcome.value
            if self.owned is None:
                self.summary.stop_reason = "ownership_unconfirmed"
                return self.summary
            while self.clock.monotonic() < self.deadline:
                if self.post_deadline is not None and self.clock.monotonic() >= self.post_deadline:
                    self.summary.stop_reason = "post_terminal_window"
                    break
                self.status()
                if (
                    self.summary.pause == "pending"
                    and self.pause_ready()
                    and self.clock.monotonic() < self.deadline
                ):
                    self.controls()
            if self.summary.pause == "pending":
                self.summary.pause = "qualification_timeout"
        except (Exception, KeyboardInterrupt) as exc:
            self.summary.stop_reason = (
                "interrupted" if isinstance(exc, KeyboardInterrupt) else "error"
            )
            self.summary.error_type = type(exc).__name__
            self.journal.write("run_error", error_type=type(exc).__name__)
        finally:
            self.cleanup()
            self.journal.write("end", summary=self.summary)
        return self.summary


def run_checkpoint(
    backend: PlaybackBackend,
    store: SessionStore,
    clock: Clock,
    journal: CheckpointJournal,
    request: PlaybackRequest,
    options: CheckpointOptions,
    *,
    drain_raw: Callable[[], object] = _discard_raw,
    phase: Callable[[str], None] = _quiet,
) -> CheckpointSummary:
    """Run one attempt; callers own the process lock, backend lifetime and private store."""
    recorded = _RecordedBackend(backend, journal, clock, drain_raw)
    return _Attempt(recorded, store, request, options, phase).run()
