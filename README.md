# Ember

Direct API proxy for wildfire data - feeds Nova frontend.

## Endpoints

- `/api/v1/fires` - NASA FIRMS fire detections
- `/api/v1/geocode` - Nominatim geocoding
- `/api/v1/weather` - Open-Meteo weather
- `/api/v1/fuel` - LANDFIRE fuel models
- `/api/v1/vegetation` - Copernicus NDVI/NDMI

## Run

```bash
cp .env.example .env
# fill in values
uv run python entrypoint.py
```

## Docker

```bash
docker compose up --build
```
