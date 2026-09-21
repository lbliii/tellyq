"""Recompute native queue observations without granting production queue ownership."""

import json
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import pytest

from tellyq.cast_backend import CastBackend
from tellyq.domain.values import (
    CompletionAttribution,
    ContentPhase,
    ContentRef,
    IdleReason,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackTarget,
    PlayerState,
    SessionSnapshot,
)
from tellyq.lifecycle import LifecycleTracker, _ReplayClock, _ReplayTransport

FIXTURES = Path(__file__).parent / "fixtures/native-queue"
A = ContentRef("youtube", "f9F7yDjSdNA")
B = ContentRef("youtube", "hgsIFyITvJE")


@dataclass(frozen=True)
class Step:
    window: int
    event: PlaybackObservation
    a: SessionSnapshot | None
    b: SessionSnapshot | None


def replay(name):
    records = [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]
    begin = records[0]
    target = PlaybackTarget(begin["device"]["uuid"], "cast")
    clock, transport = _ReplayClock(begin), _ReplayTransport()
    backend = CastBackend(transport, target, clock, read_only=True)
    # B is an explicitly declared independent observation scope. No request,
    # durable queue record, or ownership authority is transferred from A to B.
    trackers = [
        LifecycleTracker(
            PlaybackRequest(f"request-{label}", f"attempt-{label}", label, content, target),
            begin["monotonic"],
        )
        for label, content in (("a", A), ("b", B))
    ]
    steps = []
    for record in records[1:]:
        clock.record = record
        transport.batch = record["observations"]
        events = backend.observe_window(target, 1)
        backend.drain_raw_observations()
        for event in events:
            for tracker in trackers:
                tracker.observe((event,), now=clock.monotonic())
            steps.append(Step(record["sequence"], event, *(t.snapshot for t in trackers)))
    return records, steps


@pytest.mark.parametrize("name", ["play-next-04", "play-next-02"])
def test_anonymous_a_completion_survives_ads_and_different_content(name):
    _, steps = replay(name)
    first = next(step for step in steps if step.a is not None and step.a.completion is not None)
    assert first.event.content is None
    assert first.event.state == PlayerState.IDLE
    assert first.event.idle_reason == IdleReason.FINISHED
    assert first.a is not None and first.a.completion is not None
    completion = first.a.completion
    assert completion.attribution == CompletionAttribution.ORDERED_HISTORY
    assert completion.identity_sequence < completion.sequence
    assert first.event.ad_active is False
    assert not first.a.evidence.identity_confirmed
    after = steps[steps.index(first) :]
    assert all(step.a is not None and step.a.completion is completion for step in after)
    assert all(step.a.evidence.natural_completion_confirmed for step in after if step.a)
    assert not any(step.a.evidence.receiver_playback_confirmed for step in after if step.a)
    assert any(step.event.ad_active is True for step in after)


@pytest.mark.parametrize(("name", "ad_samples"), [("play-next-04", 8), ("play-next-02", 9)])
def test_ads_with_b_identity_and_advancing_positions_are_not_b_playback(name, ad_samples):
    records, steps = replay(name)
    ads = [step for step in steps if step.event.ad_active is True]
    assert len(ads) == ad_samples
    assert all(step.event.provider_evidence.phase == ContentPhase.AD for step in ads)
    assert all(step.b is not None and not step.b.has_confirmed_playback for step in ads)
    assert all(not step.b.evidence.receiver_playback_confirmed for step in ads if step.b)
    assert all(step.b.completion is None for step in ads if step.b)
    identified = [step.event for step in ads if step.event.content == B]
    assert len(identified) > 2
    assert identified[1].position > identified[0].position
    raw_ads = [
        event
        for row in records
        for event in row.get("observations", [])
        if event.get("custom_player_state") == 1081
    ]
    assert len(raw_ads) == ad_samples
    assert all(event["ad_break"] is None for event in raw_ads)


@pytest.mark.parametrize("name", ["play-next-04", "play-next-02"])
def test_b_only_qualifies_after_fresh_exact_nonad_progress_in_its_declared_scope(name):
    _, steps = replay(name)
    first = next(
        step for step in steps if step.b is not None and step.b.evidence.receiver_playback_confirmed
    )
    assert first.b is not None
    assert first.b.scope.request.content == B
    assert first.event.content == B
    assert first.event.state == PlayerState.PLAYING
    assert first.event.ad_active is False
    assert first.b.evidence.identity_confirmed
    assert not first.b.ownership_lost
    assert first.b.completion is None
    preceding = steps[: steps.index(first)]
    last_ad = max(step.event.sequence for step in preceding if step.event.ad_active is True)
    progress = [
        step.event
        for step in preceding
        if step.event.sequence > last_ad
        and step.event.content == B
        and step.event.state == PlayerState.PLAYING
        and step.event.ad_active is False
    ]
    assert progress and progress[0].position < first.event.position
    assert progress[0].playback_id == first.event.playback_id
    assert all(not step.b.evidence.receiver_playback_confirmed for step in preceding if step.b)


@pytest.mark.parametrize("name", ["play-next-04", "play-next-02"])
def test_existing_a_ownership_treats_b_as_replacement_even_in_same_receiver_media_session(name):
    _, steps = replay(name)
    replacement = next(step for step in steps if step.event.content == B)
    assert replacement.a is not None
    assert replacement.a.scope.request.content == A
    assert replacement.a.ownership_lost
    assert replacement.a.completion is not None
    assert replacement.event.session_id == replacement.a.scope.session_id
    a_media = next(step.event for step in steps if step.event.content == A)
    assert replacement.event.playback_id == a_media.playback_id
    assert all(step.a.ownership_lost for step in steps[steps.index(replacement) :] if step.a)


def test_run02_reduces_a_completion_before_b_replacement_in_the_same_window():
    _, steps = replay("play-next-02")
    terminal = next(step for step in steps if step.event.idle_reason == IdleReason.FINISHED)
    replacement = next(step for step in steps if step.event.content == B)
    assert terminal.window == replacement.window == 126
    assert terminal.event.sequence < replacement.event.sequence
    assert terminal.a is not None and replacement.a is not None
    assert not terminal.a.ownership_lost
    assert replacement.a.ownership_lost
    assert terminal.a.completion is replacement.a.completion


@pytest.mark.parametrize("name", ["play-next-04", "play-next-02"])
def test_observed_code5_stays_unknown_and_never_confirms_b(name):
    records, steps = replay(name)
    raw = [event for row in records for event in row.get("observations", [])]
    opaque = [
        step
        for event, step in zip(raw, steps, strict=True)
        if event.get("custom_player_state") == 5
    ]
    assert len(opaque) == 1
    event = opaque[0].event
    assert event.provider_evidence is not None
    assert event.provider_evidence.phase == ContentPhase.UNKNOWN
    assert event.ad_active is None
    assert opaque[0].b is not None and not opaque[0].b.evidence.receiver_playback_confirmed


@pytest.mark.parametrize(
    ("name", "sequences", "count"),
    [
        ("play-next-04", [0, *range(119, 124), *range(125, 137), 138], 53),
        ("play-next-02", [0, *range(122, 127), *range(128, 140), 141], 54),
    ],
)
def test_fixtures_preserve_ordered_windows_missing_identity_and_only_sanitized_values(
    name, sequences, count
):
    records, steps = replay(name)
    assert [row["sequence"] for row in records] == sequences
    raw = [event for row in records for event in row.get("observations", [])]
    assert len(raw) == len(steps) == count
    assert all(a["monotonic"] < b["monotonic"] for a, b in pairwise(raw))
    assert records[0]["device"] == {"uuid": "synthetic-device-native-queue", "name": None}
    assert records[0]["requested_content_id"] == A.content_id
    assert all(row["provenance"] == "sanitized-live" for row in records)
    assert all(
        not row["entire_run_observed"] and row["external_control_unobserved"] for row in records
    )
    assert all(row["recorded_at"].startswith("2000-01-01T") for row in records)
    assert all(row["kind"] != "end" for row in records)
    common_fields = {
        "schema_version",
        "kind",
        "capture_id",
        "provenance",
        "sequence",
        "recorded_at",
        "monotonic",
        "mode",
        "entire_run_observed",
        "external_control_unobserved",
    }
    assert set(records[0]) == common_fields | {"requested_content_id", "device"}
    assert all(set(row) == common_fields | {"observations"} for row in records[1:])
    terminal = next(event for event in raw if event.get("idle_reason") == "FINISHED")
    assert terminal["content_id"] is None and terminal["duration"] is None
    assert terminal["wire_media"] == "absent"
    assert terminal["wire_media_content_id"] == "unavailable"
    safe_fields = {
        "kind",
        "position",
        "duration",
        "empty_status",
        "ad_break",
        "active_input",
        "standby",
        "pause_supported",
        "app_session_id",
        "app_id",
        "app_name",
        "content_id",
        "media_session_id",
        "player_state",
        "idle_reason",
        "monotonic",
        "observed_at",
        "custom_player_state",
        "custom_player_state_shape",
        "wire_status_count",
    }
    for event in raw:
        for key, value in event.items():
            assert value is None or isinstance(value, str | int | float | bool)
            if key.startswith("wire_") and key != "wire_status_count":
                assert value in {"absent", "null", "valid", "invalid", "unavailable"}
            else:
                assert key in safe_fields
        assert event.get("content_id") in {None, A.content_id, B.content_id}
        assert event.get("app_session_id") in {None, "synthetic-session-1"}
        assert event.get("app_id") in {None, "233637DE"}
        assert event.get("media_session_id") in {None, 7001}
        assert event["observed_at"].startswith("2000-01-01T")
    text = (FIXTURES / f"{name}.jsonl").read_text()
    assert not any(value in text for value in ("http", "title", "2026-", "raw", "token", "/Users/"))
