"""Evidence store error hierarchy."""

from __future__ import annotations


class EvidenceError(Exception):
    """Base class for all evidence store errors."""


class EvidenceNotFound(EvidenceError):  # noqa: N818
    """Raised when a blob or metadata row cannot be located.

    This covers both MinIO 404s and the advisory-expired case where the blob
    is past ``EvidenceRef.expires_at`` (read-before-dereference check).
    """

    def __init__(self, evidence_id: str) -> None:
        self.evidence_id = evidence_id
        super().__init__(f"Evidence not found: {evidence_id!r}")


class EvidenceStorageError(EvidenceError):
    """Raised when a MinIO write or read fails for a non-404 reason."""


class EvidenceMetadataError(EvidenceError):
    """Raised when the Postgres metadata insert or query fails."""
