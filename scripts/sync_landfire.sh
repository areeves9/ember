#!/bin/bash
# Download LANDFIRE layers and upload to S3
# Usage: ./scripts/sync_landfire.sh

set -e

S3_BUCKET="s3://stellaris-landfire-data/Tif"
AWS_PROFILE="stellaris"
DOWNLOAD_DIR="/Volumes/Storage/landfire"
BASE_URL="https://landfire.gov/data-downloads"

# Layers to download (name, folder, filename)
# Format: "description,folder,zipfile"
declare -a LAYERS=(
    # Topographic (2020)
    "Slope Degrees,US_Topo_2020,LF2020_SlpD_220_CONUS.zip"
    "Aspect,US_Topo_2020,LF2020_Asp_220_CONUS.zip"
    "Elevation,US_Topo_2020,LF2020_Elev_220_CONUS.zip"

    # Canopy (2024) - US_250 folder
    "Canopy Height,US_250,LF2024_CH_250_CONUS.zip"
    "Canopy Base Height,US_250,LF2024_CBH_250_CONUS.zip"
    "Canopy Bulk Density,US_250,LF2024_CBD_250_CONUS.zip"
    "Canopy Cover,US_250,LF2024_CC_250_CONUS.zip"

    # Fuel (2024)
    "Fuel Model FBFM13,CONUS_LF2024,LF2024_FBFM13_CONUS.zip"

    # Vegetation (2024)
    "Existing Veg Type,CONUS_LF2024,LF2024_EVT_CONUS.zip"
    "Existing Veg Cover,CONUS_LF2024,LF2024_EVC_CONUS.zip"
    "Existing Veg Height,CONUS_LF2024,LF2024_EVH_CONUS.zip"

    # Vegetation (2020)
    "Biophysical Settings,CONUS_LF2020,LF2020_BPS_CONUS.zip"

    # Fire Regime (2016)
    "Fire Regime Groups,CONUS_LF2016,LF2016_FRG_CONUS.zip"
    "Fire Return Interval,CONUS_LF2016,LF2016_FRI_CONUS.zip"
    "Percent Fire Severity,CONUS_LF2016,LF2016_PFS_CONUS.zip"

    # Fire Regime (2024)
    "Vegetation Departure,CONUS_LF2024,LF2024_VDep_CONUS.zip"
    "Vegetation Condition Class,CONUS_LF2024,LF2024_VCC_CONUS.zip"
    "Succession Classes,CONUS_LF2024,LF2024_SClass_CONUS.zip"

    # Disturbance (2024)
    "Fuel Disturbance,CONUS_LF2024,LF2024_FDist_CONUS.zip"
)

mkdir -p "$DOWNLOAD_DIR"

echo "============================================"
echo "LANDFIRE to S3 Sync"
echo "============================================"
echo "Bucket: $S3_BUCKET"
echo "Profile: $AWS_PROFILE"
echo ""

for layer_info in "${LAYERS[@]}"; do
    IFS=',' read -r name folder zip_name <<< "$layer_info"

    zip_url="${BASE_URL}/${folder}/${zip_name}"
    zip_path="${DOWNLOAD_DIR}/${zip_name}"
    extract_dir="${DOWNLOAD_DIR}/${name// /_}"

    echo "----------------------------------------"
    echo "Layer: $name"
    echo "URL: $zip_url"

    # Extract layer code from zip name for S3 check (e.g., LF2020_SlpD_220 -> SlpD)
    layer_code=$(echo "$zip_name" | sed -E 's/LF[0-9]+_([A-Za-z0-9]+)_.*/\1/')

    # Check if already in S3
    existing=$(aws s3 ls "${S3_BUCKET}/" --profile "$AWS_PROFILE" 2>/dev/null | grep -i "_${layer_code}_" | head -1 || true)
    if [ -n "$existing" ]; then
        echo "✓ Already in S3: $existing"
        echo "  Skipping..."
        continue
    fi

    # Download
    if [ ! -f "$zip_path" ]; then
        echo "Downloading..."
        curl -L -o "$zip_path" "$zip_url" --progress-bar
    else
        echo "✓ ZIP already downloaded"
    fi

    # Extract
    echo "Extracting..."
    rm -rf "$extract_dir"
    unzip -q "$zip_path" -d "$extract_dir"

    # Find and upload TIF
    tif_file=$(find "$extract_dir" -name "*.tif" -type f | head -1)
    if [ -z "$tif_file" ]; then
        echo "✗ No TIF found in archive!"
        continue
    fi

    tif_name=$(basename "$tif_file")
    echo "Uploading: $tif_name"
    aws s3 cp "$tif_file" "${S3_BUCKET}/${tif_name}" --profile "$AWS_PROFILE"

    # Upload .ovr if exists (overviews)
    ovr_file="${tif_file}.ovr"
    if [ -f "$ovr_file" ]; then
        echo "Uploading overviews: ${tif_name}.ovr"
        aws s3 cp "$ovr_file" "${S3_BUCKET}/${tif_name}.ovr" --profile "$AWS_PROFILE"
    fi

    echo "✓ Done: $name"

    # Cleanup extracted files (keep ZIP for resume)
    rm -rf "$extract_dir"
done

echo ""
echo "============================================"
echo "Sync complete!"
echo "============================================"
echo ""
echo "S3 contents:"
aws s3 ls "${S3_BUCKET}/" --profile "$AWS_PROFILE" --human-readable

echo ""
echo "To cleanup downloaded ZIPs:"
echo "  rm -rf $DOWNLOAD_DIR"
