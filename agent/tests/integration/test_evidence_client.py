"""Integration tests for the evidence store client.

Requires live MinIO + Postgres (``task agent-infra:minio`` and
``task agent-infra:postgres`` port-forwards running).

Run with::

    uv run pytest tests/integration -q -m evidence

Environment variables (same as production):
    EVIDENCE_S3_ENDPOINT   default: http://localhost:9000
    EVIDENCE_S3_ACCESS_KEY default: minio
    EVIDENCE_S3_SECRET_KEY default: minio-dev-secret
    EVIDENCE_S3_BUCKET     default: incidents
    POSTGRES_URL           default: postgresql://devops:devops@localhost:5432/devops
"""

from __future__ import annotations

import os
import uuid

import boto3
import botocore.exceptions
import psycopg
import pytest

from agent.evidence import (
    EvidenceClient,
    EvidenceMetadataError,
    EvidenceNotFound,
    EvidenceSettings,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _settings() -> EvidenceSettings:
    return EvidenceSettings(
        s3_endpoint=os.environ.get("EVIDENCE_S3_ENDPOINT", "http://localhost:9000"),
        s3_bucket=os.environ.get("EVIDENCE_S3_BUCKET", "incidents"),
        s3_access_key=os.environ.get("EVIDENCE_S3_ACCESS_KEY", "minio"),
        s3_secret_key=os.environ.get("EVIDENCE_S3_SECRET_KEY", "minio-dev-secret"),
        postgres_url=os.environ.get(
            "POSTGRES_URL", "postgresql://devops:devops@localhost:5432/devops"
        ),
    )


def _unique_id() -> str:
    return f"test-{uuid.uuid4().hex}"


def _s3_client(settings: EvidenceSettings) -> boto3.client:  # type: ignore[valid-type]
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )


async def _infra_available(settings: EvidenceSettings) -> bool:
    """Return True only when both Postgres and MinIO are reachable."""
    try:
        async with await psycopg.AsyncConnection.connect(
            settings.postgres_url, connect_timeout=2
        ):
            pass
    except Exception:  # noqa: BLE001
        return False
    try:
        _s3_client(settings).head_bucket(Bucket=settings.s3_bucket)
    except Exception:  # noqa: BLE001
        return False
    return True


@pytest.fixture(scope="module")
def settings() -> EvidenceSettings:
    return _settings()


@pytest.fixture(scope="module")
async def client(settings: EvidenceSettings) -> EvidenceClient:
    if not await _infra_available(settings):
        pytest.skip(
            "evidence infra not reachable — run `task agent-infra:postgres` "
            "and `task agent-infra:minio` first"
        )
    return EvidenceClient(settings)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.evidence
async def test_put_get_round_trip(client: EvidenceClient, settings: EvidenceSettings) -> None:
    """put_blob followed by get_blob returns the original payload."""
    evidence_id = _unique_id()
    payload = b'{"metric": "memory_rss", "value": 134217728}'

    ref = await client.put_blob(
        evidence_id=evidence_id,
        payload=payload,
        content_type="application/json",
        incident_id="inc_test_001",
    )

    assert ref.evidence_id == evidence_id
    assert ref.storage_uri == f"s3://{settings.s3_bucket}/{evidence_id}"
    assert ref.size_bytes == len(payload)

    retrieved = await client.get_blob(evidence_id)
    assert retrieved == payload


@pytest.mark.integration
@pytest.mark.evidence
async def test_metadata_row_visible_after_put(
    client: EvidenceClient, settings: EvidenceSettings
) -> None:
    """After put_blob, a row exists in evidence_refs with the correct fields."""
    evidence_id = _unique_id()
    payload = b"prometheus scrape output"

    ref = await client.put_blob(
        evidence_id=evidence_id,
        payload=payload,
        content_type="text/plain",
        incident_id="inc_test_002",
    )

    async with await psycopg.AsyncConnection.connect(settings.postgres_url) as conn:
        row = await (
            await conn.execute(
                "SELECT evidence_id, incident_id, storage_uri, content_type, size_bytes "
                "FROM evidence_refs WHERE evidence_id = %s",
                (evidence_id,),
            )
        ).fetchone()

    assert row is not None
    assert row[0] == ref.evidence_id
    assert row[1] == "inc_test_002"
    assert row[2] == ref.storage_uri
    assert row[3] == "text/plain"
    assert row[4] == len(payload)


@pytest.mark.integration
@pytest.mark.evidence
async def test_get_blob_missing_raises_not_found(client: EvidenceClient) -> None:
    """get_blob raises EvidenceNotFound for an unknown evidence_id."""
    with pytest.raises(EvidenceNotFound) as exc_info:
        await client.get_blob("nonexistent-evidence-id-xyzzy")
    assert "nonexistent-evidence-id-xyzzy" in str(exc_info.value)


@pytest.mark.integration
@pytest.mark.evidence
async def test_record_metadata_direct_insert(
    client: EvidenceClient, settings: EvidenceSettings
) -> None:
    """record_metadata inserts a row when called with a pre-built EvidenceRef."""
    evidence_id = _unique_id()
    # Write blob directly so there is an object to reference.
    s3 = _s3_client(settings)
    s3.put_object(
        Bucket=settings.s3_bucket,
        Key=evidence_id,
        Body=b"raw loki log lines",
    )

    ref = client.make_ref(
        evidence_id=evidence_id,
        content_type="application/x-ndjson",
        size_bytes=18,
    )
    await client.record_metadata(ref, incident_id="inc_test_003")

    async with await psycopg.AsyncConnection.connect(settings.postgres_url) as conn:
        row = await (
            await conn.execute(
                "SELECT evidence_id FROM evidence_refs WHERE evidence_id = %s",
                (evidence_id,),
            )
        ).fetchone()

    assert row is not None
    assert row[0] == evidence_id


@pytest.mark.integration
@pytest.mark.evidence
async def test_best_effort_blob_delete_on_metadata_failure(
    client: EvidenceClient, settings: EvidenceSettings
) -> None:
    """When metadata insert fails, put_blob attempts to remove the orphan blob.

    Strategy: monkey-patch ``_insert_metadata`` to raise after the blob is
    written, then verify that the best-effort delete ran so no orphan blob
    remains in MinIO.
    """
    evidence_id = _unique_id()
    payload = b"canary payload"

    blob_written = False
    original_insert = client._insert_metadata  # noqa: SLF001

    async def _failing_insert(ref: object, *, incident_id: str) -> None:
        nonlocal blob_written
        blob_written = True
        raise RuntimeError("simulated metadata failure")

    client._insert_metadata = _failing_insert  # type: ignore[method-assign]
    try:
        with pytest.raises(EvidenceMetadataError):
            await client.put_blob(
                evidence_id=evidence_id,
                payload=payload,
                content_type="application/octet-stream",
            )
    finally:
        client._insert_metadata = original_insert  # type: ignore[method-assign]

    assert blob_written, "blob write should have been attempted before metadata"

    # The blob should have been removed by the best-effort delete.
    s3 = _s3_client(settings)
    with pytest.raises(botocore.exceptions.ClientError) as exc_info:
        s3.head_object(Bucket=settings.s3_bucket, Key=evidence_id)
    assert exc_info.value.response["Error"]["Code"] in ("404", "NoSuchKey")
