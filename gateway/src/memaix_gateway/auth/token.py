# SPDX-License-Identifier: AGPL-3.0-or-later
"""JWT token verification via Hydra / JWKS.

Implements the MCP SDK TokenVerifier protocol so that FastMCP's auth middleware
can call verify_token(raw_token) → AccessToken | None.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import jwt
from mcp.server.auth.provider import AccessToken

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Authentication / token-verification error."""


class HydraTokenVerifier:
    """Verify Bearer tokens against a JWKS endpoint (e.g. Ory Hydra).

    Implements the MCP SDK ``TokenVerifier`` protocol.
    """

    def __init__(
        self,
        jwks_uri: str,
        issuer: str,
        audiences: list[str] | None = None,
    ) -> None:
        self._jwks_uri = jwks_uri
        self._issuer = issuer
        # Expected token audiences (the resource server URL, both trailing-slash
        # variants). When set, the ``aud`` claim is verified and tokens minted
        # for a different resource are rejected (prevents confused-deputy).
        self._audiences = audiences or None
        self._jwks_client = jwt.PyJWKClient(jwks_uri, cache_keys=True, lifespan=3600)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an AccessToken if the JWT is valid, None on any error."""
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            decode_kwargs: dict[str, Any] = {
                "algorithms": ["RS256", "ES256"],
                "issuer": self._issuer,
            }
            if self._audiences:
                # verify_aud is on by default once an audience is supplied; a
                # token with no/mismatching aud will raise and be rejected.
                decode_kwargs["audience"] = self._audiences
            else:
                logger.warning(
                    "HydraTokenVerifier: no audiences configured — aud claim will NOT be "
                    "verified. Set auth.resource_server_url in gateway config to enable "
                    "audience verification and prevent confused-deputy attacks."
                )
                decode_kwargs["options"] = {"verify_aud": False}
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                **decode_kwargs,
            )
        except Exception as exc:
            logger.warning("token verification failed: %s", exc)
            return None

        # Scopes — Hydra uses "scp" (list or space-separated string), fallback "scope".
        raw_scope = claims.get("scp", claims.get("scope", ""))
        if isinstance(raw_scope, str):
            scopes: list[str] = raw_scope.split() if raw_scope else []
        else:
            scopes = list(raw_scope)

        # Client identity — "azp" (authorised party) is the canonical Hydra field.
        client_id: str = (
            claims.get("azp")
            or claims.get("client_id")
            or claims.get("sub", "unknown")
        )

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=claims.get("exp"),
            subject=claims.get("sub"),
            claims=claims,
        )

    @classmethod
    def from_config(cls, cfg: dict) -> "HydraTokenVerifier":
        """Construct from merged gateway config (brand/memaix/acl dict).

        Reads ``cfg["memaix"]["auth"]["issuer"]`` and derives the JWKS URI as
        ``{issuer}/.well-known/jwks.json``.
        """
        auth_cfg = cfg["memaix"]["auth"]
        issuer: str = auth_cfg["issuer"]
        # Use internal Hydra URL for JWKS to avoid external DNS/IPv6 roundtrip
        # from inside Docker. The issuer claim in JWTs still uses the public URL.
        hydra_internal = os.environ.get("HYDRA_INTERNAL_URL", "http://hydra:4444")
        jwks_uri = f"{hydra_internal}/.well-known/jwks.json"

        # Verify the aud claim against the resource server URL. Accept both
        # trailing-slash variants because claude.ai and Hydra disagree on the
        # canonical form (see server.py protected_resource_handler / dcr_handler).
        resource = auth_cfg.get("resource_server_url", issuer)
        base = resource.rstrip("/")
        audiences = [base, base + "/"]
        return cls(jwks_uri=jwks_uri, issuer=issuer, audiences=audiences)
