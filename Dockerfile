FROM python:3.13-slim

WORKDIR /app

# Install system dependencies for GDAL/rasterio
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev \
    libexpat1 \
    libproj-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy project files
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY entrypoint.py ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Expose port
EXPOSE 8001

# Run via entrypoint (configures logging before app import)
CMD ["uv", "run", "python", "entrypoint.py"]
