"""Register the three public catalog MCP server executables with local MCP clients."""

import argparse
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from platformdirs import user_cache_path

SERVERS = {
    "rutgers_soc": "rutgers-soc-mcp",
    "middlesex_courses": "middlesex-courses-mcp",
    "nj_transfer": "nj-transfer-mcp",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=["codex", "claude", "both"], default="both")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    bin_dir = root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    suffix = ".exe" if os.name == "nt" else ""
    commands = {name: bin_dir / f"{executable}{suffix}" for name, executable in SERVERS.items()}
    definitions = {name: {"command": str(command)} for name, command in commands.items()}

    clients = ["codex", "claude"] if args.client == "both" else [args.client]
    for client in clients:
        if not shutil.which(client):
            parser.error(f"{client} is not on PATH. Install it before configuring its MCP servers.")
    if not all(path.is_file() for path in commands.values()):
        parser.error("Server executables are missing. Run uv sync --locked in this clone first.")

    config_dir = root / ".codex"
    config_path = config_dir / "config.toml"
    configs = [config_path]
    if "codex" in clients:
        configs.append(
            Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
        )
    if "claude" in clients:
        configs.append(Path.home() / ".claude.json")

    backup_dir = (
        user_cache_path("rutgers-catalog-mcp")
        / "config-backups"
        / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    )
    backup_dir.mkdir(parents=True, mode=0o700)
    for index, config in enumerate(configs):
        if config.exists():
            saved = backup_dir / f"{index}-{config.name}"
            shutil.copy2(config, saved)
            if os.name != "nt":
                saved.chmod(0o600)

    for client in clients:
        for name, definition in definitions.items():
            target = definition["command"]
            if client == "codex":
                subprocess.run([client, "mcp", "add", name, "--", target], check=True)
            else:
                existing = subprocess.run(
                    [client, "mcp", "get", name], capture_output=True, text=True, check=False
                )
                if existing.returncode == 0:
                    subprocess.run([client, "mcp", "remove", "--scope", "user", name], check=True)
                subprocess.run(
                    [
                        client,
                        "mcp",
                        "add",
                        "--scope",
                        "user",
                        "--transport",
                        "stdio",
                        name,
                        "--",
                        target,
                    ],
                    check=True,
                )

    if "codex" in clients:
        config_dir.mkdir(exist_ok=True)
        config_path.write_text(
            "\n\n".join(
                f"[mcp_servers.{name}]\n"
                + "\n".join(f"{key} = {json.dumps(value)}" for key, value in definition.items())
                + "\n"
                "startup_timeout_sec = 30\ntool_timeout_sec = 120\nenabled = true"
                for name, definition in definitions.items()
            )
            + "\n",
            encoding="utf-8",
        )
    print(f"Configured {len(definitions)} servers for {', '.join(clients)}.")
    print(f"Configuration backups: {backup_dir}")
    print("Start a new client session to load the servers.")


if __name__ == "__main__":
    main()
