"""Snapshot recovery, wire validation and compare-and-swap failure injection."""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from tellyq.domain.ports import RevisionConflict
from tellyq.domain.values import (
    ContentKind,
    ContentRef,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    SessionSnapshot,
)
from tellyq.session_store import JsonSessionStore, decode_snapshot, encode_snapshot
from tests.playback_support import FakeClock


@pytest.fixture
def snapshot():
    request = PlaybackRequest(
        "request",
        "attempt",
        "item",
        ContentRef("p", "id", ContentKind.EPISODE),
        PlaybackTarget("device", "route"),
    )
    return SessionSnapshot(
        PlaybackScope(request, "session", "generation", 99, "app"),
        revision=2,
        state=PlayerState.PLAYING,
        has_confirmed_playback=True,
        stop_requested=True,
        stop_boundary=100,
    )


def test_restart_recovers_identity_but_never_monotonic_evidence(tmp_path, snapshot):
    clock = FakeClock()
    store = JsonSessionStore(tmp_path, clock)
    store.save(snapshot, expected_revision=None)
    assert store.load("attempt") == snapshot
    record = encode_snapshot(snapshot)
    assert (
        not {"latest", "monotonic", "progress_anchor", "connection_generation", "stop_boundary"}
        & record.keys()
    )
    recovered = JsonSessionStore(tmp_path, clock).current()
    assert recovered is not None and recovered.scope.request == snapshot.scope.request
    assert recovered.scope.request.content.kind == ContentKind.EPISODE
    assert recovered.scope.connection_generation != snapshot.scope.connection_generation
    assert recovered.latest is None and recovered.progress_anchor is None
    assert recovered.state == PlayerState.UNKNOWN and not recovered.has_confirmed_playback
    assert recovered.stop_requested and recovered.stop_boundary is None
    for path in (tmp_path / "sessions").glob("*.json"):
        assert path.stat().st_mode & 0o777 == 0o600


def test_revision_conflict_preserves_newer_stop_intent(tmp_path, snapshot):
    clock = FakeClock()
    first, second = JsonSessionStore(tmp_path, clock), JsonSessionStore(tmp_path, clock)
    first.save(snapshot, expected_revision=None)
    second.save(replace(snapshot, revision=3), expected_revision=2)
    with pytest.raises(RevisionConflict):
        first.save(replace(snapshot, revision=4, stop_requested=False), expected_revision=2)
    latest = second.load("attempt")
    assert latest is not None and latest.stop_requested
    with pytest.raises(RevisionConflict):
        first.save(snapshot, expected_revision=3)


def test_invalid_version_fields_and_types_fail_closed(snapshot):
    for changes in (
        {"schema_version": 2},
        {"schema_version": True},
        {"revision": True},
        {"revision": -1},
        {"stop_requested": 1},
        {"content_kind": "unknown"},
        {"session_id": ""},
        {"provider": None},
        {"application_id": 4},
        {"historical_state": "not-a-state"},
        {"unexpected": "field"},
    ):
        with pytest.raises(ValueError):
            decode_snapshot({**encode_snapshot(snapshot), **changes}, FakeClock())


def test_atomic_write_failure_keeps_old_revision(tmp_path, snapshot):
    store = JsonSessionStore(tmp_path, FakeClock())
    store.save(snapshot, expected_revision=None)
    with patch("tellyq.state.os.fsync", side_effect=OSError("disk full")), pytest.raises(OSError):
        store.save(replace(snapshot, revision=3), expected_revision=2)
    assert store.load("attempt") == snapshot
    assert not list((tmp_path / "sessions").glob("*.tmp"))


def test_active_reference_validation_and_path_safety(tmp_path, snapshot):
    store = JsonSessionStore(tmp_path, FakeClock())
    assert store.current() is None
    snapshot = replace(
        snapshot,
        scope=replace(
            snapshot.scope, request=replace(snapshot.scope.request, attempt_id="../../unsafe")
        ),
    )
    store.save(snapshot, expected_revision=None)
    assert store.current() == snapshot
    active = tmp_path / "sessions" / "active.json"
    active.write_text(json.dumps({"schema_version": 1, "attempt_id": "missing"}))
    with pytest.raises(ValueError, match="missing"):
        store.current()
    active.write_text('{"schema_version": true, "attempt_id": "missing"}')
    with pytest.raises(ValueError, match="Invalid"):
        store.current()
