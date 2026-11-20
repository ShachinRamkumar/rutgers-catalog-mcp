import os
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


def _prepare_repo(tmp_path: Path, script: dict) -> Path:
    root = tmp_path / "repo"
    binary = root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    binary.mkdir(parents=True)
    suffix = ".exe" if os.name == "nt" else ""
    for executable in script["SERVERS"].values():
        (binary / (executable + suffix)).touch()
    return root


def test_configure_clients_registers_three_catalog_servers_for_codex(tmp_path, monkeypatch):
    script = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/configure_clients.py")
    )
    main = script["main"]
    root = _prepare_repo(tmp_path, script)

    monkeypatch.setitem(main.__globals__, "__file__", str(root / "scripts/configure_clients.py"))
    monkeypatch.setitem(main.__globals__, "user_cache_path", lambda _: tmp_path / "cache")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(script["shutil"], "which", lambda name: f"/fake/{name}")
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(script["subprocess"], "run", run)
    monkeypatch.setattr(sys, "argv", ["configure_clients", "--client", "codex"])
    main()

    definitions = tomllib.loads((root / ".codex/config.toml").read_text(encoding="utf-8"))[
        "mcp_servers"
    ]
    assert set(definitions) == {"rutgers_soc", "middlesex_courses", "nj_transfer"}
    assert len(calls) == 3
    assert all(call[:3] == ["codex", "mcp", "add"] and call[4] == "--" for call in calls)


def test_configure_clients_registers_three_catalog_servers_for_claude(tmp_path, monkeypatch):
    script = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/configure_clients.py")
    )
    main = script["main"]
    root = _prepare_repo(tmp_path, script)

    monkeypatch.setitem(main.__globals__, "__file__", str(root / "scripts/configure_clients.py"))
    monkeypatch.setitem(main.__globals__, "user_cache_path", lambda _: tmp_path / "cache")
    monkeypatch.setattr(script["shutil"], "which", lambda name: f"/fake/{name}")
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[:3] == ["claude", "mcp", "get"]:
            return subprocess.CompletedProcess(command, 1)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(script["subprocess"], "run", run)
    monkeypatch.setattr(sys, "argv", ["configure_clients", "--client", "claude"])
    main()

    add_calls = [call for call in calls if call[:3] == ["claude", "mcp", "add"]]
    assert len(add_calls) == 3
    assert {call[7] for call in add_calls} == {"rutgers_soc", "middlesex_courses", "nj_transfer"}
    assert not (root / ".codex/config.toml").exists()


def test_configure_clients_fails_cleanly_when_executables_missing(tmp_path, monkeypatch):
    script = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/configure_clients.py")
    )
    main = script["main"]
    root = tmp_path / "empty-repo"
    root.mkdir()

    monkeypatch.setitem(main.__globals__, "__file__", str(root / "scripts/configure_clients.py"))
    monkeypatch.setattr(script["shutil"], "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(sys, "argv", ["configure_clients", "--client", "codex"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
