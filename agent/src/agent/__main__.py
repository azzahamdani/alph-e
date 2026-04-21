"""Allow ``python -m agent`` to drive the CLI."""

from __future__ import annotations

from agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
