"""Thin client for Bambuddy's REST API (archives and live printer status).

Shared by the logger and surveillance components. Paths like ``/archives``
are HTTP endpoints. We never read the archive's ``file_path``; the 3MF always
comes through ``/archives/{id}/download``.
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


class NotFound(BambuddyError):
    pass


class ArchiveNotFound(NotFound):
    pass


class PrinterNotFound(NotFound):
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


@dataclass
class HmsError:
    full_code: str
    severity: int | None
    description: str | None
    job_id: str | None


@dataclass
class PrinterStatus:
    """One poll of ``/printers/{id}/status``, reduced to what we log."""

    id: int
    connected: bool
    state: str  # e.g. "RUNNING"
    stage: str | None  # stg_cur_name, e.g. "Printing"
    current_print: str | None
    archive_id: int | None  # current_archive_id: links a poll to its Archive
    plate_id: int | None
    progress: float | None  # percent
    remaining_min: float | None  # remaining_time is minutes (Bambu mc_remaining_time)
    layer: int | None
    total_layers: int | None
    nozzle_temp: float | None
    nozzle_target: float | None
    bed_temp: float | None
    bed_target: float | None
    chamber_temp: float | None
    fan_part: int | None  # cooling_fan_speed, percent
    fan_aux: int | None  # big_fan1_speed, Bambu's aux fan
    fan_chamber: int | None  # big_fan2_speed, Bambu's chamber fan
    hms_errors: list[HmsError]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> PrinterStatus:
        t = d.get("temperatures") or {}
        return cls(
            id=d["id"],
            connected=bool(d.get("connected")),
            state=d.get("state") or "",
            stage=d.get("stg_cur_name"),
            current_print=d.get("current_print"),
            archive_id=d.get("current_archive_id"),
            plate_id=d.get("current_plate_id"),
            progress=d.get("progress"),
            remaining_min=d.get("remaining_time"),
            layer=d.get("layer_num"),
            total_layers=d.get("total_layers"),
            nozzle_temp=t.get("nozzle"),
            nozzle_target=t.get("nozzle_target"),
            bed_temp=t.get("bed"),
            bed_target=t.get("bed_target"),
            chamber_temp=t.get("chamber"),
            fan_part=d.get("cooling_fan_speed"),
            fan_aux=d.get("big_fan1_speed"),
            fan_chamber=d.get("big_fan2_speed"),
            hms_errors=[HmsError(full_code=e.get("full_code") or "", severity=e.get("severity"),
                                 description=e.get("description"), job_id=e.get("job_id"))
                        for e in d.get("hms_errors") or []],
            raw=d,
        )


class BambuddyClient:
    def __init__(self, base_url: str, api_key: str | None = None, *, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"X-API-Key": api_key} if api_key else {},  # omitted when Bambuddy auth is off
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> BambuddyClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, *, not_found: type[NotFound] = ArchiveNotFound,
                 **kw: Any) -> httpx.Response:
        try:
            resp = self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise BambuddyError(f"{method} {path} failed: {e}") from e
        if resp.status_code == 404:
            raise not_found(f"{path} not found", 404)
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

    def camera_stream_url(self, printer_id: int) -> str:
        """MJPEG stream URL for an external reader such as ffmpeg (needs a stream token once auth is on)."""
        return str(self._http.base_url.join(f"printers/{printer_id}/camera/stream"))

    def printer_status(self, printer_id: int) -> PrinterStatus:
        resp = self._request("GET", f"/printers/{printer_id}/status", not_found=PrinterNotFound)
        return PrinterStatus.from_json(resp.json())
