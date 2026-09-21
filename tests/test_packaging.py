"""Source archive regressions for reviewed offline test data."""

from pathlib import Path
from runpy import run_path

import pytest


def test_source_archive_requires_every_reviewed_fixture_format(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/smoke_install.py"
    check_source_fixtures = run_path(str(script))["check_source_fixtures"]
    files = (
        "tests/fixtures/cast/status.json",
        "tests/fixtures/lifecycle/capture.jsonl",
        "tests/fixtures/lifecycle/README.md",
    )
    for relative in files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic fixture")
    members = [f"tellyq-example/{relative}" for relative in files]
    check_source_fixtures(members, tmp_path)
    for missing in members:
        with pytest.raises(RuntimeError, match="missing reviewed fixtures"):
            check_source_fixtures([member for member in members if member != missing], tmp_path)
