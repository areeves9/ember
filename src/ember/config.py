"""Configuration management for Ember."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Server
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8001)
    environment: str = Field(default="development")

    # CORS
    cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:5173",
        description="Comma-separated CORS origins",
    )

    # Supabase Auth
    supabase_url: str = Field(default="", description="Supabase project URL for JWKS")
    supabase_jwt_secret: str = Field(default="", description="Supabase JWT secret (HS256 fallback)")

    # Auth0 M2M (Machine-to-Machine)
    auth0_domain: str = Field(default="", description="Auth0 domain for M2M validation")
    auth0_audience: str = Field(default="", description="Auth0 API audience/identifier")

    # API Keys
    firms_map_key: str = Field(default="", description="NASA FIRMS MAP_KEY")

    # Copernicus (for NDVI/NDMI)
    copernicus_client_id: str = Field(default="")
    copernicus_client_secret: str = Field(default="")

    # LANDFIRE COG raster data
    # S3 prefix containing LANDFIRE TIF files (e.g., s3://bucket/Tif)
    landfire_s3_prefix: str = Field(
        default="",
        description="S3 prefix for LANDFIRE layers",
    )
    # Legacy: single fuel model URL (deprecated, use landfire_s3_prefix)
    landfire_cog_url: str = Field(default="", description="FBFM40 fuel model URL (legacy)")

    # AWS credentials for S3 COG access
    aws_access_key_id: str = Field(default="", description="AWS access key for S3 COG access")
    aws_secret_access_key: str = Field(default="", description="AWS secret key for S3 COG access")
    aws_region: str = Field(default="us-west-2", description="AWS region for S3")

    # EPA AirNow
    airnow_api_key: str = Field(default="", description="EPA AirNow API key for air quality data")

    # Request timeouts
    http_timeout: float = Field(default=30.0, description="HTTP client timeout in seconds")

    # Logging
    log_level: str = Field(default="INFO", description="Log level")
    log_format: str = Field(default="text", description="Log format: text or json")

    # App metadata
    app_version: str = Field(default="0.1.0")

    @property
    def cors_origins_list(self) -> list[str]:
        """Get CORS origins as a list."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def supabase_jwks_url(self) -> str | None:
        """Get JWKS URL for Supabase JWT verification."""
        if self.supabase_url:
            return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        return None

    @property
    def auth0_jwks_url(self) -> str | None:
        """Get JWKS URL for Auth0 JWT verification."""
        if self.auth0_domain:
            return f"https://{self.auth0_domain}/.well-known/jwks.json"
        return None


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
