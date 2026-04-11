#!/usr/bin/env python3
"""
Unit tests for the expanded LANDFIRE layer suite (ORQ-136).

Tests layer registration, value transformations, and categorical
resampling for the 12 new layers added to the terrain service.
"""

import os

import pytest

# Set environment variable BEFORE any imports
os.environ.setdefault("LANDFIRE_S3_PREFIX", "s3://landfire/")

from ember.data import BPS_CODES, EVT_CODES, FDIST_CODES
from ember.services.terrain import (
    ANDERSON_13_CODES,
    CATEGORICAL_LAYERS,
    FIRE_REGIME_GROUPS,
    LAYER_PATTERNS,
    SUCCESSION_CLASSES,
    VEGETATION_CONDITION_CLASSES,
    TerrainService,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def terrain_service():
    """Provide a TerrainService with all layers discovered."""
    svc = TerrainService("s3://stellaris-landfire-data/Tif")
    known_files = [
        "LC20_SlpD_220.tif",
        "LC20_Asp_220.tif",
        "LC20_Elev_220.tif",
        "LC24_CH_250.tif",
        "LC24_CBH_250.tif",
        "LC24_CBD_250.tif",
        "LC24_CC_250.tif",
        "LC24_F40_250.tif",
        "LF2024_FBFM13_CONUS.tif",
        "LF2024_EVT_CONUS.tif",
        "LF2024_EVC_CONUS.tif",
        "LF2024_EVH_CONUS.tif",
        "LF2020_BPS_CONUS.tif",
        "LF2016_FRG_CONUS.tif",
        "LF2016_FRI_CONUS.tif",
        "LF2016_PFS_CONUS.tif",
        "LF2024_VDep_CONUS.tif",
        "LF2024_VCC_CONUS.tif",
        "LF2024_SClass_CONUS.tif",
        "LF2024_FDist_CONUS.tif",
    ]
    svc.discover_layers(known_files)
    return svc


# =============================================================================
# Layer Registration Tests
# =============================================================================


class TestLayerRegistration:
    """Verify all 20 layers are discoverable from known filenames."""

    def test_all_20_layers_registered(self, terrain_service):
        """All 20 LAYER_PATTERNS should be discovered."""
        assert len(terrain_service.available_layers) == 20

    def test_new_layers_present(self, terrain_service):
        """Each new layer should be in the available layers list."""
        new_layers = [
            "fuel_model_13",
            "vegetation_type",
            "vegetation_cover",
            "vegetation_height",
            "biophysical_settings",
            "fire_regime_group",
            "fire_return_interval",
            "percent_fire_severity",
            "vegetation_departure",
            "vegetation_condition",
            "succession_classes",
            "fuel_disturbance",
        ]
        for layer in new_layers:
            assert layer in terrain_service.available_layers, f"{layer} not registered"

    def test_existing_layers_still_present(self, terrain_service):
        """Original 8 layers should still be registered."""
        original = [
            "fuel", "slope", "aspect", "elevation",
            "canopy_height", "canopy_base_height",
            "canopy_bulk_density", "canopy_cover",
        ]
        for layer in original:
            assert layer in terrain_service.available_layers, f"{layer} missing"

    def test_layer_patterns_match_filenames(self):
        """Each pattern should match exactly one known filename."""
        known_files = [
            "LC24_F40_250.tif",
            "LF2024_FBFM13_CONUS.tif",
            "LF2024_EVT_CONUS.tif",
            "LF2016_FRG_CONUS.tif",
            "LF2024_SClass_CONUS.tif",
            "LF2024_FDist_CONUS.tif",
        ]
        for layer, pattern in LAYER_PATTERNS.items():
            matches = [f for f in known_files if f"_{pattern}_" in f]
            # Not all patterns have a file in this subset, but none should match multiple
            assert len(matches) <= 1, f"{layer} pattern '{pattern}' matched multiple files"


# =============================================================================
# Value Transformation Tests
# =============================================================================


class TestValueTransformations:
    """Verify _transform_value returns correct shapes for each new layer."""

    def test_fuel_model_13(self, terrain_service):
        result = terrain_service._transform_value("fuel_model_13", 4)
        assert result["code"] == "4 - Chaparral"
        assert result["raw"] == 4

    def test_fuel_model_13_unknown(self, terrain_service):
        result = terrain_service._transform_value("fuel_model_13", 999)
        assert "Unknown" in result["code"]
        assert result["raw"] == 999

    def test_vegetation_type(self, terrain_service):
        result = terrain_service._transform_value("vegetation_type", 7011)
        assert "name" in result
        assert result["raw"] == 7011
        assert isinstance(result["name"], str)

    def test_vegetation_cover(self, terrain_service):
        result = terrain_service._transform_value("vegetation_cover", 65)
        assert result == {"percent": 65}

    def test_vegetation_height(self, terrain_service):
        result = terrain_service._transform_value("vegetation_height", 150)
        assert result == {"meters": 15.0}

    def test_biophysical_settings(self, terrain_service):
        result = terrain_service._transform_value("biophysical_settings", 11)
        assert result["name"] == "Open Water"
        assert result["raw"] == 11

    def test_fire_regime_group(self, terrain_service):
        result = terrain_service._transform_value("fire_regime_group", 1)
        assert "Frequent low-severity" in result["group"]
        assert result["raw"] == 1

    def test_fire_return_interval(self, terrain_service):
        result = terrain_service._transform_value("fire_return_interval", 15)
        assert result == {"years": 15}

    def test_percent_fire_severity(self, terrain_service):
        result = terrain_service._transform_value("percent_fire_severity", 42)
        assert result == {"percent": 42}

    def test_vegetation_departure(self, terrain_service):
        result = terrain_service._transform_value("vegetation_departure", 78)
        assert result == {"percent": 78}

    def test_vegetation_condition(self, terrain_service):
        result = terrain_service._transform_value("vegetation_condition", 2)
        assert result["class"] == "Moderately departed"
        assert result["raw"] == 2

    def test_succession_classes(self, terrain_service):
        result = terrain_service._transform_value("succession_classes", 1)
        assert "Early succession" in result["class"]
        assert result["raw"] == 1

    def test_fuel_disturbance_fire(self, terrain_service):
        result = terrain_service._transform_value("fuel_disturbance", 131)
        assert result["type"] == "Fire"
        assert result["severity"] == "High"
        assert result["time"] == "One Year"
        assert result["raw"] == 131

    def test_fuel_disturbance_no_disturbance(self, terrain_service):
        result = terrain_service._transform_value("fuel_disturbance", 0)
        assert result["type"] == "No Disturbance"

    def test_null_value_returns_none(self, terrain_service):
        for layer in LAYER_PATTERNS:
            assert terrain_service._transform_value(layer, None) is None

    def test_existing_transforms_unchanged(self, terrain_service):
        """Original transforms should still work."""
        assert terrain_service._transform_value("fuel", 102) == {"code": "GR2", "raw": 102}
        assert terrain_service._transform_value("slope", 25) == {"degrees": 25}
        assert terrain_service._transform_value("elevation", 1200) == {"meters": 1200}
        assert terrain_service._transform_value("canopy_cover", 78) == {"percent": 78}


# =============================================================================
# Categorical Layers Tests
# =============================================================================


class TestCategoricalLayers:
    """Verify categorical layer set is correct for resampling."""

    def test_all_categorical_layers_listed(self):
        """All layers with lookup-based transforms should be in CATEGORICAL_LAYERS."""
        expected = {
            "fuel", "fuel_model_13", "vegetation_type", "biophysical_settings",
            "fire_regime_group", "vegetation_condition", "succession_classes",
            "fuel_disturbance",
        }
        assert CATEGORICAL_LAYERS == expected

    def test_continuous_layers_not_categorical(self):
        """Continuous value layers should NOT be in CATEGORICAL_LAYERS."""
        continuous = [
            "slope", "aspect", "elevation", "canopy_height",
            "canopy_base_height", "canopy_bulk_density", "canopy_cover",
            "vegetation_cover", "vegetation_height", "fire_return_interval",
            "percent_fire_severity", "vegetation_departure",
        ]
        for layer in continuous:
            assert layer not in CATEGORICAL_LAYERS, f"{layer} should not be categorical"


# =============================================================================
# Lookup Data Tests
# =============================================================================


class TestLookupData:
    """Verify lookup tables loaded correctly from JSON files."""

    def test_evt_codes_loaded(self):
        assert len(EVT_CODES) == 1068

    def test_bps_codes_loaded(self):
        assert len(BPS_CODES) == 101

    def test_fdist_codes_loaded(self):
        assert len(FDIST_CODES) == 64

    def test_fdist_structure(self):
        """Each FDist entry should have type, severity, and time."""
        for val, info in FDIST_CODES.items():
            assert "type" in info, f"FDist {val} missing type"
            assert "severity" in info, f"FDist {val} missing severity"
            assert "time" in info, f"FDist {val} missing time"

    def test_anderson_13_covers_1_through_13(self):
        for i in range(1, 14):
            assert i in ANDERSON_13_CODES, f"Anderson code {i} missing"

    def test_fire_regime_groups_covers_1_through_5(self):
        for i in range(1, 6):
            assert i in FIRE_REGIME_GROUPS, f"FRG {i} missing"

    def test_vegetation_condition_covers_1_through_3(self):
        for i in range(1, 4):
            assert i in VEGETATION_CONDITION_CLASSES, f"VCC {i} missing"

    def test_succession_classes_covers_1_through_7(self):
        for i in range(1, 8):
            assert i in SUCCESSION_CLASSES, f"SClass {i} missing"
