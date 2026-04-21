"""Evidence store client — MinIO blobs + Postgres metadata.

Write path:
    ``put_blob(...)`` → MinIO (``s3://<bucket>/<evidence_id>``)
        → ``record_metadata(...)`` → Postgres row in ``evidence_refs``
        → returns an ``EvidenceRef`` that callers embed in a ``Finding``.

Ordering invariant (ADR-0006 / arch doc): blob write commits before the
metadata row is inserted. A crash between the two leaves an orphan blob in
MinIO (harmless; lifecycle rule GCs it). It never leaves a dangling DB
reference.
"""

from __future__ import annotations

import importlib.resources
import io
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import botocore.exceptions
import psycopg

from agent.evidence.errors import (
    EvidenceMetadataError,
    EvidenceNotFound,
    EvidenceStorageError,
)
from agent.schemas import EvidenceRef

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_MIGRATION_PATH = (
    importlib.resources.files("agent.evidence") / "migrations" / "0001_initial.sql"
)


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

    @classmethod
    def from_env(cls) -> EvidenceSettings:
        """Build settings from environment variables; raises ``KeyError`` if any required var is absent."""
        return cls(
            s3_endpoint=os.environ["EVIDENCE_S3_ENDPOINT"],
            s3_bucket=os.environ.get("EVIDENCE_S3_BUCKET", "incidents"),
            s3_access_key=os.environ["EVIDENCE_S3_ACCESS_KEY"],
            s3_secret_key=os.environ["EVIDENCE_S3_SECRET_KEY"],
            postgres_url=os.environ["POSTGRES_URL"],
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class EvidenceClient:
    """Facade over MinIO (blobs) and Postgres (metadata)."""

    def __init__(self, settings: EvidenceSettings) -> None:
        self._settings = settings
        self._schema_bootstrapped = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _s3(self) -> Any:
        """Return a boto3 S3 client wired to the configured MinIO endpoint."""
        return boto3.client(
            "s3",
            endpoint_url=self._settings.s3_endpoint,
            aws_access_key_id=self._settings.s3_access_key,
            aws_secret_access_key=self._settings.s3_secret_key,
            region_name=self._settings.s3_region,
        )

    async def _ensure_schema(self) -> None:
        """Run the initial migration once per process if the table is missing.

        Uses ``CREATE TABLE IF NOT EXISTS`` so it is safe to call on every
        startup.
        """
        if self._schema_bootstrapped:
            return
        sql = _MIGRATION_PATH.read_text(encoding="utf-8")
        async with await psycopg.AsyncConnection.connect(self._settings.postgres_url) as conn:
            await conn.execute(sql)
            await conn.commit()
        self._schema_bootstrapped = True
        logger.debug("evidence schema bootstrap complete")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def put_blob(
        self,
        *,
        evidence_id: str,
        payload: bytes,
        content_type: str,
        incident_id: str = "",
    ) -> EvidenceRef:
        """Upload *payload* to MinIO then insert the metadata row.

        Blob-first invariant: the MinIO ``PUT`` must complete before the
        Postgres ``INSERT``. If the metadata insert fails, a best-effort
        ``DELETE`` is attempted against MinIO so that no dangling DB reference
        is left; the original error is re-raised.

        Parameters
        ----------
        evidence_id:
            Opaque identifier; becomes the MinIO object key.
        payload:
            Raw bytes to store.
        content_type:
            MIME-ish label; recorded in metadata and used to select a parser
            on read.
        incident_id:
            Optional parent incident identifier; stored in ``evidence_refs``
            for lookup. Defaults to empty string when not associated with a
            specific incident.
        """
        await self._ensure_schema()

        ref = self.make_ref(
            evidence_id=evidence_id,
            content_type=content_type,
            size_bytes=len(payload),
        )

        # 1. Write blob (must succeed before metadata).
        s3 = self._s3()
        try:
            s3.put_object(
                Bucket=self._settings.s3_bucket,
                Key=evidence_id,
                Body=io.BytesIO(payload),
                ContentType=content_type,
                ContentLength=len(payload),
            )
        except botocore.exceptions.BotoCoreError as exc:
            raise EvidenceStorageError(f"MinIO PUT failed for {evidence_id!r}: {exc}") from exc

        logger.debug("blob written", extra={"evidence_id": evidence_id, "size": len(payload)})

        # 2. Insert metadata row.
        try:
            await self._insert_metadata(ref, incident_id=incident_id)
        except Exception as meta_exc:
            # Best-effort rollback: attempt to delete the orphan blob.
            logger.warning(
                "metadata insert failed; attempting orphan blob delete",
                extra={"evidence_id": evidence_id},
                exc_info=True,
            )
            try:
                s3.delete_object(Bucket=self._settings.s3_bucket, Key=evidence_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "best-effort blob delete also failed; orphan blob remains",
                    extra={"evidence_id": evidence_id},
                    exc_info=True,
                )
            raise EvidenceMetadataError(
                f"metadata insert failed for {evidence_id!r}: {meta_exc}"
            ) from meta_exc

        logger.info("evidence stored", extra={"evidence_id": evidence_id})
        return ref

    async def get_blob(self, evidence_id: str) -> bytes:
        """Retrieve a blob from MinIO.

        Raises
        ------
        EvidenceNotFound
            If the object does not exist in MinIO (404 / NoSuchKey).
        EvidenceStorageError
            For any other MinIO error.
        """
        s3 = self._s3()
        try:
            response = s3.get_object(Bucket=self._settings.s3_bucket, Key=evidence_id)
            return response["Body"].read()  # type: ignore[no-any-return]
        except s3.exceptions.NoSuchKey:
            raise EvidenceNotFound(evidence_id) from None
        except botocore.exceptions.ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404"):
                raise EvidenceNotFound(evidence_id) from exc
            raise EvidenceStorageError(
                f"MinIO GET failed for {evidence_id!r}: {exc}"
            ) from exc
        except botocore.exceptions.BotoCoreError as exc:
            raise EvidenceStorageError(
                f"MinIO GET failed for {evidence_id!r}: {exc}"
            ) from exc

    async def record_metadata(self, ref: EvidenceRef, *, incident_id: str = "") -> None:
        """Insert a metadata row for a blob written externally (e.g. by a Go collector).

        This is the direct-insert path: the caller is responsible for having
        already committed the blob to MinIO before calling this method.

        Parameters
        ----------
        ref:
            Fully-populated ``EvidenceRef`` returned by the collector.
        incident_id:
            Optional parent incident identifier.
        """
        await self._ensure_schema()
        await self._insert_metadata(ref, incident_id=incident_id)

    # ------------------------------------------------------------------
    # Internal persistence helpers
    # ------------------------------------------------------------------

    async def _insert_metadata(self, ref: EvidenceRef, *, incident_id: str) -> None:
        """Write a single row to ``evidence_refs``."""
        async with await psycopg.AsyncConnection.connect(self._settings.postgres_url) as conn:
            await conn.execute(
                """
                INSERT INTO evidence_refs
                    (evidence_id, incident_id, storage_uri, content_type,
                     size_bytes, expires_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (evidence_id) DO NOTHING
                """,
                (
                    ref.evidence_id,
                    incident_id,
                    ref.storage_uri,
                    ref.content_type,
                    ref.size_bytes,
                    ref.expires_at,
                ),
            )
            await conn.commit()

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

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
