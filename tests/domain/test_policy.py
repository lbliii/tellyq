"""Synthetic ordered traces; no fixture contains real receiver information."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from tellyq.domain.policy import observe, record_display, record_receipt, refresh, request_stop
from tellyq.domain.values import (
    CapabilityEvidence,
    CommandAction,
    CommandOutcome,
    CommandReceipt,
    ContentRef,
    DisplayEvidence,
    EvidencePolicy,
    EvidenceReason,
    IdleReason,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    SessionSnapshot,
    Support,
)

AT = datetime(2026, 1, 1, tzinfo=UTC)
TARGET = PlaybackTarget("synthetic-device", "fake")
CONTENT = ContentRef("fixture-provider", "opaque-item")
REQUEST = PlaybackRequest("request-1", "attempt-1", "queue-item-1", CONTENT, TARGET)
SCOPE = PlaybackScope(REQUEST, "session-1", "connection-1", 10.0)


def sample(sequence: int, time: float, position: float | None = None) -> PlaybackObservation:
    return PlaybackObservation(
        target=TARGET,
        connection_generation="connection-1",
        sequence=sequence,
        observed_at=AT,
        monotonic=time,
        session_id="session-1",
        content=CONTENT,
        state=PlayerState.PLAYING,
        position=position,
        ad_active=False,
    )


def replay(*events: PlaybackObservation) -> SessionSnapshot:
    snapshot = SessionSnapshot(SCOPE)
    for event in events:
        snapshot = observe(snapshot, event, now=event.monotonic)
    return snapshot


def playing() -> SessionSnapshot:
    return replay(sample(1, 11.0, 0.0), sample(2, 12.0, 1.0))


def test_exact_fresh_progress_confirms_receiver_but_not_display():
    snapshot = playing()
    assert snapshot.evidence.receiver_playback_confirmed
    assert snapshot.evidence.identity_confirmed
    assert snapshot.state == PlayerState.PLAYING
    assert not snapshot.evidence.natural_completion_confirmed
    assert snapshot.display is None


@pytest.mark.parametrize("outcome", tuple(CommandOutcome))
def test_command_receipts_cannot_manufacture_playback_or_completion(outcome):
    receipt = CommandReceipt("request-1", "attempt-1", CommandAction.START, outcome, AT)
    snapshot = record_receipt(SessionSnapshot(SCOPE), receipt)
    assert snapshot.receipt == receipt
    assert snapshot.state == PlayerState.UNKNOWN
    assert not snapshot.evidence.receiver_playback_confirmed
    assert not snapshot.evidence.natural_completion_confirmed
    assert snapshot.latest is None


def test_receipt_and_display_require_exact_correlation():
    receipt = CommandReceipt(
        "request-1", "another-attempt", CommandAction.START, CommandOutcome.ACCEPTED, AT
    )
    with pytest.raises(ValueError, match="request and attempt"):
        record_receipt(SessionSnapshot(SCOPE), receipt)
    with pytest.raises(ValueError, match="content and target"):
        record_display(
            SessionSnapshot(SCOPE),
            DisplayEvidence(TARGET, ContentRef("p", "other"), True, AT, "user"),
        )


def test_visual_confirmation_stays_historical_and_separate():
    report = DisplayEvidence(TARGET, CONTENT, True, AT, "user")
    snapshot = record_display(SessionSnapshot(SCOPE), report)
    assert snapshot.display == report
    assert not snapshot.evidence.receiver_playback_confirmed
    later = refresh(observe(snapshot, sample(1, 11.0, 0.0), now=11.0), now=100.0)
    assert later.display == report
    assert later.state == PlayerState.UNKNOWN


@pytest.mark.parametrize("ad_state", [None, True])
def test_ads_and_unknown_ad_state_break_progress_pair(ad_state):
    first = sample(1, 11.0, 0.0)
    middle = replace(sample(2, 12.0, 1.0), ad_active=ad_state)
    snapshot = replay(first, middle)
    assert not snapshot.evidence.receiver_playback_confirmed
    assert snapshot.evidence.reason == (
        EvidenceReason.AD_UNKNOWN if ad_state is None else EvidenceReason.AD_ACTIVE
    )
    snapshot = observe(snapshot, sample(3, 13.0, 2.0), now=13.0)
    assert not snapshot.evidence.receiver_playback_confirmed
    snapshot = observe(snapshot, sample(4, 14.0, 3.0), now=14.0)
    assert snapshot.evidence.receiver_playback_confirmed


@pytest.mark.parametrize(
    "state", [PlayerState.PAUSED, PlayerState.BUFFERING, PlayerState.IDLE, PlayerState.UNKNOWN]
)
def test_pause_buffering_idle_and_missing_state_cannot_bridge_progress(state):
    snapshot = replay(sample(1, 11.0, 0.0), replace(sample(2, 12.0, 1.0), state=state))
    assert snapshot.state == state
    assert not snapshot.evidence.receiver_playback_confirmed
    snapshot = observe(snapshot, sample(3, 13.0, 2.0), now=13.0)
    assert not snapshot.evidence.receiver_playback_confirmed


@pytest.mark.parametrize("missing", ["content", "session_id", "position"])
def test_missing_fields_do_not_reuse_previous_values(missing):
    event = sample(2, 12.0, 1.0)
    if missing == "content":
        event = replace(event, content=None)
    elif missing == "session_id":
        event = replace(event, session_id=None)
    else:
        event = replace(event, position=None)
    snapshot = replay(sample(1, 11.0, 0.0), event, sample(3, 13.0, 2.0))
    assert not snapshot.evidence.receiver_playback_confirmed


@pytest.mark.parametrize(
    "event",
    [
        sample(1, 12.0, 1.0),  # repeated sequence with later timestamp
        sample(0, 12.0, 1.0),  # reordered sequence
        sample(2, 11.0, 1.0),  # same instant
        sample(2, 10.5, 1.0),  # time moved backwards
        sample(2, 9.0, 1.0),  # pre-dispatch observation
        sample(2, 20.0, 1.0),  # future observation
        replace(sample(2, 12.0, 1.0), connection_generation="old-generation"),
        replace(sample(2, 12.0, 1.0), target=PlaybackTarget("another-device", "fake")),
    ],
)
def test_invalid_order_scope_and_time_do_not_create_evidence(event):
    snapshot = replay(sample(1, 11.0, 0.0))
    assert observe(snapshot, event, now=12.0) == snapshot
    assert not snapshot.evidence.receiver_playback_confirmed


def test_stale_or_duplicate_events_cannot_keep_playback_alive():
    snapshot = playing()
    assert snapshot.latest is not None
    expired = observe(snapshot, snapshot.latest, now=30.0)
    assert expired.evidence.reason == EvidenceReason.STALE
    assert not expired.evidence.receiver_playback_confirmed
    assert expired.state == PlayerState.UNKNOWN
    assert expired.progress_anchor is None
    assert refresh(expired, now=31.0) == expired


def test_delayed_anchor_cannot_count_as_fresh_progress():
    snapshot = replay(sample(1, 11.0, 0.0))
    snapshot = observe(snapshot, sample(2, 14.0, 0.0), now=14.0)
    snapshot = observe(snapshot, sample(3, 16.0, 1.0), now=18.0)
    assert not snapshot.evidence.receiver_playback_confirmed


def test_stationary_samples_do_not_keep_an_old_anchor_fresh():
    snapshot = replay(sample(1, 11.0, 0.0), sample(2, 15.0, 0.0), sample(3, 16.1, 1.0))
    assert not snapshot.evidence.receiver_playback_confirmed


@pytest.mark.parametrize(
    "takeover",
    [
        replace(sample(3, 13.0, 2.0), session_id="replacement-session"),
        replace(sample(3, 13.0, 2.0), content=ContentRef("fixture-provider", "autoplay-item")),
        replace(sample(3, 13.0, 2.0), content=ContentRef("another-provider", "opaque-item")),
    ],
)
def test_takeover_is_sticky_and_late_old_session_cannot_reclaim_receiver(takeover):
    snapshot = observe(playing(), takeover, now=13.0)
    assert snapshot.ownership_lost
    assert snapshot.evidence.reason == EvidenceReason.SESSION_REPLACED
    assert not snapshot.evidence.receiver_playback_confirmed
    restored_packet = sample(4, 14.0, 3.0)
    assert observe(snapshot, restored_packet, now=14.0) == snapshot
    end = replace(sample(5, 15.0), state=PlayerState.IDLE, idle_reason=IdleReason.FINISHED)
    assert not observe(snapshot, end, now=15.0).evidence.natural_completion_confirmed


def test_reconnection_requires_a_fresh_scope_and_new_progress_pair():
    new_scope = replace(SCOPE, connection_generation="connection-2", started_monotonic=0.0)
    snapshot = SessionSnapshot(new_scope)
    assert observe(snapshot, sample(50, 11.0, 10.0), now=12.0) == snapshot
    first = replace(sample(1, 1.0, 0.0), connection_generation="connection-2")
    second = replace(sample(2, 2.0, 1.0), connection_generation="connection-2")
    snapshot = observe(snapshot, first, now=1.0)
    assert not snapshot.evidence.receiver_playback_confirmed
    snapshot = observe(snapshot, second, now=2.0)
    assert snapshot.evidence.receiver_playback_confirmed


def test_short_samples_can_accumulate_one_progress_interval():
    snapshot = replay(sample(1, 11.0, 0.0), sample(2, 11.5, 0.5), sample(3, 12.0, 1.0))
    assert snapshot.evidence.receiver_playback_confirmed


def test_seek_backwards_resets_progress_anchor():
    snapshot = replay(sample(1, 11.0, 100.0), sample(2, 12.0, 1.0))
    assert not snapshot.evidence.receiver_playback_confirmed
    snapshot = observe(snapshot, sample(3, 13.0, 2.0), now=13.0)
    assert snapshot.evidence.receiver_playback_confirmed


def test_only_explicit_correlated_natural_end_after_progress_can_finish():
    end = replace(sample(3, 13.0), state=PlayerState.IDLE, idle_reason=IdleReason.FINISHED)
    result = observe(playing(), end, now=13.0)
    assert result.state == PlayerState.ENDED
    assert result.evidence.natural_completion_confirmed
    assert not result.evidence.receiver_playback_confirmed
    assert not observe(SessionSnapshot(SCOPE), end, now=13.0).evidence.natural_completion_confirmed


@pytest.mark.parametrize("reason", [None, IdleReason.CANCELED, IdleReason.ERROR])
def test_idle_without_explicit_natural_end_never_completes(reason):
    end = replace(sample(3, 13.0, 9999.0), state=PlayerState.IDLE, duration=1.0, idle_reason=reason)
    result = observe(playing(), end, now=13.0)
    assert not result.evidence.natural_completion_confirmed
    assert result.state == PlayerState.IDLE


def test_nominal_duration_and_wall_time_do_not_finish_playback():
    event = replace(sample(3, 13.0, 9999.0), duration=1.0)
    snapshot = observe(playing(), event, now=13.0)
    assert not snapshot.evidence.natural_completion_confirmed
    snapshot = refresh(snapshot, now=1_000_000.0)
    assert snapshot.state == PlayerState.UNKNOWN
    assert not snapshot.evidence.natural_completion_confirmed


@pytest.mark.parametrize("ad_state", [None, True])
def test_ad_end_cannot_finish_content(ad_state):
    event = replace(
        sample(3, 13.0), state=PlayerState.IDLE, idle_reason=IdleReason.FINISHED, ad_active=ad_state
    )
    assert not observe(playing(), event, now=13.0).evidence.natural_completion_confirmed


def test_telemetry_gap_revokes_the_prior_playback_witness_for_completion():
    end = replace(sample(3, 50.0), state=PlayerState.IDLE, idle_reason=IdleReason.FINISHED)
    assert not observe(playing(), end, now=50.0).evidence.natural_completion_confirmed


@pytest.mark.parametrize("outcome", tuple(CommandOutcome))
def test_stop_intent_suppresses_natural_advancement_even_if_effect_is_uncertain(outcome):
    snapshot = request_stop(playing())
    receipt = CommandReceipt("request-1", "attempt-1", CommandAction.STOP, outcome, AT)
    snapshot = record_receipt(snapshot, receipt)
    end = replace(sample(3, 13.0), state=PlayerState.IDLE, idle_reason=IdleReason.FINISHED)
    assert not observe(snapshot, end, now=13.0).evidence.natural_completion_confirmed
    stopped = replace(end, idle_reason=IdleReason.CANCELED)
    assert observe(snapshot, stopped, now=13.0).state == PlayerState.STOPPED
    assert not observe(snapshot, stopped, now=13.0).evidence.natural_completion_confirmed


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), float("-inf"), 10**1000])
def test_nonfinite_numbers_and_bools_are_rejected_at_value_boundaries(bad):
    with pytest.raises(ValueError, match="finite number"):
        replace(sample(1, 11.0), position=bad)
    with pytest.raises(ValueError, match="finite number"):
        replace(sample(1, 11.0), duration=bad)
    with pytest.raises(ValueError, match="finite number"):
        replace(sample(1, 11.0), monotonic=bad)
    with pytest.raises(ValueError, match="finite number"):
        replace(SCOPE, started_monotonic=bad)
    with pytest.raises(ValueError, match="finite number"):
        refresh(SessionSnapshot(SCOPE), now=bad)
    with pytest.raises(ValueError, match="finite number"):
        EvidencePolicy(max_age_seconds=bad)


def test_observation_value_constraints_and_frozen_snapshots():
    with pytest.raises(ValueError, match="non-negative"):
        replace(sample(1, 11.0), position=-1.0)
    with pytest.raises(ValueError, match="non-negative integer"):
        replace(sample(1, 11.0), sequence=True)
    with pytest.raises(ValueError, match="timezone"):
        replace(sample(1, 11.0), observed_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="policy conclusions"):
        replace(sample(1, 11.0), state=PlayerState.ENDED)
    with pytest.raises(ValueError, match="current time"):
        refresh(playing(), now=11.0)
    for attribute in ("state", "evidence", "stop_requested"):
        with pytest.raises(FrozenInstanceError):
            setattr(playing(), attribute, None)
    assert not hasattr(playing(), "__dict__")
    assert isinstance(hash(playing()), int)


def test_presentation_changes_do_not_change_content_or_device_identity():
    assert replace(CONTENT, title="Display title") == CONTENT
    assert replace(TARGET, name="Friendly name") == TARGET


def test_known_capability_support_requires_dated_provenance():
    with pytest.raises(ValueError, match="provenance"):
        CapabilityEvidence(Support.VERIFIED)
    evidence = CapabilityEvidence(Support.VERIFIED, "synthetic contract test", AT)
    assert evidence.support == Support.VERIFIED
