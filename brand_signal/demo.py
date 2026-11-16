"""跨市场设备服务质量分析：demo模块。"""
import json
import tempfile
from pathlib import Path
from .analytics import Analytics
from .catalog import Catalog
from .intake import Intake
from .lifecycle import DeviceLifecycle
from .publication import Publication
from .service import Service
from .store import Store


def run() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "demo.sqlite3")
        catalog = Catalog(store)
        intake = Intake(store)
        service = Service(store)
        publication = Publication(store)
        lifecycle = DeviceLifecycle(store)
        catalog.add_market("ES", "Europe/Madrid")
        catalog.add_product("dryer", "Synthetic Drying Unit")
        catalog.install("unit-1", "ES", "dryer", "2026-10-01T08:00:00Z")
        batch = intake.submit("tickets", "service-upload-1", [{"event_key": "ticket-1", "id": "case-1", "device_id": "unit-1", "opened_at": "2026-10-20T22:30:00Z", "severity": "critical"}, {"event_key": "bad-1", "id": "case-bad", "device_id": "missing", "opened_at": "2026-10-20T22:30:00Z"}])
        assert batch["accepted"] == 1 and batch["rejected"] == 1
        assert intake.submit("tickets", "service-upload-1", [{"event_key": "ticket-1", "id": "case-1", "device_id": "unit-1", "opened_at": "2026-10-20T22:30:00Z", "severity": "critical"}, {"event_key": "bad-1", "id": "case-bad", "device_id": "missing", "opened_at": "2026-10-20T22:30:00Z"}])["replay"]
        service.transition("case-1", "acknowledge", "2026-10-21T00:30:00Z", 1)
        service.transition("case-1", "resolve", "2026-10-21T02:00:00Z", 2)
        service.transition("case-1", "close", "2026-10-21T03:00:00Z", 3)
        intake.submit("telemetry", "meter-upload-1", [{"event_key": "meter-1", "id": "observation-1", "device_id": "unit-1", "observed_at": "2026-10-21T05:00:00Z", "run_hours": 15, "downtime_hours": 2}])
        candidate = publication.prepare("ES", "2026-10-21", "2026-10-21", 0)
        first = publication.publish(candidate["id"], 0, candidate["source_revision"])
        before = publication.read("ES", 1)["rows"]
        intake.submit("telemetry", "meter-upload-2", [{"event_key": "meter-2", "id": "observation-2", "device_id": "unit-1", "observed_at": "2026-10-21T10:00:00Z", "run_hours": 2, "downtime_hours": 1}])
        next_candidate = publication.prepare("ES", "2026-10-21", "2026-10-21", 1)
        second = publication.publish(next_candidate["id"], 1, next_candidate["source_revision"])
        after = publication.read("ES", 1)["rows"]
        assert before == after
        delta = publication.diff("ES", 1, 2)["changes"]
        assert delta and delta[0]["before"]["telemetry_events"] == 1 and delta[0]["after"]["telemetry_events"] == 2
        # 更正安装日期：旧发布版本保持冻结，新版本反映更正后的生命周期。
        revision = catalog.list_devices()[0]["revision"]
        impact = lifecycle.impact("unit-1", "2026-10-21T06:00:00Z", expected_revision=revision)
        corrected = lifecycle.revise("unit-1", "2026-10-21T06:00:00Z", revision, "installation date corrected")
        third_candidate = publication.prepare("ES", "2026-10-21", "2026-10-21", 2)
        third = publication.publish(third_candidate["id"], 2, third_candidate["source_revision"])
        corrected_delta = publication.diff("ES", 2, 3)
        assert publication.read("ES", 2)["rows"][0] == delta[0]["after"]
        assert publication.read("ES", 3)["rows"][0]["excluded_telemetry"] >= 1
        return {
            "accepted": batch["accepted"],
            "rejected": batch["rejected"],
            "versions": [first["version"], second["version"], third["version"]],
            "old_unchanged": before == after,
            "changed_rows": len(delta),
            "correction_revision": corrected["revision"],
            "correction_excluded_events": len(corrected["dispositions"]["excluded"]),
            "correction_impact_events": impact["summary"]["events_excluded"],
            "latest_excluded_telemetry": publication.read("ES", 3)["rows"][0]["excluded_telemetry"],
            "corrected_diff_verifiable": corrected_delta["verifiable"],
            "source_revision": Analytics(store).live("ES", "2026-10-21", "2026-10-21")["source_revision"],
        }


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
