"""Bounded client-side service evidence; never opens or discovers a receiver."""

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from re import fullmatch
from time import get_clock_info, monotonic, sleep
from typing import Literal, Protocol, cast
from uuid import uuid4

from .domain.queue import QueueMode
from .domain.values import CommandAction
from .models import IPCResponse, IPCValue, ServiceCheckpointSummary, ServiceCheckpointTiming
from .runner import RunnerCommand
from .service import QueueSpec
from .service_cli import start_command


class CheckpointClient(Protocol):
    def status(self) -> IPCResponse: ...
    def submit(self, command: RunnerCommand) -> IPCResponse: ...
    def ticket(self, command_id: str) -> IPCResponse: ...


class EvidenceJournal(Protocol):
    def write(self, kind: str, **fields: object) -> None: ...


@dataclass(frozen=True, slots=True)
class ServiceCheckpointOptions:
    mode: Literal["trace", "start"] = "trace"
    seconds: float = 600
    poll_seconds: float = 0.1
    cleanup_stop: bool = False
    cleanup_seconds: float = 15
    pause_hold: float | None = None
    cancel_after: float | None = None

    def __post_init__(self) -> None:
        bounds = (
            (self.seconds, 1, 3600),
            (self.poll_seconds, 0.05, 2),
            (self.cleanup_seconds, 1, 30),
        )
        if self.pause_hold is not None:
            bounds += ((self.pause_hold, 1, 60),)
        if self.cancel_after is not None:
            bounds += ((self.cancel_after, 1, self.seconds),)
        if any(isinstance(v, bool) or not isfinite(v) or not lo <= v <= hi for v, lo, hi in bounds):
            raise ValueError("Checkpoint timing is outside its bounded range.")
        if self.mode not in {"trace", "start"} or (
            self.mode == "trace"
            and (self.cleanup_stop or self.pause_hold is not None or self.cancel_after is not None)
        ):
            raise ValueError("Trace mode permits status reads only.")


def _object(value: object) -> dict[str, IPCValue]:
    return cast(dict[str, IPCValue], value) if isinstance(value, dict) else {}


def selected_queue(response: IPCResponse, spec: QueueSpec) -> dict[str, IPCValue]:
    """Fail closed before any effect if the live endpoint differs from the manifest."""
    if not response["ok"] or "snapshot" not in response:
        raise ValueError("Service status is unavailable.")
    queue = _object(response["snapshot"]["view"]["queue"])
    target = _object(queue.get("target"))
    items = queue.get("items")
    if (
        queue.get("queue_id") != spec.queue_id
        or queue.get("mode", "legacy") != spec.mode.value
        or target.get("device_id") != spec.target.device_id
        or target.get("route") != spec.target.route
        or not isinstance(items, list)
        or queue.get("items_truncated") is not False
        or len(items) != len(spec.items)
        or any(
            (
                _object(item).get("item_id"),
                _object(item).get("provider"),
                _object(item).get("content_id"),
                _object(item).get("kind"),
            )
            != (
                expected.item_id,
                expected.content.provider,
                expected.content.content_id,
                expected.content.kind.value,
            )
            for item, expected in zip(items, spec.items, strict=True)
        )
    ):
        raise ValueError("Service queue does not match the selected manifest.")
    return queue


@dataclass(slots=True)
class _Pending:
    ticket_id: str
    sent: float
    baseline_observed_at: IPCValue
    baseline_receipt: IPCValue
    expected_attempt: str | None
    timing: ServiceCheckpointTiming


class _Checkpoint:
    def __init__(
        self,
        client: CheckpointClient,
        spec: QueueSpec,
        options: ServiceCheckpointOptions,
        journal: EvidenceJournal,
        now: Callable[[], float],
        wait: Callable[[float], None],
        code_revision: str | None,
    ) -> None:
        self.client, self.spec, self.options, self.journal = client, spec, options, journal
        self.now, self.wait, self.origin = now, wait, now()
        self.pending: dict[str, _Pending] = {}
        self.attempted: set[str] = set()
        self.attempts: list[set[str]] = [set() for _ in spec.items]
        self.summary: ServiceCheckpointSummary = {
            "schema_version": 1,
            "mode": options.mode,
            "code_revision": code_revision,
            "elapsed_ms": 0,
            "poll_seconds": options.poll_seconds,
            "clock_resolution_seconds": get_clock_info("monotonic").resolution,
            "timing_origin": "client_monotonic",
            "handoff_diagnostics_available": False,
            "stop_reason": "deadline",
            "error_type": None,
            "items": [
                {
                    "index": i + 1,
                    "verified_at_ms": None,
                    "finished_at_ms": None,
                    "distinct_attempts_observed": 0,
                }
                for i in range(len(spec.items))
            ],
            "verified_handoffs": 0,
            "commands": [],
            "cleanup": "not_requested",
            "visual_confirmation": None,
            "live_acceptance": False,
        }
        self.latest: IPCResponse | None = None

    def elapsed(self) -> float:
        return round((self.now() - self.origin) * 1000, 3)

    def sample(self) -> IPCResponse:
        response = self.client.status()
        received = self.now()
        queue = selected_queue(response, self.spec)
        self.latest = response
        self.journal.write("status", client_received_ms=self.elapsed(), response=response)
        if _object(response["snapshot"]["view"]).get("handoff") is not None:
            self.summary["handoff_diagnostics_available"] = True
        playback = _object(response["snapshot"]["view"]["playback"])
        observed_ms = round((received - self.origin) * 1000, 3)
        evidence = _object(playback.get("evidence"))
        items = cast(list[IPCValue], queue["items"])
        for index, (item, expected) in enumerate(zip(items, self.spec.items, strict=True)):
            record = self.summary["items"][index]
            if _object(item).get("intent") == "finished" and record["finished_at_ms"] is None:
                record["finished_at_ms"] = observed_ms
            if (
                queue.get("current_item_id") == expected.item_id
                and playback.get("content_id") == expected.content.content_id
            ):
                attempt = playback.get("attempt_id")
                if isinstance(attempt, str):
                    self.attempts[index].add(attempt)
                    record["distinct_attempts_observed"] = len(self.attempts[index])
                if (
                    evidence.get("receiver_playback_confirmed") is True
                    and playback.get("ownership_lost") is False
                    and record["verified_at_ms"] is None
                ):
                    record["verified_at_ms"] = observed_ms
        self.summary["verified_handoffs"] = sum(
            before["verified_at_ms"] is not None
            and before["finished_at_ms"] is not None
            and after["verified_at_ms"] is not None
            and after["verified_at_ms"] >= before["finished_at_ms"]
            for before, after in zip(self.summary["items"], self.summary["items"][1:], strict=False)
        )
        for action, command in self.pending.items():
            timing = command.timing
            if timing["ticket_completed_ms"] is None:
                ticket_response = self.client.ticket(command.ticket_id)
                ticket_received = self.now()
                self.journal.write(
                    "ticket", client_received_ms=self.elapsed(), response=ticket_response
                )
                ticket = ticket_response.get("ticket")
                if ticket is not None:
                    timing["ticket_state"] = ticket["state"]
                    if ticket["state"] in {"handled", "canceled", "failed"}:
                        timing["ticket_completed_ms"] = round(
                            (ticket_received - command.sent) * 1000, 3
                        )
            if timing["ticket_state"] in {"canceled", "failed"}:
                timing["first_verified_state_ms"] = None
                continue
            receipt = _object(playback.get("receipt"))
            fresh = (
                playback.get("observed_at") is not None
                and playback.get("observed_at") != command.baseline_observed_at
            )
            receipt_matches = (
                receipt.get("request_id") == command.ticket_id
                and receipt.get("action") == action
                and receipt.get("outcome") == "accepted"
                and receipt.get("recorded_at") != command.baseline_receipt
            )
            verified = {
                "start": evidence.get("receiver_playback_confirmed") is True,
                "pause": playback.get("state") == "paused"
                and evidence.get("identity_confirmed") is True,
                "resume": playback.get("state") == "playing"
                and evidence.get("receiver_playback_confirmed") is True,
                "stop": playback.get("state") == "stopped"
                and evidence.get("reason") == "stop_observed",
            }[action]
            if (
                timing["first_verified_state_ms"] is None
                and fresh
                and receipt_matches
                and playback.get("attempt_id") == command.expected_attempt
                and verified
                and playback.get("ownership_lost") is False
            ):
                timing["first_verified_state_ms"] = round((received - command.sent) * 1000, 3)
        return response

    def submit(self, action: Literal["start", "pause", "resume", "stop"]) -> None:
        # Revalidate immediately before submission, including cleanup after errors.
        response = self.sample()
        queue = selected_queue(response, self.spec)
        playback = _object(response["snapshot"]["view"]["playback"])
        receipt = _object(playback.get("receipt"))
        command = (
            start_command(cast(dict[str, object], queue), None)
            if action == "start"
            else RunnerCommand(str(uuid4()), CommandAction(action))
        )
        timing: ServiceCheckpointTiming = {
            "action": action,
            "submitted_ms": self.elapsed(),
            "acknowledgement_ms": None,
            "ticket_completed_ms": None,
            "first_verified_state_ms": None,
            "ticket_state": None,
            "accepted": False,
        }
        self.summary["commands"].append(timing)
        self.journal.write("submit_intent", action=action)
        sent = self.now()
        timing["submitted_ms"] = self.elapsed()
        self.attempted.add(action)
        result = self.client.submit(command)
        timing["acknowledgement_ms"] = round((self.now() - sent) * 1000, 3)
        self.journal.write("acknowledgement", response=result, timing=timing)
        expected_attempt = (
            command.request.attempt_id
            if command.request is not None
            else playback.get("attempt_id")
        )
        if result["ok"] and result.get("ticket_id") is not None:
            timing["accepted"] = True
            self.pending[action] = _Pending(
                result["ticket_id"],
                sent,
                playback.get("observed_at"),
                receipt.get("recorded_at"),
                expected_attempt if isinstance(expected_attempt, str) else None,
                timing,
            )
        else:
            raise ValueError("Service rejected the command.")

    def verified(self, action: str) -> bool:
        return (
            action in self.pending
            and self.pending[action].timing["first_verified_state_ms"] is not None
        )

    def run(self) -> ServiceCheckpointSummary:
        started = False
        pause_observed: float | None = None
        try:
            initial = self.sample()
            if self.options.mode == "start":
                queue = selected_queue(initial, self.spec)
                if queue.get("cancellation_requested") is not False or any(
                    _object(item).get("intent") != "pending"
                    for item in cast(list[IPCValue], queue["items"])
                ):
                    raise ValueError("Explicit start requires a fresh pending queue.")
                # An uncertain submission still requires opted-in cleanup.
                started = True
                self.submit("start")
            deadline = self.origin + self.options.seconds
            while self.now() < deadline:
                response = self.sample()
                playback = _object(response["snapshot"]["view"]["playback"])
                handoff = _object(response["snapshot"]["view"].get("handoff"))
                waiting_native = (
                    self.spec.mode == QueueMode.NATIVE
                    and handoff.get("reason") == "native_successor_pending"
                )
                if (
                    self.options.mode == "start"
                    and playback.get("ownership_lost") is True
                    and not waiting_native
                ):
                    self.summary["stop_reason"] = "ownership_lost"
                    break
                if (
                    self.options.cancel_after is not None
                    and self.now() - self.origin >= self.options.cancel_after
                    and "stop" not in self.pending
                ):
                    self.submit("stop")
                if self.verified("stop"):
                    self.summary["stop_reason"] = "stop_observed"
                    break
                if (
                    self.options.pause_hold is not None
                    and self.verified("start")
                    and "pause" not in self.pending
                    and "stop" not in self.pending
                ):
                    self.submit("pause")
                if self.verified("pause") and pause_observed is None:
                    pause_observed = self.now()
                if (
                    pause_observed is not None
                    and self.options.pause_hold is not None
                    and self.now() - pause_observed >= self.options.pause_hold
                    and "resume" not in self.pending
                    and "stop" not in self.pending
                ):
                    self.submit("resume")
                if (
                    self.options.mode == "start"
                    and all(item["finished_at_ms"] is not None for item in self.summary["items"])
                    and playback.get("release_confirmed") is True
                ):
                    self.summary["stop_reason"] = "queue_finished_and_released"
                    break
                if (
                    self.options.mode == "start"
                    and self.spec.mode == QueueMode.NATIVE
                    and all(item["finished_at_ms"] is not None for item in self.summary["items"])
                ):
                    # Completion ends collection; opted-in cleanup remains a
                    # separate explicit STOP and cannot manufacture a finish.
                    self.summary["stop_reason"] = "queue_finished"
                    break
                self.wait(min(self.options.poll_seconds, max(0, deadline - self.now())))
        except (Exception, KeyboardInterrupt) as exc:
            self.summary["stop_reason"] = (
                "interrupted" if isinstance(exc, KeyboardInterrupt) else "error"
            )
            self.summary["error_type"] = type(exc).__name__
        finally:
            if started and self.options.cleanup_stop:
                self.cleanup()
            self.summary["elapsed_ms"] = self.elapsed()
            self.journal.write("summary", summary=self.summary)
        return self.summary

    def cleanup(self) -> None:
        self.summary["cleanup"] = "unconfirmed"
        try:
            # A completed queue with observed release is already clean; do not add a stop.
            if self.summary["stop_reason"] == "queue_finished_and_released":
                self.summary["cleanup"] = "release_observed"
                return
            if "stop" in self.attempted and "stop" not in self.pending:
                self.summary["cleanup"] = "uncertain_stop_submission"
                return
            if "stop" not in self.pending:
                self.submit("stop")
            deadline = self.now() + self.options.cleanup_seconds
            while self.now() < deadline:
                self.sample()
                if self.verified("stop"):
                    self.summary["cleanup"] = "stop_observed"
                    return
                self.wait(min(self.options.poll_seconds, max(0, deadline - self.now())))
        except (Exception, KeyboardInterrupt) as exc:
            self.summary["cleanup"] = "error"
            self.journal.write("cleanup_error", error_type=type(exc).__name__)


def run_service_checkpoint(
    client: CheckpointClient,
    spec: QueueSpec,
    options: ServiceCheckpointOptions,
    journal: EvidenceJournal,
    *,
    code_revision: str | None = None,
    now: Callable[[], float] = monotonic,
    wait: Callable[[float], None] = sleep,
) -> ServiceCheckpointSummary:
    """Timing is submission-to-client-observation, never reconstructed receiver latency.

    ``trace`` calls status only. ``start`` requires an exact pending manifest and
    explicit cleanup_stop to send stop on exit. This function never shuts down,
    restarts or kills the foreground owner and never retries an uncertain effect.
    """
    if code_revision is not None and fullmatch(r"[0-9a-fA-F]{7,40}", code_revision) is None:
        raise ValueError("Code revision must be a Git commit identifier.")
    return _Checkpoint(client, spec, options, journal, now, wait, code_revision).run()
