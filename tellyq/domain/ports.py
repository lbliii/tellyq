"""Small structural contracts; implementations own I/O and thread safety."""

from datetime import datetime
from typing import Protocol, runtime_checkable

from .values import (
    CommandReceipt,
    PlaybackCapabilities,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    SessionSnapshot,
)


class Clock(Protocol):
    def utcnow(self) -> datetime:
        """Return a timezone-aware UTC record timestamp."""
        ...

    def monotonic(self) -> float:
        """Return a process-local monotonic instant, never a persisted deadline."""
        ...


class PlaybackBackend(Protocol):
    def capabilities(self, target: PlaybackTarget) -> PlaybackCapabilities: ...

    def start(self, request: PlaybackRequest) -> CommandReceipt:
        """Bound the remote operation; returning accepted is not playback proof."""
        ...

    def observe(self, target: PlaybackTarget) -> tuple[PlaybackObservation, ...]:
        """Return detached observations without starting or resuming playback.

        This requires no known session: callers inspect baseline/post-command
        identity through this port, then reconcile ownership to create a scope.
        """
        ...

    def stop(self, scope: PlaybackScope) -> CommandReceipt:
        """Stop only the caller's still-owned session; refuse a replacement."""
        ...


@runtime_checkable
class PlaybackControls(Protocol):
    """Optional controls, scoped to explicit media identity; never launch content."""

    def pause(self, scope: PlaybackScope, playback_id: str) -> CommandReceipt: ...

    def resume(self, scope: PlaybackScope, playback_id: str) -> CommandReceipt: ...


class RevisionConflict(Exception):
    """The stored snapshot differs from the caller's expected revision."""


class SessionStore(Protocol):
    def load(self, attempt_id: str) -> SessionSnapshot | None: ...

    def save(self, snapshot: SessionSnapshot, *, expected_revision: int | None) -> None:
        """Atomically compare and save, or raise RevisionConflict without writing.

        None requires that no record exists. Otherwise the stored revision must
        match and the incoming revision must be larger. This port is an in-process
        snapshot contract, not a wire schema or permission to reuse persisted
        monotonic evidence after restarting. Recovery must create a fresh scope.
        """
        ...
