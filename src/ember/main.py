"""
Ember FastAPI application factory.

Creates and configures the FastAPI app with:
- Lifespan management (startup/shutdown)
- CORS middleware
- Global exception handlers
- Route registration
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ember.api import add_cors_middleware, register_exception_handlers
from ember.api.logging_middleware import RequestLoggingMiddleware
from ember.config import settings
from ember.logging import get_logger
from ember.routers import (
    fires_router,
    fuel_router,
    geocode_router,
    imagery_router,
    nws_router,
    satellite_router,
    scenes_router,
    terrain_router,
    vegetation_router,
    weather_router,
)

# Get logger for this module
# NOTE: Logging is configured in entrypoint.py before importing this module
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """
    Lifespan context manager for FastAPI.

    Handles startup and shutdown logging.
    """
    logger.info("Ember starting up...")

    # Check API configurations
    if settings.firms_map_key:
        logger.info("FIRMS API key configured")
    else:
        logger.warning("FIRMS API key not configured - fire data will be limited")

    if settings.supabase_url or settings.supabase_jwt_secret:
        logger.info("Supabase auth configured")
    else:
        logger.warning("Supabase auth not configured - endpoints are open")

    if settings.copernicus_client_id:
        logger.info("Copernicus credentials configured")
    else:
        logger.info("Copernicus not configured - vegetation endpoints will return stubs")

    # Initialize terrain service with layer discovery
    if settings.landfire_s3_prefix:
        from ember.services.terrain import get_terrain_service

        terrain_svc = get_terrain_service()
        if terrain_svc:
            # Register known layers from S3
            # These are the files we've uploaded
            known_files = [
                # Existing — Topographic (2020)
                "LC20_SlpD_220.tif",  # Slope
                "LC20_Asp_220.tif",  # Aspect
                "LC20_Elev_220.tif",  # Elevation
                # Existing — Canopy (2024)
                "LC24_CH_250.tif",  # Canopy Height
                "LC24_CBH_250.tif",  # Canopy Base Height
                "LC24_CBD_250.tif",  # Canopy Bulk Density
                "LC24_CC_250.tif",  # Canopy Cover
                # Existing — Fuel (2024)
                "LC24_F40_250.tif",  # Fuel (FBFM40)
                # New — Fuel (2024)
                "LF2024_FBFM13_CONUS.tif",  # Fuel Model 13
                # New — Vegetation (2024/2020)
                "LF2024_EVT_CONUS.tif",  # Existing Vegetation Type
                "LF2024_EVC_CONUS.tif",  # Existing Vegetation Cover
                "LF2024_EVH_CONUS.tif",  # Existing Vegetation Height
                "LF2020_BPS_CONUS.tif",  # Biophysical Settings
                # New — Fire Regime (2016/2024)
                "LF2016_FRG_CONUS.tif",  # Fire Regime Groups
                "LF2016_FRI_CONUS.tif",  # Fire Return Interval
                "LF2016_PFS_CONUS.tif",  # Percent Fire Severity
                "LF2024_VDep_CONUS.tif",  # Vegetation Departure
                "LF2024_VCC_CONUS.tif",  # Vegetation Condition Class
                "LF2024_SClass_CONUS.tif",  # Succession Classes
                # New — Disturbance (2024)
                "LF2024_FDist_CONUS.tif",  # Fuel Disturbance
            ]
            discovered = terrain_svc.discover_layers(known_files)
            logger.info(f"Terrain service ready: {len(discovered)} layers available")
    else:
        logger.info("LANDFIRE_S3_PREFIX not configured - terrain endpoint unavailable")

    # Check satellite service availability and pre-warm TLE cache
    try:
        from ember.services.satellite import satellite_service

        if satellite_service._ephemeris_available:
            logger.info("Satellite tracking: available (with sun angle)")
        else:
            logger.info("Satellite tracking: available (without sun angle)")

        # Pre-warm TLE cache so first user request doesn't wait on CelesTrak
        results = await satellite_service.prewarm_tle_cache()
        ok_count = sum(1 for v in results.values() if v.startswith("ok"))
        total = len(results)
        if ok_count == total:
            logger.info(f"TLE cache pre-warmed: {ok_count}/{total} satellites ready")
        elif ok_count > 0:
            logger.warning(
                f"TLE cache partially warmed: {ok_count}/{total} satellites. "
                f"Failed: {[k for k, v in results.items() if v == 'failed']}"
            )
        else:
            logger.warning("TLE cache pre-warm failed — CelesTrak may be unreachable")
    except Exception:
        logger.warning("Satellite tracking: unavailable (skyfield not installed)")

    logger.info("STAC scene search ready (Earth Search)")

    logger.info(f"Ember ready! Listening on {settings.host}:{settings.port}")

    # Yield to run the application
    yield

    # Shutdown
    logger.info("Ember shutting down...")
    logger.info("Ember shutdown complete")


def create_app() -> FastAPI:
    """
    Create and configure FastAPI application.

    Returns:
        Configured FastAPI app
    """
    app = FastAPI(
        title="Ember",
        description="Direct API proxy for wildfire data - feeds Nova frontend",
        version=settings.app_version,
        lifespan=lifespan,
    )

    # Add CORS middleware
    add_cors_middleware(app)

    # Add request logging middleware
    app.add_middleware(RequestLoggingMiddleware)

    # Register global exception handlers
    register_exception_handlers(app)

    # Mount routers
    app.include_router(fires_router, prefix="/api/v1")
    app.include_router(geocode_router, prefix="/api/v1")
    app.include_router(imagery_router, prefix="/api/v1")
    app.include_router(nws_router, prefix="/api/v1")
    app.include_router(weather_router, prefix="/api/v1")
    app.include_router(fuel_router, prefix="/api/v1")
    app.include_router(vegetation_router, prefix="/api/v1")
    app.include_router(terrain_router, prefix="/api/v1")
    app.include_router(satellite_router, prefix="/api/v1")
    app.include_router(scenes_router, prefix="/api/v1")

    # Health check endpoint
    @app.get("/health")
    async def health():
        """Health check endpoint."""
        return {"status": "healthy", "service": "ember"}

    # Root endpoint
    @app.get("/")
    async def root():
        """Root endpoint with API info."""
        return {
            "service": "ember",
            "version": settings.app_version,
            "description": "Direct API proxy for wildfire data",
            "endpoints": {
                "fires": "/api/v1/fires",
                "geocode": "/api/v1/geocode",
                "imagery": "/api/v1/imagery",
                "nws": "/api/v1/nws",
                "weather": "/api/v1/weather",
                "fuel": "/api/v1/fuel",
                "vegetation": "/api/v1/vegetation",
                "terrain": "/api/v1/terrain",
                "satellite": "/api/v1/satellite",
                "scenes": "/api/v1/scenes",
            },
        }

    return app


# Create app instance
app = create_app()
