"""Slack integration for DevOps support workflow.

This module handles:
- Listening for @alph-e mentions in Slack
- Creating investigation threads in #devops-alfie channel
- Tagging #devops-support with investigation results
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from agent.schemas import Alert, IncidentPhase, IncidentState, Severity

router = APIRouter(prefix="/webhook", tags=["slack"])


class SlackEventWrapper(BaseModel):
    """Slack Events API wrapper."""

    type: str
    challenge: str | None = None  # For URL verification
    token: str | None = None
    team_id: str | None = None
    event: dict[str, Any] | None = None


class SlackMessageEvent(BaseModel):
    """Slack message event structure."""

    type: str
    channel: str
    user: str
    text: str
    ts: str
    thread_ts: str | None = None




class SlackClient:
    """Simple Slack Web API client."""

    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("SLACK_BOT_TOKEN", "")
        self.base_url = "https://slack.com/api"

    async def post_message(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        blocks: list[dict] | None = None
    ) -> dict[str, Any]:
        """Post a message to Slack."""
        if not self.token:
            return {"ok": False, "error": "no_token"}

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        payload = {
            "channel": channel,
            "text": text,
        }

        if thread_ts:
            payload["thread_ts"] = thread_ts
        if blocks:
            payload["blocks"] = blocks

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/chat.postMessage",
                headers=headers,
                json=payload
            )
            return response.json()




def _parse_slack_message(text: str) -> tuple[Severity, str]:
    """Parse a Slack message to extract severity and issue description."""
    text_lower = text.lower()

    # Detect severity from keywords
    if any(word in text_lower for word in ["critical", "down", "outage", "emergency"]):
        severity = Severity.critical
    elif any(word in text_lower for word in ["error", "failing", "broken", "high"]):
        severity = Severity.high
    elif any(word in text_lower for word in ["warning", "slow", "degraded", "medium"]):
        severity = Severity.medium
    else:
        severity = Severity.low

    return severity, text


def _seed_incident_from_slack(
    channel: str,
    user: str,
    text: str,
    ts: str,
    thread_ts: str | None = None
) -> IncidentState:
    """Create an IncidentState from a Slack message."""
    now = datetime.now(UTC)
    severity, description = _parse_slack_message(text)

    # Extract service name from message if possible
    service = "unknown"
    for word in ["api", "database", "web", "worker", "redis", "postgres", "demo", "leaky-service"]:
        if word in text.lower():
            service = word
            break

    alert = Alert(
        source=f"slack:{channel}",
        raw_message=description,
        service=service,
        severity=severity,
        fired_at=now,
        labels={
            "slack_channel": channel,
            "slack_user": user,
            "slack_ts": ts,
        }
    )

    if thread_ts:
        alert.labels["slack_thread_ts"] = thread_ts

    return IncidentState(
        incident_id=f"inc_{uuid.uuid4().hex[:10]}",
        alert=alert,
        phase=IncidentPhase.intake,
        created_at=now,
        updated_at=now,
    )


async def investigate_incident(incident: IncidentState, user_description: str) -> str:
    """Investigate an incident using real collectors and LLM analysis."""

    print(f"🔍 Starting investigation for {incident.incident_id}: {user_description}")

    # Collector URLs from environment variables
    kube_url = os.getenv("COLLECTOR_KUBE_URL", "http://kube-collector.agent.svc.cluster.local:8003")
    prom_url = os.getenv("COLLECTOR_PROM_URL", "http://prom-collector.agent.svc.cluster.local:8001")

    print(f"📡 Using collectors: kube={kube_url}, prom={prom_url}")

    # Time range for investigation (last 2 hours)
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(hours=2)

    findings = []

    try:
        # Call kube-collector for Kubernetes events
        kube_payload = {
            "incident_id": incident.incident_id,
            "question": f"Show events for pods in demo namespace related to {incident.alert.service}, especially OOMKilled, crashes, or resource issues",
            "hypothesis_id": "hyp_k8s",
            "time_range": {
                "start": start_time.isoformat(),
                "end": end_time.isoformat()
            },
            "scope_services": ["demo/leaky-service"],
            "environment_fingerprint": {
                "cluster": "devops-agent",
                "account": "local",
                "region": "local",
                "deploy_revision": "latest",
                "rollout_generation": "demo"
            },
            "max_internal_iterations": 3
        }

        async with httpx.AsyncClient() as client:
            kube_response = await client.post(f"{kube_url}/collect", json=kube_payload, timeout=30.0)
            if kube_response.status_code == 200:
                kube_data = kube_response.json()
                kube_finding = kube_data.get("finding", {})
                findings.append(f"**🔍 Kubernetes Analysis:**\n{kube_finding.get('summary', 'No data available')}")

                # Add follow-up suggestions
                followups = kube_finding.get("suggested_followups", [])
                if followups:
                    findings.append(f"**🔧 Suggested Actions:**\n" + "\n".join(f"• {followup}" for followup in followups[:3]))
            else:
                findings.append("**🔍 Kubernetes Analysis:** Unable to connect to kube-collector")

    except Exception as e:
        findings.append(f"**🔍 Kubernetes Analysis:** Error during investigation: {str(e)}")

    try:
        # Call prom-collector for metrics
        prom_payload = {
            "incident_id": incident.incident_id,
            "question": f"Show memory usage, CPU usage, and resource metrics for {incident.alert.service} in the last 2 hours. Look for memory spikes or OOM conditions.",
            "hypothesis_id": "hyp_metrics",
            "time_range": {
                "start": start_time.isoformat(),
                "end": end_time.isoformat()
            },
            "scope_services": ["demo/leaky-service"],
            "environment_fingerprint": {
                "cluster": "devops-agent",
                "account": "local",
                "region": "local",
                "deploy_revision": "latest",
                "rollout_generation": "demo"
            },
            "max_internal_iterations": 3
        }

        async with httpx.AsyncClient() as client:
            prom_response = await client.post(f"{prom_url}/collect", json=prom_payload, timeout=30.0)
            if prom_response.status_code == 200:
                prom_data = prom_response.json()
                prom_finding = prom_data.get("finding", {})
                findings.append(f"**📊 Metrics Analysis:**\n{prom_finding.get('summary', 'No metrics data available')}")
            else:
                findings.append("**📊 Metrics Analysis:** Unable to connect to prometheus collector")

    except Exception as e:
        findings.append(f"**📊 Metrics Analysis:** Error during metrics collection: {str(e)}")

    # Use LLM for analysis if we have Anthropic key
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if anthropic_key and findings:
        try:
            # Analyze findings with LLM
            analysis_prompt = f"""
            User reported: "{user_description}"

            Investigation findings:
            {chr(10).join(findings)}

            Based on these findings, provide a concise diagnosis and recommended next steps for this incident.
            Focus on actionable recommendations for the DevOps team.
            """

            import anthropic
            client = anthropic.Anthropic(api_key=anthropic_key)

            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                messages=[{"role": "user", "content": analysis_prompt}]
            )

            findings.append(f"**🤖 AI Analysis:**\n{message.content[0].text}")

        except Exception as e:
            findings.append(f"**🤖 AI Analysis:** Error during LLM analysis: {str(e)}")

    if not findings:
        return "*No investigation data available. Collectors may not be accessible.*"

    return "\n\n".join(findings)


@router.post(
    "/slack/events",
    status_code=status.HTTP_200_OK,
    summary="Receive Slack Events API webhook"
)
async def slack_events_webhook(
    request: Request,
    payload: SlackEventWrapper
) -> dict[str, Any]:
    """Handle Slack Events API webhook.

    This endpoint:
    1. Handles URL verification challenges
    2. Receives message events
    3. Creates incidents from relevant messages
    4. Creates Linear tickets
    5. Responds in Slack thread
    """

    # Handle URL verification
    if payload.type == "url_verification":
        return {"challenge": payload.challenge}

    # Handle event callbacks
    if payload.type == "event_callback" and payload.event:
        event = payload.event
        print(f"📥 Received Slack event: {event}")

        # Only process message events (not edits, deletes, etc.)
        if event.get("type") == "message" and not event.get("subtype"):
            text = event.get("text", "")
            print(f"📝 Processing message: {text}")

            # Check if message mentions the bot
            # Slack mentions can be: <@U123456> (user ID), @alph-e, @Alph-E, etc.
            text_lower = text.lower()
            if (
                "@alph-e" in text_lower or
                "alph-e" in text_lower or
                "<@" in text  # Actual Slack user mention format
            ):
                print(f"🎯 Bot mention detected in: {text}")
                # Create incident from Slack message
                incident = _seed_incident_from_slack(
                    channel=event.get("channel", ""),
                    user=event.get("user", ""),
                    text=text,
                    ts=event.get("ts", ""),
                    thread_ts=event.get("thread_ts")
                )

                # Initialize Slack client
                slack_client = SlackClient()

                # Get the devops-alfie channel (you'll need to get this channel ID)
                devops_alfie_channel = os.getenv("DEVOPS_ALFIE_CHANNEL", "devops-alfie")
                devops_support_tag = "#devops-support"

                # First, acknowledge in the original thread
                await slack_client.post_message(
                    channel=event.get("channel", ""),
                    text=f"👋 Got it! I'm investigating this issue. I'll post updates in <#{devops_alfie_channel}>",
                    thread_ts=event.get("thread_ts") or event.get("ts", "")
                )

                # Create main investigation thread in #devops-alfie
                investigation_message = await slack_client.post_message(
                    channel=devops_alfie_channel,
                    text=f"🔍 **New Investigation Started** - `{incident.incident_id}`\n\n{devops_support_tag}",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*🔍 New Investigation*\n`{incident.incident_id}`\n\n{devops_support_tag}"
                            }
                        },
                        {
                            "type": "section",
                            "fields": [
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Reported by:* <@{event.get('user', '')}>"
                                },
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Channel:* <#{event.get('channel', '')}>"
                                },
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Service:* {incident.alert.service}"
                                },
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Severity:* {incident.alert.severity.value}"
                                }
                            ]
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Issue Description:*\n```{text}```"
                            }
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "*🔄 Status:* Starting investigation..."
                            }
                        }
                    ]
                )

                # Start actual investigation process
                # Always run investigation, regardless of Slack posting success
                try:
                    investigation_thread_ts = investigation_message.get("ts") if investigation_message.get("ok") else None

                    if investigation_thread_ts:
                        # Post initial status update
                        await slack_client.post_message(
                            channel=devops_alfie_channel,
                            thread_ts=investigation_thread_ts,
                            text="🔄 **Starting Investigation...**\nAnalyzing Kubernetes events and metrics..."
                        )

                    # Call real collectors for investigation
                    findings = await investigate_incident(incident, text)

                    if investigation_thread_ts:
                        # Post real investigation results
                        await slack_client.post_message(
                            channel=devops_alfie_channel,
                            thread_ts=investigation_thread_ts,
                            text=f"📊 **Investigation Complete**\n\n{findings}\n\n{devops_support_tag} - Investigation findings above."
                        )
                    else:
                        # If Slack posting fails, at least log the findings
                        print(f"Investigation findings for {incident.incident_id}: {findings}")

                except Exception as e:
                    print(f"Investigation failed for {incident.incident_id}: {str(e)}")
                    investigation_thread_ts = investigation_message.get("ts") if investigation_message.get("ok") else None
                    if investigation_thread_ts:
                        await slack_client.post_message(
                            channel=devops_alfie_channel,
                            thread_ts=investigation_thread_ts,
                            text=f"📊 **Investigation Error**\n\nUnable to connect to collectors. Error: {str(e)}\n\n{devops_support_tag} - Manual investigation required."
                        )

                # TODO: Hand off to orchestrator for actual investigation
                # For now, just return success
                return {
                    "status": "accepted",
                    "incident_id": incident.incident_id,
                    "investigation_channel": devops_alfie_channel
                }

    return {"status": "ok"}


@router.post(
    "/slack/slash",
    status_code=status.HTTP_200_OK,
    summary="Handle Slack slash commands"
)
async def slack_slash_command(request: Request) -> dict[str, Any]:
    """Handle Slack slash commands like /investigate."""

    # Parse form data
    form = await request.form()
    command = form.get("command", "")
    text = form.get("text", "")
    channel_id = form.get("channel_id", "")
    user_id = form.get("user_id", "")

    if command == "/investigate":
        # Create incident from slash command
        incident = _seed_incident_from_slack(
            channel=channel_id,
            user=user_id,
            text=text,
            ts=str(datetime.now(UTC).timestamp()),
            thread_ts=None
        )

        devops_alfie_channel = os.getenv("DEVOPS_ALFIE_CHANNEL", "devops-alfie")

        # Return immediate response
        return {
            "response_type": "in_channel",
            "text": f"🔍 Starting investigation for incident `{incident.incident_id}`\nI'll post updates in <#{devops_alfie_channel}>",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Investigation Started*\nIncident ID: `{incident.incident_id}`\nI'll post updates in <#{devops_alfie_channel}>"
                    }
                }
            ]
        }

    return {"text": "Unknown command"}
