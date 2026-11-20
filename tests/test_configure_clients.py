import os
import runpy
import subprocess
import sys
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


def test_configure_clients_codex_registers_without_overwriting_repo_config(tmp_path, monkeypatch):
    script = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/configure_clients.py")
    )
    main = script["main"]
    root = _prepare_repo(tmp_path, script)
    repo_codex_config = root / ".codex" / "config.toml"
    repo_codex_config.parent.mkdir()
    repo_codex_config.write_text('model = "custom-project-setting"\n', encoding="utf-8")

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

    assert repo_codex_config.read_text(encoding="utf-8") == 'model = "custom-project-setting"\n'
    add_calls = [call for call in calls if call[:3] == ["codex", "mcp", "add"]]
    assert len(add_calls) == 3
    assert {call[3] for call in add_calls} == {"rutgers_soc", "middlesex_courses", "nj_transfer"}
    assert all(call[4] == "--" for call in add_calls)


def test_configure_clients_claude_handles_non_user_scope_collision(tmp_path, monkeypatch):
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
        # Simulate missing user-scope entry (e.g. name only exists in project scope).
        if command[:3] == ["claude", "mcp", "remove"]:
            return subprocess.CompletedProcess(command, 1, stderr="not found in user scope")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(script["subprocess"], "run", run)
    monkeypatch.setattr(sys, "argv", ["configure_clients", "--client", "claude"])
    main()

    add_calls = [call for call in calls if call[:3] == ["claude", "mcp", "add"]]
    assert len(add_calls) == 3
    assert {call[7] for call in add_calls} == {"rutgers_soc", "middlesex_courses", "nj_transfer"}


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
