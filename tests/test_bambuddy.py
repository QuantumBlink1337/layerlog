import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from layerlog import threemf
from layerlog.bambuddy import Archive, ArchiveNotFound, BambuddyClient, BambuddyError

FIXTURES = Path(__file__).parent / "fixtures"
ARCHIVE = json.loads((FIXTURES / "archive_1_printing.json").read_text())


def test_archive_from_saved_record():
    a = Archive.from_json(ARCHIVE)
    assert a.id == 1 and a.printer_id == 1 and a.plate_id == 2 and a.status == "printing"
    assert a.display_name == "Assembly_PLA_5h34m.gcode.3mf"
    assert a.model_key == "Assembly_PLA"
    assert not a.is_mini
    assert a.started_at == datetime(2026, 10, 18, 14, 33, 5, tzinfo=timezone.utc)
    assert a.duration_hours is None  # still printing
    assert a.est_hours == pytest.approx(20039 / 3600)


def test_completed_archive_from_real_record():
    a = Archive.from_json(json.loads((FIXTURES / "archive_1_completed.json").read_text()))
    assert a.status == "completed" and a.failure_reason is None
    assert a.model_key == "Assembly_PLA"
    assert a.started_at == datetime(2026, 10, 18, 19, 33, 28, 810912, tzinfo=timezone.utc)
    # 14:33 CT == 19:33 UTC: the naive timestamps are UTC.
    assert a.started_at.astimezone(ZoneInfo("America/Chicago")).hour == 14
    assert a.duration_hours == pytest.approx(20733 / 3600)
    # The wall-clock gap agrees with actual_time_seconds.
    assert (a.completed_at - a.started_at).total_seconds() == pytest.approx(a.actual_time_seconds, abs=1)


def test_archive_falls_back_to_filename_and_tolerates_missing_fields():
    a = Archive.from_json({"id": 7, "filename": "Thing_mini_PLA_10m.gcode.3mf.gcode.3mf"})
    assert a.display_name == a.filename and a.model_key == "Thing_mini_PLA" and a.is_mini
    assert a.started_at is None and a.status == ""


def make_client(handler) -> BambuddyClient:
    return BambuddyClient("http://nucleus:8000/", "secret", transport=httpx.MockTransport(handler))


def test_get_archive_sends_api_key_and_hits_api_v1():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["key"] = str(req.url), req.headers.get("X-API-Key")
        return httpx.Response(200, json=ARCHIVE)

    with make_client(handler) as c:
        assert c.get_archive(1).id == 1
    assert seen == {"url": "http://nucleus:8000/api/v1/archives/1", "key": "secret"}


def test_list_archives_drops_unset_params():
    def handler(req: httpx.Request) -> httpx.Response:
        assert dict(req.url.params) == {"printer_id": "1", "limit": "5"}
        return httpx.Response(200, json=[ARCHIVE])

    with make_client(handler) as c:
        assert [a.id for a in c.list_archives(printer_id=1, limit=5)] == [1]


def test_search_passes_query():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/archives/search" and req.url.params["q"] == "Assembly"
        return httpx.Response(200, json=[])

    with make_client(handler) as c:
        assert c.search("Assembly") == []


def test_download_feeds_the_3mf_parser():
    data = (FIXTURES / "assembly_petg_multicolor.3mf").read_bytes()

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/archives/1/download"
        return httpx.Response(200, content=data)

    with make_client(handler) as c:
        info = threemf.parse(c.download_archive(1))
    assert info.process_preset == "0.16mm High Quality @BBL P2S"


def test_404_and_server_errors_are_typed():
    with make_client(lambda r: httpx.Response(404)) as c:
        with pytest.raises(ArchiveNotFound):
            c.get_archive(99)
    with make_client(lambda r: httpx.Response(500)) as c:
        with pytest.raises(BambuddyError) as e:
            c.get_archive(1)
        assert e.value.status == 500 and not isinstance(e.value, ArchiveNotFound)


def test_connection_failure_is_wrapped():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with make_client(handler) as c:
        with pytest.raises(BambuddyError):
            c.get_archive(1)
