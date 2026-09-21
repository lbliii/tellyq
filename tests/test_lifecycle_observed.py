"""Observed terminal regression excerpt; synthetic mutations stay explicitly labelled."""

import json
import re
from pathlib import Path

import pytest

from tellyq.lifecycle import JsonlJournal, inspect_capture, replay_capture

FIXTURE = Path(__file__).parent / "fixtures/lifecycle/a3-terminal-excerpt.jsonl"


def records(path=FIXTURE):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_observed_excerpt_cannot_attribute_anonymous_endings_by_reused_session(tmp_path):
    source = records()
    assert [record["sequence"] for record in source] == [0, 70, 71, 72, 83, 120, 121, 122]
    requested = source[0]["requested_content_id"]
    media = [
        event
        for record in source
        for event in record.get("observations", [])
        if event["kind"] == "media"
    ]
    terminals = [event for event in media if event.get("idle_reason") == "FINISHED"]
    assert len(terminals) == 2
    assert all(event["content_id"] is None and event["ad_break"] is None for event in terminals)
    assert len({event["media_session_id"] for event in media}) == 1
    assert len({event["content_id"] for event in media if event["content_id"]}) == 2
    assert any(
        event["player_state"] == "PLAYING" and event["content_id"] != requested
        for event in media
        if event["content_id"]
    )
    destination = tmp_path / "replayed.jsonl"
    replay_capture(FIXTURE, destination)
    output = records(destination)
    assert all(record["provenance"] == "sanitized-live" for record in output)
    assert not any(record["evidence"]["natural_completion_confirmed"] for record in output)
    assert output[2]["evidence"]["reason"] == "ad_unknown"
    assert all(record["evidence"]["reason"] == "session_replaced" for record in output[3:])
    report = inspect_capture(FIXTURE)
    assert report["terminal_candidates"] == 2
    assert report["terminal_content"] == {"requested": 0, "other": 0, "unknown": 2}
    assert report["terminal_ad"] == {"active": 0, "inactive": 0, "unknown": 2}
    assert report["terminal_wire_diagnostics"] == 0
    assert report["end_record_present"] is False  # Selected excerpt, never a complete run.


@pytest.mark.parametrize("adversarial_old_identity", [False, True])
def test_synthetic_strengthening_still_cannot_reclaim_old_content_after_takeover(
    tmp_path, adversarial_old_identity
):
    # This is synthetic policy input derived from the shape of the observed
    # excerpt. Real messages NEVER reported inactive ads or these terminal IDs.
    source = records()
    requested = source[0]["requested_content_id"]
    for record in source:
        record["provenance"] = "synthetic"
        for event in record.get("observations", []):
            if event["kind"] == "media":
                event["ad_break"] = False
                if record["sequence"] == 122 and adversarial_old_identity:
                    event["content_id"] = requested
    changed, destination = tmp_path / "synthetic.jsonl", tmp_path / "replayed.jsonl"
    with JsonlJournal(changed) as journal:
        for record in source:
            journal.write(record)
    replay_capture(changed, destination)
    output = records(destination)
    assert output[2]["evidence"]["receiver_playback_confirmed"] is True
    assert all(record["provenance"] == "synthetic" for record in output)
    assert not any(record["evidence"]["natural_completion_confirmed"] for record in output)
    assert all(record["evidence"]["reason"] == "session_replaced" for record in output[3:])


def test_observed_fixture_has_only_reviewed_scalar_payload_and_pseudonymous_identities():
    content = FIXTURE.read_text()
    assert not any(value in content for value in ("http", "title", "2026-", "wire_"))
    allowed = {
        "kind",
        "active_input",
        "standby",
        "app_session_id",
        "app_id",
        "app_name",
        "monotonic",
        "observed_at",
        "position",
        "duration",
        "empty_status",
        "ad_break",
        "pause_supported",
        "content_id",
        "media_session_id",
        "player_state",
        "idle_reason",
    }
    identity_patterns = {
        "app_session_id": r"session-[0-9a-f]{24}",
        "app_id": r"application-[0-9a-f]{24}",
        "content_id": r"content-[0-9a-f]{24}",
    }
    for record in records():
        assert re.fullmatch(r"capture-[0-9a-f]{24}", record["capture_id"])
        assert record["recorded_at"].startswith("2000-01-01T")
        assert record["entire_run_observed"] is False
        assert record["external_control_unobserved"] is True
        if record["kind"] == "begin":
            assert re.fullmatch(r"device-[0-9a-f]{24}", record["device"]["uuid"])
            assert record["device"]["name"] is None
            assert re.fullmatch(identity_patterns["content_id"], record["requested_content_id"])
        for event in record.get("observations", []):
            assert event.keys() <= allowed
            assert all(
                value is None or isinstance(value, str | bool | int | float)
                for value in event.values()
            )
            for key, pattern in identity_patterns.items():
                if event.get(key) is not None:
                    assert re.fullmatch(pattern, event[key])
            if event["kind"] == "receiver":
                assert event["app_name"] == "YouTube"
