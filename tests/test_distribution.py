# this_file: tests/test_distribution.py
"""Exercise the release archives, including the wheel built from the source distribution."""

import configparser
import email
import json
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath


def test_release_archives_ship_public_entry_points_and_license(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["uv", "build", "--out-dir", str(tmp_path)],
        cwd=root,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    (source,) = tmp_path.glob("*.tar.gz")
    (wheel,) = tmp_path.glob("*.whl")
    with tarfile.open(source) as archive:
        source_paths = [PurePosixPath(member.name) for member in archive.getmembers()]
    assert any(path.name == "pyproject.toml" for path in source_paths)
    with zipfile.ZipFile(wheel) as archive:
        wheel_paths = [PurePosixPath(name) for name in archive.namelist()]
        (record,) = [
            path for path in wheel_paths if path.name == "METADATA" and path.parent.name.endswith(".dist-info")
        ]
        metadata = email.message_from_bytes(archive.read(str(record)))
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        assert metadata["Name"] == project["name"] == "clitwy" and metadata["Version"]
        assert not any(path.parts[0] == "clitw" for path in wheel_paths)
        assert any(path.as_posix().endswith("/share/clitwy/skills/clitwy/SKILL.md") for path in wheel_paths)
        assert any(dep.lower().startswith("mcp2cli") for dep in metadata.get_all("Requires-Dist", []))
        entries = configparser.ConfigParser(interpolation=None)
        entries.read_string(archive.read(str(record.with_name("entry_points.txt"))).decode())
        assert {"tw", "tw-doctor", "tw-groups"} <= entries["console_scripts"].keys()
        assert entries["console_scripts"]["mcxadmin-build-mcp"] == "clitwy.builders:mcp_main"
        assert entries["console_scripts"]["mcxadmin-build-openapi"] == "clitwy.builders:openapi_main"
        assert entries["console_scripts"]["mcxadmin-auth"] == "clitwy.cli:auth"
        assert entries["console_scripts"]["mcxadmin-oauth"] == "clitwy.mcp.login:main"
        providers = set(json.loads(archive.read("clitwy/mcp/catalog.json")))
        rows = archive.read("clitwy/openapi/catalogue.tsv").decode().splitlines()
        manual = set()
        for row in rows:
            if row.startswith("mcx-"):
                name, _, base, *_ = row.split("\t")
                if base:
                    providers.add(name)
                else:
                    manual.add(name)
        assert any(item.startswith("mcx-agno-api") for item in manual)
        assert manual.isdisjoint(entries["console_scripts"])
        assert "mcx-imdb-apidev-api" not in entries["console_scripts"]
        assert providers == {name for name in entries["console_scripts"] if name.startswith("mcx-")}
        assert {"tw-doctor", "tw-groups"} == {name for name in entries["console_scripts"] if name.startswith("tw-")}
        assert any(path.name == "LICENSE" and path.is_relative_to(record.parent) for path in wheel_paths)
        assert any(path.as_posix() == "clitwy/__main__.py" for path in wheel_paths)
    assert any(path.name == "LICENSE" for path in source_paths)
    for path in [*source_paths, *wheel_paths]:
        assert not path.is_absolute() and ".." not in path.parts
        assert {
            "_private",
            "vendor",
            ".env",
            ".git",
            ".venv",
            ".mcpproxy",
            "baked.json",
        }.isdisjoint(path.parts), path
