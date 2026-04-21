"""Evidence store client skeleton.

MVP1 surface only — actual I/O is stubbed to ``NotImplementedError``. The shape
here is what the orchestrator, collectors, and tests will depend on.

Write path:
    ``put_blob(...)`` → MinIO (``s3://<bucket>/<evidence_id>``)
        → ``record_metadata(...)`` → Postgres row in ``evidence_refs``
        → returns an ``EvidenceRef`` that callers embed in a ``Finding``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from agent.schemas import EvidenceRef


@dataclass(frozen=True, slots=True)
class EvidenceSettings:
    """Runtime settings for the evidence store client."""

    s3_endpoint: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    s3_region: str = "us-east-1"
    postgres_url: str = ""
    ttl_days: int = 30


class EvidenceClient:
    """Facade over MinIO (blobs) and Postgres (metadata)."""

    def __init__(self, settings: EvidenceSettings) -> None:
        self._settings = settings

    async def put_blob(
        self,
        *,
        evidence_id: str,
        payload: bytes,
        content_type: str,
    ) -> EvidenceRef:
        """Upload a blob and return the metadata envelope.

        Implementations **must** write the blob before inserting the metadata
        row; otherwise a crash between the two leaves a dangling DB reference.
        """
        raise NotImplementedError(
            "EvidenceClient.put_blob is a stub (WI-003). "
            f"Would store {len(payload)}B as {evidence_id} ({content_type})."
        )

    async def get_blob(self, evidence_id: str) -> bytes:
        raise NotImplementedError("EvidenceClient.get_blob is a stub (WI-003).")

    async def record_metadata(self, ref: EvidenceRef) -> None:
        raise NotImplementedError(
            "EvidenceClient.record_metadata is a stub (WI-003). "
            f"Would record {ref.evidence_id}."
        )

    def make_ref(
        self,
        *,
        evidence_id: str,
        content_type: str,
        size_bytes: int,
    ) -> EvidenceRef:
        """Build an ``EvidenceRef`` from settings — no I/O.

        Useful for tests and for collectors that want the ref shape before
        actually persisting the blob.
        """
        return EvidenceRef(
            evidence_id=evidence_id,
            storage_uri=f"s3://{self._settings.s3_bucket}/{evidence_id}",
            content_type=content_type,
            size_bytes=size_bytes,
            expires_at=datetime.now(UTC) + timedelta(days=self._settings.ttl_days),
        )
