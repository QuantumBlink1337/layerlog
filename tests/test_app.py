import json

from fastapi.testclient import TestClient

from layerlog.app import create_app


def saved(tmp_path):
    files = sorted(tmp_path.glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text())


def test_healthz(tmp_path):
    assert TestClient(create_app(tmp_path)).get("/healthz").json() == {"status": "ok"}


def test_captures_json_body_and_headers(tmp_path):
    client = TestClient(create_app(tmp_path))
    r = client.post("/hooks/bambuddy?x=1", json={"event": "print_started", "archive_id": 1})
    assert r.status_code == 200 and r.json()["ok"]
    rec = saved(tmp_path)
    assert rec["body"] == {"event": "print_started", "archive_id": 1}
    assert rec["query"] == {"x": "1"}
    assert rec["headers"]["content-type"] == "application/json"
    assert rec["secret_configured"] is False and rec["secret_matched_header"] is None


def test_non_json_body_is_kept_as_text(tmp_path):
    TestClient(create_app(tmp_path)).post("/hooks/bambuddy", content="hello", headers={"content-type": "text/plain"})
    assert saved(tmp_path)["body"] == "hello"


def test_secret_header_is_identified_and_redacted(tmp_path):
    client = TestClient(create_app(tmp_path, secret="s3cret-value"))
    client.post("/hooks/bambuddy", json={}, headers={"X-Webhook-Secret": "s3cret-value"})
    rec = saved(tmp_path)
    assert rec["secret_matched_header"] == "x-webhook-secret"
    assert "s3cret-value" not in json.dumps(rec)


def test_wrong_secret_is_still_captured_but_not_matched(tmp_path):
    # Capture mode must never drop a request: we're here to learn what arrives.
    client = TestClient(create_app(tmp_path, secret="right"))
    assert client.post("/hooks/bambuddy", json={}, headers={"X-Webhook-Secret": "wrong"}).status_code == 200
    rec = saved(tmp_path)
    assert rec["secret_configured"] is True and rec["secret_matched_header"] is None


def test_authorization_header_is_always_redacted(tmp_path):
    TestClient(create_app(tmp_path)).post("/hooks/bambuddy", json={}, headers={"Authorization": "Bearer abc123"})
    assert "abc123" not in json.dumps(saved(tmp_path))
