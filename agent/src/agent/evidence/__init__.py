"""Evidence store client — MinIO blobs + Postgres metadata.

Invariants (see arch doc, 'Evidence store'):

- Blob writes **must** complete before metadata commits.
- ``EvidenceRef.storage_uri`` is always an ``s3://`` URI, never a presigned URL.
- TTL is enforced by the bucket lifecycle rule; the ``expires_at`` column is a
  read-time advisory only.
"""

from agent.evidence.client import EvidenceClient, EvidenceSettings

__all__ = ["EvidenceClient", "EvidenceSettings"]
