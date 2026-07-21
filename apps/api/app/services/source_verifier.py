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

    def as_dict(self):
        return {
            "url": self.url,
            "reachable": self.reachable,
            "statusCode": self.status_code,
            "pinned": self.pinned,
        }


class HttpSourceVerifier:
    """Server-side reachability verifier; redirects are followed but non-HTTPS targets are rejected."""

    async def verify(self, sources: list[Source]) -> list[dict]:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            results = await asyncio.gather(*(self._one(client, source) for source in sources))
        failures = [item for item in results if not item.reachable]
        if failures:
            details = ", ".join(f"{item.url} ({item.status_code or 'network error'})" for item in failures)
            raise AppError(
                f"有 {len(failures)} 个来源无法由服务端访问：{details}",
                code="SOURCE_UNREACHABLE",
                status=502,
            )
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
