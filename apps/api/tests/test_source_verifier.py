import asyncio

import httpx
import pytest

from app.ai.contracts import ClaimSupportReview, Source
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
    assert raised.value.results[0].url == source().url


def test_claim_verifier_requires_provider_entailment_and_literal_page_quote():
    async def reviewer(request):
        excerpt = request["sourceExcerpts"][0]
        quote = "调度器只会把 Pod 绑定到满足约束的节点"
        assert quote in excerpt["text"]
        return ClaimSupportReview(
            supported=True,
            excerpt_id=excerpt["id"],
            exact_quote=quote,
            rationale="该句直接说明调度约束。",
        )

    def handler(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><body><h1>调度</h1><p>"
                "调度器只会把 Pod 绑定到满足约束的节点"
                "，否则 Pod 保持等待。</p></body></html>"
            ),
        )

    reports = asyncio.run(
        HttpSourceVerifier(
            claim_reviewer=reviewer,
            transport=httpx.MockTransport(handler),
        ).verify_claims([{
            "sourceClaimVersionId": "claim_1",
            "sourceVersionId": "source_1",
            "statement": "Pod 只能调度到满足约束的节点",
            "claimKind": "core_conclusion",
            "sourceTitle": "调度文档",
            "sourceUrl": "https://example.com/reference",
        }])
    )

    assert len(reports) == 1
    assert reports[0]["excerptText"] == "调度器只会把 Pod 绑定到满足约束的节点"
    assert reports[0]["report"]["exactQuoteVerified"] is True
    assert reports[0]["verificationMode"] == "provider_entailment_exact_quote"


def test_claim_verifier_rejects_a_quote_not_present_in_the_source():
    async def reviewer(request):
        return ClaimSupportReview(
            supported=True,
            excerpt_id=request["sourceExcerpts"][0]["id"],
            exact_quote="这是一段模型编造且网页中不存在的支持文字",
            rationale="错误地声称有支持。",
        )

    def handler(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="真实网页只说明 Pod 会经过调度流程。",
        )

    reports = asyncio.run(
        HttpSourceVerifier(
            claim_reviewer=reviewer,
            transport=httpx.MockTransport(handler),
        ).verify_claims([{
            "sourceClaimVersionId": "claim_1",
            "sourceVersionId": "source_1",
            "statement": "Pod 只能调度到满足约束的节点",
            "claimKind": "core_conclusion",
            "sourceTitle": "调度文档",
            "sourceUrl": "https://example.com/reference",
        }])
    )

    assert reports == []
