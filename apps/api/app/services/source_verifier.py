import asyncio
from dataclasses import dataclass
import hashlib
from html import unescape
import inspect
import ipaddress
import re
import socket
from urllib.parse import ParseResult, urljoin, urlparse

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
    """Server-side verifier restricted to public HTTPS destinations."""

    def __init__(
        self,
        *,
        claim_reviewer=None,
        claim_reviewer_model: str = "",
        transport=None,
        claim_fetch_concurrency: int = 6,
        claim_review_concurrency: int = 2,
        address_resolver=None,
    ):
        if claim_fetch_concurrency < 1:
            raise ValueError("claim_fetch_concurrency must be positive")
        if claim_review_concurrency < 1:
            raise ValueError("claim_review_concurrency must be positive")
        self.claim_reviewer = claim_reviewer
        self.claim_reviewer_model = claim_reviewer_model
        self.transport = transport
        self.claim_fetch_concurrency = claim_fetch_concurrency
        self.claim_review_concurrency = claim_review_concurrency
        self.address_resolver = (
            address_resolver or self._resolve_addresses_async
        )
        # These are process-local limits shared by every lesson using this
        # verifier instance.  They bound aggregate load when learning tasks run
        # concurrently, rather than resetting the model limit for each lesson.
        self._claim_fetch_limiter = asyncio.Semaphore(
            claim_fetch_concurrency
        )
        self._claim_review_limiter = asyncio.Semaphore(
            claim_review_concurrency
        )

    async def verify(self, sources: list[Source]) -> list[dict]:
        async with httpx.AsyncClient(
            timeout=8,
            follow_redirects=False,
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
            response = await self._request_public(client, "HEAD", source.url)
            status = response.status_code
            if not 200 <= status < 400:
                response = await self._request_public(
                    client,
                    "GET",
                    source.url,
                    headers={"Range": "bytes=0-0"},
                )
                status = response.status_code
            reachable = 200 <= status < 400
        except (httpx.HTTPError, httpx.InvalidURL):
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
        urls = list(dict.fromkeys(
            str(candidate.get("sourceUrl", ""))
            for candidate in candidates
        ))
        async with httpx.AsyncClient(
            timeout=12,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            documents = await asyncio.gather(*(
                self._fetch_claim_document(client, url)
                for url in urls
            ))
            document_cache = dict(zip(urls, documents, strict=True))
            reports = await asyncio.gather(*(
                self._review_claim_candidate(
                    candidate,
                    document_cache.get(
                        str(candidate.get("sourceUrl", ""))
                    ),
                )
                for candidate in candidates
            ))
        # asyncio.gather preserves candidate order.  Governance writes can
        # therefore remain deterministic even though external I/O is parallel.
        return [report for report in reports if report is not None]

    async def _fetch_claim_document(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> str | None:
        async with self._claim_fetch_limiter:
            return await self._fetch_document_text(client, url)

    async def _review_claim_candidate(
        self,
        candidate: dict,
        document: str | None,
    ) -> dict | None:
        if not document:
            return None
        url = str(candidate.get("sourceUrl", ""))
        excerpts = self._relevant_excerpts(
            document,
            str(candidate.get("statement", "")),
        )
        async with self._claim_review_limiter:
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
            return None
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
            return None
        offset = document.find(quote)
        if offset < 0:
            return None
        return {
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
        }

    @staticmethod
    def _resolve_addresses(host: str, port: int) -> list[str]:
        return sorted({
            item[4][0]
            for item in socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        })

    @classmethod
    async def _resolve_addresses_async(cls, host: str, port: int) -> list[str]:
        return await asyncio.to_thread(cls._resolve_addresses, host, port)

    async def _public_https_destination(
        self,
        url: str,
    ) -> tuple[ParseResult, list[str]] | None:
        parsed = urlparse(url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        try:
            addresses = self.address_resolver(parsed.hostname, parsed.port or 443)
            if inspect.isawaitable(addresses):
                addresses = await addresses
            if not addresses or not all(
                ipaddress.ip_address(address).is_global
                for address in addresses
            ):
                return None
            return parsed, sorted(set(addresses))
        except (OSError, TypeError, ValueError):
            return None

    async def _is_public_https(self, url: str) -> bool:
        return await self._public_https_destination(url) is not None

    async def _request_public(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        stream: bool = False,
        max_redirects: int = 5,
    ) -> httpx.Response:
        current = url
        for redirect_count in range(max_redirects + 1):
            destination = await self._public_https_destination(current)
            if not destination:
                raise httpx.InvalidURL(
                    "source URL must resolve only to public HTTPS addresses"
                )
            parsed, addresses = destination
            original_host = parsed.hostname
            host_header = (
                f"[{original_host}]"
                if ":" in original_host
                else original_host
            )
            if parsed.port and parsed.port != 443:
                host_header = f"{host_header}:{parsed.port}"
            connection_error = None
            for raw_address in addresses:
                address = ipaddress.ip_address(raw_address).compressed
                pinned_host = f"[{address}]" if ":" in address else address
                pinned_netloc = (
                    f"{pinned_host}:{parsed.port}"
                    if parsed.port and parsed.port != 443
                    else pinned_host
                )
                pinned_url = parsed._replace(netloc=pinned_netloc).geturl()
                request = client.build_request(
                    method,
                    pinned_url,
                    headers={**(headers or {}), "Host": host_header},
                    extensions={"sni_hostname": original_host},
                )
                try:
                    response = await client.send(request, stream=stream)
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                    connection_error = error
            else:
                if connection_error:
                    raise connection_error
                raise httpx.ConnectError("source has no usable public address")
            location = response.headers.get("location")
            if response.status_code not in {301, 302, 303, 307, 308} or not location:
                return response
            await response.aclose()
            if redirect_count == max_redirects:
                raise httpx.TooManyRedirects(
                    "source exceeded redirect limit",
                    request=request,
                )
            current = urljoin(current, location)
        raise httpx.TooManyRedirects("source exceeded redirect limit")

    async def _fetch_document_text(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> str | None:
        response = None
        try:
            response = await self._request_public(
                client,
                "GET",
                url,
                stream=True,
            )
            response.raise_for_status()
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
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
            return None
        finally:
            if response is not None:
                await response.aclose()
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

        URL reachability alone must never become semantic claim support; this
        fixture is only for explicit demo and contract-test environments.
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
