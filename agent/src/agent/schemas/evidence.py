"""Evidence store references and environment fingerprints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EvidenceRef(BaseModel):
    """A pointer to a raw observability payload held in the evidence store.

    Agents pass ``EvidenceRef`` around, not raw blobs. See ADR-0006.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(..., description="Opaque ID; globally unique per incident-lifetime.")
    storage_uri: str = Field(..., description="Where to dereference (e.g. s3://incidents/<id>.jsonl).")
    content_type: str = Field(..., description="MIME-ish content type; determines parser selection.")
    size_bytes: int = Field(..., ge=0)
    expires_at: datetime = Field(..., description="TTL boundary; dereference fails after this.")
