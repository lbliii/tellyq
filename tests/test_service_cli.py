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
    monkeypatch.setattr(
        service_cli, "owner_command", Mock(side_effect=IPCUnavailable("private secret"))
    )
    monkeypatch.setattr(cli, "owner_command", Mock(side_effect=IPCUnavailable("private secret")))
    assert cli.main([command]) == 1
    direct.assert_not_called()
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["type"] == (
        "owner_unavailable" if command in service_cli.OWNER_TOOL_COMMANDS else "IPCUnavailable"
    )
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
    monkeypatch.setattr(service_cli, "owner_command", command)
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


@pytest.mark.parametrize("action", ["start", "status", "stop"])
def test_owner_tool_python_and_cli_parity(monkeypatch, tmp_path, capsys, action):
    (tmp_path / "runner.sock").touch()
    response = {"schema_version": 1, "ok": True, "code": "accepted", "ticket_id": "retry-a"}
    owner = Mock(return_value=response)
    monkeypatch.setattr(service_cli, "owner_command", owner)
    direct = service_cli.owner_tool_command(
        action, tmp_path, command_id="retry-a" if action != "status" else None
    )
    args = ["--runtime", str(tmp_path), action]
    if action != "status":
        args.extend(["--command-id", "retry-a"])
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out) == direct["response"]
    assert owner.call_count == 2
    assert owner.call_args.kwargs["command_id"] == ("retry-a" if action != "status" else None)


@pytest.mark.parametrize("action", ["start", "stop"])
def test_bad_owner_id_is_shared_structured_error(monkeypatch, tmp_path, capsys, action):
    (tmp_path / "runner.sock").touch()
    owner = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(service_cli, "owner_command", owner)
    direct = service_cli.owner_tool_command(action, tmp_path, command_id="bad id")
    assert cli.main(["--runtime", str(tmp_path), action, "--command-id", "bad id"]) == 1
    assert json.loads(capsys.readouterr().out) == direct
    assert direct["code"] == "invalid_command_id"
    owner.assert_not_called()


def test_owner_tool_retries_and_uncertain_failure(monkeypatch, tmp_path):
    accepted = {"schema_version": 1, "ok": True, "code": "accepted", "ticket_id": "same"}
    owner = Mock(return_value=accepted)
    monkeypatch.setattr(service_cli, "owner_command", owner)
    assert (
        service_cli.owner_tool_command("start", tmp_path, command_id="same")["response"] == accepted
    )
    assert (
        service_cli.owner_tool_command("start", tmp_path, command_id="same")["response"] == accepted
    )
    assert owner.call_count == 2
    owner.side_effect = IPCUnavailable("private socket address")
    result = service_cli.owner_tool_command("stop", tmp_path, command_id="same-stop")
    assert result["code"] == "owner_unavailable"
    assert "unknown" in result["error"]["message"]
    assert "private socket address" not in str(result)


@pytest.mark.parametrize("action", ["start", "status", "stop"])
def test_explicit_owner_mode_has_shared_result_without_endpoint(
    monkeypatch, tmp_path, capsys, action
):
    direct = Mock()
    monkeypatch.setattr(cli, "execute", direct)
    owner = Mock(side_effect=IPCUnavailable("private socket address"))
    monkeypatch.setattr(service_cli, "owner_command", owner)
    expected = service_cli.owner_tool_command(action, tmp_path)
    assert cli.main(["--runtime", str(tmp_path), action, "--owner"]) == 1
    assert json.loads(capsys.readouterr().out) == expected
    assert expected["code"] == "owner_unavailable"
    assert "private socket address" not in str(expected)
    direct.assert_not_called()


@pytest.mark.parametrize("action", ["start", "status", "stop"])
def test_explicit_owner_mode_matches_python_success(monkeypatch, tmp_path, capsys, action):
    response = {"schema_version": 1, "ok": True, "code": "accepted", "ticket_id": "same"}
    monkeypatch.setattr(service_cli, "owner_command", Mock(return_value=response))
    expected = service_cli.owner_tool_command(action, tmp_path)
    assert cli.main(["--runtime", str(tmp_path), action, "--owner"]) == 0
    assert json.loads(capsys.readouterr().out) == expected


def test_explicit_owner_mode_validates_id_without_dispatch(monkeypatch, tmp_path, capsys):
    direct = Mock()
    owner = Mock()
    monkeypatch.setattr(cli, "execute", direct)
    monkeypatch.setattr(service_cli, "owner_command", owner)
    assert cli.main(["--runtime", str(tmp_path), "start", "--owner", "--command-id", "bad id"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result == service_cli.owner_tool_command("start", tmp_path, command_id="bad id")
    direct.assert_not_called()
    owner.assert_not_called()
