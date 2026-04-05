FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Shared deps with ML extras (cached while pyproject.toml unchanged)
COPY shared/pyproject.toml /shared/pyproject.toml
RUN mkdir -p /shared/karaoke_shared && touch /shared/karaoke_shared/__init__.py \
    && pip install --no-cache-dir -e "/shared[ml]"

# PyTorch CPU (needed by sentence-transformers)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Rec-service deps (cached while pyproject.toml unchanged)
COPY rec-service/pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir .

# Source code (changes here don't rebuild deps)
COPY shared/karaoke_shared/ /shared/karaoke_shared/
COPY rec-service/app/ /app/app/

CMD ["python", "-m", "app.main"]
