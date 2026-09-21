"""Release guards explain existing refusals without expanding release authority."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tellyq.domain.values import (
    CompletionAttribution,
    CompletionEvidence,
    ContentPhase,
    ContentRef,
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
from tellyq.handoff import (
    HandoffDisposition,
    HandoffReason,
    ReleaseAuthority,
    may_release,
    release_decision,
    track_release,
)
from tellyq.models import IPCResponse, IPCTaskView
from tellyq.runner import TaskView
from tellyq.runner_codec import decode_response, encode_response, wire_view


@pytest.fixture
def release_rig():
    target = PlaybackTarget("PRIVATE-device", "cast", "PRIVATE-name")
    request = PlaybackRequest(
        "request", "attempt", "item", ContentRef("youtube", "PRIVATE-content"), target
    )
    scope = PlaybackScope(request, "PRIVATE-session", "PRIVATE-connection", 0, "PRIVATE-app")
    terminal = PlaybackObservation(
        target,
        scope.connection_generation,
        10,
        datetime(2026, 1, 1, tzinfo=UTC),
        100,
        session_id=scope.session_id,
        application_id=scope.application_id,
        playback_id="PRIVATE-media",
        content=request.content,
        identity_update=IdentityUpdate.EXPLICIT,
        state=PlayerState.IDLE,
        idle_reason=IdleReason.FINISHED,
        position=50,
        ad_active=False,
        provider_evidence=ProviderEvidence("PRIVATE-provider-source", ContentPhase.FINISHED),
    )
    sample = SessionSnapshot(
        scope,
        latest=terminal,
        completion=CompletionEvidence(
            terminal.observed_at, 100, 10, 9, CompletionAttribution.EXPLICIT, "PRIVATE-source"
        ),
    )
    receiver = replace(
        terminal,
        sequence=11,
        monotonic=100.1,
        session_active=True,
        content=None,
        position=None,
        provider_evidence=None,
        state=PlayerState.UNKNOWN,
    )
    media = replace(terminal, sequence=12, monotonic=100.2)
    return sample, ReleaseAuthority(terminal, 10, 100), receiver, media


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"sequence": 12}, HandoffReason.SEQUENCE_GAP),
        ({"monotonic": 99}, HandoffReason.CLOCK_DISCONTINUITY),
        ({"monotonic": 106}, HandoffReason.OBSERVATION_GAP),
        ({"target": PlaybackTarget("other", "cast")}, HandoffReason.TARGET_CHANGED),
        ({"connection_generation": "other"}, HandoffReason.CONNECTION_CHANGED),
        ({"connection_reset": True}, HandoffReason.CONNECTION_RESET),
        ({"ad_active": True}, HandoffReason.AD_ACTIVE),
        ({"content": ContentRef("youtube", "other")}, HandoffReason.CONTENT_CHANGED),
        ({"session_id": "other"}, HandoffReason.SESSION_CHANGED),
        ({"application_id": "other"}, HandoffReason.APPLICATION_CHANGED),
        ({"playback_id": "other"}, HandoffReason.MEDIA_CHANGED),
        ({"state": PlayerState.PLAYING}, HandoffReason.PLAYBACK_RESTARTED),
        ({"state": PlayerState.PAUSED}, HandoffReason.PLAYBACK_RESTARTED),
        (
            {"provider_evidence": ProviderEvidence("fake", ContentPhase.AD)},
            HandoffReason.PROVIDER_ACTIVE,
        ),
        (
            {"provider_evidence": ProviderEvidence("fake", ContentPhase.PLAYING)},
            HandoffReason.PROVIDER_ACTIVE,
        ),
        (
            {"provider_evidence": ProviderEvidence("fake", ContentPhase.PAUSED)},
            HandoffReason.PROVIDER_ACTIVE,
        ),
        ({"position": 0}, HandoffReason.POSITION_REWOUND),
    ],
)
def test_first_veto_is_precise_and_survives_duplicate_batches_and_later_idle(
    release_rig, changes, reason
):
    sample, authority, receiver, media = release_rig
    event = (
        replace(media, sequence=11, **changes)
        if "sequence" not in changes
        else replace(media, **changes)
    )
    blocked = track_release(sample, (event,), authority)
    assert blocked is not None and blocked.blocked
    assert blocked.first_veto is not None and blocked.first_veto.reason == reason
    assert not may_release(sample, blocked, (receiver, media), now=101)
    assert track_release(sample, (event,), blocked) == blocked
    idle = replace(
        receiver,
        sequence=blocked.last_sequence + 1,
        monotonic=blocked.last_monotonic + 0.1,
        session_active=False,
        session_id=None,
        application_id=None,
    )
    later = track_release(sample, (idle,), blocked)
    assert later is not None and later.blocked and later.first_veto == blocked.first_veto


@pytest.mark.parametrize(
    ("which", "changes", "reason"),
    [
        (
            "receiver",
            {"session_active": False, "session_id": None, "application_id": None},
            HandoffReason.RECEIVER_INACTIVE,
        ),
        ("receiver", {"session_id": "other"}, HandoffReason.RECEIVER_IDENTITY_MISMATCH),
        ("receiver", {"application_id": "other"}, HandoffReason.RECEIVER_IDENTITY_MISMATCH),
        (
            "receiver",
            {"target": PlaybackTarget("other", "cast")},
            HandoffReason.RECEIVER_IDENTITY_MISMATCH,
        ),
        ("receiver", {"connection_generation": "other"}, HandoffReason.RECEIVER_IDENTITY_MISMATCH),
        ("receiver", {"monotonic": 90}, HandoffReason.RECEIVER_STALE),
        ("receiver", {"monotonic": 102}, HandoffReason.RECEIVER_STALE),
        ("media", {"monotonic": 90}, HandoffReason.MEDIA_STALE),
        ("media", {"monotonic": 102}, HandoffReason.MEDIA_STALE),
        ("media", {"content": None}, HandoffReason.MEDIA_IDENTITY_MISSING),
        (
            "media",
            {"identity_update": IdentityUpdate.OMITTED},
            HandoffReason.MEDIA_IDENTITY_MISSING,
        ),
        (
            "media",
            {"content": ContentRef("youtube", "other")},
            HandoffReason.MEDIA_IDENTITY_MISMATCH,
        ),
        ("media", {"session_id": None}, HandoffReason.MEDIA_IDENTITY_MISMATCH),
        ("media", {"application_id": None}, HandoffReason.MEDIA_IDENTITY_MISMATCH),
        ("media", {"playback_id": "other"}, HandoffReason.MEDIA_IDENTITY_MISMATCH),
        (
            "media",
            {"target": PlaybackTarget("other", "cast")},
            HandoffReason.MEDIA_IDENTITY_MISMATCH,
        ),
        ("media", {"connection_generation": "other"}, HandoffReason.MEDIA_IDENTITY_MISMATCH),
        ("media", {"ad_active": True}, HandoffReason.AD_ACTIVE),
        ("media", {"state": PlayerState.PLAYING}, HandoffReason.MEDIA_STATE_UNQUALIFIED),
        ("media", {"state": PlayerState.PAUSED}, HandoffReason.MEDIA_STATE_UNQUALIFIED),
        ("media", {"state": PlayerState.UNKNOWN}, HandoffReason.MEDIA_STATE_UNQUALIFIED),
        ("media", {"idle_reason": IdleReason.CANCELED}, HandoffReason.MEDIA_STATE_UNQUALIFIED),
        (
            "media",
            {"provider_evidence": ProviderEvidence("fake", ContentPhase.AD)},
            HandoffReason.PROVIDER_PHASE_UNQUALIFIED,
        ),
    ],
)
def test_fresh_current_identity_guards_explain_unchanged_refusals(
    release_rig, which, changes, reason
):
    sample, authority, receiver, media = release_rig
    if which == "receiver":
        receiver = replace(receiver, **changes)
    else:
        media = replace(media, **changes)
    diagnostic = release_decision(sample, authority, (receiver, media), now=101)
    assert diagnostic.disposition == HandoffDisposition.HOLD and diagnostic.reason == reason
    assert not may_release(sample, authority, (receiver, media), now=101)


@pytest.mark.parametrize(
    "changes",
    [
        {"playback_id": None},
        {"ad_active": None},
        {"ad_active": True},
        {"state": PlayerState.BUFFERING},
        {"idle_reason": None},
        {"target": PlaybackTarget("other", "cast")},
        {"connection_generation": "other"},
        {"session_id": None},
        {"application_id": None},
        {"content": ContentRef("youtube", "other")},
    ],
)
def test_terminal_qualification_is_not_relaxed(release_rig, changes):
    sample, authority, receiver, media = release_rig
    authority = replace(authority, terminal=replace(authority.terminal, **changes))
    result = release_decision(sample, authority, (receiver, media), now=101)
    assert result.reason == HandoffReason.TERMINAL_UNQUALIFIED
    assert not may_release(sample, authority, (receiver, media), now=101)


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ("authority", HandoffReason.AUTHORITY_REQUIRED),
        ("blocked", HandoffReason.AUTHORITY_BLOCKED),
        ("completion", HandoffReason.COMPLETION_REQUIRED),
        ("mismatched_completion", HandoffReason.COMPLETION_MISMATCH),
        ("ownership", HandoffReason.OWNERSHIP_LOST),
        ("stop", HandoffReason.STOP_REQUESTED),
        ("stale", HandoffReason.TERMINAL_STALE),
        ("receiver", HandoffReason.RECEIVER_MISSING),
        ("media", HandoffReason.MEDIA_MISSING),
    ],
)
def test_missing_stale_and_revoked_authority_hold(release_rig, condition, reason):
    sample, authority, receiver, media = release_rig
    events, now = (receiver, media), 101
    if condition == "authority":
        authority = None
    elif condition == "blocked":
        authority = replace(authority, blocked=True)
    elif condition == "completion":
        sample = replace(sample, completion=None)
    elif condition == "mismatched_completion":
        sample = replace(sample, completion=replace(sample.completion, sequence=9))
    elif condition == "ownership":
        sample = replace(sample, ownership_lost=True)
    elif condition == "stop":
        sample = replace(sample, stop_requested=True)
    elif condition == "stale":
        now = 106
    elif condition == "receiver":
        events = (media,)
    else:
        events = (receiver,)
    assert release_decision(sample, authority, events, now=now).reason == reason
    assert not may_release(sample, authority, events, now=now)


@pytest.mark.parametrize("state", [PlayerState.IDLE, PlayerState.BUFFERING])
@pytest.mark.parametrize(
    "phase", [None, ContentPhase.UNKNOWN, ContentPhase.FINISHED, ContentPhase.BUFFERING]
)
def test_unchanged_narrow_release_eligibility_preserves_unknown_ads(release_rig, state, phase):
    sample, authority, receiver, media = release_rig
    media = replace(
        media,
        state=state,
        ad_active=None,
        provider_evidence=ProviderEvidence("fake", phase) if phase else None,
    )
    authority = track_release(sample, (receiver, media), authority)
    decision = release_decision(sample, authority, (receiver, media), now=101)
    assert may_release(sample, authority, (receiver, media), now=101)
    assert decision.reason == HandoffReason.RELEASE_ELIGIBLE
    assert decision.evidence is not None
    assert decision.evidence.ad_active is None
    assert decision.evidence.provider_phase == phase


def test_public_projection_is_bounded_private_free_and_age_is_captured(release_rig):
    sample, authority, receiver, media = release_rig
    reset = replace(
        media,
        state=PlayerState.BUFFERING,
        position=0,
        ad_active=None,
        provider_evidence=ProviderEvidence("PRIVATE-source", ContentPhase.UNKNOWN),
    )
    authority = track_release(sample, (receiver, reset), authority)
    diagnosis = release_decision(sample, authority, (receiver, reset), now=101)
    view = TaskView(handoff=diagnosis)
    encoded = json.dumps(wire_view(view))
    assert "PRIVATE" not in encoded and len(encoded) < 2000
    handoff = wire_view(view)["handoff"]
    assert handoff is not None and handoff["first_veto"] is not None
    assert handoff["evidence"] is not None
    assert handoff["reason"] == "authority_blocked"
    assert handoff["first_veto"]["reason"] == "position_rewound"
    assert handoff["first_veto"]["evidence"]["terminal_position"] == 50
    assert handoff["first_veto"]["evidence"]["position"] == 0
    assert handoff["first_veto"]["evidence"]["content_matches"] is True
    assert handoff["first_veto"]["evidence"]["ad_active"] is None
    assert handoff["first_veto"]["evidence"]["sequence_delta"] == 1
    assert handoff["first_veto"]["evidence"]["age_seconds"] is None
    assert handoff["evidence"]["age_seconds"] == pytest.approx(0.8)
    later = release_decision(sample, authority, (receiver, reset), now=110)
    assert later.evidence is not None
    assert later.evidence.age_seconds == pytest.approx(9.8)
    assert json.dumps(wire_view(view)) == encoded  # Cached views do not pretend to refresh.
    assert later.first_veto == diagnosis.first_veto


def test_version_one_decoder_accepts_old_and_additive_views():
    # The decoder never hydrates projected data into domain authority.
    views: tuple[IPCTaskView, ...] = ({"playback": None, "queue": None}, wire_view(TaskView()))
    for view in views:
        response: IPCResponse = {
            "schema_version": 1,
            "ok": True,
            "code": "ticket",
            "ticket_id": "command",
            "ticket": {
                "command_id": "command",
                "action": "start",
                "state": "handled",
                "failure": None,
                "view": view,
            },
        }
        assert decode_response(encode_response(response)) == response
