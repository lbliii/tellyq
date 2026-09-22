import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tellyq import __main__ as cli
from tellyq.service_cli import OWNER_TOOL_SPECS
from tellyq.state import read


def test_help_has_no_runtime_side_effects(tmp_path, monkeypatch, capsys):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(cli, "RUNTIME", runtime)
    with pytest.raises(SystemExit) as result:
        cli.main(["--help"])
    assert result.value.code == 0
    assert "start" in capsys.readouterr().out
    assert not runtime.exists()


def test_owner_tool_help_uses_shared_command_specs(tmp_path, monkeypatch, capsys):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(cli, "RUNTIME", runtime)
    with pytest.raises(SystemExit) as result:
        cli.main(["--help"])
    assert result.value.code == 0
    root_help = capsys.readouterr().out
    for spec in OWNER_TOOL_SPECS:
        assert spec.description in " ".join(root_help.split())
        with pytest.raises(SystemExit) as command_help:
            cli.main([spec.name, "--help"])
        assert command_help.value.code == 0
        text = capsys.readouterr().out
        assert ("--command-id" in text) == spec.accepts_command_id
        assert "--owner" not in text
        assert "--device" not in text
        assert "--seconds" not in text
    assert not runtime.exists()


def test_queue_is_json_and_does_not_contact_a_device(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "RUNTIME", tmp_path)
    assert cli.main(["queue", "--device", "00000000-0000-4000-8000-000000000001"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "queued"
    assert report["commands"] == []
    assert report["observations"] == []
    saved = read(tmp_path / "queue.json")
    assert saved is not None
    assert saved["items"][0]["state"] == "queued"


def test_invalid_device_returns_structured_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "RUNTIME", tmp_path)
    assert cli.main(["queue", "--device", "not-a-uuid"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["type"] == "ValueError"
    assert not (tmp_path / "queue.json").exists()


def test_invalid_window_never_dispatches(tmp_path, monkeypatch, capsys):
    dispatch = Mock()
    monkeypatch.setattr(cli, "execute", dispatch)
    monkeypatch.setattr(cli, "RUNTIME", tmp_path / "runtime")
    with pytest.raises(SystemExit) as result:
        cli.main(["probe", "--device", "00000000-0000-4000-8000-000000000001", "--seconds", "0"])
    assert result.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    dispatch.assert_not_called()


def test_lock_failure_still_produces_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "RUNTIME", tmp_path)
    monkeypatch.setattr(cli, "execute", Mock(side_effect=RuntimeError("Command already running")))
    assert cli.main(["probe", "--device", "00000000-0000-4000-8000-000000000001"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "RuntimeError"


def test_persistence_rejects_non_object_json(tmp_path: Path):
    path = tmp_path / "queue.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="Expected a JSON object"):
        read(path)
