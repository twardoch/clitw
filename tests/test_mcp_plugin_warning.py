# this_file: tests/test_mcp_plugin_warning.py
"""The optional Logfire/OpenTelemetry mismatch must not hide unrelated CLI diagnostics."""

import os
import subprocess
import sys


def test_cli_keeps_other_plugins_and_restores_warnings(tmp_path):
    (tmp_path / "failed_plugin.py").write_text(
        """raise ImportError("cannot import name '_ExtendedAttributes' from 'opentelemetry.util.types' (fixture)")\n"""
    )
    metadata = tmp_path / "fixture_plugin-1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: fixture-plugin\nVersion: 1\n")
    (metadata / "entry_points.txt").write_text(
        "[pydantic]\nlogfire-plugin = failed_plugin:plugin\nother-plugin = failed_plugin:plugin\n"
    )
    program = """
import warnings
from clitwy.mcp import runtime
def invoke(*args):
    from pydantic import BaseModel
    class Example(BaseModel):
        value: str
    assert Example(value='hello').value == 'hello'
    warnings.warn('unrelated provider warning')
runtime.invoke = invoke
previous = warnings.filters[:]
assert runtime.main(['state', 'source', 'name', 'pin']) == 0
assert warnings.filters == previous
"""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), *sys.path]), HOME=str(tmp_path))
    env.pop("PYDANTIC_DISABLE_PLUGINS", None)
    result = subprocess.run(
        [sys.executable, "-c", program], env=env, capture_output=True, text=True, timeout=15, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "`logfire-plugin` Pydantic plugin" not in result.stderr
    assert "`other-plugin` Pydantic plugin" in result.stderr
    assert "unrelated provider warning" in result.stderr
