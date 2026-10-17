"""Run unchanged upstream checks with credentials removed and sockets blocked.

Usage: python scripts/run_offline.py pytest -q
       python scripts/run_offline.py scenarios
This is a verification harness, not an Agent evaluator or runtime component.
"""

from __future__ import annotations

import os
import runpy
import socket
import sys
from pathlib import Path


def configure_offline() -> None:
    """Force the rule model and reject network connections before app imports."""
    os.environ["SUPPORT_MODEL_PROVIDER"] = "offline"
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    for name in tuple(os.environ):
        if name.endswith("API_KEY") or name in {"SUPPORT_DB", "LANGCHAIN_TRACING"}:
            os.environ.pop(name, None)

    def blocked(*args, **kwargs):
        raise RuntimeError("Network connections are disabled by the Stage 0 offline harness")

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.create_connection = blocked


def main() -> None:
    """Dispatch only the original pytest suite or scenario module."""
    if len(sys.argv) < 2 or sys.argv[1] not in {"pytest", "scenarios"}:
        raise SystemExit("Usage: run_offline.py {pytest [pytest args...]|scenarios}")
    command, args = sys.argv[1], sys.argv[2:]
    if command == "scenarios" and args:
        raise SystemExit("The offline scenario command takes no provider arguments")
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root / "src"))
    configure_offline()
    if command == "pytest":
        sys.argv = ["pytest", *args]
        runpy.run_module("pytest", run_name="__main__")
    else:
        sys.argv = ["support_agent.evaluate", "--provider", "offline"]
        runpy.run_module("support_agent.evaluate", run_name="__main__")


if __name__ == "__main__":
    main()
