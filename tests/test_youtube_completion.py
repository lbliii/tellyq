"""Synthetic wire-to-policy traces; never live receiver fixtures or network effects."""

import json
from datetime import UTC, datetime

import pytest

from tellyq.cast_backend import CastBackend
from tellyq.cast_messages import normalize_message
from tellyq.domain import policy
from tellyq.domain.values import (
    CompletionAttribution,
    ContentPhase,
    ContentRef,
    EvidenceReason,
    IdentityUpdate,
    IdleReason,
    PlaybackRequest,
    PlaybackTarget,
    PlayerState,
)
from tellyq.lifecycle import LifecycleTracker, replay_capture
from tellyq.models import Observation
from tellyq.youtube_state import YOUTUBE_APPLICATION_ID, YOUTUBE_STATE_SOURCE
from tests.playback_support import FakeClock

TARGET = PlaybackTarget("private-fixture-device", "cast")
CONTENT = ContentRef("youtube", "fixture-requested-title")
REQUEST = PlaybackRequest("request", "attempt", "item", CONTENT, TARGET)
ABSENT = object()


def wire(code=1, *, media=ABSENT, state="PLAYING", idle=None, position=10, **fields):
    status = {
        "mediaSessionId": 37,
        "playerState": state,
        "currentTime": position,
        "customData": {"playerState": code},
        **fields,
    }
    if media is not ABSENT:
        status["media"] = media
    if idle is not None:
        status["idleReason"] = idle
    return {"type": "MEDIA_STATUS", "status": [status]}


def identified(code=1, *, state="PLAYING", position=10, content=CONTENT.content_id, **fields):
    return wire(
        code,
        media={"contentId": content, "duration": 1},
        state=state,
        position=position,
        **fields,
    )


def terminal(**fields):
    return wire(0, state="IDLE", idle="FINISHED", position=13, **fields)


class Transport:
    def __init__(self):
        self.batch: list[Observation] = []
        self.windows = []

    def observe(self, seconds):
        self.windows.append(seconds)
        batch, self.batch = self.batch, []
        return batch

    def play(self, content_id):
        raise AssertionError("No playback in tests")

    def quit(self):
        raise AssertionError("No stop in tests")


class Trace:
    def __init__(self, app_id=YOUTUBE_APPLICATION_ID):
        self.clock = FakeClock()
        self.transport = Transport()
        self.backend = CastBackend(self.transport, TARGET, self.clock, read_only=True)
        self.tracker = LifecycleTracker(REQUEST, 100)
        self.app_id = app_id
        self.raw = []

    def receiver(self, at, *, app_id=None, session="private-fixture-session"):
        return {
            "kind": "receiver",
            "app_id": app_id or self.app_id,
            "app_name": "YouTube",
            "app_session_id": session,
            "monotonic": at,
        }

    def event(self, frame, at):
        event = normalize_message(frame)
        assert event is not None
        return {**event, "monotonic": at}

    def feed(self, frame, at, *, receiver=True):
        self.transport.batch = ([self.receiver(at - 0.05)] if receiver else []) + [
            self.event(frame, at)
        ]
        self.raw.extend(self.transport.batch)
        self.clock.instant = at
        events = self.backend.observe(TARGET)
        self.tracker.observe(events, now=at)
        return events[-1]

    def playing(self):
        self.feed(identified(position=10), 101)
        self.feed(identified(position=12), 103)
        return self

    @property
    def snapshot(self):
        value = self.tracker.snapshot
        assert value is not None
        return value


def test_fresh_source_qualified_progress_and_incremental_terminal_keep_provenance():
    trace = Trace().playing()
    assert trace.snapshot.evidence.receiver_playback_confirmed
    event = trace.feed(terminal(), 104)
    assert event.content is None
    assert event.provider_evidence is not None
    assert event.provider_evidence.phase == ContentPhase.FINISHED
    assert event.ad_active is False
    assert trace.snapshot.state == PlayerState.ENDED
    assert trace.snapshot.evidence.natural_completion_confirmed
    assert not trace.snapshot.evidence.identity_confirmed
    completion = trace.snapshot.completion
    assert completion is not None
    assert completion.attribution == CompletionAttribution.ORDERED_HISTORY
    assert completion.source == YOUTUBE_STATE_SOURCE
    assert completion.identity_sequence < completion.sequence


@pytest.mark.parametrize("code", [True, False, None, "0", 0.0, [], {}, -(2**32), 2**32, -1, 5, 77])
def test_malformed_and_unknown_codes_do_not_qualify_content(code):
    trace = Trace().playing()
    event = trace.feed(wire(code, state="IDLE", idle="FINISHED"), 104)
    assert event.ad_active is None
    assert not trace.snapshot.evidence.natural_completion_confirmed


@pytest.mark.parametrize("code", range(1080, 1086))
def test_every_ad_family_code_blocks_completion_and_invalidates_prior_chain(code):
    trace = Trace().playing()
    event = trace.feed(wire(code, state="IDLE", idle="FINISHED"), 104)
    assert event.ad_active is True
    assert not trace.snapshot.has_confirmed_playback
    trace.feed(terminal(), 105)
    assert not trace.snapshot.evidence.natural_completion_confirmed
    trace.feed(identified(position=14), 106)
    trace.feed(identified(position=16), 108)
    trace.feed(wire(0, state="IDLE", idle="FINISHED", position=17), 109)
    assert trace.snapshot.evidence.natural_completion_confirmed


@pytest.mark.parametrize(
    "status",
    [
        {"breakId": "fixture-break"},
        {"currentBreakTime": 0},
        {},
        None,
        "invalid",
        {"currentBreakTime": -1},
        {"currentBreakTime": True},
    ],
)
def test_explicit_or_ambiguous_cast_break_cannot_qualify_ordinary_content(status):
    trace = Trace().playing()
    event = trace.feed(terminal(breakStatus=status), 104)
    if status in ({"breakId": "fixture-break"}, {"currentBreakTime": 0}):
        assert event.ad_active is True
    else:
        assert event.ad_active is None
    assert not trace.snapshot.evidence.natural_completion_confirmed


@pytest.mark.parametrize(
    "frame",
    [
        wire(0, state="PLAYING", idle="FINISHED"),
        wire(0, state="IDLE"),
        wire(0, state="IDLE", idle="CANCELLED"),
        wire(1, state="IDLE", idle="FINISHED"),
        wire(2, state="PLAYING"),
        wire(3, state="PAUSED"),
        wire(1, state="PLAYING", idle="ERROR"),
    ],
)
def test_standard_provider_contradictions_remain_unknown(frame):
    trace = Trace().playing()
    event = trace.feed(frame, 104)
    assert event.ad_active is None
    assert not trace.snapshot.evidence.natural_completion_confirmed


@pytest.mark.parametrize("media", [None, {}, [], "bad", {"contentId": None}, {"contentId": ""}])
def test_only_true_omission_can_use_history(media):
    trace = Trace().playing()
    trace.feed(terminal(media=media), 104)
    assert not trace.snapshot.evidence.natural_completion_confirmed


@pytest.mark.parametrize("extended", [None, {}, [], {"media": {}}, {"media": None}])
def test_extended_status_ambiguity_cannot_be_treated_as_omitted(extended):
    trace = Trace().playing()
    trace.feed(terminal(extendedStatus=extended), 104)
    assert not trace.snapshot.evidence.natural_completion_confirmed


def test_multiple_statuses_and_missing_media_id_do_not_qualify_terminal():
    for frame in (
        {"type": "MEDIA_STATUS", "status": terminal()["status"] * 2},
        terminal(mediaSessionId=None),
    ):
        trace = Trace().playing()
        trace.feed(frame, 104)
        assert not trace.snapshot.evidence.natural_completion_confirmed


def test_known_app_label_is_insufficient_and_stale_receiver_does_not_qualify():
    trace = Trace("another-application").playing()
    assert not trace.snapshot.evidence.receiver_playback_confirmed
    trace.feed(terminal(), 104)
    assert not trace.snapshot.evidence.natural_completion_confirmed
    trace = Trace().playing()
    trace.feed(terminal(), 110, receiver=False)
    assert not trace.snapshot.evidence.natural_completion_confirmed


@pytest.mark.parametrize(
    "middle",
    [
        identified(5, state="BUFFERING"),
        identified(-1, state="BUFFERING"),
        wire(1, state="PLAYING", media=None),
        identified(position=0),
        identified(mediaSessionId=38),
        {"type": "MEDIA_STATUS", "status": []},
        {"type": "LOAD_FAILED", "reason": "FAILED"},
    ],
)
def test_unknown_backwards_or_media_boundary_invalidates_old_progress(middle):
    trace = Trace().playing()
    trace.feed(middle, 104)
    trace.feed(terminal(), 105)
    assert not trace.snapshot.evidence.natural_completion_confirmed


@pytest.mark.parametrize("code,state", [(2, "PAUSED"), (3, "BUFFERING")])
def test_qualified_same_content_pause_or_buffering_can_preserve_history(code, state):
    trace = Trace().playing()
    trace.feed(identified(code, state=state, position=12), 104)
    assert not trace.snapshot.evidence.receiver_playback_confirmed
    trace.feed(terminal(), 105)
    assert trace.snapshot.evidence.natural_completion_confirmed


def test_incremental_nonterminal_does_not_refresh_exact_identity_anchor():
    trace = Trace().playing()
    event = trace.feed(wire(1, position=12.5), 104)
    assert event.content is None
    assert not trace.snapshot.evidence.identity_confirmed
    trace.feed(terminal(), 105)
    assert trace.snapshot.evidence.natural_completion_confirmed
    trace = Trace().playing()
    for at in range(104, 110):
        trace.feed(wire(1, position=12.5), at)
    trace.feed(terminal(), 110)
    assert not trace.snapshot.evidence.natural_completion_confirmed


def test_receiver_status_alone_does_not_hide_media_gap():
    trace = Trace().playing()
    for at in (105, 107, 109):
        trace.clock.instant = at
        trace.transport.batch = [trace.receiver(at)]
        trace.tracker.observe(trace.backend.observe(TARGET), now=at)
    trace.feed(terminal(), 110)
    assert not trace.snapshot.evidence.natural_completion_confirmed


def test_content_replacement_same_media_id_stays_lost():
    trace = Trace().playing()
    trace.feed(identified(content="different-title"), 104)
    trace.feed(terminal(), 105)
    assert trace.snapshot.ownership_lost
    assert not trace.snapshot.evidence.natural_completion_confirmed
    trace.feed(identified(position=20), 106)
    assert trace.snapshot.ownership_lost


def test_stop_intent_prevents_even_qualified_terminal():
    trace = Trace().playing()
    trace.tracker.snapshot = policy.request_stop(trace.snapshot, boundary=103.5)
    trace.feed(terminal(), 104)
    assert not trace.snapshot.evidence.natural_completion_confirmed


def test_completion_is_historical_once_and_ownership_is_independent():
    trace = Trace().playing()
    trace.feed(terminal(), 104)
    completion = trace.snapshot.completion
    trace.feed(identified(5, state="BUFFERING", position=0), 105)
    assert trace.snapshot.completion is completion
    assert trace.snapshot.state == PlayerState.ENDED
    assert not trace.snapshot.evidence.identity_confirmed
    trace.feed(terminal(), 106)
    assert trace.snapshot.completion is completion
    trace.tracker.snapshot = policy.refresh(trace.snapshot, now=120)
    assert trace.snapshot.evidence.reason == EvidenceReason.STALE
    assert trace.snapshot.completion is completion
    assert not trace.snapshot.evidence.receiver_playback_confirmed
    assert not trace.snapshot.evidence.identity_confirmed
    trace.feed(identified(content="another-title"), 121)
    assert trace.snapshot.ownership_lost
    assert trace.snapshot.completion is completion
    assert trace.snapshot.evidence.natural_completion_confirmed
    trace.feed(identified(position=22), 122)
    assert trace.snapshot.ownership_lost


def test_early_terminal_in_routine_batch_is_retained_before_later_unknown_state():
    trace = Trace().playing()
    trace.transport.batch = [
        trace.receiver(104),
        trace.event(terminal(), 104.1),
        trace.event(identified(5, state="BUFFERING", position=0), 105),
    ]
    trace.clock.instant = 106
    trace.tracker.observe(trace.backend.observe(TARGET), now=106)
    assert trace.transport.windows == [2, 2, 2]
    assert trace.snapshot.evidence.natural_completion_confirmed
    assert trace.snapshot.latest is not None
    assert trace.snapshot.latest.state == PlayerState.BUFFERING


def test_duration_or_single_identified_sample_never_establishes_completion():
    trace = Trace()
    trace.feed(identified(position=9999), 101)
    trace.feed(terminal(), 102)
    assert not trace.snapshot.evidence.natural_completion_confirmed


@pytest.mark.parametrize("boundary", [None, "partial", "gap"])
def test_sanitized_roundtrip_retains_reviewed_provider_inputs_and_historical_attribution(
    tmp_path, boundary
):
    trace = Trace().playing()
    trace.feed(terminal(), 104)
    begin = {
        "kind": "begin",
        "schema_version": 1,
        "capture_id": "private-capture",
        "provenance": "live",
        "sequence": 0,
        "recorded_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        "monotonic": 100,
        "mode": "passive",
        "entire_run_observed": False,
        "external_control_unobserved": True,
        "device": {"uuid": TARGET.device_id, "name": None},
        "requested_content_id": CONTENT.content_id,
    }
    records = [begin]
    for index in range(0, len(trace.raw), 2):
        if index == 4 and boundary is not None:
            records.append(
                {
                    **begin,
                    "kind": "gap" if boundary == "gap" else "window",
                    "sequence": len(records),
                    "monotonic": 103.5,
                    "partial": boundary == "partial",
                    "observations": [],
                }
            )
        at = trace.raw[index + 1]["monotonic"]
        records.append(
            {
                **begin,
                "kind": "window",
                "sequence": len(records),
                "monotonic": at,
                "observations": trace.raw[index : index + 2],
            }
        )
    records.append(
        {
            **begin,
            "kind": "end",
            "sequence": len(records),
            "monotonic": 105,
            "stop_reason": "deadline",
        }
    )
    source, output = tmp_path / "source.jsonl", tmp_path / "output.jsonl"
    source.write_text("".join(json.dumps(record) + "\n" for record in records))
    replay_capture(source, output)
    public = output.read_text()
    assert "private-" not in public
    assert CONTENT.content_id not in public
    assert YOUTUBE_APPLICATION_ID in public
    assert all(
        record["provenance"] == "sanitized-live" for record in map(json.loads, public.splitlines())
    )
    last = json.loads(public.splitlines()[-1])
    assert last["evidence"]["natural_completion_confirmed"] is (boundary is None)
    if boundary is None:
        assert last["evidence"]["completion"]["historical"]
        assert last["evidence"]["completion"]["attribution"] == "ordered_history"
    else:
        assert "completion" not in last["evidence"]
    assert not last["entire_run_observed"]
    assert last["external_control_unobserved"]


def test_provider_source_change_requires_a_new_progress_pair():
    from dataclasses import replace

    trace = Trace().playing()
    original = trace.snapshot.latest
    assert original is not None and original.provider_evidence is not None
    changed = replace(
        original,
        sequence=original.sequence + 1,
        monotonic=104,
        position=13,
        provider_evidence=replace(original.provider_evidence, source="another-reviewed-contract"),
    )
    updated = policy.observe(trace.snapshot, changed, now=104)
    assert not updated.has_confirmed_playback
    assert not updated.evidence.receiver_playback_confirmed
    terminal_event = replace(
        changed,
        sequence=changed.sequence + 1,
        monotonic=105,
        content=None,
        state=PlayerState.IDLE,
        idle_reason=IdleReason.FINISHED,
        identity_update=IdentityUpdate.OMITTED,
        provider_evidence=replace(changed.provider_evidence, phase=ContentPhase.FINISHED),
    )
    assert not policy.observe(
        updated, terminal_event, now=105
    ).evidence.natural_completion_confirmed


def test_sequence_gap_retires_chain_without_claiming_replacement():
    from dataclasses import replace

    trace = Trace().playing()
    event = trace.snapshot.latest
    assert event is not None
    missing = replace(event, sequence=event.sequence + 2, monotonic=104, position=13)
    snapshot = policy.observe(trace.snapshot, missing, now=104)
    assert not snapshot.has_confirmed_playback
    assert not snapshot.ownership_lost


def test_status_preserves_new_completion_followed_by_replacement_in_same_batch():
    from tellyq.application import PlaybackApplication
    from tests.playback_support import MemoryStore

    trace = Trace().playing()
    owned = trace.snapshot
    store = MemoryStore()
    store.save(owned, expected_revision=None)
    trace.transport.batch = [
        trace.receiver(104),
        trace.event(terminal(), 104.1),
        trace.receiver(105, session="replacement-fixture-session"),
        trace.event(identified(content="replacement-content"), 105.1),
    ]
    # The read returns after the receiver callbacks, keeping each within freshness.
    original_observe = trace.transport.observe

    def returning(seconds):
        trace.clock.instant = 106
        return original_observe(seconds)

    trace.transport.observe = returning
    result = PlaybackApplication(trace.backend, store, trace.clock).status(REQUEST, owned)
    assert result.snapshot is not None
    assert result.snapshot.completion is not None
    assert result.snapshot.ownership_lost
    assert result.snapshot.evidence.natural_completion_confirmed
    assert not result.snapshot.evidence.receiver_playback_confirmed
    assert result.reason == EvidenceReason.SESSION_REPLACED
    assert store.load(REQUEST.attempt_id) == result.snapshot


def test_decoder_rejects_boolean_status_count_even_in_normalized_replay():
    from tellyq.youtube_state import interpret_player_state

    event = normalize_message(terminal())
    assert event is not None
    event["wire_status_count"] = True
    evidence = interpret_player_state(event, application_id=YOUTUBE_APPLICATION_ID)
    assert evidence.phase == ContentPhase.UNKNOWN
