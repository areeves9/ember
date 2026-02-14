"""Supabase JWT authentication middleware."""

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from ember.config import settings

security = HTTPBearer(auto_error=False)

# Cache for JWKS
_jwks_cache: dict | None = None


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
    Verify Supabase JWT token.

    Returns the decoded token payload if valid.
    Raises HTTPException if invalid or missing.
    """
    # Skip auth in development if not configured
    if (
        settings.is_development
        and not settings.supabase_url
        and not settings.supabase_jwt_secret
    ):
        return {"sub": "dev-user", "email": "dev@localhost"}

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    try:
        # Try JWKS (ES256) first
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
                return payload

        # Fall back to HS256 with secret
        if settings.supabase_jwt_secret:
            payload = jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
            )
            return payload

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authentication method configured",
        )

    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# Dependency for protected routes
require_auth = Depends(verify_token)
