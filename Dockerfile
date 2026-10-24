FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV CAPTURE_DIR=/data/captures
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "layerlog.app:app", "--host", "0.0.0.0", "--port", "8000"]
