"""Test unified authentication functionality."""

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError

from ember.auth import verify_token
from ember.config import settings


class TestUnifiedAuth:
    """Test unified authentication with Supabase and Auth0 support."""

    async def test_dev_bypass_when_no_auth_configured(self):
        """Test that dev bypass works when neither auth method is configured."""
        # Mock settings to simulate no auth configured
        original_supabase_url = settings.supabase_url
        original_supabase_jwt_secret = settings.supabase_jwt_secret
        original_auth0_domain = settings.auth0_domain
        original_environment = settings.environment

        try:
            # Configure settings for test
            settings.supabase_url = ""
            settings.supabase_jwt_secret = ""
            settings.auth0_domain = ""
            settings.environment = "development"

            result = await verify_token(credentials=None)

            # Should return dev user
            assert result["sub"] == "dev-user"
            assert result["email"] == "dev@localhost"
            assert result["auth_type"] == "dev"

        finally:
            # Restore original settings
            settings.supabase_url = original_supabase_url
            settings.supabase_jwt_secret = original_supabase_jwt_secret
            settings.auth0_domain = original_auth0_domain
            settings.environment = original_environment

    async def test_missing_authorization_header(self):
        """Test that missing authorization header raises 401."""
        original_supabase_url = settings.supabase_url
        original_auth0_domain = settings.auth0_domain
        original_environment = settings.environment

        try:
            # Configure settings to require auth
            settings.supabase_url = "https://test.supabase.co"
            settings.auth0_domain = ""
            settings.environment = "production"

            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=None)

            assert exc_info.value.status_code == 401
            assert "Missing authorization header" in str(exc_info.value.detail)

        finally:
            settings.supabase_url = original_supabase_url
            settings.auth0_domain = original_auth0_domain
            settings.environment = original_environment

    async def test_auth0_m2m_token_validation(self, mocker):
        """Test Auth0 M2M token validation."""
        # Mock Auth0 settings
        original_auth0_domain = settings.auth0_domain
        original_auth0_audience = settings.auth0_audience
        original_supabase_url = settings.supabase_url
        original_environment = settings.environment

        try:
            settings.auth0_domain = "test.auth0.com"
            settings.auth0_audience = "https://test.api"
            settings.supabase_url = ""  # Disable Supabase to test Auth0 only
            settings.environment = "production"

            # Mock JWKS and token
            mock_jwks = {
                "keys": [
                    {
                        "kid": "test-kid",
                        "kty": "RSA",
                        "alg": "RS256",
                        "use": "sig",
                        "n": "mock_n",
                        "e": "AQAB",
                    }
                ]
            }

            # Mock token with RS256 algorithm
            mock_token = "mock.auth0.token"
            mock_payload = {
                "sub": "auth0-service",
                "iss": "https://test.auth0.com/",
                "aud": "https://test.api",
            }

            # Mock the JWKS fetch and JWT decode
            mocker.patch("ember.auth.fetch_auth0_jwks", return_value=mock_jwks)
            mocker.patch(
                "ember.auth.get_signing_key", return_value=mock_jwks["keys"][0]
            )
            mocker.patch("jose.jwt.decode", return_value=mock_payload)

            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=mock_token
            )

            result = await verify_token(credentials=credentials)

            # Should return Auth0 payload with m2m auth_type
            assert result["sub"] == "auth0-service"
            assert result["auth_type"] == "m2m"

        finally:
            settings.auth0_domain = original_auth0_domain
            settings.auth0_audience = original_auth0_audience
            settings.supabase_url = original_supabase_url
            settings.environment = original_environment

    async def test_supabase_jwt_validation(self, mocker):
        """Test Supabase JWT validation (existing functionality)."""
        # Mock Supabase settings
        original_supabase_url = settings.supabase_url
        original_auth0_domain = settings.auth0_domain
        original_environment = settings.environment

        try:
            settings.supabase_url = "https://test.supabase.co"
            settings.auth0_domain = ""  # Disable Auth0 to test Supabase only
            settings.environment = "production"

            # Mock JWKS and token
            mock_jwks = {
                "keys": [
                    {
                        "kid": "test-kid",
                        "kty": "EC",
                        "alg": "ES256",
                        "use": "sig",
                        "crv": "P-256",
                        "x": "mock_x",
                        "y": "mock_y",
                    }
                ]
            }

            mock_token = "mock.supabase.token"
            mock_payload = {
                "sub": "supabase-user",
                "email": "user@test.com",
                "aud": "authenticated",
            }

            # Mock the JWKS fetch, key construction, and JWT decode
            mocker.patch("ember.auth.fetch_jwks", return_value=mock_jwks)
            mocker.patch(
                "ember.auth.get_signing_key", return_value=mock_jwks["keys"][0]
            )
            # Mock jwk.construct to return a mock public key
            mock_public_key = mocker.MagicMock()
            mocker.patch("jose.jwk.construct", return_value=mock_public_key)
            mocker.patch("jose.jwt.decode", return_value=mock_payload)

            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=mock_token
            )

            result = await verify_token(credentials=credentials)

            # Should return Supabase payload with user auth_type
            assert result["sub"] == "supabase-user"
            assert result["email"] == "user@test.com"
            assert result["auth_type"] == "user"

        finally:
            settings.supabase_url = original_supabase_url
            settings.auth0_domain = original_auth0_domain
            settings.environment = original_environment

    async def test_auth0_fallback_to_supabase(self, mocker):
        """Test that Auth0 failure falls back to Supabase."""
        # Mock both Auth0 and Supabase settings
        original_auth0_domain = settings.auth0_domain
        original_auth0_audience = settings.auth0_audience
        original_supabase_url = settings.supabase_url
        original_environment = settings.environment

        try:
            settings.auth0_domain = "test.auth0.com"
            settings.auth0_audience = "https://test.api"
            settings.supabase_url = "https://test.supabase.co"
            settings.environment = "production"

            # Mock Auth0 to fail (raise JWTError)
            mocker.patch(
                "ember.auth.fetch_auth0_jwks", side_effect=Exception("Auth0 failed")
            )

            # Mock Supabase JWKS and token
            mock_jwks = {
                "keys": [
                    {
                        "kid": "test-kid",
                        "kty": "EC",
                        "alg": "ES256",
                        "use": "sig",
                        "crv": "P-256",
                        "x": "mock_x",
                        "y": "mock_y",
                    }
                ]
            }

            mock_token = "mock.supabase.token"
            mock_payload = {
                "sub": "supabase-user",
                "email": "user@test.com",
                "aud": "authenticated",
            }

            # Mock the Supabase JWKS fetch, key construction, and JWT decode
            mocker.patch("ember.auth.fetch_jwks", return_value=mock_jwks)
            mocker.patch(
                "ember.auth.get_signing_key", return_value=mock_jwks["keys"][0]
            )
            # Mock jwk.construct to return a mock public key
            mock_public_key = mocker.MagicMock()
            mocker.patch("jose.jwk.construct", return_value=mock_public_key)
            mocker.patch("jose.jwt.decode", return_value=mock_payload)

            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=mock_token
            )

            result = await verify_token(credentials=credentials)

            # Should return Supabase payload with user auth_type (fallback worked)
            assert result["sub"] == "supabase-user"
            assert result["auth_type"] == "user"

        finally:
            settings.auth0_domain = original_auth0_domain
            settings.auth0_audience = original_auth0_audience
            settings.supabase_url = original_supabase_url
            settings.environment = original_environment
    async def test_invalid_token_format(self, mocker):
        """Test that malformed token raises 401."""
        original_supabase_url = settings.supabase_url
        original_auth0_domain = settings.auth0_domain
        original_environment = settings.environment

        try:
            settings.supabase_url = "https://test.supabase.co"
            settings.auth0_domain = ""
            settings.environment = "production"

            # Mock JWKS fetch to avoid network calls
            mocker.patch("ember.auth.fetch_jwks", return_value={"keys": []})

            # Pass an invalid JWT (not even proper format)
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials="not.a.valid.jwt"
            )

            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=credentials)

            assert exc_info.value.status_code == 401
            assert "Invalid token" in str(exc_info.value.detail)

        finally:
            settings.supabase_url = original_supabase_url
            settings.auth0_domain = original_auth0_domain
            settings.environment = original_environment

    async def test_expired_token(self, mocker):
        """Test that expired token raises 401."""
        original_supabase_url = settings.supabase_url
        original_auth0_domain = settings.auth0_domain
        original_environment = settings.environment

        try:
            settings.supabase_url = "https://test.supabase.co"
            settings.auth0_domain = ""
            settings.environment = "production"

            mock_jwks = {
                "keys": [
                    {
                        "kid": "test-kid",
                        "kty": "EC",
                        "alg": "ES256",
                        "use": "sig",
                        "crv": "P-256",
                        "x": "mock_x",
                        "y": "mock_y",
                    }
                ]
            }

            mock_token = "mock.expired.token"

            # Mock JWKS fetch and key lookup
            mocker.patch("ember.auth.fetch_jwks", return_value=mock_jwks)
            mocker.patch(
                "ember.auth.get_signing_key", return_value=mock_jwks["keys"][0]
            )
            mock_public_key = mocker.MagicMock()
            mocker.patch("jose.jwk.construct", return_value=mock_public_key)

            mocker.patch(
                "jose.jwt.decode", side_effect=ExpiredSignatureError("Token expired")
            )

            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=mock_token
            )

            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=credentials)

            assert exc_info.value.status_code == 401
            assert "Token expired" in str(exc_info.value.detail)

        finally:
            settings.supabase_url = original_supabase_url
            settings.auth0_domain = original_auth0_domain
            settings.environment = original_environment

    async def test_wrong_audience(self, mocker):
        """Test that token with wrong audience raises 401."""
        original_auth0_domain = settings.auth0_domain
        original_auth0_audience = settings.auth0_audience
        original_environment = settings.environment

        try:
            settings.auth0_domain = "test.auth0.com"
            settings.auth0_audience = "https://expected.api"
            settings.environment = "production"

            mock_jwks = {
                "keys": [
                    {
                        "kid": "test-kid",
                        "kty": "RSA",
                        "alg": "RS256",
                        "use": "sig",
                        "n": "mock_n",
                        "e": "AQAB",
                    }
                ]
            }

            mock_token = "mock.auth0.token"

            # Mock JWKS and key
            mocker.patch("ember.auth.fetch_auth0_jwks", return_value=mock_jwks)
            mocker.patch(
                "ember.auth.get_signing_key", return_value=mock_jwks["keys"][0]
            )
            mock_public_key = mocker.MagicMock()
            mocker.patch("jose.jwk.construct", return_value=mock_public_key)

            mocker.patch(
                "jose.jwt.decode",
                side_effect=JWTClaimsError("Invalid audience"),
            )

            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=mock_token
            )

            # Should try Auth0 and fail, then try Supabase and also fail
            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=credentials)

            assert exc_info.value.status_code == 401

        finally:
            settings.auth0_domain = original_auth0_domain
            settings.auth0_audience = original_auth0_audience
            settings.environment = original_environment

    async def test_wrong_issuer(self, mocker):
        """Test that token with wrong issuer raises 401."""
        original_auth0_domain = settings.auth0_domain
        original_auth0_audience = settings.auth0_audience
        original_environment = settings.environment

        try:
            settings.auth0_domain = "expected.auth0.com"
            settings.auth0_audience = "https://test.api"
            settings.environment = "production"

            mock_jwks = {
                "keys": [
                    {
                        "kid": "test-kid",
                        "kty": "RSA",
                        "alg": "RS256",
                        "use": "sig",
                        "n": "mock_n",
                        "e": "AQAB",
                    }
                ]
            }

            mock_token = "mock.auth0.token"

            # Mock JWKS and key
            mocker.patch("ember.auth.fetch_auth0_jwks", return_value=mock_jwks)
            mocker.patch(
                "ember.auth.get_signing_key", return_value=mock_jwks["keys"][0]
            )
            mock_public_key = mocker.MagicMock()
            mocker.patch("jose.jwk.construct", return_value=mock_public_key)

            mocker.patch(
                "jose.jwt.decode",
                side_effect=JWTClaimsError("Invalid issuer"),
            )

            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=mock_token
            )

            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=credentials)

            assert exc_info.value.status_code == 401

        finally:
            settings.auth0_domain = original_auth0_domain
            settings.auth0_audience = original_auth0_audience
            settings.environment = original_environment

    async def test_kid_not_in_jwks(self, mocker):
        """Test that token with kid not in JWKS fails gracefully."""
        original_supabase_url = settings.supabase_url
        original_auth0_domain = settings.auth0_domain
        original_environment = settings.environment

        try:
            settings.supabase_url = "https://test.supabase.co"
            settings.auth0_domain = ""
            settings.environment = "production"

            # JWKS has one key
            mock_jwks = {
                "keys": [
                    {
                        "kid": "key-in-jwks",
                        "kty": "EC",
                        "alg": "ES256",
                        "use": "sig",
                        "crv": "P-256",
                        "x": "mock_x",
                        "y": "mock_y",
                    }
                ]
            }

            mock_token = "mock.token.with.different.kid"

            # Mock JWKS fetch
            mocker.patch("ember.auth.fetch_jwks", return_value=mock_jwks)
            # Mock get_signing_key to return None (kid not found)
            mocker.patch("ember.auth.get_signing_key", return_value=None)

            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=mock_token
            )

            # Should fail because no signing key matches
            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=credentials)

            assert exc_info.value.status_code == 401

        finally:
            settings.supabase_url = original_supabase_url
            settings.auth0_domain = original_auth0_domain
            settings.environment = original_environment

    async def test_both_auth_methods_fail(self, mocker):
        """Test that when both Auth0 and Supabase fail, 401 is raised."""
        original_auth0_domain = settings.auth0_domain
        original_auth0_audience = settings.auth0_audience
        original_supabase_url = settings.supabase_url
        original_supabase_jwt_secret = settings.supabase_jwt_secret
        original_environment = settings.environment

        try:
            settings.auth0_domain = "test.auth0.com"
            settings.auth0_audience = "https://test.api"
            settings.supabase_url = "https://test.supabase.co"
            settings.supabase_jwt_secret = ""  # No HS256 fallback
            settings.environment = "production"

            mock_token = "mock.invalid.token"

            # Mock both to fail
            mocker.patch(
                "ember.auth.fetch_auth0_jwks",
                side_effect=Exception("Auth0 JWKS fetch failed"),
            )
            mocker.patch(
                "ember.auth.fetch_jwks",
                side_effect=Exception("Supabase JWKS fetch failed"),
            )

            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=mock_token
            )

            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=credentials)

            assert exc_info.value.status_code == 401
            assert "Invalid authentication token" in str(exc_info.value.detail)

        finally:
            settings.auth0_domain = original_auth0_domain
            settings.auth0_audience = original_auth0_audience
            settings.supabase_url = original_supabase_url
            settings.supabase_jwt_secret = original_supabase_jwt_secret
            settings.environment = original_environment

    async def test_jwks_fetch_failure_fallback(self, mocker):
        """Test that JWKS fetch failure is logged and handled."""
        original_auth0_domain = settings.auth0_domain
        original_auth0_audience = settings.auth0_audience
        original_supabase_url = settings.supabase_url
        original_environment = settings.environment

        try:
            settings.auth0_domain = "test.auth0.com"
            settings.auth0_audience = "https://test.api"
            settings.supabase_url = "https://test.supabase.co"
            settings.environment = "production"

            mock_token = "mock.token"

            # Mock Auth0 JWKS fetch to fail (network error)
            mocker.patch(
                "ember.auth.fetch_auth0_jwks",
                side_effect=Exception("Network timeout"),
            )

            # Mock Supabase to succeed
            mock_jwks = {
                "keys": [
                    {
                        "kid": "test-kid",
                        "kty": "EC",
                        "alg": "ES256",
                        "use": "sig",
                        "crv": "P-256",
                        "x": "mock_x",
                        "y": "mock_y",
                    }
                ]
            }
            mock_payload = {
                "sub": "supabase-user",
                "email": "user@test.com",
                "aud": "authenticated",
            }

            mocker.patch("ember.auth.fetch_jwks", return_value=mock_jwks)
            mocker.patch(
                "ember.auth.get_signing_key", return_value=mock_jwks["keys"][0]
            )
            mock_public_key = mocker.MagicMock()
            mocker.patch("jose.jwk.construct", return_value=mock_public_key)
            mocker.patch("jose.jwt.decode", return_value=mock_payload)

            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=mock_token
            )

            result = await verify_token(credentials=credentials)

            # Should fallback to Supabase successfully
            assert result["sub"] == "supabase-user"
            assert result["auth_type"] == "user"

        finally:
            settings.auth0_domain = original_auth0_domain
            settings.auth0_audience = original_auth0_audience
            settings.supabase_url = original_supabase_url
            settings.environment = original_environment

    async def test_supabase_hs256_fallback(self, mocker):
        """Test Supabase HS256 fallback when JWKS is not configured."""
        original_supabase_url = settings.supabase_url
        original_supabase_jwt_secret = settings.supabase_jwt_secret
        original_auth0_domain = settings.auth0_domain
        original_environment = settings.environment

        try:
            # Configure with JWT secret but no URL (so no JWKS)
            settings.supabase_url = ""
            settings.supabase_jwt_secret = "test-secret-key"
            settings.auth0_domain = ""
            settings.environment = "production"

            mock_token = "mock.supabase.token"
            mock_payload = {
                "sub": "supabase-user",
                "email": "user@test.com",
                "aud": "authenticated",
            }

            # Mock jwt.decode to succeed with HS256
            mocker.patch("jose.jwt.decode", return_value=mock_payload)

            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=mock_token
            )

            result = await verify_token(credentials=credentials)

            # Should succeed using HS256 secret
            assert result["sub"] == "supabase-user"
            assert result["email"] == "user@test.com"
            assert result["auth_type"] == "user"

        finally:
            settings.supabase_url = original_supabase_url
            settings.supabase_jwt_secret = original_supabase_jwt_secret
            settings.auth0_domain = original_auth0_domain
            settings.environment = original_environment
