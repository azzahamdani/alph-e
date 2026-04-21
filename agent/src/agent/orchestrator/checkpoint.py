"""LangGraph Postgres checkpointer wiring.

The checkpointer persists ``IncidentState`` after every node so a crash can
resume from the last committed state. Invariant: checkpoint commit must
happen *after* any evidence blob has been durably written — otherwise a
restart may dispatch a collector twice and cache the second result under
the same key.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — type-only import
    from collections.abc import AsyncIterator


@asynccontextmanager
async def postgres_checkpointer(postgres_url: str | None = None) -> "AsyncIterator[Any]":
    """Yield a LangGraph Postgres checkpointer bound to ``postgres_url``.

    Defers the ``langgraph-checkpoint-postgres`` import so the skeleton can
    run in environments that haven't resolved the full dep tree yet. MVP1
    replaces this with the real wiring once the Postgres instance exists.
    """
    url = postgres_url or os.environ.get("POSTGRES_URL")
    if not url:
        raise RuntimeError(
            "POSTGRES_URL is required for the Postgres checkpointer. "
            "Run `task infra:up` and export it, or pass `postgres_url=`."
        )
    try:
        from langgraph.checkpoint.postgres.aio import (  # type: ignore[import-not-found]
            AsyncPostgresSaver,
        )
    except ImportError as exc:  # pragma: no cover — surfaces in production wiring
        raise RuntimeError(
            "langgraph-checkpoint-postgres is not installed. "
            "Install agent dependencies with `task agent:install`."
        ) from exc

    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        yield saver
