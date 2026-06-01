#!/usr/bin/env python3
"""
bootstrap_auth.py — Pre-populate the mcp-remote OAuth token cache on the host.

Remote, OAuth-protected MCP servers are bridged with ``npx -y mcp-remote <url>``
and cache their tokens under ``MCP_REMOTE_CONFIG_DIR``.  The very first OAuth
grant needs a human in a browser once; afterwards mcp-remote refreshes silently.

This script reads the configured providers, finds every ``package.command`` that
runs ``mcp-remote``, and runs each one with stdin/stdout attached so you can
complete the browser flow.  The resulting token cache is written to
``MCP_REMOTE_CONFIG_DIR`` (default ``./.mcp-auth``, matching the dev
docker-compose bind mount) so a subsequent ``docker compose up`` starts with the
cache already warm — no interactive prompts in the container.

Usage:
    python3 bootstrap_auth.py                 # bootstrap every remote provider
    python3 bootstrap_auth.py <url> [<url>…]  # bootstrap explicit URL(s)

Environment:
    MCP_TOOL_CONFIG_DIR   where provider YAMLs live (default ./tools, then /app/tools)
    MCP_REMOTE_CONFIG_DIR where mcp-remote caches tokens (default ./.mcp-auth)
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml


def _config_dir() -> Path:
    env = os.environ.get("MCP_TOOL_CONFIG_DIR")
    if env:
        return Path(env)
    if Path("tools").is_dir():
        return Path("tools")
    return Path("/app/tools")


def extract_remote_commands(specs: list[dict]) -> list[str]:
    """Return every package command that bridges a remote server via mcp-remote."""
    commands: list[str] = []
    for spec in specs:
        pkg = spec.get("package") or {}
        command = (pkg.get("command") or "").strip()
        if command and "mcp-remote" in command and command not in commands:
            commands.append(command)
    return commands


def command_url(command: str) -> str | None:
    """Best-effort: pull the first http(s) URL out of an mcp-remote command."""
    for token in shlex.split(command):
        if token.startswith("http://") or token.startswith("https://"):
            return token
    return None


def load_specs(config_dir: Path) -> list[dict]:
    specs: list[dict] = []
    for path in sorted(config_dir.glob("*.yaml")):
        try:
            specs.append(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        except Exception as exc:  # noqa: BLE001
            print(f"  ! skipping {path.name}: {exc}")
    return specs


def run_bootstrap(commands: list[str], cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["MCP_REMOTE_CONFIG_DIR"] = str(cache_dir)
    print(f"→ token cache: {cache_dir}\n")
    for command in commands:
        url = command_url(command) or command
        print(f"=== Authorizing {url} ===")
        print(f"    running: {command}")
        print("    Complete the browser flow, then press Ctrl-C to continue.\n")
        try:
            subprocess.run(shlex.split(command), env=env, check=False)
        except KeyboardInterrupt:
            print("\n    (moving on)\n")
    print("✓ Done. Start the proxy with the same MCP_REMOTE_CONFIG_DIR mounted.")


def main(argv: list[str]) -> int:
    if argv:
        commands = [f"npx -y mcp-remote {url}" for url in argv]
    else:
        config_dir = _config_dir()
        if not config_dir.is_dir():
            print(f"✗ config dir not found: {config_dir} (set MCP_TOOL_CONFIG_DIR)")
            return 1
        commands = extract_remote_commands(load_specs(config_dir))
        if not commands:
            print(f"No mcp-remote providers found in {config_dir}. "
                  "Pass server URL(s) explicitly to bootstrap them anyway.")
            return 0

    cache_dir = Path(os.environ.get("MCP_REMOTE_CONFIG_DIR", "./.mcp-auth"))
    run_bootstrap(commands, cache_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
