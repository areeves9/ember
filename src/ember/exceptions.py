"""
Custom exceptions for Ember.

Provides structured exceptions with to_dict() for API responses.
"""

from typing import Any


class EmberError(Exception):
    """Base exception for all Ember errors."""

    status_code: int = 500
    error_code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        self.message = message
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        """Convert exception to dict for API response."""
        return {
            "error": self.error_code,
            "message": self.message,
            "details": self.details,
        }


class ExternalAPIError(EmberError):
    """Error from external API (FIRMS, Nominatim, etc.)."""

    status_code = 502
    error_code = "EXTERNAL_API_ERROR"


class AuthenticationError(EmberError):
    """Authentication failed."""

    status_code = 401
    error_code = "AUTHENTICATION_ERROR"


class AuthorizationError(EmberError):
    """Authorization failed (valid token but insufficient permissions)."""

    status_code = 403
    error_code = "AUTHORIZATION_ERROR"


class ValidationError(EmberError):
    """Request validation error."""

    status_code = 400
    error_code = "VALIDATION_ERROR"


class NotFoundError(EmberError):
    """Resource not found."""

    status_code = 404
    error_code = "NOT_FOUND"


class RateLimitError(EmberError):
    """Rate limit exceeded."""

    status_code = 429
    error_code = "RATE_LIMIT_EXCEEDED"
