import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    "boundary",
    [
        "before_dispatch",
        "after_effect_before_ack",
        "after_completion",
        "cancellation",
        "takeover",
    ],
)
def test_service_process_loss_never_replays_or_advances(boundary):
    result = subprocess.run(
        [sys.executable, "-m", "tests.service_recovery_scenario", boundary],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "boundary": boundary,
        "starts": int(boundary != "before_dispatch"),
        "replayed_effects": 0,
        "successor_starts": 0,
    }


@pytest.mark.timeout(20)
@pytest.mark.parametrize("poll_seconds", [0.05, 0.3])
def test_checkpoint_client_observes_three_item_composed_service(poll_seconds):
    result = subprocess.run(
        [sys.executable, "-m", "tests.service_recovery_scenario", "checkpoint", str(poll_seconds)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    assert summary["verified_handoffs"] == 2
    assert not summary["live_acceptance"]
    assert len(summary["items"]) == 3
