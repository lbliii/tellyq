"""Composition imports must not require Cast or optional interface frameworks."""

import subprocess
import sys


def test_application_and_storage_import_without_device_dependencies():
    script = """
import importlib.abc
import sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pychromecast', 'casttube', 'zeroconf', 'milo', 'chirp'}:
            raise AssertionError('forbidden dependency: ' + fullname)
sys.meta_path.insert(0, Block())
import tellyq.application
import tellyq.controller
import tellyq.session_store
import tellyq.cast_backend
import tellyq.domain.policy
import tellyq.__main__
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
