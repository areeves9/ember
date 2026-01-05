"""External API services."""

from ember.services.copernicus import CopernicusService
from ember.services.firms import FirmsService
from ember.services.landfire import LandfireService
from ember.services.nominatim import NominatimService
from ember.services.openmeteo import OpenMeteoService

__all__ = [
    "FirmsService",
    "NominatimService",
    "OpenMeteoService",
    "LandfireService",
    "CopernicusService",
]
