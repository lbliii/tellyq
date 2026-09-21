"""Persistence behavior uses synthetic identities and never a receiver connection."""

import json
import os
import subprocess
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from tellyq.clock import SystemClock
from tellyq.models import QueueDocument
from tellyq.programs import DEFAULT_CONTENT_ID
from tellyq.state import command_lock, load_queue, make_queue, read, save

DEVICE_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def queue() -> QueueDocument:
    return make_queue(DEVICE_ID)


@pytest.fixture
def session() -> dict[str, object]:
    return {
        "device_id": DEVICE_ID,
        "content_id": DEFAULT_CONTENT_ID,
        "app_id": "synthetic-youtube-app",
        "app_session_id": "synthetic-session",
    }


def write_unchecked(path: Path, value: object) -> None:
    """Model a malformed or legacy file that did not pass through save()."""
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize(
    "state", ["queued", "starting", "playing", "failed", "unconfirmed", "stopped", "finished"]
)
def test_existing_v1_queue_preserves_supported_fields(tmp_path, queue, state):
    queue["items"][0]["state"] = state
    queue["last_report"] = "runtime/runs/synthetic-report.json"
    # Extra display query parameters do not change the exact program identity.
    queue["items"][0]["url"] += "&t=0"
    write_unchecked(tmp_path / "queue.json", queue)
    assert read(tmp_path / "queue.json") == queue
    assert load_queue(tmp_path) == queue
    save(tmp_path / "queue.json", load_queue(tmp_path))
    assert read(tmp_path / "queue.json") == queue


def test_original_unversioned_session_round_trips(tmp_path, session):
    path = tmp_path / "session.json"
    write_unchecked(path, session)
    assert read(path) == session
    save(path, session)
    loaded = read(path)
    assert loaded == session
    assert loaded is not None
    assert "schema_version" not in loaded


def test_explicit_v1_session_uses_the_same_fields(tmp_path, session):
    session["schema_version"] = 1
    save(tmp_path / "session.json", session)
    assert read(tmp_path / "session.json") == session


@pytest.mark.parametrize("name", ["queue.json", "session.json", "report.json"])
def test_missing_state_is_distinct_from_invalid_state(tmp_path, name):
    assert read(tmp_path / name) is None
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError, match="Queue one Bob Ross episode"):
        load_queue(tmp_path)


@pytest.mark.parametrize("value", [None, [], "text", 1, True])
def test_read_requires_top_level_object(tmp_path, value):
    path = tmp_path / "queue.json"
    write_unchecked(path, value)
    with pytest.raises(ValueError, match="Expected a JSON object"):
        read(path)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("schema_version", True, "schema_version"),
        ("schema_version", 1.0, "schema_version"),
        ("schema_version", "1", "schema_version"),
        ("schema_version", 2, "Unsupported schema version"),
        ("schema_version", 0, "Unsupported schema version"),
        ("device_id", None, "device_id"),
        ("device_id", 123, "device_id"),
        ("device_id", "invalid", "device_id"),
        ("updated_at", None, "updated_at"),
        ("updated_at", "yesterday", "updated_at"),
        ("updated_at", "2026-09-21", "updated_at"),
        ("updated_at", "2026-09-21T12:00:00", "updated_at"),
        ("items", None, "items"),
        ("items", {}, "items"),
        ("items", [], "items"),
        ("items", [None], "items"),
        ("items", [{}, {}], "items"),
        ("last_report", False, "last_report"),
        ("last_report", " ", "last_report"),
    ],
)
def test_queue_rejects_invalid_required_types_and_values(tmp_path, queue, field, value, error):
    queue[field] = value
    path = tmp_path / "queue.json"
    write_unchecked(path, queue)
    with pytest.raises(ValueError, match=error):
        read(path)
    with pytest.raises(ValueError, match=error):
        load_queue(tmp_path)


@pytest.mark.parametrize("field", ["schema_version", "device_id", "updated_at", "items"])
def test_queue_rejects_missing_required_fields(tmp_path, queue, field):
    del queue[field]
    write_unchecked(tmp_path / "queue.json", queue)
    with pytest.raises(ValueError, match=field):
        read(tmp_path / "queue.json")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", ""),
        ("id", False),
        ("service", "netflix"),
        ("service", None),
        ("content_id", "different-id"),
        ("content_id", []),
        ("title", " "),
        ("title", 1),
        ("url", None),
        ("url", "https://example.invalid/watch?v=FozIp7Va7dY"),
        ("url", "https://www.youtube.com/watch?v=different-id"),
        ("url", "https://www.youtube.com/watch?v=FozIp7Va7dY&v=other"),
        ("url", "https://["),
        ("state", "complete"),
        ("state", False),
    ],
)
def test_queue_rejects_corrupt_item_fields(tmp_path, queue, field, value):
    queue["items"][0][field] = value
    write_unchecked(tmp_path / "queue.json", queue)
    with pytest.raises(ValueError, match=r"queue\.json\.items\[0\]"):
        load_queue(tmp_path)


@pytest.mark.parametrize("field", ["id", "service", "content_id", "title", "url", "state"])
def test_queue_rejects_missing_item_fields(tmp_path, queue, field):
    del queue["items"][0][field]
    write_unchecked(tmp_path / "queue.json", queue)
    with pytest.raises(ValueError, match=field):
        read(tmp_path / "queue.json")


@pytest.mark.parametrize("field", ["device_id", "content_id", "app_id", "app_session_id"])
@pytest.mark.parametrize("value", [None, "", True, []])
def test_session_rejects_invalid_identity_fields(tmp_path, session, field, value):
    session[field] = value
    write_unchecked(tmp_path / "session.json", session)
    with pytest.raises(ValueError, match=field):
        read(tmp_path / "session.json")


@pytest.mark.parametrize("field", ["device_id", "content_id", "app_id", "app_session_id"])
def test_session_rejects_missing_fields(tmp_path, session, field):
    del session[field]
    write_unchecked(tmp_path / "session.json", session)
    with pytest.raises(ValueError, match=field):
        read(tmp_path / "session.json")


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("device_id", "invalid-uuid", "device_id"),
        ("content_id", "different-id", "content_id"),
        ("schema_version", 2, "Unsupported schema version"),
        ("schema_version", True, "schema_version"),
        ("schema_version", "1", "schema_version"),
    ],
)
def test_session_rejects_invalid_ids_and_future_versions(tmp_path, session, field, value, error):
    session[field] = value
    write_unchecked(tmp_path / "session.json", session)
    with pytest.raises(ValueError, match=error):
        read(tmp_path / "session.json")


@pytest.mark.parametrize("name", ["queue.json", "session.json"])
def test_unknown_fields_are_rejected_without_rewriting_existing_state(
    tmp_path, queue, session, name
):
    value = queue if name == "queue.json" else session
    value["unknown-field"] = "do-not-drop"
    path = tmp_path / name
    write_unchecked(path, value)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unsupported fields"):
        read(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", ["start", "status", "stop", "probe"])
@pytest.mark.parametrize("name", ["queue.json", "session.json"])
def test_invalid_state_fails_before_controller_connects(tmp_path, command, name):
    from tellyq.controller import execute

    connect = Mock(side_effect=AssertionError("Controller must validate before connecting"))
    write_unchecked(tmp_path / name, {"device_id": DEVICE_ID})
    report, code = execute(command, tmp_path, device_id=DEVICE_ID, backend_factory=connect)
    assert code == 1
    assert report["error"]["type"] == "ValueError"
    assert report["commands"] == []
    connect.assert_not_called()


def test_generic_reports_remain_generic(tmp_path):
    value = {"schema_version": 99, "items": "not a queue", "nested": {"position": 1.25}}
    path = tmp_path / "report.json"
    save(path, value)
    assert read(path) == value


@pytest.mark.parametrize(
    "source",
    [
        '{"value":',
        '{"value": NaN}',
        '{"value": Infinity}',
        '{"value": -Infinity}',
        '{"value": 1e999}',
        '{"value": 1, "value": 2}',
        '{"nested": {"x": 1, "x": 2}}',
    ],
)
def test_invalid_json_is_not_mistaken_for_missing_state(tmp_path, source):
    path = tmp_path / "report.json"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(ValueError):
        read(path)


def test_invalid_utf8_has_a_safe_actionable_error(tmp_path):
    path = tmp_path / "queue.json"
    path.write_bytes(b'{"private": "\xff"}')
    with pytest.raises(ValueError, match=r"queue\.json must contain UTF-8 JSON"):
        read(path)


def test_validation_errors_do_not_expose_values_even_in_tracebacks(tmp_path, queue):
    private_value = "synthetic-sensitive-value"
    queue["updated_at"] = private_value
    write_unchecked(tmp_path / "queue.json", queue)
    with pytest.raises(ValueError) as error:
        read(tmp_path / "queue.json")
    assert private_value not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("name", ["queue.json", "session.json"])
def test_save_rejects_bad_state_without_replacing_the_original(tmp_path, queue, session, name):
    path = tmp_path / name
    save(path, queue if name == "queue.json" else session)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        save(path, {"schema_version": 2})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("failure", [OSError("disk error"), KeyboardInterrupt()])
def test_interruption_before_replace_preserves_old_state_and_cleans_temp(
    tmp_path, monkeypatch, failure
):
    path = tmp_path / "state.json"
    save(path, {"value": "old"})

    def interrupt(_descriptor: int) -> None:
        raise failure

    monkeypatch.setattr(os, "fsync", interrupt)
    with pytest.raises(type(failure)):
        save(path, {"value": "new"})
    assert read(path) == {"value": "old"}
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_replace_failure_preserves_old_state_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    save(path, {"value": "old"})

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("synthetic rename failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="rename failure"):
        save(path, {"value": "new"})
    assert read(path) == {"value": "old"}
    assert not list(tmp_path.glob(".*.tmp"))


def test_serialization_failure_preserves_old_state_and_cleans_temp(tmp_path):
    path = tmp_path / "state.json"
    save(path, {"value": "old"})
    with pytest.raises(ValueError, match="Out of range float values"):
        save(path, {"value": float("nan")})
    assert read(path) == {"value": "old"}
    assert not list(tmp_path.glob(".*.tmp"))


def test_new_state_and_temporary_files_are_private(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    path = runtime / "state.json"
    original_replace = Path.replace
    modes = []

    def inspect_replace(source: Path, destination: Path) -> Path:
        modes.append(source.stat().st_mode & 0o777)
        assert read(source) == {"value": "new"}
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", inspect_replace)
    save(path, {"value": "new"})
    assert modes == [0o600]
    assert path.stat().st_mode & 0o777 == 0o600
    assert runtime.stat().st_mode & 0o777 == 0o700
    assert not list(runtime.glob(".*.tmp"))


def test_separate_process_cannot_acquire_held_lock_and_can_after_release(tmp_path):
    code = """
import sys
from pathlib import Path
from tellyq.state import command_lock
try:
    with command_lock(Path(sys.argv[1])):
        pass
except RuntimeError:
    sys.exit(42)
"""
    with command_lock(tmp_path):
        held = subprocess.run([sys.executable, "-c", code, str(tmp_path)], timeout=5, check=False)
        assert held.returncode == 42
        assert (tmp_path / "command.lock").stat().st_mode & 0o777 == 0o600
    released = subprocess.run([sys.executable, "-c", code, str(tmp_path)], timeout=5, check=False)
    assert released.returncode == 0


def test_lock_is_released_when_command_fails(tmp_path):
    with pytest.raises(ValueError, match="command failed"), command_lock(tmp_path):
        raise ValueError("command failed")
    with command_lock(tmp_path):
        pass


def test_storage_imports_without_cast_and_does_not_create_runtime(tmp_path):
    code = """
import sys
from pathlib import Path
def deny_network(event, args):
    if event.startswith("socket."):
        raise AssertionError("Imports must not access the network")
sys.addaudithook(deny_network)
sys.modules["pychromecast"] = None
sys.modules["casttube"] = None
sys.modules["zeroconf"] = None
import tellyq.clock
import tellyq.programs
import tellyq.state
assert "tellyq.cast" not in sys.modules
assert not Path("runtime").exists()
assert tellyq.state.make_queue("00000000-0000-4000-8000-000000000001")["schema_version"] == 1
assert not Path("runtime").exists()
"""
    subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, timeout=5, check=True)


def test_system_clock_returns_aware_utc_and_monotonic_values():
    clock = SystemClock()
    before = datetime.now(UTC)
    instant = clock.utcnow()
    after = datetime.now(UTC)
    assert before <= instant <= after
    assert instant.tzinfo is UTC
    first = clock.monotonic()
    second = clock.monotonic()
    assert isinstance(first, float)
    assert second >= first
