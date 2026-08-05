import asyncio
from dataclasses import dataclass
import hashlib
from html import unescape
import re

import httpx

from ..ai.contracts import Source
from ..core.errors import AppError


@dataclass(frozen=True)
class Verification:
    url: str
    reachable: bool
    status_code: int
    pinned: bool

    @property
    def failure_reason(self) -> str:
        if self.status_code in {401, 403, 405, 429}:
            return "access_restricted"
        if self.status_code in {404, 410}:
            return "not_found"
        if self.status_code == 0:
            return "network_error"
        return "http_error"

    def as_dict(self):
        return {
            "url": self.url,
            "reachable": self.reachable,
            "statusCode": self.status_code,
            "pinned": self.pinned,
            "verificationStatus": (
                "verified"
                if self.reachable
                else "server_unverifiable"
                if self.failure_reason == "access_restricted"
                else "failed"
            ),
        }

    def failure_dict(self):
        return {
            **self.as_dict(),
            "reason": self.failure_reason,
        }


class SourceVerificationError(AppError):
    """A source gate failure with machine-readable, non-secret details."""

    def __init__(
        self,
        failures: list[Verification],
        *,
        results: list[Verification] | None = None,
    ):
        self.failures = tuple(failures)
        self.results = tuple(results or failures)
        access_restricted = all(
            item.failure_reason == "access_restricted"
            for item in failures
        )
        if access_restricted:
            details = ", ".join(
                f"{item.url}（站点拒绝自动检查，HTTP {item.status_code}）"
                for item in failures
            )
            message = (
                f"有 {len(failures)} 个来源当前无法由服务端核验：{details}。"
                "这不代表来源内容错误，系统会尝试替换为可核验来源。"
            )
            code = "SOURCE_UNVERIFIABLE"
        else:
            details = ", ".join(
                f"{item.url} ({item.status_code or 'network error'})"
                for item in failures
            )
            message = f"有 {len(failures)} 个来源无法由服务端访问：{details}"
            code = "SOURCE_UNREACHABLE"
        super().__init__(
            message,
            code=code,
            status=502,
            retryable=True,
        )


class HttpSourceVerifier:
    """Server-side reachability verifier; redirects are followed but non-HTTPS targets are rejected."""

    def __init__(
        self,
        *,
        claim_reviewer=None,
        claim_reviewer_model: str = "",
        transport=None,
    ):
        self.claim_reviewer = claim_reviewer
        self.claim_reviewer_model = claim_reviewer_model
        self.transport = transport

    async def verify(self, sources: list[Source]) -> list[dict]:
        async with httpx.AsyncClient(
            timeout=8,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            results = await asyncio.gather(
                *(self._one(client, source) for source in sources)
            )
        # A site refusing automated HEAD/Range requests is different from a
        # missing or unreachable source. Preserve that distinction in lineage
        # and allow publication with an explicit "server_unverifiable" label.
        failures = [
            item
            for item in results
            if not item.reachable
            and item.failure_reason != "access_restricted"
        ]
        if failures:
            raise SourceVerificationError(failures, results=results)
        return [item.as_dict() for item in results]

    async def _one(self, client: httpx.AsyncClient, source: Source) -> Verification:
        status = 0
        try:
            response = await client.head(source.url)
            status = response.status_code
            if not 200 <= status < 400:
                response = await client.get(source.url, headers={"Range": "bytes=0-0"})
                status = response.status_code
            reachable = 200 <= status < 400
            final_url = response.url
            reachable = reachable and final_url.scheme == "https"
        except httpx.HTTPError:
            reachable = False
        pinned = source.kind != "source_code" or source.version in source.url
        return Verification(source.url, reachable, status, pinned)

    async def verify_claims(self, candidates: list[dict]) -> list[dict]:
        """Verify semantic support with a provider review plus an exact page quote.

        The model may only select evidence.  The server independently checks
        that its quote is a literal substring of the fetched HTTPS document;
        unsupported, inaccessible, or malformed reviews remain unverified.
        """

        if not self.claim_reviewer or not candidates:
            return []
        reports = []
        document_cache: dict[str, str | None] = {}
        async with httpx.AsyncClient(
            timeout=12,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            for candidate in candidates:
                url = str(candidate.get("sourceUrl", ""))
                if url not in document_cache:
                    document_cache[url] = await self._fetch_document_text(
                        client,
                        url,
                    )
                document = document_cache[url]
                if not document:
                    continue
                excerpts = self._relevant_excerpts(
                    document,
                    str(candidate.get("statement", "")),
                )
                review = await self.claim_reviewer({
                    "claim": {
                        "statement": candidate.get("statement", ""),
                        "kind": candidate.get("claimKind", ""),
                    },
                    "source": {
                        "title": candidate.get("sourceTitle", ""),
                        "url": url,
                    },
                    "sourceExcerpts": excerpts,
                })
                if not review.supported:
                    continue
                excerpt = next(
                    (
                        item
                        for item in excerpts
                        if item["id"] == review.excerpt_id
                    ),
                    None,
                )
                quote = review.exact_quote.strip()
                if not excerpt or len(quote) < 12 or quote not in excerpt["text"]:
                    continue
                offset = document.find(quote)
                if offset < 0:
                    continue
                reports.append({
                    "sourceClaimVersionId": candidate["sourceClaimVersionId"],
                    "sourceVersionId": candidate["sourceVersionId"],
                    "locatorType": "normalized_document_offset",
                    "locator": {
                        "sourceUrl": url,
                        "excerptId": review.excerpt_id,
                        "start": offset,
                        "end": offset + len(quote),
                    },
                    "excerptText": quote,
                    "supportType": "supports",
                    "verificationMode": "provider_entailment_exact_quote",
                    "verificationRuleVersion": "claim_support_v1",
                    "report": {
                        "reviewer": "configured_provider",
                        "reviewerModel": self.claim_reviewer_model,
                        "rationale": review.rationale,
                        "documentSha256": hashlib.sha256(
                            document.encode()
                        ).hexdigest(),
                        "exactQuoteVerified": True,
                    },
                })
        return reports

    @staticmethod
    async def _fetch_document_text(
        client: httpx.AsyncClient,
        url: str,
    ) -> str | None:
        if not url.startswith("https://"):
            return None
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                if response.url.scheme != "https":
                    return None
                content_type = response.headers.get("content-type", "").lower()
                if not any(
                    kind in content_type
                    for kind in ("text/", "json", "xml", "html")
                ):
                    return None
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 1_500_000:
                        break
                raw = bytes(payload).decode(response.encoding or "utf-8", "replace")
        except (httpx.HTTPError, UnicodeError):
            return None
        if "html" in content_type:
            raw = re.sub(
                r"(?is)<(script|style|noscript)[^>]*>.*?</\1>",
                " ",
                raw,
            )
            raw = re.sub(r"(?s)<[^>]+>", " ", raw)
        return " ".join(unescape(raw).split())

    @staticmethod
    def _relevant_excerpts(document: str, statement: str) -> list[dict]:
        chunk_size = 5_500
        stride = 4_500
        chunks = [
            document[start:start + chunk_size]
            for start in range(0, max(len(document), 1), stride)
        ] or [document]
        lowered = statement.casefold()
        latin_terms = set(re.findall(r"[a-z0-9_]{3,}", lowered))
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
        terms = latin_terms | {
            chinese[index:index + 2]
            for index in range(max(0, len(chinese) - 1))
        }

        def score(value: str) -> tuple[int, int]:
            normalized = value.casefold()
            return (
                sum(normalized.count(term) for term in terms if term),
                -document.find(value),
            )

        ranked = sorted(chunks, key=score, reverse=True)[:4]
        return [
            {"id": f"excerpt_{index}", "text": value}
            for index, value in enumerate(ranked, 1)
        ]


class AcceptingSourceVerifier:
    """Explicit deterministic verifier for local demos and contract tests."""

    async def verify(self, sources: list[Source]) -> list[dict]:
        return [
            Verification(
                source.url,
                True,
                200,
                source.kind != "source_code" or source.version in source.url,
            ).as_dict()
            for source in sources
        ]

    async def verify_claims(self, candidates: list[dict]) -> list[dict]:
        """Explicit fixture-only claim verification, separate from reachability.

        Production ``HttpSourceVerifier`` deliberately does not implement this
        method: URL reachability must never become semantic claim support.
        """

        return [
            {
                "sourceClaimVersionId": item["sourceClaimVersionId"],
                "sourceVersionId": item["sourceVersionId"],
                "locatorType": "deterministic_fixture",
                "locator": {
                    "contentBlockVersionId": item["contentBlockVersionId"],
                    "sourceUrl": item["sourceUrl"],
                },
                "excerptText": item["statement"],
                "supportType": "supports",
                "verificationMode": "deterministic_claim_fixture",
                "verificationRuleVersion": "claim_fixture_v1",
                "report": {
                    "fixture": True,
                    "semanticSupport": "explicitly_accepted_for_demo_or_contract_test",
                },
            }
            for item in candidates
        ]
