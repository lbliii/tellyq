"""Immutable application values; wire decoding belongs to an adapter.

All monotonic instants in one scope come from the same process clock. They must
never be restored as comparable instants after a process or connection restart.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import isfinite


def require_instant(value: float, name: str) -> None:
    """Reject Python bools and invalid numbers even at a typed public boundary."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} must be a finite number")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("record timestamps must include a timezone")


class ContentKind(StrEnum):
    VIDEO = "video"
    EPISODE = "episode"


@dataclass(frozen=True, slots=True)
class ContentRef:
    provider: str
    content_id: str
    kind: ContentKind = ContentKind.VIDEO
    title: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not self.provider or not self.content_id:
            raise ValueError("content requires a provider and opaque content ID")


@dataclass(frozen=True, slots=True)
class PlaybackTarget:
    device_id: str
    route: str
    name: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not self.device_id or not self.route:
            raise ValueError("target requires a stable device ID and adapter route")


@dataclass(frozen=True, slots=True)
class PlaybackRequest:
    request_id: str
    attempt_id: str
    queue_item_id: str
    content: ContentRef
    target: PlaybackTarget

    def __post_init__(self) -> None:
        if not self.request_id or not self.attempt_id or not self.queue_item_id:
            raise ValueError("request, attempt and queue item IDs are required")


@dataclass(frozen=True, slots=True)
class PlaybackScope:
    """Explicit ownership established by the caller, never by a command receipt."""

    request: PlaybackRequest
    session_id: str
    connection_generation: str
    started_monotonic: float
    application_id: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id or not self.connection_generation:
            raise ValueError("scope requires a session and connection generation")
        require_instant(self.started_monotonic, "start boundary")


class Support(StrEnum):
    UNKNOWN = "unknown"
    VERIFIED = "verified"
    ADVERTISED = "advertised"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    support: Support = Support.UNKNOWN
    source: str | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.support != Support.UNKNOWN and (not self.source or self.observed_at is None):
            raise ValueError("known capability support requires dated provenance")
        if self.observed_at is not None:
            _require_aware(self.observed_at)


@dataclass(frozen=True, slots=True)
class PlaybackCapabilities:
    exact_launch: CapabilityEvidence = CapabilityEvidence()
    stop: CapabilityEvidence = CapabilityEvidence()
    identity: CapabilityEvidence = CapabilityEvidence()
    progress: CapabilityEvidence = CapabilityEvidence()
    completion: CapabilityEvidence = CapabilityEvidence()
    display: CapabilityEvidence = CapabilityEvidence()
    pause: CapabilityEvidence = CapabilityEvidence()
    resume: CapabilityEvidence = CapabilityEvidence()


class ErrorCode(StrEnum):
    DEVICE_UNAVAILABLE = "device_unavailable"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    SESSION_REPLACED = "session_replaced"
    COMMAND_OUTCOME_UNKNOWN = "command_outcome_unknown"
    CONTROL_REJECTED = "control_rejected"


@dataclass(frozen=True, slots=True)
class PlaybackError:
    code: ErrorCode
    message: str
    operation_id: str
    uncertain: bool
    recovery: str


class CommandAction(StrEnum):
    START = "start"
    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"


class CommandOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ControlStage(StrEnum):
    APPLICATION = "application"
    BACKEND = "backend"
    TRANSPORT_GUARD = "transport_guard"
    TRANSPORT = "transport"
    RECEIVER = "receiver"


class ControlReason(StrEnum):
    OWNERSHIP_UNVERIFIED = "ownership_unverified"
    CONNECTION_CHANGED = "connection_changed"
    IDENTITY_CHANGED = "identity_changed"
    MEDIA_UNAVAILABLE = "media_unavailable"
    STALE_OBSERVATION = "stale_observation"
    STATE_MISMATCH = "state_mismatch"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    READ_ONLY = "read_only"
    ACTION_UNSUPPORTED = "action_unsupported"
    DESTINATION_UNAVAILABLE = "destination_unavailable"
    INVALID_MEDIA_ID = "invalid_media_id"
    MEDIA_STATUS = "media_status"
    INVALID_REQUEST = "invalid_request"
    INVALID_PLAYER_STATE = "invalid_player_state"
    RESPONSE_UNKNOWN = "response_unknown"
    RESPONSE_TIMEOUT = "response_timeout"
    MESSAGE_NOT_SENT = "message_not_sent"
    TRANSPORT_EXCEPTION = "transport_exception"
    LEGACY_RESULT = "legacy_result"


@dataclass(frozen=True, slots=True)
class ControlDiagnostic:
    """Fixed provenance labels only; never wire data or exception messages."""

    stage: ControlStage
    reason: ControlReason


@dataclass(frozen=True, slots=True)
class ControlResponse:
    outcome: CommandOutcome
    diagnostic: ControlDiagnostic


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    request_id: str
    attempt_id: str
    action: CommandAction
    outcome: CommandOutcome
    recorded_at: datetime
    error: PlaybackError | None = None
    requested_at: datetime | None = None
    diagnostic: ControlDiagnostic | None = None

    def __post_init__(self) -> None:
        _require_aware(self.recorded_at)
        if self.requested_at is not None:
            _require_aware(self.requested_at)


class PlayerState(StrEnum):
    UNKNOWN = "unknown"
    IDLE = "idle"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"
    ENDED = "ended"
    STOPPED = "stopped"


class IdleReason(StrEnum):
    FINISHED = "finished"
    CANCELED = "canceled"
    ERROR = "error"


class IdentityUpdate(StrEnum):
    UNKNOWN = "unknown"
    EXPLICIT = "explicit"
    OMITTED = "omitted"


class ContentPhase(StrEnum):
    UNKNOWN = "unknown"
    PLAYING = "playing"
    PAUSED = "paused"
    BUFFERING = "buffering"
    FINISHED = "finished"
    AD = "ad"


@dataclass(frozen=True, slots=True)
class ProviderEvidence:
    """Source-qualified interpretation supplied by an adapter, never a cached identity."""

    source: str
    phase: ContentPhase

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("provider evidence requires a source")


class CompletionAttribution(StrEnum):
    EXPLICIT = "explicit"
    ORDERED_HISTORY = "ordered_history"


@dataclass(frozen=True, slots=True)
class CompletionEvidence:
    """Historical terminal witness; independent of current ownership/telemetry freshness."""

    observed_at: datetime
    monotonic: float
    sequence: int
    identity_sequence: int
    attribution: CompletionAttribution
    source: str


@dataclass(frozen=True, slots=True)
class PlaybackObservation:
    target: PlaybackTarget
    connection_generation: str
    sequence: int
    observed_at: datetime
    monotonic: float
    session_id: str | None = None
    content: ContentRef | None = None
    state: PlayerState = PlayerState.UNKNOWN
    position: float | None = None
    duration: float | None = None
    ad_active: bool | None = None
    idle_reason: IdleReason | None = None
    source: str = "receiver"
    application_id: str | None = None
    playback_id: str | None = None
    session_active: bool | None = None
    connection_reset: bool = False
    pause_supported: bool | None = None
    identity_update: IdentityUpdate = IdentityUpdate.UNKNOWN
    provider_evidence: ProviderEvidence | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at)
        require_instant(self.monotonic, "observation time")
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ValueError("sequence must be a non-negative integer")
        if not self.connection_generation or not self.source:
            raise ValueError("observation generation and source are required")
        for name, value in (("position", self.position), ("duration", self.duration)):
            if value is not None:
                require_instant(value, name)
                if value < 0:
                    raise ValueError(f"{name} must be non-negative")
        if self.ad_active is not None and not isinstance(self.ad_active, bool):
            raise ValueError("ad state must be true, false or unknown")
        if self.pause_supported is not None and not isinstance(self.pause_supported, bool):
            raise ValueError("pause support must be true, false or unknown")
        if self.session_active is False and (
            self.session_id is not None
            or self.application_id is not None
            or self.content is not None
        ):
            raise ValueError("an explicit session exit cannot also identify an active session")
        if self.state in (PlayerState.ENDED, PlayerState.STOPPED):
            raise ValueError("ended and stopped are policy conclusions, not raw observations")


@dataclass(frozen=True, slots=True)
class DisplayEvidence:
    """Historical display evidence, independent of receiver playback evidence."""

    target: PlaybackTarget
    content: ContentRef
    confirmed: bool
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        _require_aware(self.observed_at)
        if not self.source:
            raise ValueError("display evidence requires a source")


class EvidenceReason(StrEnum):
    AWAITING_OBSERVATION = "awaiting_observation"
    STALE = "stale"
    IDENTITY_UNKNOWN = "identity_unknown"
    SESSION_REPLACED = "session_replaced"
    AD_ACTIVE = "ad_active"
    AD_UNKNOWN = "ad_unknown"
    NOT_PLAYING = "not_playing"
    POSITION_UNKNOWN = "position_unknown"
    AWAITING_PROGRESS = "awaiting_progress"
    PROGRESS_CONFIRMED = "progress_confirmed"
    NATURAL_COMPLETION = "natural_completion"
    STOP_OBSERVED = "stop_observed"


@dataclass(frozen=True, slots=True)
class PlaybackEvidence:
    identity_confirmed: bool = False
    receiver_playback_confirmed: bool = False
    natural_completion_confirmed: bool = False
    reason: EvidenceReason = EvidenceReason.AWAITING_OBSERVATION


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    scope: PlaybackScope
    revision: int = 0
    state: PlayerState = PlayerState.UNKNOWN
    evidence: PlaybackEvidence = PlaybackEvidence()
    receipt: CommandReceipt | None = None
    latest: PlaybackObservation | None = None
    progress_anchor: PlaybackObservation | None = None
    has_confirmed_playback: bool = False
    completion_anchor: PlaybackObservation | None = None
    completion: CompletionEvidence | None = None
    stop_requested: bool = False
    stop_boundary: float | None = None
    last_sequence: int = -1
    last_monotonic: float | None = None
    ownership_lost: bool = False
    display: DisplayEvidence | None = None


@dataclass(frozen=True, slots=True)
class EvidencePolicy:
    max_age_seconds: float = 5.0
    min_progress_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        for name, value in (
            ("maximum observation age", self.max_age_seconds),
            ("minimum progress interval", self.min_progress_interval_seconds),
        ):
            require_instant(value, name)
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.min_progress_interval_seconds > self.max_age_seconds:
            raise ValueError("progress interval must fit inside the freshness window")
