"""Offline checkpoint execution through the real application and synthetic hardware."""

import json
import stat
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from runpy import run_path
from uuid import UUID

import pytest

from tellyq.checkpoint import CheckpointJournal, CheckpointOptions, run_checkpoint
from tellyq.domain.values import (
    CapabilityEvidence,
    CommandAction,
    CommandOutcome,
    CommandReceipt,
    ContentRef,
    IdleReason,
    PlaybackCapabilities,
    PlaybackObservation,
    PlaybackRequest,
    PlaybackTarget,
    PlayerState,
    Support,
)
from tellyq.session_store import JsonSessionStore
from tests.playback_support import FakeClock

TARGET = PlaybackTarget(str(UUID(int=1)), "cast")
CONTENT = ContentRef("youtube", "abcdefghijk")
REQUEST = PlaybackRequest("request", "attempt", "item", CONTENT, TARGET)
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "checkpoint_youtube.py"


class Backend:
    def __init__(self, clock, directory):
        self.clock, self.directory = clock, directory
        self.started = False
        self.stopped = False
        self.busy = False
        self.unknown_idle = False
        self.calls = 0
        self.sequence = 0
        self.state = PlayerState.PLAYING
        self.content = CONTENT
        self.session = "owned-session"
        self.playback_id = "1"
        self.position = 0
        self.ad = False
        self.support = Support.ADVERTISED
        self.natural = False
        self.commands = []
        self.hook = lambda _backend: None
        self.stop_error = False
        self.start_error = False
        self.silent_start = False
        self.silent_pause = False
        self.request = REQUEST

    def capabilities(self, _target):
        evidence = CapabilityEvidence(self.support, "synthetic", self.clock.utcnow())
        return PlaybackCapabilities(pause=evidence, resume=evidence)

    def receipt(self, action):
        # Check the file on disk during the effect, not only its final contents.
        records = journal_records(self.directory)
        assert records[-1]["kind"] == "command_intent"
        assert records[-1]["action"] == action.value
        self.commands.append(action.value)
        return CommandReceipt(
            self.request.request_id,
            self.request.attempt_id,
            action,
            CommandOutcome.ACCEPTED,
            self.clock.utcnow(),
        )

    def start(self, request):
        assert request.content == CONTENT
        self.request = request
        receipt = self.receipt(CommandAction.START)
        if self.start_error:
            raise TimeoutError("PRIVATE transport data")
        self.started = not self.silent_start
        return receipt

    def pause(self, _scope, _playback_id):
        receipt = self.receipt(CommandAction.PAUSE)
        if not self.silent_pause:
            self.state = PlayerState.PAUSED
        return receipt

    def resume(self, _scope, _playback_id):
        receipt = self.receipt(CommandAction.RESUME)
        self.state = PlayerState.PLAYING
        return receipt

    def stop(self, scope):
        assert scope.session_id == self.session
        assert self.content == CONTENT
        receipt = self.receipt(CommandAction.STOP)
        if self.stop_error:
            raise TimeoutError("PRIVATE cleanup data")
        self.stopped = True
        return receipt

    def observe(self, target):
        assert target == TARGET
        self.calls += 1
        self.hook(self)
        self.clock.tick(1)
        self.sequence += 1
        active = (self.started or self.busy) and not self.stopped
        receiver = PlaybackObservation(
            TARGET,
            "generation",
            self.sequence,
            self.clock.utcnow(),
            self.clock.monotonic(),
            session_active=None if self.unknown_idle else active,
            session_id=self.session if active else None,
            application_id="youtube" if active else None,
        )
        if not active:
            return (receiver,)
        events = [receiver]
        for delta in (0.1, 1.2):
            self.clock.tick(delta)
            self.sequence += 1
            if self.state == PlayerState.PLAYING:
                self.position += delta
            ended = self.natural and self.position > 5
            events.append(
                replace(
                    receiver,
                    sequence=self.sequence,
                    monotonic=self.clock.monotonic(),
                    session_active=None,
                    content=self.content,
                    playback_id=self.playback_id,
                    state=PlayerState.IDLE if ended else self.state,
                    position=self.position,
                    duration=6,
                    ad_active=self.ad,
                    pause_supported=True,
                    idle_reason=IdleReason.FINISHED if ended else None,
                )
            )
        return tuple(events)


def journal_records(directory):
    return [json.loads(line) for line in (directory / "journal.jsonl").read_text().splitlines()]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clock = FakeClock()
    directory = tmp_path / "runtime" / "attempt"
    return clock, directory, Backend(clock, directory)


def execute(setup, **options):
    clock, directory, backend = setup
    store = JsonSessionStore(directory / "session-store", clock)
    with CheckpointJournal(directory, clock) as journal:
        summary = run_checkpoint(
            backend, store, clock, journal, REQUEST, CheckpointOptions(**options)
        )
    return summary, journal_records(directory)


def test_baselines_precede_single_launch_and_cleanup_is_separate(setup):
    summary, records = execute(setup, seconds=15)
    backend = setup[2]
    assert backend.commands == ["start", "stop"]
    first_intent = next(
        index for index, record in enumerate(records) if record["kind"] == "command_intent"
    )
    assert sum(record["kind"] == "observations" for record in records[:first_intent]) == 2
    assert summary.launch_outcome == "accepted"
    assert summary.receiver_playback_observed
    assert not summary.natural_completion_observed
    assert summary.stop_reason == "deadline"
    assert summary.cleanup == "observed"
    assert summary.cleanup_state == "stopped"
    assert summary.last_state == "playing"
    assert summary.visual_confirmation is None
    assert [record["sequence"] for record in records] == list(range(len(records)))


@pytest.mark.parametrize("mode", ["busy", "unknown", "became_busy"])
def test_no_launch_without_positive_idle_at_both_baselines(setup, mode):
    backend = setup[2]
    backend.busy = mode == "busy"
    backend.unknown_idle = mode == "unknown"
    if mode == "became_busy":

        def become_busy(current):
            current.busy = current.calls >= 2

        backend.hook = become_busy
    summary, records = execute(setup, seconds=15)
    assert backend.commands == []
    assert summary.launch_outcome == "not_attempted"
    assert not any(record["kind"] == "command_intent" for record in records)


def test_pause_hold_observes_continuously_and_resumes_once(setup):
    summary, records = execute(setup, seconds=40, pause_hold=5)
    assert setup[2].commands == ["start", "pause", "resume", "stop"]
    assert summary.pause == summary.resume == "observed"
    start = next(index for index, record in enumerate(records) if record["kind"] == "hold_begin")
    end = next(index for index, record in enumerate(records) if record["kind"] == "hold_end")
    assert any(record["kind"] == "observations" for record in records[start:end])
    assert records[end]["completed"] is True


def test_unobserved_pause_never_resumes_or_retries(setup):
    setup[2].silent_pause = True
    summary, _records = execute(setup, seconds=25, pause_hold=5)
    assert setup[2].commands == ["start", "pause", "stop"]
    assert summary.pause == "unconfirmed"
    assert summary.resume == "refused_unverified_pause"


def test_current_control_guard_refusal_is_journaled_without_retry(setup):
    def buffering_during_pause(current):
        if current.calls == 5:
            current.state = PlayerState.BUFFERING

    setup[2].hook = buffering_during_pause
    summary, records = execute(setup, seconds=25, pause_hold=5)
    assert summary.pause == "refused"
    assert setup[2].commands == ["start", "stop"]
    errors = [record for record in records if record["kind"] == "application_error"]
    assert errors[0]["diagnostic"] == {"stage": "application", "reason": "state_mismatch"}


def test_ad_unknown_never_becomes_completion_from_duration_or_deadline(setup):
    setup[2].ad = None
    setup[2].natural = True
    summary, _records = execute(setup, seconds=40, after_terminal=5)
    assert summary.terminal_candidates > 0
    assert summary.stop_reason == "deadline"
    assert not summary.natural_completion_observed
    assert not summary.receiver_playback_observed


def test_unknown_terminal_does_not_shorten_capture_before_real_completion(setup):
    def early_unknown_terminal(current):
        current.natural = current.calls == 6 or current.calls >= 10
        current.ad = None if current.calls == 6 else False

    setup[2].hook = early_unknown_terminal
    summary, records = execute(setup, seconds=40, after_terminal=5)
    assert summary.natural_completion_observed
    assert summary.stop_reason == "post_terminal_window"
    first_terminal = next(record for record in records if record["kind"] == "terminal_candidate")
    completion = next(record for record in records if record["kind"] == "completion_evidence")
    assert first_terminal["event"]["ad_active"] is None
    assert completion["monotonic"] > first_terminal["monotonic"] + 5
    cleanup = next(
        record for record in records if record["kind"] == "phase" and record["phase"] == "cleanup"
    )
    assert cleanup["monotonic"] >= completion["monotonic"] + 5


def test_changed_media_after_verified_pause_refuses_resume_dispatch(setup):
    def changed_media(current):
        if current.calls >= 10:
            current.playback_id = "replacement-media"

    setup[2].hook = changed_media
    summary, records = execute(setup, seconds=40, pause_hold=5)
    assert "resume" not in setup[2].commands
    assert summary.resume != "observed"
    assert any(
        record["kind"] == "command_refused" and record["reason"] == "identity_changed"
        for record in records
    )


def test_hold_deadline_does_not_send_late_resume(setup):
    summary, _records = execute(setup, seconds=15, pause_hold=60)
    assert setup[2].commands == ["start", "pause", "stop"]
    assert summary.resume == "deadline"


def test_budget_expiry_at_second_baseline_prevents_start(setup):
    def slow_baseline(current):
        if current.calls == 2:
            current.clock.tick(20)

    setup[2].hook = slow_baseline
    summary, records = execute(setup, seconds=15)
    assert setup[2].commands == []
    assert summary.launch_outcome == "not_attempted"
    assert any(
        record["kind"] == "command_refused" and record["reason"] == "deadline" for record in records
    )


def test_completion_transition_retained_after_replacement_and_never_stops_replacement(setup):
    backend = setup[2]
    backend.natural = True

    def replace_after_ending(current):
        if current.calls >= 8:
            current.content = ContentRef("youtube", "other-video")
            current.session = "replacement-session"

    backend.hook = replace_after_ending
    summary, records = execute(setup, seconds=40, after_terminal=10)
    assert summary.natural_completion_observed
    completion = [record for record in records if record["kind"] == "completion_evidence"]
    assert len(completion) == 1
    assert completion[0]["snapshot"]["latest"]["idle_reason"] == "finished"
    assert backend.commands == ["start"]
    assert summary.cleanup == "refused_replacement"


def test_known_other_content_same_receiver_is_not_cleaned_up(setup):
    def replacement(current):
        if current.calls >= 5:
            current.content = ContentRef("youtube", "other-video")

    setup[2].hook = replacement
    summary, _records = execute(setup, seconds=20)
    assert setup[2].commands == ["start"]
    assert summary.cleanup == "refused_replacement"


def test_replacement_during_hold_prevents_resume_and_cleanup(setup):
    def replacement(current):
        if "pause" in current.commands and current.calls >= 7:
            current.content = ContentRef("youtube", "other-video")

    setup[2].hook = replacement
    summary, _records = execute(setup, seconds=30, pause_hold=10)
    assert setup[2].commands == ["start", "pause"]
    assert summary.resume == "refused_replacement"
    assert summary.cleanup == "refused_replacement"


def test_start_exception_retains_attempt_and_unknown_result_without_retry(setup):
    setup[2].start_error = True
    summary, records = execute(setup, seconds=15)
    assert setup[2].commands == ["start"]
    assert summary.launch_outcome == "unknown"
    assert summary.cleanup == "refused_no_ownership"
    assert any(
        record["kind"] == "command_error" and record["error_type"] == "TimeoutError"
        for record in records
    )
    assert "PRIVATE" not in (setup[1] / "journal.jsonl").read_text()


def test_original_error_survives_failed_cleanup(setup):
    def interrupted(current):
        if current.calls == 5:
            raise KeyboardInterrupt

    setup[2].hook = interrupted
    setup[2].stop_error = True
    summary, records = execute(setup, seconds=20)
    assert summary.stop_reason == "interrupted"
    assert summary.error_type == "KeyboardInterrupt"
    assert summary.cleanup == "error"
    assert setup[2].commands == ["start", "stop"]
    assert any(
        record["kind"] == "command_error" and record["error_type"] == "TimeoutError"
        for record in records
    )
    assert records[-1]["kind"] == "end"


def test_no_ownership_does_not_try_cleanup(setup):
    setup[2].silent_start = True
    summary, _records = execute(setup, seconds=15)
    assert summary.launch_outcome == "accepted"
    assert summary.stop_reason == "ownership_unconfirmed"
    assert summary.cleanup == "refused_no_ownership"
    assert setup[2].commands == ["start"]


@pytest.mark.parametrize(
    "options",
    [
        {"seconds": 14},
        {"seconds": 601},
        {"seconds": float("nan")},
        {"seconds": True},
        {"after_terminal": 4},
        {"after_terminal": float("inf")},
        {"pause_hold": 4},
        {"pause_hold": 61},
    ],
)
def test_window_limits(options):
    with pytest.raises(ValueError):
        CheckpointOptions(**options)


def test_private_exclusive_output_and_symlink_escape(setup, tmp_path):
    clock, directory, _backend = setup
    with CheckpointJournal(directory, clock) as journal:
        journal.write("example", visual_confirmation=None)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "journal.jsonl").stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        CheckpointJournal(directory, clock)
    outside = tmp_path / "public"
    outside.mkdir()
    (tmp_path / "runtime" / "escape").symlink_to(outside)
    with pytest.raises(ValueError):
        CheckpointJournal(tmp_path / "runtime" / "escape" / "attempt", clock)


def test_cli_uses_private_config_isolated_store_and_safe_stdout(setup, monkeypatch, capsys):
    clock, directory, backend = setup
    config = directory.parent / "device.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"device_id": TARGET.device_id}))
    legacy = directory.parent / "session.json"
    legacy.write_bytes(b"opaque legacy data preserved without parsing")

    @contextmanager
    def open_fake(target, _clock, seconds):
        assert target == TARGET and seconds == 6
        backend.drain_raw_observations = list
        yield backend, {}

    import tellyq.cast_backend
    import tellyq.clock

    monkeypatch.setattr(tellyq.cast_backend, "open_backend", open_fake)
    monkeypatch.setattr(tellyq.clock, "SystemClock", lambda: clock)
    main = run_path(str(SCRIPT))["main"]
    assert (
        main(
            [
                "start",
                "--device-config",
                str(config),
                "--content",
                CONTENT.content_id,
                "--output",
                str(directory),
                "--seconds",
                "15",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    for secret in (
        TARGET.device_id,
        CONTENT.content_id,
        "owned-session",
        "generation",
        str(directory),
    ):
        assert secret not in output.out + output.err
    assert json.loads(output.out)["legacy_state_unchanged"] is True
    assert legacy.read_bytes() == b"opaque legacy data preserved without parsing"
    assert list((directory / "session-store" / "sessions").glob("*.json"))
    assert stat.S_IMODE((directory / "summary.json").stat().st_mode) == 0o600
    commands = backend.commands.copy()
    assert (
        main(
            [
                "start",
                "--device-config",
                str(config),
                "--content",
                CONTENT.content_id,
                "--output",
                str(directory),
            ]
        )
        == 1
    )
    assert backend.commands == commands


def test_cli_requires_explicit_start_before_any_network():
    main = run_path(str(SCRIPT))["main"]
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_cli_connection_failure_retains_final_artifact_and_no_private_error(
    setup, monkeypatch, capsys
):
    _clock, directory, _backend = setup
    config = directory.parent / "device.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"device_id": TARGET.device_id}))

    @contextmanager
    def unavailable(*_args):
        raise TimeoutError("PRIVATE address")
        yield

    import tellyq.cast_backend

    monkeypatch.setattr(tellyq.cast_backend, "open_backend", unavailable)
    main = run_path(str(SCRIPT))["main"]
    assert (
        main(
            [
                "start",
                "--device-config",
                str(config),
                "--content",
                CONTENT.content_id,
                "--output",
                str(directory),
            ]
        )
        == 1
    )
    result = json.loads((directory / "summary.json").read_text())
    assert result["stop_reason"] == "connection_error"
    assert result["launch_outcome"] == "not_attempted"
    assert result["visual_confirmation"] is None
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err


def test_cli_holds_existing_global_command_lock_before_connect(setup, monkeypatch, capsys):
    import tellyq.cast_backend
    from tellyq.state import command_lock

    _clock, directory, _backend = setup
    config = directory.parent / "device.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"device_id": TARGET.device_id}))
    connected = []

    def forbidden(*_args):
        connected.append(True)
        raise AssertionError("Must not connect")

    monkeypatch.setattr(tellyq.cast_backend, "open_backend", forbidden)
    main = run_path(str(SCRIPT))["main"]
    with command_lock(directory.parent):
        assert (
            main(
                [
                    "start",
                    "--device-config",
                    str(config),
                    "--content",
                    CONTENT.content_id,
                    "--output",
                    str(directory),
                ]
            )
            == 1
        )
    assert connected == []
    assert not directory.exists()
    assert json.loads(capsys.readouterr().out)["error"] == "checkpoint_failed"
