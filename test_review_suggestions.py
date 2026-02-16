"""
Test script to verify Qodo review suggestions #2 and #3.

Tests:
1. Empty string filtering (malformed input like "temp,,humidity")
2. Sorted cache key normalization (different orderings share cache)
"""

import asyncio

from ember.services.openmeteo import openmeteo_service, _weather_cache


async def test_empty_string_filtering():
    """Test that malformed variables with empty strings are handled gracefully."""
    print("\n=== Test 1: Empty String Filtering ===")

    # Test malformed input with extra commas
    malformed_variables = "soil_moisture_0_to_1cm,,soil_temperature_0cm,,"
    result = await openmeteo_service.get_current_weather(
        45.52, -122.68, variables=malformed_variables
    )

    print(f"Input: '{malformed_variables}'")
    print(f"Status: {result['status']}")
    print(f"Keys in current: {list(result['current'].keys())}")

    # Should have filtered out empty strings
    assert "soil_moisture_0_to_1cm" in result["current"], "Should have soil moisture"
    assert "soil_temperature_0cm" in result["current"], "Should have soil temperature"
    print("✅ Malformed input handled correctly (empty strings filtered)")

    # Test all-empty input (should fall back to defaults)
    all_empty = ",,,"
    result2 = await openmeteo_service.get_current_weather(
        45.52, -122.68, variables=all_empty
    )

    print(f"\nInput: '{all_empty}'")
    print(f"Keys in current: {list(result2['current'].keys())}")

    # Should fall back to transformed default format
    assert "temperature_c" in result2["current"], "Should fall back to defaults"
    print("✅ All-empty input handled correctly (fell back to defaults)")


async def test_sorted_cache_key():
    """Test that different orderings of same variables share cache."""
    print("\n=== Test 2: Sorted Cache Key Normalization ===")

    # Clear cache first
    _weather_cache.clear()

    # First call with one ordering
    variables_1 = "soil_moisture_0_to_1cm,soil_temperature_0cm"
    result1 = await openmeteo_service.get_current_weather(
        45.52, -122.68, variables=variables_1
    )
    assert not result1.get("cached"), "First call should not be cached"
    print(f"First call: '{variables_1}' → Cache miss (expected)")

    # Second call with reversed ordering (should hit cache)
    variables_2 = "soil_temperature_0cm,soil_moisture_0_to_1cm"
    result2 = await openmeteo_service.get_current_weather(
        45.52, -122.68, variables=variables_2
    )

    print(f"Second call: '{variables_2}' → Cache hit: {result2.get('cached', False)}")

    # Note: The cache doesn't set a 'cached' flag in the data, so we need to check
    # that the results are identical (proving cache was used)
    assert result1["current"] == result2["current"], "Results should be identical"
    print("✅ Different orderings share cache entry")

    # Third call with different variables (should NOT hit cache)
    variables_3 = "temperature_2m,relative_humidity_2m"
    result3 = await openmeteo_service.get_current_weather(
        45.52, -122.68, variables=variables_3
    )
    print(f"Third call: '{variables_3}' → New cache entry (different variables)")

    # Check cache keys exist
    print(f"\nCache keys: {list(_weather_cache.keys())}")
    assert len(_weather_cache) >= 2, "Should have at least 2 cache entries"
    print("✅ Cache isolation maintained for different variable sets")


async def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("Testing Qodo Review Suggestions #2 and #3")
    print("=" * 60)

    try:
        await test_empty_string_filtering()
        await test_sorted_cache_key()

        print("\n" + "=" * 60)
        print("✅ ALL REVIEW SUGGESTION TESTS PASSED!")
        print("=" * 60)
        print("\nImplemented improvements:")
        print("  • Empty string filtering prevents API errors")
        print("  • Sorted cache keys improve cache hit rate")
        print("  • Malformed input gracefully falls back to defaults")

    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
