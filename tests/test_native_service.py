import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.timeout(20)
def test_native_three_item_checkpoint_through_foreground_owner_and_real_ipc():
    result = subprocess.run(
        [sys.executable, "-m", "tests.native_service_scenario"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    assert summary["verified_handoffs"] == 2
    assert summary["cleanup"] == "stop_observed"
    assert not summary["live_acceptance"]
