"""FastAPI receiver for Alertmanager webhooks.

Contract: Alertmanager POSTs a v4 webhook payload to ``/webhook/alertmanager``.
We validate, normalise, and seed an ``IncidentState`` — one per *firing* alert
in the batch — then hand off to the orchestrator.

MVP1 stays synchronous: the POST returns 200 with the seeded incident ids.
Post-MVP this will hand to a queue so the HTTP round-trip stays fast.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from agent.schemas import Alert, IncidentPhase, IncidentState, Severity

router = APIRouter(prefix="/webhook", tags=["intake"])


class AlertmanagerAlert(BaseModel):
    """Single alert inside an Alertmanager webhook payload."""

    model_config = ConfigDict(extra="ignore")

    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime  # noqa: N815 — Alertmanager wire format is camelCase
    endsAt: datetime | None = None  # noqa: N815
    fingerprint: str | None = None


class AlertmanagerPayload(BaseModel):
    """Alertmanager webhook v4 payload.

    We only use the fields we trust; extras are ignored.
    """

    model_config = ConfigDict(extra="ignore")

    version: str | None = None
    groupKey: str | None = None  # noqa: N815
    status: str
    receiver: str
    alerts: list[AlertmanagerAlert]


def _severity_from_label(raw: str | None) -> Severity:
    match (raw or "").lower():
        case "critical" | "page":
            return Severity.critical
        case "high" | "error":
            return Severity.high
        case "medium" | "warning" | "warn":
            return Severity.medium
        case _:
            return Severity.low


def _service_from_labels(labels: dict[str, str]) -> str:
    for key in ("service", "app", "app_kubernetes_io_name", "job"):
        if value := labels.get(key):
            return value
    return "unknown"


def _seed_incident(alert: AlertmanagerAlert, receiver: str) -> IncidentState:
    """Normalise a single firing alert into a seed ``IncidentState``."""
    now = datetime.now(UTC)
    seeded_alert = Alert(
        source=f"alertmanager:{receiver}",
        raw_message=alert.annotations.get("description")
        or alert.annotations.get("summary")
        or alert.labels.get("alertname", "unknown-alert"),
        service=_service_from_labels(alert.labels),
        severity=_severity_from_label(alert.labels.get("severity")),
        fired_at=alert.startsAt,
        labels=alert.labels,
    )
    return IncidentState(
        incident_id=f"inc_{uuid.uuid4().hex[:10]}",
        alert=seeded_alert,
        phase=IncidentPhase.intake,
        created_at=now,
        updated_at=now,
    )


class IntakeResponse(BaseModel):
    """What the webhook tells Alertmanager when a batch is accepted."""

    accepted: int
    ignored: int
    incidents: list[str]


@router.post(
    "/alertmanager",
    response_model=IntakeResponse,
    status_code=status.HTTP_200_OK,
    summary="Receive Alertmanager webhook v4 payload.",
)
async def alertmanager_webhook(payload: AlertmanagerPayload) -> IntakeResponse:
    """Seed an ``IncidentState`` per firing alert; return the ids.

    Non-firing (``resolved``) alerts are ignored for now — MVP1 cares only
    about active incidents. The agent detects resolution itself via collectors.
    """
    if not payload.alerts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Alertmanager payload contained no alerts.",
        )

    accepted: list[IncidentState] = []
    ignored = 0
    for alert in payload.alerts:
        if alert.status != "firing":
            ignored += 1
            continue
        accepted.append(_seed_incident(alert, payload.receiver))

    # TODO(WI-009): hand each seeded IncidentState to the orchestrator graph.
    # For MVP1 skeleton we just return the ids so the Alertmanager route is
    # observable end-to-end.

    return IntakeResponse(
        accepted=len(accepted),
        ignored=ignored,
        incidents=[inc.incident_id for inc in accepted],
    )


def build_app(extra_routes: list[APIRouter] | None = None) -> FastAPI:
    """Factory so tests can mount a fresh app without import-time side effects."""
    app = FastAPI(
        title="alph-e intake",
        version="0.0.1",
        description="DevOps investigation agent — inbound webhook surface.",
    )
    app.include_router(router)
    for extra in extra_routes or []:
        app.include_router(extra)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": "alph-e-intake"}

    return app


app = build_app()
