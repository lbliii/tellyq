"""Optional native queue effects, separate from playback/completion evidence.

The caller journals intent before dispatch and owns at-most-once dispatch and the
one-approved-successor limit. An accepted successor can continue on the receiver
while this process is disconnected; a local observation hold cannot stop it.
Reconnect requires observation and reconciliation, never automatic redispatch.
These are in-process values, not a persisted or JSON contract.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .values import (
    CapabilityEvidence,
    CommandOutcome,
    ContentRef,
    ControlStage,
    PlaybackScope,
    PlaybackTarget,
)


class NativeQueueAction(StrEnum):
    PLAY_NEXT = "play_next"
    CLEAR = "clear"


class NativeQueueReason(StrEnum):
    READ_ONLY = "read_only"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    PROVIDER_MISMATCH = "provider_mismatch"
    CONNECTION_CHANGED = "connection_changed"
    IDENTITY_CHANGED = "identity_changed"
    MEDIA_UNAVAILABLE = "media_unavailable"
    STALE_OBSERVATION = "stale_observation"
    STATE_MISMATCH = "state_mismatch"
    AD_STATE_UNQUALIFIED = "ad_state_unqualified"
    SESSION_UNINITIALIZED = "session_uninitialized"
    COMMAND_RETURNED = "command_returned"
    TRANSPORT_EXCEPTION = "transport_exception"
    RESPONSE_UNKNOWN = "response_unknown"


@dataclass(frozen=True, slots=True)
class NativeQueueRequest:
    operation_id: str
    action: NativeQueueAction
    scope: PlaybackScope
    playback_id: str
    successor: ContentRef | None = None

    def __post_init__(self) -> None:
        if not self.operation_id or not self.playback_id:
            raise ValueError("native queue operation and current playback IDs are required")
        if not isinstance(self.action, NativeQueueAction):
            raise ValueError("native queue action must be explicit")
        if (self.action == NativeQueueAction.PLAY_NEXT) != (self.successor is not None):
            raise ValueError("play_next requires an exact successor; clear has no successor")


@dataclass(frozen=True, slots=True)
class NativeQueueDiagnostic:
    """Fixed provenance only; never pairing material, wire data or exception text."""

    stage: ControlStage
    reason: NativeQueueReason


@dataclass(frozen=True, slots=True)
class NativeQueueResponse:
    outcome: CommandOutcome
    diagnostic: NativeQueueDiagnostic


@dataclass(frozen=True, slots=True)
class NativeQueueReceipt:
    """Dispatch result only, with no queue membership, cancellation or playback proof."""

    request: NativeQueueRequest
    outcome: CommandOutcome
    recorded_at: datetime
    diagnostic: NativeQueueDiagnostic

    def __post_init__(self) -> None:
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("native queue receipt timestamp must include a timezone")


@dataclass(frozen=True, slots=True)
class NativeQueueCapabilities:
    play_next: CapabilityEvidence = field(default_factory=CapabilityEvidence)
    clear: CapabilityEvidence = field(default_factory=CapabilityEvidence)


@runtime_checkable
class NativeQueueBackend(Protocol):
    """Optional port; ordinary playback backends need not support native queues."""

    def native_queue_capabilities(self, target: PlaybackTarget) -> NativeQueueCapabilities: ...

    def native_queue(self, request: NativeQueueRequest) -> NativeQueueReceipt:
        """Attempt one guarded mutation, without launch, observation or automatic retry.

        Caller ownership includes authority over the entire receiver queue for
        CLEAR. A successful clear command does not establish an empty queue or
        stop playback. UNKNOWN must be reconciled, never automatically retried.
        """
        ...
