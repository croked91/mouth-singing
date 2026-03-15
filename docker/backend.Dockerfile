FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Shared package
COPY shared/pyproject.toml /shared/pyproject.toml
COPY shared/karaoke_shared/ /shared/karaoke_shared/
RUN pip install --no-cache-dir /shared/

# Backend dependencies (install from pyproject.toml first for caching)
COPY backend/pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir .

# Backend source code
COPY backend/app/ /app/app/

# Pre-create data directories
RUN mkdir -p /data/sqlite /data/media/mp3 /data/media/instrumental /data/media/clips

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
