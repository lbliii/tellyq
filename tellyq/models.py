"""Typed records for the current JSON interface; no device or framework dependencies.

Report version 1 retains the original CLI fields and adds explicit evidence
reasons. Snapshot records store durable identity, never process-local evidence.
"""

from typing import Literal, NotRequired, Required, TypedDict

type WireFieldShape = Literal["absent", "null", "valid", "invalid", "unavailable"]


class MediaWireDiagnostics(TypedDict, total=False):
    """Fixed field-shape allowlist; never arbitrary keys, values or prior status."""

    wire_status_count: int | None
    wire_media: WireFieldShape
    wire_media_content_id: WireFieldShape
    wire_extended_status: WireFieldShape
    wire_extended_media: WireFieldShape
    wire_extended_content_id: WireFieldShape
    wire_break_status: WireFieldShape
    wire_break_id: WireFieldShape
    wire_break_clip_id: WireFieldShape
    wire_break_time: WireFieldShape
    wire_break_clip_time: WireFieldShape
    wire_status_custom_data: WireFieldShape
    wire_media_custom_data: WireFieldShape
    wire_extended_media_custom_data: WireFieldShape
    wire_media_breaks: WireFieldShape
    wire_media_break_clips: WireFieldShape
    wire_current_item_id: WireFieldShape
    wire_loading_item_id: WireFieldShape
    wire_preloaded_item_id: WireFieldShape


class Device(TypedDict):
    uuid: str
    name: str | None
    model: NotRequired[str | None]
    type: NotRequired[str | None]


class Observation(MediaWireDiagnostics, total=False):
    kind: Required[str]
    playback_id: str | None
    sequence: int
    connection_generation: str
    source: str
    observed_at: str
    monotonic: float
    empty_status: bool | None
    custom_player_state: int | None
    custom_player_state_shape: WireFieldShape
    identity_update: str
    provider_phase: str | None
    provider_source: str | None
    media_session_id: int | None
    content_id: str | None
    title: str | None
    player_state: str | None
    position: float | None
    duration: float | None
    idle_reason: str | None
    ad_break: bool | None
    pause_supported: bool | None
    app_id: str | None
    app_name: str | None
    app_session_id: str | None
    active_input: bool | None
    standby: bool | None
    type: str
    reason: str | None
    code: int | None


class Evidence(TypedDict):
    receiver_playback_confirmed: bool
    identity_observed: NotRequired[bool]
    playing_observed: NotRequired[bool]
    advancing_position_observed: NotRequired[bool]
    visual_confirmation: NotRequired[bool | None]
    natural_completion_confirmed: NotRequired[bool]
    reason: NotRequired[str]
    completion: NotRequired[CompletionRecord]


class CompletionRecord(TypedDict):
    historical: Literal[True]
    attribution: str
    source: str
    sequence: int
    identity_sequence: int
    observed_at: str


class CompletionFields(TypedDict, total=False):
    completion: CompletionRecord


class ControlDiagnostic(TypedDict):
    stage: str
    reason: str


class CommandReceipt(TypedDict):
    action: str
    requested_at: str
    recorded_at: NotRequired[str]
    returned: bool
    outcome: NotRequired[str]
    diagnostic: NotRequired[ControlDiagnostic]


class QueueItem(TypedDict):
    id: str
    service: str
    content_id: str
    title: str
    url: str
    state: str


class QueueDocument(TypedDict):
    schema_version: int
    device_id: str
    updated_at: str
    items: list[QueueItem]
    last_report: NotRequired[str]


class Error(TypedDict):
    type: str
    message: str
    diagnostic: NotRequired[ControlDiagnostic]


class Report(TypedDict, total=False):
    schema_version: int
    observed_state: str
    attempt_id: str
    capabilities: dict[str, str]
    command: str
    started_at: str
    ended_at: str
    python: str
    gil_enabled: bool
    commands: list[CommandReceipt]
    observations: list[Observation]
    devices: list[Device]
    device: Device
    queue: QueueDocument
    state: str
    requested_content_id: str
    evidence: Evidence
    control_observed: bool
    stop_confirmed: bool
    stop_verification_source: str | None
    error: Error
    report_path: str


class QueueManifestItemJSON(TypedDict):
    item_id: str
    provider: Literal["youtube"]
    content_id: str
    kind: NotRequired[Literal["video"]]
    title: NotRequired[str | None]


class QueueManifestTargetJSON(TypedDict):
    device_id: str
    route: Literal["cast"]
    name: NotRequired[str | None]


class QueueManifestJSON(TypedDict):
    schema_version: Literal[1]
    queue_id: str
    target: QueueManifestTargetJSON
    items: list[QueueManifestItemJSON]


class ServiceReport(TypedDict):
    schema_version: Literal[1]
    event: Literal["initializing", "ready", "stopping", "closed"]
    snapshot: IPCRunnerSnapshot


class SnapshotRecord(TypedDict):
    """Durable identity and history only; monotonic evidence is deliberately absent."""

    schema_version: int
    revision: int
    request_id: str
    attempt_id: str
    queue_item_id: str
    provider: str
    content_kind: str
    content_id: str
    title: str | None
    device_id: str
    route: str
    name: str | None
    session_id: str
    application_id: str | None
    historical_state: str
    stop_requested: bool


class LifecycleRecord(Report):
    """Incremental normalized lifecycle journal; passive attachment is never a full run."""

    kind: Literal["begin", "window", "gap", "end"]
    capture_id: str
    provenance: Literal["live", "synthetic", "sanitized-live"]
    sequence: int
    recorded_at: str
    monotonic: float
    mode: Literal["passive"]
    entire_run_observed: Literal[False]
    external_control_unobserved: Literal[True]
    attached: NotRequired[bool]
    partial: NotRequired[bool]
    gap_seconds: NotRequired[float]
    gap_kind: NotRequired[Literal["transport", "media"]]
    stop_reason: NotRequired[Literal["deadline", "interrupted", "backend_error", "cancelled"]]


class LifecycleInspection(TypedDict):
    """Identity-free counts for diagnostic review, never completion authorization."""

    schema_version: Literal[1]
    provenance: Literal["synthetic", "sanitized-live"]
    end_record_present: bool
    stop_reason: Literal["deadline", "interrupted", "backend_error", "cancelled"] | None
    windows: int
    partial_windows: int
    media_observations: int
    wire_diagnostic_observations: int
    wire_fields: dict[str, dict[WireFieldShape, int]]
    terminal_candidates: int
    partial_terminal_candidates: int
    terminal_content: dict[Literal["requested", "other", "unknown"], int]
    terminal_ad: dict[Literal["active", "inactive", "unknown"], int]
    terminal_wire_diagnostics: int
    terminal_wire_fields: dict[str, dict[WireFieldShape, int]]
    new_hardware_evidence: Literal[False]


type MetadataValueType = Literal[
    "object", "array", "string", "boolean", "number", "null", "invalid"
]


class MetadataStructure(TypedDict):
    """Fixed-key structural summary; arbitrary provider names/values never cross it."""

    shape: WireFieldShape
    entries: int | None
    inspected_nodes: int
    maximum_depth: int
    truncated: bool
    types: dict[MetadataValueType, int]


class YouTubeMetadataSample(TypedDict):
    sequence: int
    observed_at: str
    monotonic: float
    status_count: int | None
    first_status_only: Literal[True]
    content_relation: Literal["requested", "other", "unknown"]
    media_session_relation: Literal["first", "same", "changed", "unknown"]
    player_state: str | None
    custom_player_state: int | None
    custom_player_state_shape: WireFieldShape
    idle_reason: str | None
    position: float | None
    duration: float | None
    ad_break: bool | None
    custom_data: dict[Literal["status", "media", "extended_media"], MetadataStructure]


class PrivateMetadataField(TypedDict):
    """Untrusted names for private runtime inspection only; never a public export."""

    path: list[str]
    value_type: MetadataValueType
    entries: int | None


class PrivateMetadataSchema(TypedDict):
    sequence: int
    fields: dict[Literal["status", "media", "extended_media"], list[PrivateMetadataField]]
    truncated: bool


class YouTubeMetadataRecord(TypedDict, total=False):
    schema_version: Required[Literal[1]]
    kind: Required[Literal["begin", "sample", "end"]]
    observed_at: Required[str]
    read_only: Required[Literal[True]]
    completion_authorized: Required[Literal[False]]
    sample: YouTubeMetadataSample
    private_schema: PrivateMetadataSchema
    stop_reason: Literal["deadline", "interrupted", "backend_error"]
    samples: int
    dropped_samples: int
    seconds: float


# Version 1 private Unix-domain owner protocol. No device effect follows from ok alone.
type IPCValue = str | int | float | bool | list[IPCValue] | dict[str, IPCValue] | None
type IPCAction = Literal["start", "stop", "pause", "resume"]
type IPCCode = Literal[
    "status",
    "accepted",
    "ticket",
    "shutdown",
    "shutdown_timeout",
    "invalid_request",
    "command_conflict",
    "command_rejected",
    "runner_unavailable",
    "mailbox_full",
    "history_full",
    "ticket_missing",
    "server_busy",
    "response_too_large",
    "internal_error",
]


class IPCContent(TypedDict):
    provider: str
    content_id: str
    kind: Literal["video", "episode"]
    title: str | None


class IPCTarget(TypedDict):
    device_id: str
    route: str
    name: str | None


class IPCPlaybackRequest(TypedDict):
    request_id: str
    attempt_id: str
    queue_item_id: str
    content: IPCContent
    target: IPCTarget


class IPCCommand(TypedDict):
    command_id: str
    action: IPCAction
    request: IPCPlaybackRequest | None


class IPCRequest(TypedDict):
    schema_version: Literal[1]
    operation: Literal["status", "submit", "ticket", "shutdown"]
    command: NotRequired[IPCCommand]
    command_id: NotRequired[str]
    timeout: NotRequired[float]


class IPCHandoffEvidence(TypedDict):
    sequence: int
    age_seconds: float | None
    since_terminal_seconds: float | None
    sequence_delta: int | None
    observation_delta_seconds: float | None
    terminal_sequence: int | None
    terminal_position: float | None
    state: str
    idle_reason: str | None
    position: float | None
    ad_active: bool | None
    provider_phase: str | None
    identity_update: str
    session_active: bool | None
    connection_reset: bool
    target_matches: bool
    connection_matches: bool
    content_matches: bool | None
    session_matches: bool | None
    application_matches: bool | None
    media_matches: bool | None


class IPCHandoffVeto(TypedDict):
    reason: str
    evidence: IPCHandoffEvidence


class IPCHandoffDiagnostic(TypedDict):
    stage: str
    disposition: str
    reason: str
    evidence: IPCHandoffEvidence | None
    first_veto: IPCHandoffVeto | None
    queue_reason: str | None


class IPCTaskView(TypedDict):
    """Explicit read-only projections; nested values are JSON, never hydrated objects."""

    playback: dict[str, IPCValue] | None
    queue: dict[str, IPCValue] | None
    handoff: NotRequired[IPCHandoffDiagnostic | None]


class IPCTicket(TypedDict):
    command_id: str
    action: IPCAction
    state: Literal["queued", "running", "handled", "canceled", "failed"]
    failure: str | None
    view: IPCTaskView | None


class IPCRunnerSnapshot(TypedDict):
    phase: Literal["new", "starting", "running", "stopping", "stopped", "failed"]
    revision: int
    owns_device: bool
    cancellation_requested: bool
    pending_commands: int
    active_command_id: str | None
    last_command_id: str | None
    view: IPCTaskView
    failure: str | None


class IPCResponse(TypedDict):
    schema_version: Literal[1]
    ok: bool
    code: IPCCode
    snapshot: NotRequired[IPCRunnerSnapshot]
    ticket: NotRequired[IPCTicket]
    ticket_id: NotRequired[str]


class MCPError(TypedDict):
    """Safe, machine-readable error returned by the optional MCP surface."""

    type: str
    message: str


class MCPResult(TypedDict):
    """The MCP tool result mirrors the foreground owner's IPC response."""

    schema_version: Literal[1]
    ok: bool
    code: str
    response: NotRequired[IPCResponse]
    error: NotRequired[MCPError]


class ServiceCheckpointTiming(TypedDict):
    action: IPCAction
    submitted_ms: float
    acknowledgement_ms: float | None
    ticket_completed_ms: float | None
    first_verified_state_ms: float | None
    ticket_state: str | None
    accepted: bool


class ServiceCheckpointItem(TypedDict):
    index: int
    verified_at_ms: float | None
    finished_at_ms: float | None
    distinct_attempts_observed: int


class ServiceCheckpointSummary(TypedDict):
    schema_version: Literal[1]
    mode: Literal["trace", "start"]
    code_revision: str | None
    elapsed_ms: float
    poll_seconds: float
    clock_resolution_seconds: float
    timing_origin: Literal["client_monotonic"]
    handoff_diagnostics_available: bool
    stop_reason: str
    error_type: str | None
    items: list[ServiceCheckpointItem]
    verified_handoffs: int
    commands: list[ServiceCheckpointTiming]
    cleanup: str
    visual_confirmation: None
    live_acceptance: Literal[False]
