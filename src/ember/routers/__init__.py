"""API routers."""

from ember.routers.fires import router as fires_router
from ember.routers.fuel import router as fuel_router
from ember.routers.geocode import router as geocode_router
from ember.routers.imagery import router as imagery_router
from ember.routers.nws import router as nws_router
from ember.routers.satellite import router as satellite_router
from ember.routers.scenes import router as scenes_router
from ember.routers.terrain import router as terrain_router
from ember.routers.vegetation import router as vegetation_router
from ember.routers.weather import router as weather_router

__all__ = [
    "fires_router",
    "geocode_router",
    "imagery_router",
    "nws_router",
    "weather_router",
    "fuel_router",
    "vegetation_router",
    "terrain_router",
    "satellite_router",
    "scenes_router",
]
