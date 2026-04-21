"""Command-line entry points for the agent.

Usage (once deps are installed):

    python -m agent serve              # start the Intake/FastAPI surface on :8000
    python -m agent fire <fixture.json> # drive the graph off a webhook fixture

MVP1 scope only — no LLM calls; everything resolves to skeleton stubs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "agent.intake.webhook:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


async def _fire_fixture(path: Path) -> int:
    from fastapi.testclient import TestClient

    from agent.intake.webhook import build_app

    payload = json.loads(path.read_text())
    client = TestClient(build_app())
    response = client.post("/webhook/alertmanager", json=payload)
    sys.stdout.write(json.dumps(response.json(), indent=2) + "\n")
    return 0 if response.status_code == 200 else 1


def _cmd_fire(args: argparse.Namespace) -> int:
    path = Path(args.fixture)
    if not path.exists():
        sys.stderr.write(f"fixture not found: {path}\n")
        return 2
    return asyncio.run(_fire_fixture(path))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent", description="alph-e DevOps agent CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="Run the Intake FastAPI service.")
    p_serve.add_argument("--host", default="0.0.0.0", help="Bind host (default 0.0.0.0).")
    p_serve.add_argument("--port", type=int, default=8000, help="Bind port (default 8000).")
    p_serve.add_argument("--reload", action="store_true", help="Enable autoreload for dev.")
    p_serve.set_defaults(func=_cmd_serve)

    p_fire = sub.add_parser("fire", help="POST a fixture webhook payload at the Intake app.")
    p_fire.add_argument("fixture", help="Path to an Alertmanager JSON fixture.")
    p_fire.set_defaults(func=_cmd_fire)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    func: Any = args.func
    return int(func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
