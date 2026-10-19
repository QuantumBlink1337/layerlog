"""Thin client for Bambuddy's REST API (archives only).

Paths like ``/archives`` are HTTP endpoints. We never read the archive's
``file_path``; the 3MF always comes through ``/archives/{id}/download``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from . import names


class BambuddyError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ArchiveNotFound(BambuddyError):
    pass


def _dt(value: Any) -> datetime | None:
    """Bambuddy times are naive ISO strings in UTC (confirmed against a real archive)."""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Archive:
    id: int
    printer_id: int | None
    filename: str
    print_name: str | None
    content_hash: str | None
    plate_id: int | None
    status: str
    started_at: datetime | None
    completed_at: datetime | None  # a placeholder while status == "printing"
    print_time_seconds: float | None  # estimated
    actual_time_seconds: float | None
    failure_reason: str | None
    thumbnail_path: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Archive:
        return cls(
            id=d["id"],
            printer_id=d.get("printer_id"),
            filename=d.get("filename") or "",
            print_name=d.get("print_name"),
            content_hash=d.get("content_hash"),
            plate_id=d.get("plate_id"),
            status=d.get("status") or "",
            started_at=_dt(d.get("started_at")),
            completed_at=_dt(d.get("completed_at")),
            print_time_seconds=d.get("print_time_seconds"),
            actual_time_seconds=d.get("actual_time_seconds"),
            failure_reason=d.get("failure_reason"),
            thumbnail_path=d.get("thumbnail_path"),
            raw=d,
        )

    @property
    def display_name(self) -> str:
        return self.print_name or self.filename

    @property
    def model_key(self) -> str:
        return names.model_key(self.display_name)

    @property
    def is_mini(self) -> bool:
        return names.is_mini(self.display_name) or names.is_mini(self.filename)

    @property
    def duration_hours(self) -> float | None:
        s = self.actual_time_seconds
        return None if s is None else s / 3600

    @property
    def est_hours(self) -> float | None:
        s = self.print_time_seconds
        return None if s is None else s / 3600


class BambuddyClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"X-API-Key": api_key},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> BambuddyClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            resp = self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise BambuddyError(f"{method} {path} failed: {e}") from e
        if resp.status_code == 404:
            raise ArchiveNotFound(f"{path} not found", 404)
        if resp.is_error:
            raise BambuddyError(f"{method} {path} -> HTTP {resp.status_code}", resp.status_code)
        return resp

    def list_archives(self, *, printer_id: int | None = None, project_id: int | None = None,
                      date_from: str | None = None, date_to: str | None = None,
                      limit: int | None = None, offset: int | None = None) -> list[Archive]:
        params = {k: v for k, v in dict(printer_id=printer_id, project_id=project_id,
                                        date_from=date_from, date_to=date_to,
                                        limit=limit, offset=offset).items() if v is not None}
        return [Archive.from_json(d) for d in self._request("GET", "/archives", params=params).json()]

    def search(self, q: str) -> list[Archive]:
        return [Archive.from_json(d) for d in self._request("GET", "/archives/search", params={"q": q}).json()]

    def get_archive(self, archive_id: int) -> Archive:
        return Archive.from_json(self._request("GET", f"/archives/{archive_id}").json())

    def download_archive(self, archive_id: int) -> bytes:
        """The archived 3MF (can be tens of MB)."""
        return self._request("GET", f"/archives/{archive_id}/download").content
