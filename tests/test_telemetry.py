import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from layerlog.bambuddy import BambuddyClient, PrinterStatus
from layerlog.surveillance.telemetry import TelemetryLog, run

FIXTURES = Path(__file__).parent / "fixtures"
STATUS = json.loads((FIXTURES / "printer_1_status_printing.json").read_text())
T0 = datetime(2026, 11, 8, 21, 0, tzinfo=timezone.utc)


def status(**over) -> PrinterStatus:
    return PrinterStatus.from_json({**STATUS, **over})


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_poll_is_written_to_its_archive_file_without_raw(tmp_path):
    path = TelemetryLog(tmp_path).observe(status(), T0)
    assert path == tmp_path / "archive_27.jsonl"
    [rec] = lines(path)
    assert rec["ts"] == "2026-11-08T21:00:00+00:00"
    assert rec["layer"] == 553 and rec["state"] == "RUNNING" and rec["archive_id"] == 27
    assert rec["hms_errors"][0]["full_code"] == "0501040000030002"
    assert "raw" not in rec


def test_parked_printer_logs_the_transition_once(tmp_path):
    tlog = TelemetryLog(tmp_path)
    tlog.observe(status(), T0)
    tlog.observe(status(layer_num=554), T0 + timedelta(seconds=2))
    assert tlog.observe(status(state="FINISH"), T0 + timedelta(seconds=4)) is not None
    assert tlog.observe(status(state="FINISH"), T0 + timedelta(seconds=6)) is None
    assert [r["state"] for r in lines(tmp_path / "archive_27.jsonl")] == ["RUNNING", "RUNNING", "FINISH"]


def test_unknown_states_are_logged_and_no_archive_is_skipped(tmp_path):
    tlog = TelemetryLog(tmp_path)
    assert tlog.observe(status(state="PAUSE"), T0) is not None
    assert tlog.observe(status(state="PAUSE"), T0) is not None
    assert tlog.observe(status(current_archive_id=None), T0) is None


def test_run_survives_failed_polls(tmp_path):
    replies = iter([httpx.Response(200, json=STATUS), httpx.Response(500),
                    httpx.Response(200, json={**STATUS, "layer_num": 554})])
    client = BambuddyClient("http://bambuddy:8000", transport=httpx.MockTransport(lambda r: next(replies)))
    clock = iter([T0, T0 + timedelta(seconds=4)])
    with client:
        run(client, 1, TelemetryLog(tmp_path), 2, now=lambda: next(clock), sleep=lambda s: None, polls=3)
    assert [r["layer"] for r in lines(tmp_path / "archive_27.jsonl")] == [553, 554]


def test_client_omits_api_key_header_when_none():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["key"] = req.headers.get("X-API-Key")
        return httpx.Response(200, json=STATUS)

    with BambuddyClient("http://bambuddy:8000", transport=httpx.MockTransport(handler)) as c:
        c.printer_status(1)
    assert seen["key"] is None
