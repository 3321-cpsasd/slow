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
            address_resolver=lambda _host, _port: ["93.184.216.34"],
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
            address_resolver=lambda _host, _port: ["93.184.216.34"],
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


def test_claim_verifier_fetches_each_url_once_and_preserves_candidate_order():
    fetches = []
    quote = "调度器只会把工作负载绑定到满足全部约束的节点"

    async def reviewer(request):
        # Finish in a different order from the input to prove that concurrent
        # reviews do not make governance writes nondeterministic.
        delay = (4 - int(request["claim"]["statement"][-1])) * 0.005
        await asyncio.sleep(delay)
        return ClaimSupportReview(
            supported=True,
            excerpt_id=request["sourceExcerpts"][0]["id"],
            exact_quote=quote,
            rationale="网页原文直接支持该主张。",
        )

    def handler(request):
        fetches.append({
            "url": str(request.url),
            "host": request.headers["host"],
            "sni": request.extensions["sni_hostname"],
        })
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text=quote,
        )

    candidates = [
        {
            "sourceClaimVersionId": f"claim_{index}",
            "sourceVersionId": "source_1",
            "statement": f"调度约束结论 {index}",
            "claimKind": "core_conclusion",
            "sourceTitle": "调度文档",
            "sourceUrl": "https://example.com/reference",
        }
        for index in range(4)
    ]
    reports = asyncio.run(
        HttpSourceVerifier(
            claim_reviewer=reviewer,
            transport=httpx.MockTransport(handler),
            claim_review_concurrency=4,
            address_resolver=lambda _host, _port: ["93.184.216.34"],
        ).verify_claims(candidates)
    )

    assert fetches == [{
        "url": "https://93.184.216.34/reference",
        "host": "example.com",
        "sni": "example.com",
    }]
    assert [item["sourceClaimVersionId"] for item in reports] == [
        "claim_0",
        "claim_1",
        "claim_2",
        "claim_3",
    ]


def test_claim_verifier_bounds_fetch_and_review_concurrency_across_calls():
    fetch_active = 0
    max_fetch_active = 0
    review_active = 0
    max_review_active = 0
    quote = "调度器只会把工作负载绑定到满足全部约束的节点"

    async def handler(_request):
        nonlocal fetch_active, max_fetch_active
        fetch_active += 1
        max_fetch_active = max(max_fetch_active, fetch_active)
        await asyncio.sleep(0.01)
        fetch_active -= 1
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text=quote,
        )

    async def reviewer(request):
        nonlocal review_active, max_review_active
        review_active += 1
        max_review_active = max(max_review_active, review_active)
        await asyncio.sleep(0.01)
        review_active -= 1
        return ClaimSupportReview(
            supported=True,
            excerpt_id=request["sourceExcerpts"][0]["id"],
            exact_quote=quote,
            rationale="网页原文直接支持该主张。",
        )

    def candidates(prefix):
        return [
            {
                "sourceClaimVersionId": f"{prefix}_claim_{index}",
                "sourceVersionId": f"{prefix}_source_{index}",
                "statement": f"调度约束结论 {index}",
                "claimKind": "core_conclusion",
                "sourceTitle": "调度文档",
                "sourceUrl": f"https://example.com/{prefix}/{index}",
            }
            for index in range(3)
        ]

    async def run():
        verifier = HttpSourceVerifier(
            claim_reviewer=reviewer,
            transport=httpx.MockTransport(handler),
            claim_fetch_concurrency=2,
            claim_review_concurrency=2,
            address_resolver=lambda _host, _port: ["93.184.216.34"],
        )
        await asyncio.gather(
            verifier.verify_claims(candidates("first")),
            verifier.verify_claims(candidates("second")),
        )

    asyncio.run(run())

    assert max_fetch_active == 2
    assert max_review_active == 2


@pytest.mark.parametrize(
    ("url", "addresses"),
    [
        ("https://localhost/internal", ["127.0.0.1"]),
        ("https://metadata.internal/latest", ["169.254.169.254"]),
        ("https://mixed.example/reference", ["93.184.216.34", "10.0.0.8"]),
    ],
)
def test_claim_verifier_never_fetches_non_public_destinations(url, addresses):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text="private content")

    reports = asyncio.run(
        HttpSourceVerifier(
            claim_reviewer=lambda _request: None,
            transport=httpx.MockTransport(handler),
            address_resolver=lambda _host, _port: addresses,
        ).verify_claims([{
            "sourceClaimVersionId": "claim_1",
            "sourceVersionId": "source_1",
            "statement": "private claim",
            "claimKind": "core_conclusion",
            "sourceTitle": "private source",
            "sourceUrl": url,
        }])
    )

    assert reports == []
    assert requests == []


def test_claim_verifier_revalidates_every_redirect_target():
    requests = []

    def resolver(host, _port):
        return {
            "public.example": ["93.184.216.34"],
            "internal.example": ["10.0.0.8"],
        }[host]

    def handler(request):
        requests.append({
            "url": str(request.url),
            "host": request.headers["host"],
            "sni": request.extensions["sni_hostname"],
        })
        return httpx.Response(
            302,
            headers={"location": "https://internal.example/secret"},
        )

    reports = asyncio.run(
        HttpSourceVerifier(
            claim_reviewer=lambda _request: None,
            transport=httpx.MockTransport(handler),
            address_resolver=resolver,
        ).verify_claims([{
            "sourceClaimVersionId": "claim_1",
            "sourceVersionId": "source_1",
            "statement": "redirected claim",
            "claimKind": "core_conclusion",
            "sourceTitle": "redirecting source",
            "sourceUrl": "https://public.example/reference",
        }])
    )

    assert reports == []
    assert requests == [{
        "url": "https://93.184.216.34/reference",
        "host": "public.example",
        "sni": "public.example",
    }]


def test_reachability_check_rejects_private_dns_before_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    with pytest.raises(SourceVerificationError) as raised:
        asyncio.run(
            HttpSourceVerifier(
                transport=httpx.MockTransport(handler),
                address_resolver=lambda _host, _port: ["127.0.0.1"],
            ).verify([source()])
        )

    assert raised.value.code == "SOURCE_UNREACHABLE"
    assert requests == []


def test_reachability_tries_another_validated_public_address():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.host == "151.101.1.69":
            raise httpx.ConnectError("first address unavailable", request=request)
        return httpx.Response(200)

    report = asyncio.run(
        HttpSourceVerifier(
            transport=httpx.MockTransport(handler),
            address_resolver=lambda _host, _port: [
                "151.101.1.69",
                "93.184.216.34",
            ],
        ).verify([source()])
    )

    assert report[0]["reachable"] is True
    assert requests == [
        "https://151.101.1.69/reference",
        "https://93.184.216.34/reference",
    ]
