FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tzdata \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV TELEMETRY_DIR=/data/telemetry RECORDINGS_DIR=/data/recordings
VOLUME /data
CMD ["python", "-m", "layerlog.surveillance"]
