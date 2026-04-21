"""Idempotent action executor.

Responsibilities:
- Check the idempotency key (``ActionIntent.hash``) against already-recorded
  execution records so retries cannot fan out duplicate mutations.
- For MVP1, execution is **dry-run only**: log what would happen and write a
  record to the evidence store.
- Mutations MUST run under ``KUBECONFIG_AGENT`` — never the ambient context.
- On compensation, derive the inverse operation from ``ActionIntent.rollback_hint``
  and record both forward and compensating records.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from agent.evidence.client import EvidenceClient
from agent.schemas.incident import Action, ActionIntent, ActionStatus, ActionType

_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

_KUBECONFIG_AGENT_ENV: Final[str] = "KUBECONFIG_AGENT"


class ExecutionRecord(BaseModel):
    """A persisted record of an execution attempt (forward or compensating).

    The idempotency key is ``intent_hash``; if a record with the same hash
    already exists in the evidence store the executor skips execution.
    """

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    intent_hash: str
    action_type: ActionType
    target: str
    parameters: dict[str, str | int | bool] = Field(default_factory=dict)
    is_compensation: bool = Field(
        default=False,
        description="True when this record is a rollback/compensating action.",
    )
    dry_run: bool = Field(
        default=True,
        description="MVP1 always True; set False when real mutations land.",
    )
    kubeconfig_path: str | None = Field(
        default=None,
        description="KUBECONFIG_AGENT value at execution time.",
    )
    outcome: str = Field(
        default="dry_run_success",
        description="Outcome label: dry_run_success | skipped_idempotent | failed",
    )
    executed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    notes: str = ""


class IdempotentExecutor:
    """Execute a list of ActionIntents exactly once each.

    The executor holds an in-process seen-set (``_executed_hashes``).  In
    production the evidence store acts as the durable idempotency log;
    ``_executed_hashes`` is pre-populated from previously stored records so
    cross-process retries are also covered (once F-06 lands).
    """

    def __init__(
        self,
        evidence_client: EvidenceClient | None = None,
        *,
        already_executed: set[str] | None = None,
    ) -> None:
        self._evidence = evidence_client
        # Seed from durable store if provided (enables cross-process idempotency).
        self._executed_hashes: set[str] = set(already_executed or [])

    def _kubeconfig_path(self) -> str | None:
        return os.environ.get(_KUBECONFIG_AGENT_ENV)

    async def _persist_record(self, record: ExecutionRecord) -> None:
        """Write the execution record to the evidence store (best-effort in MVP1)."""
        if self._evidence is None:
            return
        try:
            payload = json.dumps(record.model_dump(mode="json")).encode()
            await self._evidence.put_blob(
                evidence_id=f"exec-record-{record.record_id}",
                payload=payload,
                content_type="application/json",
            )
        except NotImplementedError:
            # Evidence store is still a stub (WI-003); log and continue.
            _LOGGER.debug(
                "Evidence store stub — skipping persistence for record %s",
                record.record_id,
            )
        except Exception:
            _LOGGER.exception(
                "Failed to persist execution record %s — continuing (idempotency key still held in memory)",
                record.record_id,
            )

    async def execute(self, intent: ActionIntent) -> tuple[ExecutionRecord, Action]:
        """Execute ``intent`` idempotently.

        Returns ``(record, action)`` where ``action`` is the ``Action`` to
        append to ``IncidentState.actions_taken``.

        If the intent has already been executed the record will have
        ``outcome="skipped_idempotent"`` and the action ``status=succeeded``
        (the original execution succeeded; idempotent skip is safe).
        """
        if intent.hash in self._executed_hashes:
            _LOGGER.info(
                "Idempotent skip: intent %r already executed", intent.hash
            )
            record = ExecutionRecord(
                intent_hash=intent.hash,
                action_type=intent.action_type,
                target=intent.target,
                parameters=dict(intent.parameters),
                outcome="skipped_idempotent",
                kubeconfig_path=self._kubeconfig_path(),
                notes="Idempotent skip — execution record already present.",
            )
            action = Action(
                id=str(uuid.uuid4()),
                type=intent.action_type,
                description=f"[idempotent-skip] {intent.expected_effect}",
                status=ActionStatus.succeeded,
                intent_hash=intent.hash,
                executed_at=record.executed_at,
            )
            return record, action

        # Dry-run execution: log the would-be mutation.
        kubeconfig = self._kubeconfig_path()
        _LOGGER.info(
            "DRY-RUN execution of intent %r: type=%s target=%s kubeconfig=%s",
            intent.hash,
            intent.action_type,
            intent.target,
            kubeconfig or "<not set — KUBECONFIG_AGENT missing>",
        )

        record = ExecutionRecord(
            intent_hash=intent.hash,
            action_type=intent.action_type,
            target=intent.target,
            parameters=dict(intent.parameters),
            kubeconfig_path=kubeconfig,
            notes=(
                f"DRY-RUN: would execute {intent.action_type} on {intent.target}. "
                f"Expected effect: {intent.expected_effect}"
            ),
        )

        self._executed_hashes.add(intent.hash)
        await self._persist_record(record)

        action = Action(
            id=str(uuid.uuid4()),
            type=intent.action_type,
            description=intent.expected_effect,
            status=ActionStatus.succeeded,
            intent_hash=intent.hash,
            executed_at=record.executed_at,
        )
        return record, action

    async def compensate(self, intent: ActionIntent) -> tuple[ExecutionRecord, Action]:
        """Execute the compensating (rollback) action for ``intent``.

        The inverse is derived from ``intent.rollback_hint``; it is always
        recorded as a distinct ``ExecutionRecord`` (``is_compensation=True``).
        """
        _LOGGER.info(
            "DRY-RUN compensation for intent %r: rollback_hint=%r",
            intent.hash,
            intent.rollback_hint,
        )

        kubeconfig = self._kubeconfig_path()
        record = ExecutionRecord(
            intent_hash=intent.hash,
            action_type=ActionType.rollback,
            target=intent.target,
            parameters=dict(intent.parameters),
            is_compensation=True,
            kubeconfig_path=kubeconfig,
            notes=(
                f"DRY-RUN compensation: {intent.rollback_hint}. "
                f"Original target: {intent.target}"
            ),
        )

        await self._persist_record(record)

        action = Action(
            id=str(uuid.uuid4()),
            type=ActionType.rollback,
            description=f"Compensation: {intent.rollback_hint}",
            status=ActionStatus.succeeded,
            intent_hash=intent.hash,
            executed_at=record.executed_at,
        )
        return record, action
