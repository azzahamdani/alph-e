"""Intake webhook — seeds an IncidentState per firing alert."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from agent.intake.webhook import build_app


def _fixture_path() -> Path:
    return (
        Path(__file__).parent.parent
        / "fixtures"
        / "incidents"
        / "oom-leaky-service.json"
    )


def test_webhook_seeds_incident_from_fixture() -> None:
    client = TestClient(build_app())
    payload = json.loads(_fixture_path().read_text())
    response = client.post("/webhook/alertmanager", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] >= 1
    assert body["ignored"] >= 0
    assert all(inc.startswith("inc_") for inc in body["incidents"])


def test_webhook_rejects_empty_payload() -> None:
    client = TestClient(build_app())
    response = client.post(
        "/webhook/alertmanager",
        json={"status": "firing", "receiver": "alph-e", "alerts": []},
    )
    assert response.status_code == 400


def test_webhook_ignores_resolved_alerts() -> None:
    client = TestClient(build_app())
    response = client.post(
        "/webhook/alertmanager",
        json={
            "status": "resolved",
            "receiver": "alph-e",
            "alerts": [
                {
                    "status": "resolved",
                    "labels": {"alertname": "X", "severity": "warning"},
                    "annotations": {},
                    "startsAt": "2026-04-21T14:00:00Z",
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 0
    assert body["ignored"] == 1
