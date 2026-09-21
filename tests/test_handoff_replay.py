"""Sanitized observed timelines are recomputed through Cast and lifecycle policy."""

import json
import re
from pathlib import Path

import pytest

from tellyq.cast_backend import CastBackend
from tellyq.domain.values import ContentPhase, ContentRef, PlaybackRequest, PlaybackTarget
from tellyq.handoff import HandoffDisposition, HandoffReason, release_decision, track_release
from tellyq.lifecycle import LifecycleTracker, _ReplayClock, _ReplayTransport
from tellyq.runner import TaskView
from tellyq.runner_codec import wire_view

FIXTURES = Path(__file__).parent / "fixtures/handoff"


def replay(name):
    records = [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]
    begin = records[0]
    target = PlaybackTarget(begin["device"]["uuid"], "cast")
    request = PlaybackRequest(
        "synthetic",
        "synthetic",
        "synthetic",
        ContentRef("youtube", begin["requested_content_id"]),
        target,
    )
    clock, transport = _ReplayClock(begin), _ReplayTransport()
    backend = CastBackend(transport, target, clock, read_only=True)
    tracker = LifecycleTracker(request, begin["monotonic"])
    authority, results = None, []
    for record in records[1:]:
        clock.record = record
        transport.batch = record["observations"]
        events = backend.observe_window(target, 2)
        backend.drain_raw_observations()
        tracker.observe(events, now=clock.monotonic())
        if tracker.snapshot is not None:
            authority = track_release(tracker.snapshot, events, authority)
            diagnostic = release_decision(
                tracker.snapshot, authority, events, now=clock.monotonic()
            )
            results.append((tracker.snapshot, authority, diagnostic))
    return records, results


@pytest.mark.parametrize("name", ["run-a1", "run-b"])
def test_observed_same_content_code5_position_reset_is_the_sticky_veto(name):
    records, results = replay(name)
    completed = [result for result in results if result[0].completion is not None]
    assert completed and completed[0][1] is not None
    assert completed[0][1].first_veto is None
    blocked = [result for result in completed if result[1].blocked]
    assert blocked and all(result[2].disposition == HandoffDisposition.HOLD for result in blocked)
    first_veto = blocked[0][1].first_veto
    assert first_veto.reason == HandoffReason.POSITION_REWOUND
    assert first_veto.evidence.content_matches is True
    assert first_veto.evidence.position == 0
    assert first_veto.evidence.terminal_position > 0
    assert first_veto.evidence.ad_active is None
    assert first_veto.evidence.provider_phase == ContentPhase.UNKNOWN
    assert all(result[1].first_veto == first_veto for result in blocked)
    assert all(result[0].completion == completed[0][0].completion for result in completed)
    assert not any(result[0].ownership_lost for result in completed)
    # Code 5 is observed only in the input; diagnostics never infer it from BUFFERING.
    assert any(
        event.get("custom_player_state") == 5
        for row in records
        for event in row.get("observations", [])
    )
    assert "custom_player_state" not in json.dumps(wire_view(TaskView(handoff=blocked[0][2])))


def test_observed_different_content_then_ad_keeps_original_finish_and_refuses_release():
    records, results = replay("run-a2")
    completed = [result for result in results if result[0].completion is not None]
    assert completed and all(result[0].ownership_lost for result in completed)
    veto = completed[0][1].first_veto
    assert veto.reason == HandoffReason.CONTENT_CHANGED
    assert veto.evidence.content_matches is False
    assert all(result[1].blocked and result[1].first_veto == veto for result in completed)
    assert all(result[0].completion == completed[0][0].completion for result in completed)
    assert all(result[2].disposition == HandoffDisposition.HOLD for result in completed)
    assert any(
        event.get("custom_player_state") == 1081
        for row in records
        for event in row.get("observations", [])
    )
    assert any(result[2].evidence.ad_active is True for result in completed)


@pytest.mark.parametrize(
    ("name", "sequences"),
    [
        ("run-a1", [0, *range(65, 74)]),
        ("run-b", [0, *range(39, 48)]),
        ("run-a2", [0, *range(55, 64)]),
    ],
)
def test_fixtures_retain_exact_selected_windows_and_only_sanitized_values(name, sequences):
    text = (FIXTURES / f"{name}.jsonl").read_text()
    rows = [json.loads(line) for line in text.splitlines()]
    assert [row["sequence"] for row in rows] == sequences
    assert not any(private in text for private in ("http", "title", "2026-", "raw", "token"))
    assert rows[0]["device"]["name"] is None
    identities = {
        "app_session_id": r"session-[0-9a-f]{24}",
        "app_id": r"(?:233637DE|application-[0-9a-f]{24})",
        "content_id": r"content-[0-9a-f]{24}",
    }
    safe_scalars = {
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
    for row in rows:
        assert row["provenance"] == "sanitized-live" and not row["entire_run_observed"]
        assert row["external_control_unobserved"] and row["kind"] != "end"
        assert row["recorded_at"].startswith("2000-01-01T")
        for event in row.get("observations", []):
            for key, value in event.items():
                assert value is None or isinstance(value, str | int | float | bool)
                if key.startswith("wire_") and key != "wire_status_count":
                    assert value in {"absent", "null", "valid", "invalid", "unavailable"}
                else:
                    assert key in safe_scalars
            for key, pattern in identities.items():
                if event.get(key) is not None:
                    assert re.fullmatch(pattern, event[key])
