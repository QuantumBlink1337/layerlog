"""Webhook receiver.

For now this only *captures*: every request to ``/hooks/bambuddy`` is saved
as a JSON file so we can see Bambuddy's real payloads before writing the
handler. Secret-bearing headers are redacted in the saved files.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

log = logging.getLogger("layerlog")

MAX_BODY_BYTES = 10 * 1024 * 1024
_SENSITIVE_HEADERS = {"authorization", "x-api-key", "x-webhook-secret", "cookie"}


def _redact(headers: dict[str, str], secret: str | None) -> tuple[dict[str, str], str | None]:
    """Redact sensitive headers; also report which header (if any) carried our secret."""
    out: dict[str, str] = {}
    matched: str | None = None
    for name, value in headers.items():
        if secret and hmac.compare_digest(value.encode(), secret.encode()):
            matched = name
        sensitive = name.lower() in _SENSITIVE_HEADERS or name == matched
        out[name] = f"<redacted, {len(value)} chars>" if sensitive else value
    return out, matched


def create_app(capture_dir: Path | str, secret: str | None = None) -> FastAPI:
    capture_dir = Path(capture_dir)
    app = FastAPI(title="layerlog")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/hooks/bambuddy")
    async def bambuddy_hook(request: Request) -> dict[str, Any]:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "payload too large")
        headers, matched = _redact(dict(request.headers), secret)
        try:
            parsed: Any = json.loads(body)
        except ValueError:
            parsed = body.decode("utf-8", errors="replace")

        now = datetime.now(timezone.utc)
        record = {
            "received_at": now.isoformat(),
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "secret_configured": bool(secret),
            "secret_matched_header": matched,
            "headers": headers,
            "body": parsed,
        }
        capture_dir.mkdir(parents=True, exist_ok=True)
        path = capture_dir / f"{now:%Y%m%dT%H%M%S%f}_{uuid.uuid4().hex[:6]}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        log.info("captured webhook -> %s (%d bytes)", path.name, len(body))
        return {"ok": True, "saved": path.name}

    return app


app = create_app(os.environ.get("CAPTURE_DIR", "captures"), os.environ.get("WEBHOOK_SECRET") or None)
