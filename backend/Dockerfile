FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared package first so that backend's pyproject.toml can
# reference it. The build context is the project root (set in docker-compose.yml).
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared/

# Install backend Python dependencies.
# Copy only pyproject.toml first to benefit from Docker layer caching:
# dependencies are reinstalled only when pyproject.toml changes.
COPY backend/pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir .

# Copy application source code.
COPY backend/app/ /app/app/

# Pre-create data directories so the app can write without extra configuration.
RUN mkdir -p \
    /data/sqlite \
    /data/media/uploads \
    /data/media/instrumental \
    /data/media/clips \
    /data/media/catalog \
    /data/models

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
