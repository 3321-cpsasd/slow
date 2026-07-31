import base64
import hashlib
import hmac
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from ..core.errors import AppError


SUPPORTED_ID_TOKEN_ALGORITHMS = ("RS256", "PS256", "ES256")


@dataclass(frozen=True)
class OidcIdentity:
    issuer: str
    subject: str
    display_name: str
    email: str
    email_verified: bool


class OidcClient:
    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.http = httpx.AsyncClient(timeout=15, transport=transport)
        self._discovery: dict | None = None

    def validate_configuration(self) -> None:
        if not self.issuer or not self.client_id or not self.redirect_uri:
            raise RuntimeError(
                "OIDC mode requires issuer, client id, and redirect URI"
            )
        if not self.issuer.startswith("https://") and not self.issuer.startswith(
            "http://127.0.0.1"
        ):
            raise RuntimeError("OIDC issuer must use HTTPS")

    async def close(self) -> None:
        await self.http.aclose()

    async def discovery(self) -> dict:
        if self._discovery is not None:
            return self._discovery
        response = await self.http.get(
            f"{self.issuer}/.well-known/openid-configuration"
        )
        response.raise_for_status()
        document = response.json()
        if document.get("issuer", "").rstrip("/") != self.issuer:
            raise AppError(
                "OIDC discovery issuer 不匹配",
                code="OIDC_ISSUER_MISMATCH",
                status=502,
            )
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            endpoint = document.get(field)
            if not endpoint:
                raise AppError(
                    f"OIDC discovery 缺少 {field}",
                    code="OIDC_DISCOVERY_INVALID",
                    status=502,
                )
            parsed = urlparse(endpoint)
            if parsed.scheme != "https" and not (
                parsed.scheme == "http"
                and parsed.hostname == "127.0.0.1"
            ):
                raise AppError(
                    f"OIDC discovery 的 {field} 必须使用 HTTPS",
                    code="OIDC_DISCOVERY_INVALID",
                    status=502,
                )
        self._discovery = document
        return document

    async def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_verifier: str,
    ) -> str:
        document = await self.discovery()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{document['authorization_endpoint']}?{query}"

    async def exchange(
        self,
        *,
        code: str,
        nonce: str,
        code_verifier: str,
    ) -> OidcIdentity:
        document = await self.discovery()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": code_verifier,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        token_response = await self.http.post(
            document["token_endpoint"],
            data=data,
            headers={"Accept": "application/json"},
        )
        if token_response.status_code >= 400:
            raise AppError(
                "OIDC 授权码交换失败",
                code="OIDC_TOKEN_EXCHANGE_FAILED",
                status=401,
            )
        id_token = token_response.json().get("id_token")
        if not id_token:
            raise AppError(
                "OIDC 响应缺少 ID Token",
                code="OIDC_ID_TOKEN_MISSING",
                status=401,
            )
        jwks_response = await self.http.get(document["jwks_uri"])
        jwks_response.raise_for_status()
        try:
            token = jwt.decode(
                id_token,
                KeySet.import_key_set(jwks_response.json()),
                algorithms=SUPPORTED_ID_TOKEN_ALGORITHMS,
            )
            claims = token.claims
            jwt.JWTClaimsRegistry(
                leeway=30,
                iss={"essential": True, "value": self.issuer},
                sub={"essential": True},
                aud={"essential": True, "value": self.client_id},
                exp={"essential": True},
                iat={"essential": True},
            ).validate(
                claims
            )
        except JoseError as error:
            raise AppError(
                "OIDC ID Token 校验失败",
                code="OIDC_ID_TOKEN_INVALID",
                status=401,
            ) from error
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise AppError(
                "OIDC nonce 校验失败",
                code="OIDC_NONCE_INVALID",
                status=401,
            )
        return OidcIdentity(
            issuer=str(claims["iss"]),
            subject=str(claims["sub"]),
            display_name=str(
                claims.get("name")
                or claims.get("preferred_username")
                or claims.get("email")
                or "学习者"
            ),
            email=str(claims.get("email") or ""),
            email_verified=bool(claims.get("email_verified", False)),
        )
