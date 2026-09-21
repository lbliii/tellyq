"""Typed records for the current JSON interface; no device or framework dependencies.

Report version 1 retains the original CLI fields and adds explicit evidence
reasons. Snapshot records store durable identity, never process-local evidence.
"""

from typing import NotRequired, Required, TypedDict


class Device(TypedDict):
    uuid: str
    name: str | None
    model: NotRequired[str | None]
    type: NotRequired[str | None]


class Observation(TypedDict, total=False):
    kind: Required[str]
    playback_id: str | None
    sequence: int
    connection_generation: str
    source: str
    observed_at: str
    monotonic: float
    empty_status: bool | None
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


class CommandReceipt(TypedDict):
    action: str
    requested_at: str
    recorded_at: NotRequired[str]
    returned: bool
    outcome: NotRequired[str]


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
