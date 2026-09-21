"""Prove the core can load in an interpreter with no site packages at all."""

import subprocess
import sys
from pathlib import Path


def test_core_imports_without_optional_dependencies_or_import_side_effects(tmp_path):
    project = Path(__file__).resolve().parents[2]
    script = """
import sys
sys.path.insert(0, sys.argv[1])
def audit(event, args):
    if event.startswith('socket.') or event in ('os.mkdir', 'os.rename', 'os.remove'):
        raise AssertionError('core import performed I/O: ' + event)
    if event == 'open' and len(args) > 1 and isinstance(args[1], str):
        if any(flag in args[1] for flag in 'wax+'):
            raise AssertionError('core import wrote a file')
sys.addaudithook(audit)
from tellyq.domain import policy, ports, values
for forbidden in ('pychromecast', 'zeroconf', 'casttube', 'requests', 'milo', 'chirp'):
    assert not any(name == forbidden or name.startswith(forbidden + '.') for name in sys.modules)
assert policy.observe
assert ports.PlaybackBackend
assert values.SessionSnapshot
assert 'tellyq.cast' not in sys.modules
assert 'tellyq.controller' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, str(project)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())
