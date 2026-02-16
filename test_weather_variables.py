"""
Manual test script for weather variables parameter.

Run this to verify that the variables parameter works correctly.

Usage:
    python test_weather_variables.py
"""

import asyncio

from ember.services.openmeteo import openmeteo_service


async def test_default_variables():
    """Test with default variables (backward compatibility)."""
    print("\n=== Test 1: Default Variables ===")
    result = await openmeteo_service.get_current_weather(45.52, -122.68)
    print(f"Status: {result['status']}")
    print(f"Keys in result: {list(result.keys())}")
    print(f"Keys in current: {list(result['current'].keys())}")
    print(f"Temperature (transformed): {result['current'].get('temperature_c')}")
    assert "temperature_c" in result["current"], "Should have transformed keys"
    assert "status" in result, "Should have status field"
    print("✅ Default variables test passed")


async def test_custom_variables_soil():
    """Test with custom soil moisture variables."""
    print("\n=== Test 2: Custom Variables (Soil Moisture) ===")
    variables = "soil_moisture_0_to_1cm,soil_moisture_1_to_3cm,soil_temperature_0cm"
    result = await openmeteo_service.get_current_weather(
        45.52, -122.68, variables=variables
    )
    print(f"Status: {result['status']}")
    print(f"Keys in result: {list(result.keys())}")
    print(f"Keys in current: {list(result['current'].keys())}")
    print(
        f"Soil moisture 0-1cm: {result['current'].get('soil_moisture_0_to_1cm')} m³/m³"
    )
    assert "soil_moisture_0_to_1cm" in result["current"], "Should have soil moisture"
    assert (
        "temperature_c" not in result["current"]
    ), "Should NOT have transformed keys"
    print("✅ Custom soil variables test passed")


async def test_custom_variables_solar():
    """Test with custom solar radiation variables."""
    print("\n=== Test 3: Custom Variables (Solar Radiation) ===")
    variables = "shortwave_radiation,direct_radiation,diffuse_radiation,direct_normal_irradiance"
    result = await openmeteo_service.get_current_weather(
        45.52, -122.68, variables=variables
    )
    print(f"Status: {result['status']}")
    print(f"Keys in current: {list(result['current'].keys())}")
    print(f"Shortwave radiation: {result['current'].get('shortwave_radiation')} W/m²")
    assert "shortwave_radiation" in result["current"], "Should have solar radiation"
    print("✅ Custom solar variables test passed")


async def test_custom_variables_pressure():
    """Test with custom atmospheric pressure variables."""
    print("\n=== Test 4: Custom Variables (Atmospheric Pressure) ===")
    variables = "temperature_1000hPa,temperature_850hPa,relative_humidity_1000hPa,relative_humidity_850hPa"
    result = await openmeteo_service.get_current_weather(
        45.52, -122.68, variables=variables
    )
    print(f"Status: {result['status']}")
    print(f"Keys in current: {list(result['current'].keys())}")
    print(f"Temperature 1000hPa: {result['current'].get('temperature_1000hPa')}°C")
    assert "temperature_1000hPa" in result["current"], "Should have pressure levels"
    print("✅ Custom pressure variables test passed")


async def test_cache_isolation():
    """Test that cache properly isolates default vs custom variables."""
    print("\n=== Test 5: Cache Isolation ===")

    # First call with default variables
    result1 = await openmeteo_service.get_current_weather(45.52, -122.68)
    assert "temperature_c" in result1["current"], "First call should be transformed"

    # Second call with custom variables (same location)
    result2 = await openmeteo_service.get_current_weather(
        45.52, -122.68, variables="soil_moisture_0_to_1cm"
    )
    assert (
        "soil_moisture_0_to_1cm" in result2["current"]
    ), "Second call should have raw format"
    assert (
        "temperature_c" not in result2["current"]
    ), "Second call should NOT be transformed"

    # Third call with default variables again (should use cached transformed version)
    result3 = await openmeteo_service.get_current_weather(45.52, -122.68)
    assert "temperature_c" in result3["current"], "Third call should be transformed"

    print("✅ Cache isolation test passed")


async def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("Testing Weather Variables Parameter Implementation")
    print("=" * 60)

    try:
        await test_default_variables()
        await test_custom_variables_soil()
        await test_custom_variables_solar()
        await test_custom_variables_pressure()
        await test_cache_isolation()

        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED!")
        print("=" * 60)
        print("\nThe variables parameter is working correctly:")
        print("  • Default behavior preserved (backward compatible)")
        print("  • Custom variables return raw Open-Meteo format")
        print("  • Cache properly isolates different variable sets")
        print("  • Specialized variables (soil, solar, pressure) work")

    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
