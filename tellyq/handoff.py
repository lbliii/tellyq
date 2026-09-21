"""Process-local authority to release a completed session; never completion evidence."""

from dataclasses import dataclass, replace

from .domain.values import (
    ContentPhase,
    IdentityUpdate,
    IdleReason,
    PlaybackObservation,
    PlayerState,
    SessionSnapshot,
)


@dataclass(frozen=True, slots=True)
class ReleaseAuthority:
    terminal: PlaybackObservation
    last_sequence: int
    last_monotonic: float
    blocked: bool = False


def track_release(
    snapshot: SessionSnapshot,
    events: tuple[PlaybackObservation, ...],
    authority: ReleaseAuthority | None,
) -> ReleaseAuthority | None:
    """Retain the actual terminal and veto replay, gaps, ads or replacement permanently."""
    witness = snapshot.completion
    if witness is None:
        return None
    if authority is None:
        terminal = next(
            (
                event
                for event in events
                if event.sequence == witness.sequence and event.monotonic == witness.monotonic
            ),
            None,
        )
        if terminal is None:
            return None
        authority = ReleaseAuthority(terminal, terminal.sequence, terminal.monotonic)
    terminal = authority.terminal
    scope = snapshot.scope
    for event in events:
        if event.sequence <= terminal.sequence:
            continue
        if event.sequence <= authority.last_sequence:
            continue  # The same returned batch may be passed through two application layers.
        blocked = (
            authority.blocked
            or event.sequence != authority.last_sequence + 1
            or not 0 < event.monotonic - authority.last_monotonic <= 5
            or event.target != scope.request.target
            or event.connection_generation != scope.connection_generation
            or event.connection_reset
            or event.ad_active is True
            or (event.content is not None and event.content != scope.request.content)
            or (event.session_id is not None and event.session_id != scope.session_id)
            or (event.application_id is not None and event.application_id != scope.application_id)
            or (event.playback_id is not None and event.playback_id != terminal.playback_id)
            or event.state in {PlayerState.PLAYING, PlayerState.PAUSED}
            or (
                event.provider_evidence is not None
                and event.provider_evidence.phase
                in {ContentPhase.AD, ContentPhase.PLAYING, ContentPhase.PAUSED}
            )
            or (
                event.position is not None
                and terminal.position is not None
                and event.position < terminal.position
            )
        )
        authority = replace(
            authority, last_sequence=event.sequence, last_monotonic=event.monotonic, blocked=blocked
        )
    return authority


def may_release(
    snapshot: SessionSnapshot,
    authority: ReleaseAuthority | None,
    events: tuple[PlaybackObservation, ...],
    *,
    now: float,
) -> bool:
    """Require fresh explicit current identity; unknown provider state remains unknown."""
    if authority is None or authority.blocked or snapshot.completion is None:
        return False
    terminal, scope = authority.terminal, snapshot.scope
    witness = snapshot.completion
    if (
        snapshot.ownership_lost
        or snapshot.stop_requested
        or not 0 <= now - terminal.monotonic <= 5
        or terminal.sequence != witness.sequence
        or terminal.monotonic != witness.monotonic
        or terminal.playback_id is None
        or terminal.ad_active is not False
        or terminal.state != PlayerState.IDLE
        or terminal.idle_reason != IdleReason.FINISHED
        or terminal.target != scope.request.target
        or terminal.connection_generation != scope.connection_generation
        or terminal.session_id != scope.session_id
        or terminal.application_id != scope.application_id
        or terminal.content not in (None, scope.request.content)
    ):
        return False
    receiver = next((event for event in reversed(events) if event.session_active is not None), None)
    media = next((event for event in reversed(events) if event.session_active is None), None)
    if receiver is None or media is None:
        return False
    return (
        receiver.session_active is True
        and receiver.session_id == scope.session_id
        and receiver.application_id == scope.application_id
        and receiver.target == media.target == scope.request.target
        and receiver.connection_generation
        == media.connection_generation
        == scope.connection_generation
        and 0 <= now - receiver.monotonic <= 5
        and 0 <= now - media.monotonic <= 5
        and media.content == scope.request.content
        and media.identity_update == IdentityUpdate.EXPLICIT
        and media.session_id == scope.session_id
        and media.application_id == scope.application_id
        and media.playback_id == terminal.playback_id
        and media.ad_active is not True
        and media.state in {PlayerState.IDLE, PlayerState.BUFFERING}
        and (media.state != PlayerState.IDLE or media.idle_reason == IdleReason.FINISHED)
        and (
            media.provider_evidence is None
            or media.provider_evidence.phase
            in {ContentPhase.UNKNOWN, ContentPhase.BUFFERING, ContentPhase.FINISHED}
        )
    )
