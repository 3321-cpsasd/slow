import asyncio
from dataclasses import dataclass

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

    async def verify(self, sources: list[Source]) -> list[dict]:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
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
