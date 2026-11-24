# this_file: src/clitwy/utilities/groups.py
"""Manage MCPProxy servers by name or YAML tag over its Unix socket or on disk."""

import argparse
import json
import os
import tempfile
from pathlib import Path

import httpx
import yaml
from filelock import FileLock

from clitwy import paths


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve a bridge's symlink to its real configuration file.
    path = path.resolve()
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        pending = Path(directory) / "config"
        with pending.open("w") as output:
            output.write(text)
        os.chmod(pending, path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(pending, path)


def _sets(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict) or not all(
        isinstance(s, str) and isinstance(tags, list) and all(isinstance(t, str) for t in tags)
        for s, tags in data.items()
    ):
        raise ValueError("Expected a YAML map of server names to lists of tags")
    return data


def _tokens(value: str) -> set[str]:
    return {t.strip().lower() for t in value.split(",") if t.strip()}


def _toggle(action: str, targets: str | None, servers: list[dict], tags_by_name: dict[str, set[str]]) -> int:
    defined = set().union(*tags_by_name.values()) - {"*"}
    tokens = _tokens(targets or "") if action != "all" else set()
    tags = {t.removeprefix("@") for t in tokens if action == "tag" or t.startswith("@")}
    names = {t for t in tokens if not t.startswith("@") and action != "tag"}
    configured = {s["name"].lower() for s in servers}
    unknown = (names - configured) | (tags - defined)
    if unknown:
        print("Unknown names or tags: " + ", ".join(sorted(unknown)))
    valid_tags = tags & defined
    if action == "tag" and not valid_tags:
        raise ValueError("Select at least one defined tag")
    matched = names | {n for n, labels in tags_by_name.items() if valid_tags and (labels & valid_tags or "*" in labels)}
    changed = 0
    for server in servers:
        current = server.get("enabled", False)
        match = server["name"].lower() in matched
        desired = {
            "all": True,
            "off": current and targets is not None and not match,
            "on": current or match,
            "tag": match,
        }[action]
        if current != desired:
            server["enabled"] = desired
            changed += 1
    return changed


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tw groups", description=__doc__)
    parser.add_argument("--bridge-dir", type=Path, help="MCPProxy directory (or CLITW_MCPROXY_DIR)")
    parser.add_argument("--config", type=Path, help="Path to mcp_config.json")
    parser.add_argument("--tags", type=Path, help="Path to server/tag YAML")
    parser.add_argument("--socket", type=Path, help="Path to MCPProxy's Unix domain socket")
    subs = parser.add_subparsers(dest="action", required=True)
    for action in ("list", "all", "tag", "on", "off", "scan"):
        sub = subs.add_parser(action)
        if action in ("tag", "off", "scan"):
            sub.add_argument("targets", nargs="?", default=None)
        elif action == "on":
            sub.add_argument("targets")
    parser.set_defaults(targets=None)
    opt = parser.parse_args(argv)
    bridge = opt.bridge_dir or os.environ.get("CLITW_MCPROXY_DIR")
    if not bridge and not opt.config and not opt.tags:
        parser.error("supply --bridge-dir, CLITW_MCPROXY_DIR, or explicit file paths")
    base = Path(bridge).expanduser() if bridge else (opt.config or opt.tags).expanduser().parent
    config_path = (opt.config or base / "mcp_config.json").expanduser()
    tags_path = (opt.tags or base / "mcpp.yaml").expanduser()
    socket_path = (opt.socket or base / "mcpproxy.sock").expanduser()
    if opt.action == "list" or (opt.action == "tag" and opt.targets is None):
        return _execute(config_path, tags_path, socket_path, opt)
    paths.home().mkdir(mode=0o700, parents=True, exist_ok=True)
    with FileLock(paths.home() / "groups.lock", timeout=30):
        return _execute(config_path, tags_path, socket_path, opt)


def _execute(config_path: Path, tags_path: Path, socket_path: Path, opt: argparse.Namespace) -> int:
    sets = _sets(tags_path)
    tags_by_name = {n.lower(): {tag.lower() for tag in tags} for n, tags in sets.items()}
    defined = set().union(*tags_by_name.values()) - {"*"}
    if opt.action == "tag" and opt.targets is None:
        for tag in sorted(defined):
            print(f"{tag}: " + ", ".join(sorted(n for n in sets if {tag, "*"} & tags_by_name[n.lower()])))
        return 0

    config = json.loads(config_path.read_text())
    if not isinstance(config, dict):
        raise TypeError("Invalid MCPProxy configuration")
    with httpx.Client(
        base_url="http://localhost",
        transport=httpx.HTTPTransport(uds=str(socket_path)),
        headers={"X-API-Key": config.get("api_key", "")},
        timeout=10,
    ) as client:
        live = False
        if socket_path.exists():
            try:
                response = client.get("/api/v1/config")
            except httpx.ConnectError:
                pass  # Stale socket from a stopped daemon. Use the on-disk config.
            else:
                response.raise_for_status()
                config = response.json()["data"]["config"]
                live = True
        servers = config["mcpServers"]
        if not isinstance(servers, list) or not all(
            isinstance(s, dict) and isinstance(s.get("name"), str) for s in servers
        ):
            raise ValueError("Expected a list of named servers in mcpServers")
        if opt.action == "list":
            for server in servers:
                state = "quarantined" if server.get("quarantined") else "on" if server.get("enabled", False) else "off"
                print(f"{server['name']}\t{state}")
            return 0
        if opt.action == "scan":
            labels = [t.strip() for t in (opt.targets or "new").split(",") if t.strip()] or ["new"]
            new = [s["name"] for s in servers if s["name"].lower() not in tags_by_name]
            if new:
                sets.update({n: labels.copy() for n in new})
                _write(tags_path, yaml.safe_dump(sets, sort_keys=True))
            print(f"Added {len(new)} server(s).")
            return 0

        changed = _toggle(opt.action, opt.targets, servers, tags_by_name)
        if changed and live:
            patch = client.patch("/api/v1/config", json={"mcpServers": servers})
            patch.raise_for_status()
            if patch.json().get("data", {}).get("requires_restart"):
                print("Restart MCPProxy to apply these changes.")
        elif changed:
            _write(config_path, json.dumps(config, indent=2) + "\n")
        active = [s["name"] for s in servers if s.get("enabled", False)]
        print(f"{changed} changed. Active servers: " + (", ".join(active) or "none"))
    return 0
