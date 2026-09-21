"""Passive successor scopes use actual ordered evidence, never command receipts."""

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tellyq.cast_backend import CastBackend
from tellyq.domain import policy
from tellyq.domain.values import (
    CompletionAttribution,
    CompletionEvidence,
    ContentKind,
    ContentPhase,
    ContentRef,
    EvidenceReason,
    IdentityUpdate,
    IdleReason,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    ProviderEvidence,
    SessionSnapshot,
)
from tellyq.lifecycle import _ReplayClock, _ReplayTransport
from tellyq.native_observation import (
    NativeObservationRefusal,
    NativePlaybackTracker,
    NativeSuccessorAuthorization,
    NativeSuccessorTracker,
)
from tellyq.youtube_state import YOUTUBE_APPLICATION_ID, YOUTUBE_STATE_SOURCE

AT = datetime(2000, 1, 1, tzinfo=UTC)
TARGET = PlaybackTarget("synthetic-device", "cast")
A, B, C = (ContentRef("youtube", item) for item in ("video-a", "video-b", "video-c"))
REQUEST_A = PlaybackRequest("request-a", "attempt-a", "item-a", A, TARGET)
REQUEST_B = PlaybackRequest("reservation-b", "attempt-b", "item-b", B, TARGET)
AUTH = NativeSuccessorAuthorization(
    "operation", REQUEST_A, REQUEST_B, YOUTUBE_APPLICATION_ID, "session"
)


def receiver(sequence, instant, **changes):
    return replace(
        PlaybackObservation(
            TARGET,
            "connection",
            sequence,
            AT,
            instant,
            session_id="session",
            application_id=YOUTUBE_APPLICATION_ID,
            session_active=True,
            source="receiver_status",
        ),
        **changes,
    )


def media(sequence, instant, position=0, **changes):
    return replace(
        PlaybackObservation(
            TARGET,
            "connection",
            sequence,
            AT,
            instant,
            session_id="session",
            application_id=YOUTUBE_APPLICATION_ID,
            content=B,
            playback_id="1",
            state=PlayerState.PLAYING,
            position=position,
            ad_active=False,
            identity_update=IdentityUpdate.EXPLICIT,
            source="cast_media",
            provider_evidence=ProviderEvidence(YOUTUBE_STATE_SOURCE, ContentPhase.PLAYING),
        ),
        **changes,
    )


def current():
    return NativePlaybackTracker(
        REQUEST_B,
        application_id=YOUTUBE_APPLICATION_ID,
        session_id="session",
        connection_generation="connection",
        started_monotonic=10,
    )


def live_predecessor():
    snapshot = SessionSnapshot(
        PlaybackScope(REQUEST_A, "session", "connection", 9, YOUTUBE_APPLICATION_ID)
    )
    for event in (receiver(1, 10), media(2, 10.1, 8, content=A), media(3, 11.2, 9, content=A)):
        snapshot = policy.observe(snapshot, event, now=event.monotonic)
    assert snapshot.has_confirmed_playback
    return snapshot


def transition():
    return NativeSuccessorTracker(
        AUTH,
        connection_generation="connection",
        started_monotonic=11.2,
        predecessor=live_predecessor(),
    )


def ending(sequence=5, instant=11.4):
    return media(
        sequence,
        instant,
        position=None,
        content=None,
        identity_update=IdentityUpdate.OMITTED,
        state=PlayerState.IDLE,
        idle_reason=IdleReason.FINISHED,
        provider_evidence=ProviderEvidence(YOUTUBE_STATE_SOURCE, ContentPhase.FINISHED),
    )


def test_current_item_scope_requires_fresh_receiver_then_explicit_identity_and_progress():
    tracker = current()
    assert tracker.snapshot is None and not tracker.ready
    tracker.observe((media(1, 10.1),), now=10.1)
    assert tracker.snapshot is None
    tracker.observe((receiver(2, 10.2), media(3, 11, 0)), now=11)
    assert tracker.snapshot is not None and not tracker.ready
    assert tracker.snapshot.scope.started_monotonic == 10.2
    assert tracker.snapshot.receipt is None
    snapshot = tracker.observe((media(4, 12.1, 1),), now=12.1)
    assert snapshot is not None and tracker.ready
    assert snapshot.scope.request == REQUEST_B
    assert snapshot.receipt is None and snapshot.completion is None
    tracker.observe((), now=18)
    assert not tracker.ready and tracker.snapshot.evidence.reason == EvidenceReason.STALE


def test_same_batch_terminal_and_successor_preserve_a_completion_without_a_b_start():
    tracker = transition()
    tracker.observe((receiver(4, 11.3), ending(), media(6, 11.5, 0), media(7, 12.6, 1)), now=12.6)
    assert tracker.predecessor.completion is not None
    assert tracker.predecessor.completion.attribution == CompletionAttribution.ORDERED_HISTORY
    assert tracker.predecessor.ownership_lost
    assert tracker.predecessor.evidence.natural_completion_confirmed
    assert tracker.ready and tracker.refusal is None
    assert tracker.successor is not None
    assert tracker.successor.scope.started_monotonic == 11.4
    assert tracker.successor.receipt is None
    assert tracker.successor.completion is None


def test_ads_can_establish_b_identity_but_never_progress_and_a_finish_survives():
    tracker = transition()
    tracker.observe((receiver(4, 11.3), ending()), now=11.4)
    completion = tracker.predecessor.completion
    tracker.observe(
        tuple(
            media(
                seq,
                instant,
                position,
                ad_active=True,
                provider_evidence=ProviderEvidence(YOUTUBE_STATE_SOURCE, ContentPhase.AD),
            )
            for seq, instant, position in ((6, 11.5, 0), (7, 12.6, 1))
        ),
        now=12.6,
    )
    assert tracker.successor is not None
    assert tracker.successor.evidence.identity_confirmed
    assert not tracker.ready and not tracker.successor.has_confirmed_playback
    tracker.observe((receiver(8, 13), media(9, 13.1, 0), media(10, 14.2, 1)), now=14.2)
    assert tracker.ready and tracker.predecessor.completion is completion


def test_b_may_be_observed_without_fabricating_an_unseen_a_finish():
    tracker = transition()
    tracker.observe((receiver(4, 11.3), media(5, 11.4), media(6, 12.5, 1)), now=12.5)
    assert tracker.ready
    assert tracker.predecessor.completion is None
    assert not tracker.predecessor.evidence.natural_completion_confirmed


def test_waiting_for_b_allows_current_a_and_receivers_across_windows():
    tracker = transition()
    tracker.observe((receiver(4, 11.3), media(5, 11.4, 9.2, content=A)), now=11.4)
    tracker.observe((media(6, 12.5, 10.3, content=A),), now=12.5)
    assert tracker.refusal is None and tracker.successor is None
    tracker.observe((media(7, 12.6), media(8, 13.7, 1)), now=13.7)
    assert tracker.ready


@pytest.mark.parametrize("before_identity", [True, False])
@pytest.mark.parametrize(
    "change,reason",
    [
        ({"content": C}, NativeObservationRefusal.UNEXPECTED_CONTENT),
        ({"session_id": "unrelated"}, NativeObservationRefusal.SESSION_CHANGED),
        ({"application_id": "unrelated"}, NativeObservationRefusal.SESSION_CHANGED),
        ({"connection_generation": "replacement"}, NativeObservationRefusal.CONNECTION_CHANGED),
        ({"connection_reset": True}, NativeObservationRefusal.CONNECTION_CHANGED),
    ],
)
def test_unrelated_takeover_is_sticky_and_return_to_b_cannot_adopt(before_identity, change, reason):
    tracker = current()
    tracker.observe((receiver(1, 10.1),), now=10.1)
    if not before_identity:
        tracker.observe((media(2, 10.2),), now=10.2)
    tracker.observe((media(3, 11, **change),), now=11)
    tracker.observe((receiver(4, 11.1), media(5, 11.2), media(6, 12.3, 1)), now=12.3)
    assert tracker.refusal == reason and not tracker.ready
    assert tracker.snapshot is None or tracker.snapshot.ownership_lost


def test_session_exit_refuses_even_if_expected_b_returns():
    tracker = current()
    exited = receiver(1, 10.1, application_id=None, session_id=None, session_active=False)
    tracker.observe((exited, receiver(2, 10.2), media(3, 11), media(4, 12.1, 1)), now=12.1)
    assert tracker.refusal == NativeObservationRefusal.SESSION_EXITED
    assert tracker.snapshot is None and not tracker.ready


def test_a_cannot_reappear_after_b_identity_and_reclaim_that_occurrence():
    tracker = transition()
    tracker.observe((receiver(4, 11.3), media(5, 11.4), media(6, 12.5, 1)), now=12.5)
    tracker.observe((media(7, 12.6, 10, content=A), media(8, 13.7, 2)), now=13.7)
    assert tracker.refusal == NativeObservationRefusal.UNEXPECTED_CONTENT
    assert not tracker.ready


@pytest.mark.parametrize(
    "change",
    [
        {"content": None},
        {"identity_update": IdentityUpdate.OMITTED},
        {"identity_update": IdentityUpdate.UNKNOWN},
        {"session_id": None},
        {"application_id": None},
        {"playback_id": None},
    ],
)
def test_initial_missing_identity_cannot_establish_b_scope(change):
    tracker = current()
    tracker.observe((receiver(1, 10.1), media(2, 11, **change)), now=11)
    assert tracker.snapshot is None and not tracker.ready


@pytest.mark.parametrize("ad_active", [True, None])
def test_ad_or_unknown_ad_breaks_progress_pair(ad_active):
    tracker = current()
    tracker.observe((receiver(1, 10.1), media(2, 11)), now=11)
    tracker.observe((media(3, 12.1, 1, ad_active=ad_active),), now=12.1)
    assert not tracker.ready
    tracker.observe((media(4, 13.2, 2),), now=13.2)
    assert not tracker.ready
    tracker.observe((media(5, 14.3, 3),), now=14.3)
    assert tracker.ready


@pytest.mark.parametrize(
    "event",
    [
        media(2, 11),
        media(1, 11.1),
        media(3, 10.9),
        media(3, 20),
        media(3, 9),
        media(3, 12.1, 1, target=PlaybackTarget("unrelated", "cast")),
    ],
)
def test_stale_future_duplicate_reordered_or_wrong_target_events_do_not_prove_progress(event):
    tracker = current()
    tracker.observe((receiver(1, 10.1), media(2, 11)), now=11)
    tracker.observe((event,), now=12.1)
    assert not tracker.ready and tracker.refusal is None


def test_gap_requires_new_receiver_and_two_new_nonad_progress_samples():
    tracker = current()
    tracker.observe((receiver(1, 10.1), media(2, 11)), now=11)
    tracker.observe((media(4, 12.1, 1),), now=12.1)
    assert not tracker.ready
    tracker.observe((receiver(5, 12.2), media(6, 12.3, 1.2)), now=12.3)
    assert not tracker.ready
    tracker.observe((media(7, 13.4, 2.3),), now=13.4)
    assert tracker.ready


def test_stale_receiver_identity_cannot_establish_scope_from_fresh_media():
    tracker = current()
    tracker.observe((receiver(1, 10.1),), now=10.1)
    tracker.observe((media(2, 16, 1), media(3, 17.1, 2)), now=17.1)
    assert tracker.snapshot is None and not tracker.ready


def test_readiness_requires_a_receiver_witness_fresh_at_the_latest_now():
    tracker = current()
    tracker.observe((receiver(1, 10.1), media(2, 13), media(3, 14.1, 1)), now=14.1)
    assert tracker.ready
    tracker.observe((), now=15.2)
    assert tracker.snapshot is not None and tracker.snapshot.evidence.receiver_playback_confirmed
    assert not tracker.ready


@pytest.mark.parametrize("playback_id", [None, ""])
def test_later_missing_media_sessions_cannot_form_a_progress_pair(playback_id):
    tracker = current()
    tracker.observe((receiver(1, 10.1), media(2, 11)), now=11)
    tracker.observe(
        (media(3, 12.1, 1, playback_id=playback_id), media(4, 13.2, 2, playback_id=playback_id)),
        now=13.2,
    )
    assert not tracker.ready
    assert tracker.snapshot is not None and tracker.snapshot.progress_anchor is None


def test_incomplete_receiver_witness_breaks_progress_until_fresh_media_pair():
    tracker = current()
    tracker.observe((receiver(1, 10.1), media(2, 11)), now=11)
    tracker.observe(
        (receiver(3, 11.1, application_id=None), receiver(4, 11.2), media(5, 12.1, 1)), now=12.1
    )
    assert not tracker.ready
    tracker.observe((media(6, 13.2, 2),), now=13.2)
    assert tracker.ready


def test_connection_change_retires_a_live_predecessor_evidence():
    tracker = transition()
    tracker.observe((receiver(4, 11.3, connection_generation="replacement"),), now=11.3)
    assert tracker.refusal == NativeObservationRefusal.CONNECTION_CHANGED
    assert tracker.predecessor.ownership_lost
    assert not tracker.predecessor.evidence.receiver_playback_confirmed


def test_new_media_session_requires_its_own_progress_pair():
    tracker = current()
    tracker.observe((receiver(1, 10.1), media(2, 11)), now=11)
    tracker.observe((media(3, 12.1, 1, playback_id="2"),), now=12.1)
    assert not tracker.ready
    tracker.observe((media(4, 13.2, 2, playback_id="2"),), now=13.2)
    assert tracker.ready


def test_reconnect_preserves_only_historical_completion_not_old_monotonic_authority():
    completion = CompletionEvidence(
        AT, 9000, 800, 799, CompletionAttribution.ORDERED_HISTORY, YOUTUBE_STATE_SOURCE
    )
    tracker = NativeSuccessorTracker.reconnect(
        AUTH,
        connection_generation="new-connection",
        started_monotonic=10,
        predecessor_completion=completion,
    )
    assert tracker.predecessor.completion is completion
    assert tracker.predecessor.latest is tracker.predecessor.progress_anchor is None
    assert tracker.predecessor.completion_anchor is None
    assert not tracker.predecessor.has_confirmed_playback
    tracker.observe((receiver(1, 9, connection_generation="old-connection"),), now=10)
    assert tracker.successor is None and tracker.refusal is None
    events = tuple(
        replace(event, connection_generation="new-connection")
        for event in (receiver(1, 10.1), media(2, 11), media(3, 12.1, 1))
    )
    tracker.observe(events, now=12.1)
    assert tracker.ready and tracker.successor is not None
    assert tracker.successor.scope.connection_generation == "new-connection"
    assert tracker.successor.scope.started_monotonic == 10.1
    assert tracker.successor.receipt is None
    assert tracker.predecessor.completion is completion


def test_reconnect_without_a_completion_does_not_restore_or_invent_it():
    tracker = NativeSuccessorTracker.reconnect(
        AUTH, connection_generation="connection", started_monotonic=10
    )
    tracker.observe((receiver(1, 10.1), media(2, 11), media(3, 12.1, 1)), now=12.1)
    assert tracker.ready and tracker.predecessor.completion is None


@pytest.mark.parametrize(
    "change",
    [
        {"content": A},
        {"target": PlaybackTarget("other", "cast")},
        {"attempt_id": "attempt-a"},
        {"queue_item_id": "item-a"},
    ],
)
def test_authorization_refuses_ambiguous_repeat_or_mismatched_successor(change):
    with pytest.raises(ValueError):
        replace(AUTH, successor=replace(REQUEST_B, **change))


def test_authorization_is_immutable():
    with pytest.raises(FrozenInstanceError):
        AUTH.__setattr__("operation_id", "another")


def test_repeated_id_is_ambiguous_even_when_content_kind_differs():
    with pytest.raises(ValueError):
        replace(AUTH, successor=replace(REQUEST_B, content=replace(A, kind=ContentKind.EPISODE)))


@pytest.mark.parametrize("change", [{"ownership_lost": True}, {"stop_requested": True}])
def test_live_seed_cannot_restore_lost_or_canceled_ownership(change):
    with pytest.raises(ValueError, match="live predecessor"):
        NativeSuccessorTracker(
            AUTH,
            connection_generation="connection",
            started_monotonic=11.2,
            predecessor=replace(live_predecessor(), **change),
        )


def test_live_constructor_refuses_snapshot_from_another_connection():
    with pytest.raises(ValueError, match="live predecessor"):
        NativeSuccessorTracker(
            AUTH,
            connection_generation="fresh",
            started_monotonic=11.2,
            predecessor=live_predecessor(),
        )


@pytest.mark.parametrize("name", ["play-next-02", "play-next-04"])
def test_sanitized_actual_native_chronology_qualifies_expected_b_without_commands(name):
    path = Path(__file__).parent / "fixtures/native-queue" / f"{name}.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    begin = records[0]
    target = PlaybackTarget(begin["device"]["uuid"], "cast")
    clock, transport = _ReplayClock(begin), _ReplayTransport()
    backend = CastBackend(transport, target, clock, read_only=True)
    auth = NativeSuccessorAuthorization(
        "operation",
        replace(REQUEST_A, target=target, content=ContentRef("youtube", "f9F7yDjSdNA")),
        replace(REQUEST_B, target=target, content=ContentRef("youtube", "hgsIFyITvJE")),
        YOUTUBE_APPLICATION_ID,
        "synthetic-session-1",
    )
    tracker = NativeSuccessorTracker(
        auth, connection_generation=backend.generation, started_monotonic=begin["monotonic"]
    )
    ready = False
    completion = None
    for record in records[1:]:
        clock.record, transport.batch = record, record["observations"]
        events = backend.observe_window(target, 1)
        for event in events:
            tracker.observe((event,), now=clock.monotonic())
            if completion is not None:
                assert tracker.predecessor.completion is completion
            completion = tracker.predecessor.completion
            if event.ad_active is True:
                assert not tracker.ready
            ready = ready or tracker.ready
        assert tracker.refusal is None
    assert completion is not None and ready
    assert tracker.successor is not None and tracker.successor.receipt is None


def test_module_import_has_no_transport_dependencies_or_side_effects(tmp_path):
    project = Path(__file__).resolve().parents[1]
    script = """
import sys
sys.path.insert(0, sys.argv[1])
def audit(event, args):
    if event.startswith('socket.') or event in ('os.mkdir', 'os.rename', 'os.remove'):
        raise AssertionError(event)
    if event == 'open' and len(args) > 1 and isinstance(args[1], str):
        if any(flag in args[1] for flag in 'wax+'):
            raise AssertionError('write')
sys.addaudithook(audit)
from tellyq.native_observation import NativePlaybackTracker, NativeSuccessorTracker
assert NativePlaybackTracker and NativeSuccessorTracker
assert not any(name in sys.modules for name in ('pychromecast', 'requests', 'casttube'))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, str(project)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())
