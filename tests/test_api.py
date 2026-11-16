import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from brand_signal.api import create_app


class ApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.client = TestClient(create_app(str(Path(directory.name) / "http.sqlite3")))

    def seed(self):
        self.assertEqual(self.client.post("/markets", json={"code": "ES", "timezone": "Europe/Madrid"}).status_code, 200)
        self.assertEqual(self.client.post("/products", json={"code": "dryer", "name": "Synthetic Dryer"}).status_code, 200)
        self.assertEqual(self.client.post("/devices", json={"id": "d1", "market": "ES", "product": "dryer", "installed_at": "2026-10-01T00:00:00Z"}).status_code, 200)

    def test_http_business_journey(self):
        self.seed()
        upload = self.client.post("/ingest/tickets", json={"request_key": "r1", "rows": [{"event_key": "e1", "id": "t1", "device_id": "d1", "opened_at": "2026-10-20T22:30:00Z", "severity": "high"}]})
        self.assertEqual(upload.status_code, 200)
        self.assertEqual(upload.json()["accepted"], 1)
        revision = self.client.get("/analytics/ES", params={"from_day": "2026-10-21", "to_day": "2026-10-21"}).json()["source_revision"]
        candidate = self.client.post("/candidates", json={"market": "ES", "from_day": "2026-10-21", "to_day": "2026-10-21", "expected_head": 0}).json()
        self.assertEqual(candidate["source_revision"], revision)
        published = self.client.post(f"/candidates/{candidate['id']}/publish", json={"expected_head": 0, "expected_source_revision": revision})
        self.assertEqual(published.status_code, 200)
        frozen = self.client.get("/publications/ES/1").json()
        self.assertEqual(frozen["rows"][0]["ticket_opened"], 1)
        self.assertIn("ticket_opened", self.client.get("/publications/ES/1/csv").text)

    def test_domain_error_has_code(self):
        response = self.client.post("/markets", json={"code": "ES", "timezone": "Unreal/Zone"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_timezone")

    def test_schema_error_is_visible(self):
        response = self.client.post("/products", json={"code": "dryer"})
        self.assertEqual(response.status_code, 422)

    def test_missing_publication_is_not_live_fallback(self):
        self.seed()
        response = self.client.get("/publications/ES/1")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "unknown_publication")

    def test_http_source_replay(self):
        self.seed()
        body = {"request_key": "stable", "rows": [{"event_key": "e1", "id": "m1", "device_id": "d1", "observed_at": "2026-10-21T00:00:00Z", "run_hours": 12, "downtime_hours": 1}]}
        first = self.client.post("/ingest/telemetry", json=body)
        second = self.client.post("/ingest/telemetry", json=body)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(second.json()["replay"])
        self.assertEqual(first.json()["id"], second.json()["id"])

    def test_http_ticket_revision_conflict(self):
        self.seed()
        self.client.post("/ingest/tickets", json={"request_key": "r1", "rows": [{"event_key": "e1", "id": "t1", "device_id": "d1", "opened_at": "2026-10-20T22:30:00Z"}]})
        response = self.client.post("/tickets/t1/transitions", json={"action": "acknowledge", "at": "2026-10-21T00:00:00Z", "expected_revision": 1})
        self.assertEqual(response.status_code, 200)
        stale = self.client.post("/tickets/t1/transitions", json={"action": "resolve", "at": "2026-10-21T01:00:00Z", "expected_revision": 1})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"], "stale_ticket")

    def test_http_lifecycle_correction_flow(self):
        self.seed()
        self.client.post("/ingest/tickets", json={"request_key": "r1", "rows": [{"event_key": "e1", "id": "t1", "device_id": "d1", "opened_at": "2026-10-05T08:00:00Z"}]})
        self.client.post("/ingest/telemetry", json={"request_key": "m1", "rows": [{"event_key": "x1", "id": "o1", "device_id": "d1", "observed_at": "2026-10-05T09:00:00Z", "run_hours": 10, "downtime_hours": 1}]})
        candidate = self.client.post("/candidates", json={"market": "ES", "from_day": "2026-10-05", "to_day": "2026-10-05", "expected_head": 0}).json()
        self.client.post(f"/candidates/{candidate['id']}/publish", json={"expected_head": 0, "expected_source_revision": candidate["source_revision"]})
        preview = self.client.post("/devices/d1/lifecycle/impact", json={"installed_at": "2026-10-10T00:00:00Z", "expected_revision": 1, "from_day": "2026-10-05", "to_day": "2026-10-05"})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["summary"]["events_excluded"], 2)
        self.assertEqual(preview.json()["publications"][0]["version"], 1)
        corrected = self.client.post("/devices/d1/lifecycle", json={"installed_at": "2026-10-10T00:00:00Z", "expected_revision": 1, "note": "overseas field ticket"})
        self.assertEqual(corrected.status_code, 200)
        self.assertEqual(corrected.json()["revision"], 2)
        conflict = self.client.post("/devices/d1/lifecycle", json={"installed_at": "2026-10-11T00:00:00Z", "expected_revision": 1})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"], "stale_device")
        history = self.client.get("/devices/d1/lifecycle").json()
        self.assertEqual([item["action"] for item in history["revisions"]], ["installed", "installation_corrected"])
        excluded = self.client.get("/devices/d1/lifecycle/dispositions").json()
        self.assertEqual({item["source"] for item in excluded["dispositions"]}, {"tickets", "telemetry"})
        listed = self.client.get("/lifecycle/exclusions/ES", params={"from_day": "2026-10-05", "to_day": "2026-10-05"}).json()
        self.assertEqual(listed["count"], 2)
        self.assertEqual(self.client.get("/publications/ES/1").json()["rows"][0]["ticket_opened"], 1)
        advanced = self.client.post("/candidates", json={"market": "ES", "from_day": "2026-10-05", "to_day": "2026-10-05", "expected_head": 1}).json()
        self.client.post(f"/candidates/{advanced['id']}/publish", json={"expected_head": 1, "expected_source_revision": advanced["source_revision"]})
        latest = self.client.get("/publications/ES/2").json()
        self.assertEqual(latest["rows"][0]["ticket_opened"], 0)
        self.assertEqual(latest["rows"][0]["excluded_tickets"], 1)
        diff = self.client.get("/publications/ES/diff/1/2").json()
        self.assertTrue(diff["verifiable"])
        self.assertIn("ticket_opened", diff["changes"][0]["fields_changed"])
        lineage = self.client.get("/lineage/ES/1/2026-10-05/dryer").json()
        self.assertEqual(len(lineage["source_rows"]), 2)
        lineage_new = self.client.get("/lineage/ES/2/2026-10-05/dryer").json()
        self.assertEqual(lineage_new["source_rows"], [])
        self.assertEqual(len(lineage_new["excluded_source_rows"]), 2)
        audit = self.client.get("/audit", params={"subject": "d1"}).json()
        self.assertIn("device_revision_conflict", [item["action"] for item in audit])
