FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /worker

# Install shared package first so Docker layer cache is reused on worker-only changes.
COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/

# Install worker package.
COPY worker/pyproject.toml worker/
COPY worker/app/ worker/app/
RUN pip install --no-cache-dir /worker/

COPY worker/entrypoint.sh /worker/entrypoint.sh
RUN chmod +x /worker/entrypoint.sh

WORKDIR /worker

ENTRYPOINT ["/worker/entrypoint.sh"]
