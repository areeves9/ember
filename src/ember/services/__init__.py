"""External API services."""

from ember.services.airquality import AirQualityService
from ember.services.copernicus import CopernicusService
from ember.services.firms import FirmsService
from ember.services.landfire import LandfireService
from ember.services.nominatim import NominatimService
from ember.services.nws import NWSService
from ember.services.openmeteo import OpenMeteoService
from ember.services.satellite import SatelliteService

__all__ = [
    "AirQualityService",
    "FirmsService",
    "NominatimService",
    "NWSService",
    "OpenMeteoService",
    "LandfireService",
    "CopernicusService",
    "SatelliteService",
]
