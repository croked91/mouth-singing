FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Shared package (with ML extras)
COPY shared/pyproject.toml /shared/pyproject.toml
COPY shared/karaoke_shared/ /shared/karaoke_shared/
RUN pip install --no-cache-dir "/shared[ml]"

# PyTorch CPU (needed by sentence-transformers)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Rec-service dependencies
COPY rec-service/pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir .

# Rec-service source code
COPY rec-service/app/ /app/app/

CMD ["python", "-m", "app.main"]
