"""
Test script to verify Copilot review suggestions #3 and #4.

Tests:
1. Deduplication of duplicate variables (e.g., "temp,temp,humidity")
2. current_units included in response for custom variables
"""

import asyncio

from ember.services.openmeteo import openmeteo_service, _weather_cache


async def test_variable_deduplication():
    """Test that duplicate variables are deduplicated."""
    print("\n=== Test 1: Variable Deduplication ===")

    # Clear cache
    _weather_cache.clear()

    # Test with duplicates
    duplicated_variables = "soil_moisture_0_to_1cm,soil_temperature_0cm,soil_moisture_0_to_1cm"
    result = await openmeteo_service.get_current_weather(
        45.52, -122.68, variables=duplicated_variables
    )

    print(f"Input: '{duplicated_variables}'")
    print(f"Status: {result['status']}")
    print(f"Keys in current: {list(result['current'].keys())}")

    # Check response has both variables (deduplicated)
    assert "soil_moisture_0_to_1cm" in result["current"], "Should have soil moisture"
    assert "soil_temperature_0cm" in result["current"], "Should have soil temperature"
    print("✅ Duplicates deduplicated correctly")

    # Check cache key doesn't have duplicates
    cache_keys = list(_weather_cache.keys())
    print(f"\nCache key: {cache_keys[0]}")

    # Cache key should only have each variable once (sorted)
    assert "soil_moisture_0_to_1cm,soil_temperature_0cm" in cache_keys[0], (
        "Cache key should have deduplicated and sorted variables"
    )
    print("✅ Cache key doesn't contain duplicates")

    # Test that same variables in different order use the same cache
    _weather_cache.clear()

    # First call with duplicates
    result1 = await openmeteo_service.get_current_weather(
        45.52, -122.68, variables="temperature_2m,temperature_2m,relative_humidity_2m,temperature_2m"
    )

    # Second call with different order but no duplicates (should match after dedup)
    result2 = await openmeteo_service.get_current_weather(
        45.52, -122.68, variables="relative_humidity_2m,temperature_2m"
    )

    # Should use same cache key (both deduplicate to temperature_2m,relative_humidity_2m, then sorted)
    assert result1["current"] == result2["current"], (
        "Different orderings with duplicates should share cache"
    )
    print("✅ Duplicates from different calls share cache correctly")


async def test_current_units_included():
    """Test that current_units is included for custom variables."""
    print("\n=== Test 2: current_units Inclusion ===")

    # Test with custom variables
    result = await openmeteo_service.get_current_weather(
        45.52, -122.68, variables="soil_moisture_0_to_1cm,soil_temperature_0cm"
    )

    print(f"Keys in result: {list(result.keys())}")

    # Check current_units is present
    assert "current_units" in result, "Should include current_units"
    assert isinstance(result["current_units"], dict), "current_units should be a dict"

    print(f"current_units keys: {list(result['current_units'].keys())}")
    print(f"current_units: {result['current_units']}")

    # Verify units are provided for requested variables
    assert "soil_moisture_0_to_1cm" in result["current_units"], (
        "Should have units for soil_moisture_0_to_1cm"
    )
    assert "soil_temperature_0cm" in result["current_units"], (
        "Should have units for soil_temperature_0cm"
    )

    print("✅ current_units included with proper unit information")

    # Test that default variables don't include current_units
    result_default = await openmeteo_service.get_current_weather(45.52, -122.68)

    print(f"\nDefault result keys: {list(result_default.keys())}")
    assert "current_units" not in result_default, (
        "Default variables should NOT include current_units (backward compatibility)"
    )
    print("✅ Default variables don't include current_units (backward compatible)")


async def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("Testing Copilot Review Suggestions #3 and #4")
    print("=" * 60)

    try:
        await test_variable_deduplication()
        await test_current_units_included()

        print("\n" + "=" * 60)
        print("✅ ALL COPILOT SUGGESTION TESTS PASSED!")
        print("=" * 60)
        print("\nImplemented improvements:")
        print("  • Duplicate variables are deduplicated")
        print("  • Cache keys don't bloat with duplicates")
        print("  • current_units included for custom variables")
        print("  • Units help interpret arbitrary variable values")

    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
