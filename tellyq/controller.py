"""Reusable one-program operations, independent of CLI argument parsing."""

import sys
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .cast import VIDEO_ID, Connection, connect, device_dict, discovery, now
from .evidence import playback_evidence, video_id
from .models import CommandReceipt, Evidence, Observation, Report
from .state import command_lock, load_queue, make_queue, read, save


def current_state(events: list[Observation], evidence: Evidence) -> str:
    """Report current receiver state conservatively; never infer completion from time."""
    receivers = [e for e in events if e["kind"] == "receiver"]
    if receivers and receivers[-1].get("app_name") != "YouTube":
        return "unconfirmed"
    media = [e for e in events if e["kind"] == "media"]
    if not media:
        return "unconfirmed"
    latest = media[-1]
    if (
        video_id(latest.get("content_id")) == VIDEO_ID
        and latest.get("player_state") == "IDLE"
        and latest.get("idle_reason") == "FINISHED"
        and not latest.get("ad_break")
    ):
        return "finished"
    if (
        evidence["receiver_playback_confirmed"]
        and latest.get("player_state") == "PLAYING"
        and video_id(latest.get("content_id")) == VIDEO_ID
        and not latest.get("ad_break")
    ):
        return "playing"
    return "unconfirmed"


def start_playback(
    connection: Connection,
    device_id: str,
    seconds: float,
    runtime: Path,
    report: Report,
) -> None:
    previous_receivers = [e for e in report["observations"] if e["kind"] == "receiver"]
    previous_session = previous_receivers[-1].get("app_session_id") if previous_receivers else None
    report["requested_content_id"] = VIDEO_ID
    boundary = monotonic()
    command: CommandReceipt = {"action": "play_video", "requested_at": now(), "returned": False}
    report["commands"].append(command)
    try:
        connection.youtube.play_video(VIDEO_ID)
        command["returned"] = True
    finally:
        report["observations"] += connection.observe(seconds)
        events = [e for e in report["observations"] if e["monotonic"] >= boundary]
        report["evidence"] = playback_evidence(events, VIDEO_ID)
        report["state"] = current_state(events, report["evidence"])
        receivers = [e for e in events if e["kind"] == "receiver"]
        latest = receivers[-1] if receivers else {"kind": "receiver"}
        if (
            latest.get("app_name") == "YouTube"
            and latest.get("app_session_id")
            and (command["returned"] or latest["app_session_id"] != previous_session)
        ):
            save(
                runtime / "session.json",
                {
                    "device_id": device_id,
                    "content_id": VIDEO_ID,
                    "app_id": latest["app_id"],
                    "app_session_id": latest["app_session_id"],
                },
            )


def stop_playback(connection: Connection, device_id: str, runtime: Path, report: Report) -> None:
    session = read(runtime / "session.json")
    receivers = [e for e in report["observations"] if e["kind"] == "receiver"]
    latest = receivers[-1] if receivers else {"kind": "receiver"}
    if (
        not session
        or session["device_id"] != device_id
        or not session.get("app_session_id")
        or latest.get("app_id") != session["app_id"]
        or latest.get("app_session_id") != session["app_session_id"]
    ):
        raise ValueError(
            "The saved YouTube session is no longer active; refusing to stop another session."
        )
    media = [e for e in report["observations"] if e["kind"] == "media" and e.get("content_id")]
    if media and video_id(media[-1]["content_id"]) != session.get("content_id", VIDEO_ID):
        raise ValueError("Different content is now playing; refusing to stop it.")
    boundary = monotonic()
    command: CommandReceipt = {"action": "quit_app", "requested_at": now(), "returned": False}
    report["commands"].append(command)
    try:
        connection.cast.quit_app(timeout=10)
        command["returned"] = True
    finally:
        report["observations"] += connection.observe(6)
        after = [
            e
            for e in report["observations"]
            if e["kind"] == "receiver" and e["monotonic"] >= boundary
        ]
        report["stop_confirmed"] = bool(after and after[-1].get("app_id") != session["app_id"])
        report["stop_verification_source"] = (
            "receiver_app_exit" if report["stop_confirmed"] else None
        )
        report["state"] = "stopped" if report["stop_confirmed"] else "unconfirmed"


def execute(
    command: str,
    runtime: Path,
    device_id: str | None = None,
    seconds: float = 30,
) -> tuple[Report, int]:
    report: Report = {
        "command": command,
        "started_at": now(),
        "python": sys.version,
        "gil_enabled": sys._is_gil_enabled(),
        "commands": [],
        "observations": [],
    }
    code = 0
    queue = None
    queue_started = False
    session = None
    with command_lock(runtime):
        try:
            if command not in {"discover", "queue", "probe", "start", "status", "stop"}:
                raise ValueError("Unknown command.")
            if not 5 <= seconds <= 120:
                raise ValueError("Observation window must be between 5 and 120 seconds.")
            if command == "discover":
                with discovery() as (browser, _):
                    report["devices"] = sorted(
                        [device_dict(d) for d in list(browser.devices.values())],
                        key=lambda d: d["name"] or "",
                    )
                save(runtime / "discovery.json", report)
            elif command == "queue":
                if device_id is None:
                    raise ValueError("Select an explicit device UUID from discover.")
                queue = make_queue(device_id)
                save(runtime / "queue.json", queue)
                report["queue"] = queue
                report["state"] = "queued"
            else:
                queue = read(runtime / "queue.json")
                session = read(runtime / "session.json")
                if command == "start":
                    queue = load_queue(runtime)
                    device_id = queue["device_id"]
                    if queue["items"][0]["state"] not in {
                        "queued",
                        "stopped",
                        "finished",
                        "failed",
                    }:
                        raise ValueError(
                            "This queue was already started; use status or stop before requeueing."
                        )
                device_id = (
                    device_id or (session or {}).get("device_id") or (queue or {}).get("device_id")
                )
                if not device_id:
                    raise ValueError("Select an explicit device UUID from discover.")
                with connect(device_id) as (connection, device):
                    report["device"] = device
                    report["observations"] += connection.observe(2)
                    if command in {"probe", "start"}:
                        if command == "start" and queue is not None:
                            queue["items"][0]["state"] = "starting"
                            save(runtime / "queue.json", queue)
                            queue_started = True
                        start_playback(connection, device_id, seconds, runtime, report)
                    elif command == "status":
                        report["observations"] += connection.observe(6)
                        report["evidence"] = playback_evidence(report["observations"], VIDEO_ID)
                        report["state"] = current_state(report["observations"], report["evidence"])
                    elif command == "stop":
                        stop_playback(connection, device_id, runtime, report)
        except (Exception, KeyboardInterrupt) as exc:
            report["error"] = {"type": type(exc).__name__, "message": str(exc)}
            report.setdefault("state", "unconfirmed" if report["commands"] else "failed")
            code = 1
        report["ended_at"] = now()
        path = runtime / "runs" / f"{uuid4()}.json"
        report["report_path"] = str(path)
        save(path, report)
        status_update = (
            command == "status"
            and queue
            and "error" not in report
            and queue["device_id"] == device_id
            and queue["items"][0]["state"] in {"starting", "playing", "unconfirmed"}
        )
        if status_update:
            receivers = [e for e in report["observations"] if e["kind"] == "receiver"]
            latest_session = receivers[-1].get("app_session_id") if receivers else None
            if not session or session.get("app_session_id") != latest_session:
                status_update = False
        if queue and (
            queue_started
            or status_update
            or (
                command == "stop"
                and report.get("stop_confirmed")
                and queue["device_id"] == device_id
            )
        ):
            queue["items"][0]["state"] = report["state"]
            queue["updated_at"] = now()
            queue["last_report"] = str(path)
            save(runtime / "queue.json", queue)
        return report, code
