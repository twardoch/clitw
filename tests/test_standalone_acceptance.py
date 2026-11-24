# this_file: tests/test_standalone_acceptance.py
"""Exercise published provider commands as a shell user with no package tools on PATH."""

import json
import os
import shutil
import subprocess
import sysconfig
from pathlib import Path


def test_exported_openapi_command_runs_without_tw_or_state_environment(tmp_path: Path) -> None:
    home = tmp_path / "empty home"
    home.mkdir()
    state = tmp_path / "private state"
    public = tmp_path / "selected bin"
    builder = shutil.which("mcxadmin-build-openapi", path=sysconfig.get_path("scripts"))
    assert builder, "Install the project so that its public console entry points are available"
    env = {**os.environ, "HOME": str(home), "CLITW_HOME": str(state)}
    built = subprocess.run(
        [builder, "--bin-dir", str(public), "mcx-archive-wayback-api"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    env.pop("CLITW_HOME")
    env["PATH"] = str(public) + os.pathsep + os.defpath
    assert shutil.which("tw", path=env["PATH"]) is None
    assert shutil.which("mcx-archive-wayback", path=str(public)) is None
    exported = shutil.which("mcx-archive-wayback-api", path=str(public))
    assert exported
    result = subprocess.run(
        [exported, "--list", "--json"],
        cwd=home,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert [op["path"] for op in json.loads(result.stdout)] == ["/wayback/available"]
