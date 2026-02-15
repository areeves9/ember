"""Test unified authentication functionality."""

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

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
                        "e": "AQAB"
                    }
                ]
            }

            # Mock token with RS256 algorithm
            mock_token = "mock.auth0.token"
            mock_payload = {
                "sub": "auth0-service",
                "iss": "https://test.auth0.com/",
                "aud": "https://test.api"
            }

            # Mock the JWKS fetch and JWT decode
            mocker.patch('ember.auth.fetch_auth0_jwks', return_value=mock_jwks)
            mocker.patch('ember.auth.get_signing_key', return_value=mock_jwks["keys"][0])
            mocker.patch('jose.jwt.decode', return_value=mock_payload)

            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer",
                credentials=mock_token
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
                        "y": "mock_y"
                    }
                ]
            }

            mock_token = "mock.supabase.token"
            mock_payload = {
                "sub": "supabase-user",
                "email": "user@test.com",
                "aud": "authenticated"
            }

            # Mock the JWKS fetch and JWT decode
            mocker.patch('ember.auth.fetch_jwks', return_value=mock_jwks)
            mocker.patch('ember.auth.get_signing_key', return_value=mock_jwks["keys"][0])
            mocker.patch('jose.jwt.decode', return_value=mock_payload)

            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer",
                credentials=mock_token
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
            mocker.patch('ember.auth.fetch_auth0_jwks', side_effect=Exception("Auth0 failed"))

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
                        "y": "mock_y"
                    }
                ]
            }

            mock_token = "mock.supabase.token"
            mock_payload = {
                "sub": "supabase-user",
                "email": "user@test.com",
                "aud": "authenticated"
            }

            # Mock the Supabase JWKS fetch and JWT decode
            mocker.patch('ember.auth.fetch_jwks', return_value=mock_jwks)
            mocker.patch('ember.auth.get_signing_key', return_value=mock_jwks["keys"][0])
            mocker.patch('jose.jwt.decode', return_value=mock_payload)

            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer",
                credentials=mock_token
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