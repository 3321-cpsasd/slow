import asyncio

import pytest

from app.ai.contracts import Source
from app.services.source_verifier import (
    HttpSourceVerifier,
    SourceVerificationError,
    Verification,
)


def source():
    return Source(
        title="Reference",
        url="https://example.com/reference",
        kind="official",
        version="2026-08-02",
    )


def test_access_restricted_source_is_recorded_without_blocking(monkeypatch):
    async def restricted(_self, _client, item):
        return Verification(item.url, False, 403, True)

    monkeypatch.setattr(HttpSourceVerifier, "_one", restricted)
    report = asyncio.run(HttpSourceVerifier().verify([source()]))

    assert report == [{
        "url": "https://example.com/reference",
        "reachable": False,
        "statusCode": 403,
        "pinned": True,
        "verificationStatus": "server_unverifiable",
    }]


def test_missing_source_remains_a_blocking_failure(monkeypatch):
    async def missing(_self, _client, item):
        return Verification(item.url, False, 404, True)

    monkeypatch.setattr(HttpSourceVerifier, "_one", missing)
    with pytest.raises(SourceVerificationError) as raised:
        asyncio.run(HttpSourceVerifier().verify([source()]))

    assert raised.value.code == "SOURCE_UNREACHABLE"
    assert raised.value.failures[0].failure_reason == "not_found"
