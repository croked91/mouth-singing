FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Shared deps (cached while pyproject.toml unchanged)
COPY shared/pyproject.toml /shared/pyproject.toml
RUN mkdir -p /shared/karaoke_shared && touch /shared/karaoke_shared/__init__.py \
    && pip install --no-cache-dir -e /shared/

# Backend deps (cached while pyproject.toml unchanged)
COPY backend/pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir .

# Source code (changes here don't rebuild deps)
COPY shared/karaoke_shared/ /shared/karaoke_shared/
COPY backend/app/ /app/app/

RUN mkdir -p /data/sqlite /data/media/mp3 /data/media/instrumental /data/media/clips

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
