"""Unified authentication middleware supporting Supabase JWT and Auth0 M2M tokens."""

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from ember.config import settings

security = HTTPBearer(auto_error=False)

# Cache for JWKS
_jwks_cache: dict | None = None

# Cache for Auth0 JWKS
_auth0_jwks_cache: dict | None = None


async def fetch_jwks() -> dict:
    """Fetch JWKS from Supabase."""
    global _jwks_cache
    if _jwks_cache is not None:
        return _jwks_cache

    jwks_url = settings.supabase_jwks_url
    if not jwks_url:
        return {}

    async with httpx.AsyncClient() as client:
        response = await client.get(jwks_url, timeout=10.0)
        response.raise_for_status()
        _jwks_cache = response.json()
        return _jwks_cache


async def fetch_auth0_jwks() -> dict:
    """Fetch JWKS from Auth0."""
    global _auth0_jwks_cache
    if _auth0_jwks_cache is not None:
        return _auth0_jwks_cache

    jwks_url = settings.auth0_jwks_url
    if not jwks_url:
        return {}

    async with httpx.AsyncClient() as client:
        response = await client.get(jwks_url, timeout=10.0)
        response.raise_for_status()
        _auth0_jwks_cache = response.json()
        return _auth0_jwks_cache


def get_signing_key(token: str, jwks: dict) -> str | None:
    """Extract the signing key from JWKS that matches the token's kid."""
    try:
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid or "keys" not in jwks:
            return None

        for key in jwks["keys"]:
            if key.get("kid") == kid:
                return key
        return None
    except JWTError:
        return None


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """
    Unified auth - accepts Supabase JWT OR Auth0 M2M tokens.

    Returns the decoded token payload with auth_type indicator.
    """
    # Dev bypass - only if BOTH auth methods unconfigured
    if (
        settings.is_development
        and not settings.supabase_url
        and not settings.supabase_jwt_secret
        and not settings.auth0_domain
    ):
        return {"sub": "dev-user", "email": "dev@localhost", "auth_type": "dev"}

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Check if any auth method is configured
    auth_configured = bool(
        (settings.auth0_domain and settings.auth0_audience)
        or settings.supabase_jwks_url
        or settings.supabase_jwt_secret
    )

    # Try Auth0 M2M first (if configured)
    if settings.auth0_domain and settings.auth0_audience:
        try:
            jwks = await fetch_auth0_jwks()
            signing_key = get_signing_key(token, jwks)
            if signing_key:
                payload = jwt.decode(
                    token,
                    signing_key,
                    algorithms=["RS256"],
                    audience=settings.auth0_audience,
                    issuer=f"https://{settings.auth0_domain}/",
                )
                return {**payload, "auth_type": "m2m"}
        except Exception:
            pass  # Fall through to try Supabase

    # Try Supabase JWT
    try:
        # Existing Supabase validation logic (JWKS ES256 then HS256 fallback)
        if settings.supabase_jwks_url:
            jwks = await fetch_jwks()
            signing_key = get_signing_key(token, jwks)
            if signing_key:
                payload = jwt.decode(
                    token,
                    signing_key,
                    algorithms=["ES256"],
                    audience="authenticated",
                )
                return {**payload, "auth_type": "user"}

        if settings.supabase_jwt_secret:
            payload = jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
            )
            return {**payload, "auth_type": "user"}

        # If we got here, no auth method is configured
        if not auth_configured:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No authentication method configured",
            )

        # Auth is configured but token failed validation
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# Dependency for protected routes
require_auth = Depends(verify_token)
