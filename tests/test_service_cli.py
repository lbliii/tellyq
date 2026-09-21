import json
from unittest.mock import Mock

import pytest

from tellyq import __main__ as cli
from tellyq import service_cli
from tellyq.domain.values import CommandAction
from tellyq.runner_ipc import IPCUnavailable


def queue():
    return {
        "queue_id": "queue-a",
        "target": {
            "device_id": "00000000-0000-4000-8000-000000000001",
            "route": "cast",
            "name": None,
        },
        "items": [
            {
                "item_id": "first",
                "provider": "youtube",
                "content_id": "FozIp7Va7dY",
                "kind": "video",
                "intent": "finished",
            },
            {
                "item_id": "second",
                "provider": "youtube",
                "content_id": "f9F7yDjSdNA",
                "kind": "video",
                "intent": "pending",
            },
        ],
    }


def test_start_retry_does_not_select_successor():
    first = service_cli.start_command(queue(), None)
    assert first == service_cli.start_command(queue(), None)
    assert first.request is not None
    assert first.request.queue_item_id == "first"
    explicit = service_cli.start_command(queue(), "request-a")
    assert explicit.request is not None
    assert explicit.command_id == explicit.request.request_id == "request-a"
    assert explicit == service_cli.start_command(queue(), "request-a")


@pytest.mark.parametrize("command", ["start", "status", "stop", "pause", "resume"])
def test_selected_ipc_failure_never_falls_back(tmp_path, monkeypatch, capsys, command):
    monkeypatch.setattr(cli, "RUNTIME", tmp_path)
    (tmp_path / "runner.sock").symlink_to(tmp_path / "missing")
    direct = Mock()
    monkeypatch.setattr(cli, "execute", direct)
    monkeypatch.setattr(cli, "owner_command", Mock(side_effect=IPCUnavailable("private secret")))
    assert cli.main([command]) == 1
    direct.assert_not_called()
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["type"] == "IPCUnavailable"
    assert "private secret" not in str(report)


def test_stop_client_does_not_wait_for_status(monkeypatch, tmp_path):
    client = Mock()
    client.submit.return_value = {
        "schema_version": 1,
        "ok": True,
        "code": "accepted",
        "ticket_id": "original-stop",
    }
    monkeypatch.setattr(service_cli, "RunnerIPCClient", Mock(return_value=client))
    result = service_cli.owner_command("stop", tmp_path, command_id="retry-stop")
    client.status.assert_not_called()
    assert client.submit.call_args.args[0].action == CommandAction.STOP
    assert result["ticket_id"] == "original-stop"


def test_explicit_device_mismatch_never_submits(monkeypatch, tmp_path):
    client = Mock()
    client.status.return_value = {
        "schema_version": 1,
        "ok": True,
        "code": "status",
        "snapshot": {"view": {"queue": queue()}},
    }
    monkeypatch.setattr(service_cli, "RunnerIPCClient", Mock(return_value=client))
    with pytest.raises(ValueError, match="differs"):
        service_cli.owner_command("stop", tmp_path, device="00000000-0000-4000-8000-000000000002")
    client.submit.assert_not_called()


def test_explicit_runtime_and_ipc_options(monkeypatch, tmp_path, capsys):
    runtime = tmp_path / "owner"
    runtime.mkdir()
    (runtime / "runner.sock").touch()
    command = Mock(return_value={"schema_version": 1, "ok": True, "code": "accepted"})
    monkeypatch.setattr(cli, "owner_command", command)
    assert cli.main(["--runtime", str(runtime), "pause", "--command-id", "pause-a"]) == 0
    assert command.call_args.args == ("pause", runtime)
    assert command.call_args.kwargs["command_id"] == "pause-a"
    capsys.readouterr()
    command.reset_mock()
    assert cli.main(["--runtime", str(runtime), "start", "--seconds", "10"]) == 1
    command.assert_not_called()


def test_owner_command_id_without_owner_never_dispatches(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "RUNTIME", tmp_path)
    direct = Mock()
    monkeypatch.setattr(cli, "execute", direct)
    assert cli.main(["start", "--command-id", "retry"]) == 1
    direct.assert_not_called()
    capsys.readouterr()


def test_serve_validates_before_composition(monkeypatch, tmp_path, capsys):
    path = tmp_path / "invalid.json"
    path.write_text("{}")
    run = Mock()
    monkeypatch.setattr(cli, "run_foreground", run)
    assert cli.main(["serve", "--manifest", str(path)]) == 1
    run.assert_not_called()
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["serve", "--import-legacy"])
