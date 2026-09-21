"""Offline queue decisions over synthetic, ordered playback traces."""

import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tellyq.domain import policy
from tellyq.domain.queue import (
    ExecutionState,
    JournalCommand,
    QueueAttempt,
    QueueEntry,
    QueueIntent,
    QueueSnapshot,
)
from tellyq.domain.queue_policy import (
    QueueAuthority,
    QueueDisposition,
    QueueReason,
    SkipIntent,
    decide_queue,
)
from tellyq.domain.values import (
    CommandAction,
    CompletionAttribution,
    ContentPhase,
    ContentRef,
    EvidencePolicy,
    IdentityUpdate,
    IdleReason,
    PlaybackEvidence,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackScope,
    PlaybackTarget,
    PlayerState,
    ProviderEvidence,
    SessionSnapshot,
)
from tellyq.queue_store import SQLiteQueueStore

AT = datetime(2026, 1, 1, tzinfo=UTC)
TARGET = PlaybackTarget("synthetic-device", "synthetic-route")
CONTENT = ContentRef("synthetic-provider", "program-one")
AUTHORITY = QueueAuthority("queue", 0, "connection", 10.0)
REQUEST = PlaybackRequest("request", "attempt", "first", CONTENT, TARGET)
SCOPE = PlaybackScope(REQUEST, "session", "connection", 11.0, "application")


def empty_queue():
    return QueueSnapshot(
        "queue",
        TARGET,
        0,
        0,
        None,
        False,
        (QueueEntry("first", CONTENT), QueueEntry("second", ContentRef("p", "second"))),
    )


def current_queue():
    queue = empty_queue()
    return replace(
        queue,
        revision=3,
        current_item_id="first",
        items=(replace(queue.items[0], intent=QueueIntent.CURRENT), queue.items[1]),
        attempts=(QueueAttempt("attempt", "first", QueueIntent.CURRENT, AT),),
        commands=(
            JournalCommand(
                "start", "attempt", CommandAction.START, ExecutionState.ACKNOWLEDGED, 0, AT, AT
            ),
        ),
    )


def sample(sequence, at, *, state=PlayerState.PLAYING, phase=ContentPhase.PLAYING):
    return PlaybackObservation(
        TARGET,
        "connection",
        sequence,
        AT,
        at,
        session_id="session",
        content=CONTENT,
        state=state,
        position=at - 12,
        duration=1,
        ad_active=False,
        application_id="application",
        playback_id="media",
        identity_update=IdentityUpdate.EXPLICIT,
        provider_evidence=ProviderEvidence("synthetic-contract", phase),
    )


def playing():
    snapshot = SessionSnapshot(SCOPE)
    for event in (sample(1, 12.0), sample(2, 13.0)):
        snapshot = policy.observe(snapshot, event, now=event.monotonic)
    assert snapshot.evidence.receiver_playback_confirmed
    return snapshot


def ended(*, anonymous=True):
    terminal = replace(
        sample(3, 14.0, state=PlayerState.IDLE, phase=ContentPhase.FINISHED),
        idle_reason=IdleReason.FINISHED,
        content=None if anonymous else CONTENT,
        identity_update=IdentityUpdate.OMITTED if anonymous else IdentityUpdate.EXPLICIT,
    )
    snapshot = policy.observe(playing(), terminal, now=14.0)
    assert snapshot.completion is not None
    return snapshot


def idle(sequence=4, at=15.0):
    return PlaybackObservation(
        TARGET,
        "connection",
        sequence,
        AT,
        at,
        state=PlayerState.IDLE,
        session_active=False,
    )


def settle(queue, decision):
    change = decision.settlement
    assert change is not None
    return replace(
        queue,
        revision=queue.revision + 1,
        items=tuple(
            replace(item, intent=change.outcome) if item.item_id == change.item_id else item
            for item in queue.items
        ),
        attempts=tuple(
            replace(attempt, intent=change.outcome, decision_id=change.decision_id)
            if attempt.attempt_id == change.attempt_id
            else attempt
            for attempt in queue.attempts
        ),
    )


def decide(queue, snapshot, *, now=14.0, **kwargs):
    return decide_queue(queue, snapshot, now=now, authority=AUTHORITY, **kwargs)


def assert_hold(decision, reason):
    assert decision.disposition == QueueDisposition.HOLD
    assert decision.reason == reason
    assert decision.next_item_id is None


def test_initial_start_selects_first_pending_only_with_fresh_explicit_idle():
    queue = empty_queue()
    assert_hold(decide(queue, None), QueueReason.RECEIVER_UNCONFIRMED)
    result = decide(queue, None, receiver=idle(at=14.0))
    assert result.disposition == QueueDisposition.READY
    assert result.next_item_id == "first"
    assert result.settlement is None
    assert result.expected_revision == queue.revision
    assert result.generation == queue.generation
    assert queue.items[0].intent == QueueIntent.PENDING


@pytest.mark.parametrize("anonymous", [False, True])
def test_completion_requires_settlement_reload_then_separate_next_decision(anonymous):
    queue, snapshot = current_queue(), ended(anonymous=anonymous)
    result = decide(queue, snapshot)
    assert_hold(result, QueueReason.COMPLETION_OBSERVED)
    assert result.settlement is not None
    assert result.settlement.outcome == QueueIntent.FINISHED
    assert result.settlement.attempt_id == "attempt"
    assert result.settlement.item_id == "first"
    saved = settle(queue, result)
    next_result = decide(saved, snapshot)
    assert next_result.disposition == QueueDisposition.READY
    assert next_result.next_item_id == "second"
    assert next_result.settlement is None
    assert next_result.expected_revision == saved.revision


def test_repeated_terminal_decisions_have_stable_identity_and_are_consumed_once():
    queue, snapshot = current_queue(), ended()
    first = decide(queue, snapshot)
    assert first == decide(queue, snapshot)
    assert first.settlement == decide(replace(queue, revision=10), snapshot, now=15).settlement
    saved = settle(queue, first)
    duplicate = policy.observe(snapshot, snapshot.latest, now=14.0)
    assert decide(saved, duplicate).settlement is None


def test_decision_identity_is_scoped_to_attempt_and_witness():
    queue, snapshot = current_queue(), ended()
    first = decide(queue, snapshot).settlement
    changed_attempt = replace(queue.attempts[0], attempt_id="another-attempt")
    changed_queue = replace(
        queue,
        attempts=(changed_attempt,),
        commands=(replace(queue.commands[0], attempt_id="another-attempt"),),
    )
    changed_session = replace(
        snapshot,
        scope=replace(snapshot.scope, request=replace(REQUEST, attempt_id="another-attempt")),
    )
    other = decide(changed_queue, changed_session).settlement
    assert first is not None and other is not None
    assert first.decision_id != other.decision_id
    assert snapshot.completion is not None
    newer = replace(
        snapshot, completion=replace(snapshot.completion, monotonic=14.1), last_monotonic=14.1
    )
    another = decide(queue, newer, now=14.1).settlement
    assert another is not None
    assert first.decision_id != another.decision_id


@pytest.mark.parametrize("kind", ["replacement", "app", "reset"])
def test_terminal_then_replacement_preserves_settlement_but_holds_next(kind):
    snapshot = ended()
    event = sample(4, 15.0)
    if kind == "replacement":
        event = replace(event, content=ContentRef("p", "autoplay"))
    elif kind == "app":
        event = replace(event, session_id="another-session")
    else:
        event = replace(event, connection_reset=True)
    snapshot = policy.observe(snapshot, event, now=15.0)
    assert snapshot.ownership_lost and snapshot.completion is not None
    result = decide(current_queue(), snapshot, now=15.0)
    assert_hold(result, QueueReason.RECEIVER_REPLACED)
    assert result.settlement is not None
    saved = settle(current_queue(), result)
    after = decide(saved, snapshot, now=16.0, receiver=idle(5, 16.0))
    assert_hold(after, QueueReason.RECEIVER_REPLACED)
    assert after.settlement is None


@pytest.mark.parametrize(
    "state,phase,ad",
    [
        (PlayerState.PLAYING, ContentPhase.AD, True),
        (PlayerState.BUFFERING, ContentPhase.UNKNOWN, None),
        (PlayerState.PAUSED, ContentPhase.PAUSED, False),
        (PlayerState.PLAYING, ContentPhase.PLAYING, False),
        (PlayerState.UNKNOWN, ContentPhase.UNKNOWN, None),
    ],
)
def test_newer_media_cannot_reuse_historical_terminal_for_launch(state, phase, ad):
    snapshot = policy.observe(
        ended(), replace(sample(4, 15.0, state=state, phase=phase), ad_active=ad), now=15.0
    )
    result = decide(current_queue(), snapshot, now=15.0)
    assert result.settlement is not None
    assert_hold(
        decide(settle(current_queue(), result), snapshot, now=15.0),
        QueueReason.RECEIVER_UNCONFIRMED,
    )


def test_fresh_matching_receiver_heartbeat_does_not_refresh_old_terminal():
    snapshot = policy.observe(
        ended(), replace(sample(4, 15.0), content=None, session_active=True), now=15.0
    )
    result = decide(current_queue(), snapshot, now=15.0)
    assert result.settlement is not None
    assert_hold(
        decide(settle(current_queue(), result), snapshot, now=15.0),
        QueueReason.RECEIVER_UNCONFIRMED,
    )


def test_stale_witness_can_settle_history_but_never_authorize_a_launch():
    snapshot = policy.refresh(ended(), now=30.0)
    result = decide(current_queue(), snapshot, now=30.0)
    assert result.settlement is not None
    saved = settle(current_queue(), result)
    assert_hold(decide(saved, snapshot, now=30.0), QueueReason.RECEIVER_UNCONFIRMED)
    assert decide(saved, snapshot, now=30.0, receiver=idle(4, 30.0)).next_item_id == "second"


@pytest.mark.parametrize(
    "state,phase,ad",
    [
        (PlayerState.PLAYING, ContentPhase.PLAYING, False),
        (PlayerState.PAUSED, ContentPhase.PAUSED, False),
        (PlayerState.BUFFERING, ContentPhase.BUFFERING, False),
        (PlayerState.IDLE, ContentPhase.AD, True),
        (PlayerState.UNKNOWN, ContentPhase.UNKNOWN, None),
    ],
)
def test_duration_progress_ads_and_pause_cannot_finish_without_witness(state, phase, ad):
    event = replace(
        sample(3, 14.0, state=state, phase=phase),
        position=1000,
        duration=1,
        ad_active=ad,
        idle_reason=IdleReason.FINISHED if state == PlayerState.IDLE else None,
    )
    snapshot = policy.observe(playing(), event, now=14.0)
    result = decide(current_queue(), snapshot)
    assert_hold(result, QueueReason.AWAITING_COMPLETION)
    assert result.settlement is None


def test_ended_state_and_evidence_flag_alone_are_insufficient():
    snapshot = replace(
        playing(),
        state=PlayerState.ENDED,
        evidence=PlaybackEvidence(natural_completion_confirmed=True),
    )
    result = decide(current_queue(), snapshot)
    assert_hold(result, QueueReason.AWAITING_COMPLETION)
    assert result.settlement is None


@pytest.mark.parametrize("where", ["queue", "session", "journal"])
def test_stop_and_cancellation_dominate_completion_and_explicit_skip(where):
    queue, snapshot = current_queue(), ended()
    if where == "queue":
        queue = replace(queue, cancellation_requested=True)
    elif where == "session":
        snapshot = policy.request_stop(snapshot)
    else:
        queue = replace(
            queue,
            commands=(
                *queue.commands,
                JournalCommand(
                    "stop", "attempt", CommandAction.STOP, ExecutionState.PENDING, 0, AT, AT
                ),
            ),
        )
    for skip in (None, SkipIntent("skip", "attempt", "first")):
        result = decide(queue, snapshot, skip=skip)
        assert_hold(result, QueueReason.CANCELED)
        assert result.settlement is None


def test_cancellation_between_settlement_and_prepare_start_blocks_advancement():
    snapshot, queue = ended(), current_queue()
    saved = settle(queue, decide(queue, snapshot))
    canceled = replace(saved, revision=saved.revision + 1, cancellation_requested=True)
    result = decide(canceled, snapshot)
    assert_hold(result, QueueReason.CANCELED)
    assert result.expected_revision == canceled.revision


def test_skip_is_explicit_idempotent_local_intent_without_remote_completion():
    queue, snapshot = current_queue(), playing()
    skip = SkipIntent("operator-skip", "attempt", "first")
    result = decide(queue, snapshot, skip=skip)
    assert_hold(result, QueueReason.SKIP_REQUESTED)
    assert result.settlement is not None
    assert result.settlement.outcome == QueueIntent.SKIPPED
    assert result == decide(queue, snapshot, skip=skip)
    assert snapshot.completion is None
    saved = settle(queue, result)
    assert_hold(decide(saved, snapshot), QueueReason.RECEIVER_UNCONFIRMED)
    fresh_idle = decide(saved, snapshot, now=15.0, receiver=idle())
    assert fresh_idle.next_item_id == "second"
    assert fresh_idle.settlement is None


@pytest.mark.parametrize(
    "skip", [SkipIntent("skip", "wrong", "first"), SkipIntent("skip", "attempt", "wrong")]
)
def test_mismatched_skip_cannot_consume_an_attempt(skip):
    result = decide(current_queue(), ended(), skip=skip)
    assert_hold(result, QueueReason.SCOPE_MISMATCH)
    assert result.settlement is None


@pytest.mark.parametrize(
    "state",
    [
        ExecutionState.PENDING,
        ExecutionState.DISPATCHED,
        ExecutionState.UNCERTAIN,
        ExecutionState.REJECTED,
    ],
)
def test_unresolved_or_rejected_start_requires_reconciliation_even_with_a_witness(state):
    queue = current_queue()
    queue = replace(queue, commands=(replace(queue.commands[0], state=state),))
    result = decide(queue, ended())
    assert_hold(result, QueueReason.COMMAND_UNRESOLVED)
    assert result.settlement is None


@pytest.mark.parametrize(
    "authority",
    [
        None,
        replace(AUTHORITY, queue_id="other"),
        replace(AUTHORITY, generation=1),
        replace(AUTHORITY, connection_generation="other"),
        replace(AUTHORITY, started_monotonic=12.0),
        replace(AUTHORITY, started_monotonic=30.0),
    ],
)
def test_absent_wrong_or_new_owner_authority_cannot_reuse_old_history(authority):
    result = decide_queue(current_queue(), ended(), now=14.0, authority=authority)
    assert result.disposition == QueueDisposition.HOLD
    assert result.settlement is None
    assert result.next_item_id is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt_id", "other-attempt"),
        ("queue_item_id", "other-item"),
        ("content", ContentRef("p", "other")),
        ("target", PlaybackTarget("other-device", "synthetic-route")),
    ],
)
def test_request_correlation_is_required(field, value):
    snapshot = ended()
    snapshot = replace(snapshot, scope=replace(SCOPE, request=replace(REQUEST, **{field: value})))
    result = decide(current_queue(), snapshot)
    assert_hold(result, QueueReason.SCOPE_MISMATCH)
    assert result.settlement is None


def test_restarted_session_history_is_not_completion_or_authority():
    restored = SessionSnapshot(SCOPE)
    assert_hold(decide(current_queue(), restored), QueueReason.AWAITING_COMPLETION)
    snapshot = ended()
    saved = settle(current_queue(), decide(current_queue(), snapshot))
    assert_hold(decide(saved, None, receiver=idle(at=14.0)), QueueReason.SESSION_REQUIRED)
    assert_hold(decide(saved, restored), QueueReason.INVALID_COMPLETION)


def test_recovered_queue_generation_and_attention_state_hold_old_workers():
    queue = replace(
        current_queue(),
        generation=1,
        cancellation_requested=True,
        items=(QueueEntry("first", CONTENT, QueueIntent.NEEDS_ATTENTION), empty_queue().items[1]),
        attempts=(QueueAttempt("attempt", "first", QueueIntent.NEEDS_ATTENTION, AT),),
    )
    assert_hold(decide(queue, ended()), QueueReason.CANCELED)
    queue = replace(queue, cancellation_requested=False)
    assert_hold(decide(queue, ended()), QueueReason.SCOPE_MISMATCH)
    result = decide_queue(queue, ended(), now=14.0, authority=replace(AUTHORITY, generation=1))
    assert_hold(result, QueueReason.NEEDS_ATTENTION)


@pytest.mark.parametrize(
    "changes",
    [
        {"monotonic": 7.0},
        {"monotonic": float("nan")},
        {"monotonic": 100.0},
        {"sequence": 999},
        {"sequence": True},
        {"identity_sequence": 999},
        {"source": ""},
        {"observed_at": AT.replace(tzinfo=None)},
        {"attribution": CompletionAttribution.EXPLICIT},
    ],
)
def test_inconsistent_witness_fails_closed(changes):
    snapshot = ended()
    assert snapshot.completion is not None
    snapshot = replace(snapshot, completion=replace(snapshot.completion, **changes))
    result = decide(current_queue(), snapshot)
    assert_hold(result, QueueReason.INVALID_COMPLETION)
    assert result.settlement is None


@pytest.mark.parametrize(
    "event",
    [
        idle(at=8.0),
        idle(at=20.0),
        replace(idle(at=14.0), connection_reset=True),
        replace(idle(at=14.0), target=PlaybackTarget("other", "route")),
        replace(idle(at=14.0), connection_generation="old"),
        replace(idle(at=14.0), session_active=None),
        replace(idle(at=14.0), state=PlayerState.UNKNOWN, session_active=None),
    ],
)
def test_uncertain_or_mismatched_idle_cannot_start_initial_queue(event):
    assert_hold(decide(empty_queue(), None, receiver=event), QueueReason.RECEIVER_UNCONFIRMED)


@pytest.mark.parametrize(
    "event", [idle(1, 15.0), idle(9, 12.0), idle(2, 13.0), idle(2, 15.0), idle(9, 13.0)]
)
def test_old_idle_does_not_override_newer_playback(event):
    queue, snapshot = current_queue(), playing()
    saved = settle(queue, decide(queue, snapshot, skip=SkipIntent("skip", "attempt", "first")))
    assert_hold(decide(saved, snapshot, now=15.0, receiver=event), QueueReason.RECEIVER_UNCONFIRMED)


def test_new_unknown_receiver_message_cannot_be_hidden_behind_terminal():
    snapshot = ended()
    saved = settle(current_queue(), decide(current_queue(), snapshot))
    assert_hold(
        decide(saved, snapshot, now=15.0, receiver=replace(idle(), session_active=None)),
        QueueReason.RECEIVER_UNCONFIRMED,
    )


def test_final_item_settles_then_reports_exhaustion_without_a_start_effect():
    queue, snapshot = current_queue(), ended()
    queue = replace(queue, items=queue.items[:1])
    saved = settle(queue, decide(queue, snapshot))
    result = decide(saved, snapshot)
    assert result.disposition == QueueDisposition.COMPLETE
    assert result.reason == QueueReason.EXHAUSTED
    assert result.next_item_id is None
    assert result.settlement is None


def test_legacy_finished_items_are_not_current_process_authority():
    queue = empty_queue()
    queue = replace(
        queue, items=(replace(queue.items[0], intent=QueueIntent.FINISHED), queue.items[1])
    )
    assert_hold(decide(queue, None, receiver=idle(at=14.0)), QueueReason.NEEDS_ATTENTION)


def test_selection_preserves_order_across_repeated_content():
    queue, snapshot = current_queue(), ended()
    queue = replace(queue, items=(*queue.items, QueueEntry("third", CONTENT)))
    saved = settle(queue, decide(queue, snapshot))
    assert decide(saved, snapshot).next_item_id == "second"


@pytest.mark.parametrize(
    "kind",
    [
        "duplicates",
        "current",
        "missing-command",
        "wrong-command",
        "out-of-order",
        "missing-decision",
        "future-command",
    ],
)
def test_inconsistent_queue_snapshots_cannot_make_decisions(kind):
    queue, snapshot = current_queue(), ended()
    if kind == "duplicates":
        queue = replace(queue, items=queue.items + queue.items[:1])
    elif kind == "current":
        queue = replace(queue, current_item_id="second")
    elif kind == "missing-command":
        queue = replace(queue, commands=())
    elif kind == "wrong-command":
        queue = replace(queue, commands=(replace(queue.commands[0], attempt_id="other"),))
    elif kind == "out-of-order":
        queue = replace(queue, items=tuple(reversed(queue.items)))
    elif kind == "missing-decision":
        queue = replace(
            queue,
            items=(replace(queue.items[0], intent=QueueIntent.FINISHED), queue.items[1]),
            attempts=(replace(queue.attempts[0], intent=QueueIntent.FINISHED),),
        )
    else:
        queue = replace(queue, commands=(replace(queue.commands[0], generation=1),))
    result = decide(queue, snapshot)
    assert_hold(result, QueueReason.INVALID_QUEUE)
    assert result.settlement is None


def test_policy_age_is_explicit_and_values_are_immutable():
    snapshot = ended()
    saved = settle(current_queue(), decide(current_queue(), snapshot))
    result = decide(saved, snapshot, now=16.0, policy=EvidencePolicy(max_age_seconds=1.0))
    assert_hold(result, QueueReason.RECEIVER_UNCONFIRMED)
    with pytest.raises(FrozenInstanceError):
        result.next_item_id = "injected"
    with pytest.raises(FrozenInstanceError):
        AUTHORITY.__setattr__("generation", 1)
    with pytest.raises(ValueError, match="current time"):
        decide(saved, snapshot, now=float("nan"))


def test_settled_decision_must_match_the_current_process_witness():
    queue, snapshot = current_queue(), ended()
    saved = settle(queue, decide(queue, snapshot))
    saved = replace(saved, attempts=(replace(saved.attempts[0], decision_id="other-witness"),))
    result = decide(saved, snapshot, now=15.0, receiver=idle())
    assert_hold(result, QueueReason.INVALID_COMPLETION)
    assert result.settlement is None


@pytest.mark.parametrize("cancel", [False, True])
def test_policy_settlement_round_trips_through_durable_store(tmp_path, cancel):
    store = SQLiteQueueStore(tmp_path / "runtime")
    queue = store.create("queue", TARGET, empty_queue().items)
    queue = store.prepare_start(
        "queue",
        "first",
        attempt_id="attempt",
        command_id="start",
        at=AT,
        expected_revision=queue.revision,
    )
    queue = store.mark_dispatched("queue", "start", at=AT, expected_revision=queue.revision)
    queue = store.record_outcome(
        "queue", "start", ExecutionState.ACKNOWLEDGED, at=AT, expected_revision=queue.revision
    )
    snapshot = ended()
    decision = decide(queue, snapshot)
    settlement = decision.settlement
    assert settlement is not None
    saved = store.settle_attempt(
        decision.queue_id,
        settlement.attempt_id,
        settlement.outcome,
        decision_id=settlement.decision_id,
        expected_revision=decision.expected_revision,
    )
    duplicate = store.settle_attempt(
        decision.queue_id,
        settlement.attempt_id,
        settlement.outcome,
        decision_id=settlement.decision_id,
        expected_revision=decision.expected_revision,
    )
    assert duplicate == saved
    if cancel:
        saved = store.cancel_queue("queue", expected_revision=saved.revision)
    reloaded = store.load("queue")
    assert reloaded == saved
    result = decide(reloaded, snapshot)
    assert result.settlement is None
    if cancel:
        assert_hold(result, QueueReason.CANCELED)
    else:
        assert result.disposition == QueueDisposition.READY
        assert result.next_item_id == "second"
        started = store.prepare_start(
            "queue",
            result.next_item_id,
            attempt_id="attempt-two",
            command_id="start-two",
            at=AT,
            expected_revision=result.expected_revision,
        )
        assert started.current_item_id == "second"
        assert started.commands[-1].state == ExecutionState.PENDING


@pytest.mark.parametrize(
    "values",
    [
        {"queue_id": ""},
        {"connection_generation": ""},
        {"generation": True},
        {"generation": -1},
        {"started_monotonic": float("inf")},
    ],
)
def test_authority_rejects_invalid_boundaries(values):
    with pytest.raises(ValueError):
        replace(AUTHORITY, **values)


@pytest.mark.parametrize(
    "values", [("", "attempt", "first"), ("skip", "", "first"), ("skip", "attempt", "")]
)
def test_skip_requires_explicit_complete_identity(values):
    with pytest.raises(ValueError):
        SkipIntent(*values)


def test_queue_policy_imports_without_dependencies_or_io(tmp_path):
    project = Path(__file__).resolve().parents[2]
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
from tellyq.domain.queue_policy import decide_queue
assert decide_queue
for forbidden in ('pychromecast', 'zeroconf', 'requests', 'tellyq.queue_store', 'tellyq.cast'):
    assert forbidden not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, str(project)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())
