"""Prompt STOP through the real foreground task, application and queue journal."""

from tellyq.domain.queue import ExecutionState, QueueIntent
from tellyq.domain.values import CommandAction, PlayerState
from tellyq.runner import RunnerCommand
from tests.test_playback_task import rig as rig
from tests.test_playback_task import start


def test_incremental_stop_crosses_owner_wrapper_and_persists_observed_outcome(rig, monkeypatch):
    task, backend, store, cancellation, event, _ = rig
    start(task, cancellation)
    observe = backend.observe
    batches = []

    def observe_until(target, ready):
        events = observe(target)
        batches.append(events)
        assert ready(events)
        return events

    def fixed_window(_target):
        raise AssertionError("Stop should use the optional prompt observation capability")

    monkeypatch.setattr(backend, "observe_until", observe_until, raising=False)
    monkeypatch.setattr(backend, "observe", fixed_window)
    event.set()
    stopped = task.handle(RunnerCommand("stop", CommandAction.STOP), cancellation)
    assert len(batches) == 2
    assert task._events == batches[-1]
    assert stopped.playback.state == PlayerState.STOPPED
    assert stopped.queue.items[0].intent == QueueIntent.STOPPED
    assert store.load("queue").commands[-1].state == ExecutionState.ACKNOWLEDGED
    assert [a for a, _ in backend.commands] == ["start", "stop"]
