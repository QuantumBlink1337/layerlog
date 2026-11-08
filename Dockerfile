FROM python:3.12-slim AS base
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tzdata \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# `docker build --target test .` runs the suite against this image's ffmpeg (mpjpeg
# recording with wallclock timestamps, ass/drawtext rendering, concat). The build
# fails if any test fails; the plain build below skips this stage.
FROM base AS test
RUN pip install --no-cache-dir 'pytest>=8'
COPY tests ./tests
RUN ffmpeg -version | head -n1 && python -m pytest -q -rs -p no:cacheprovider

FROM base
# Telemetry is small and written every 2 s: keep it on the cache (appdata).
# Raw recordings (~3.7 GB/hour) and renders (kept forever) go to the array.
ENV TELEMETRY_DIR=/data/telemetry RECORDINGS_DIR=/recordings RENDERS_DIR=/renders \
    PYTHONUNBUFFERED=1 XDG_CACHE_HOME=/tmp
VOLUME ["/data", "/recordings", "/renders"]
CMD ["python", "-m", "layerlog.surveillance"]
