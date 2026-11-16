import unittest
from brand_signal.errors import DomainError
from brand_signal.exports import Exports
from brand_signal.investigations import Investigations
from brand_signal.lifecycle import DeviceLifecycle
from brand_signal.lineage import Lineage
from brand_signal.reconciliation import Reconciliation
from support import Scenario


class LifecycleCorrectionTests(Scenario):
    def setUp(self):
        super().setUp()
        self.lifecycle = DeviceLifecycle(self.store)

    def revise(self, installed_at, expected_revision=None, note="install date corrected"):
        revision = expected_revision if expected_revision is not None else self.catalog.list_devices()[0]["revision"]
        return self.lifecycle.revise("device-1", installed_at, revision, note)

    def test_revision_requires_current_device_revision(self):
        with self.assertRaises(DomainError) as error:
            self.lifecycle.revise("device-1", "2026-10-05T00:00:00Z", 2)
        self.assertEqual(error.exception.code, "stale_device")

    def test_revision_rejects_unchanged_installation(self):
        with self.assertRaises(DomainError) as error:
            self.lifecycle.revise("device-1", "2026-10-01T00:00:00Z", 1)
        self.assertEqual(error.exception.code, "installation_unchanged")

    def test_revision_cannot_follow_retirement(self):
        self.catalog.retire("device-1", "2026-10-20T00:00:00Z", 1)
        with self.assertRaises(DomainError) as error:
            self.lifecycle.revise("device-1", "2026-10-20T00:00:00Z", 2)
        self.assertEqual(error.exception.code, "invalid_lifecycle")

    def test_late_installation_excludes_accepted_events_but_keeps_them(self):
        self.submit_ticket(at="2026-10-02T08:00:00Z")
        self.submit_meter(at="2026-10-03T08:00:00Z")
        result = self.revise("2026-10-10T00:00:00Z")
        self.assertEqual(result["revision"], 2)
        self.assertEqual([item["event_id"] for item in result["dispositions"]["excluded"]], ["ticket-1", "meter-1"])
        row = self.analytics.live("ES", "2026-10-02", "2026-10-03")["rows"]
        by_day = {item["day"]: item for item in row}
        self.assertEqual(by_day["2026-10-02"]["ticket_opened"], 0)
        self.assertEqual(by_day["2026-10-02"]["excluded_tickets"], 1)
        self.assertEqual(by_day["2026-10-03"]["telemetry_events"], 0)
        self.assertEqual(by_day["2026-10-03"]["excluded_telemetry"], 1)
        # 原始事件仍然可查询，没有被静默删除。
        self.assertEqual(self.service.ticket("ticket-1")["opened_at"], "2026-10-02T08:00:00+00:00")
        self.assertEqual(len(self.telemetry.list_for_device("device-1")), 1)
        dispositions = self.lifecycle.dispositions("device-1")
        self.assertEqual({item["disposition"] for item in dispositions["dispositions"]}, {"excluded"})
        self.assertEqual(dispositions["count"], 2)

    def test_early_installation_restores_previously_excluded_events(self):
        self.submit_ticket(at="2026-10-02T08:00:00Z")
        self.revise("2026-10-10T00:00:00Z")
        self.assertEqual(self.analytics.live("ES", "2026-10-02", "2026-10-02")["rows"][0]["ticket_opened"], 0)
        restored = self.revise("2026-09-20T00:00:00Z", expected_revision=2)
        self.assertEqual([item["event_id"] for item in restored["dispositions"]["restored"]], ["ticket-1"])
        row = self.analytics.live("ES", "2026-10-02", "2026-10-02")["rows"][0]
        self.assertEqual(row["ticket_opened"], 1)
        self.assertEqual(row["excluded_tickets"], 0)

    def test_impact_reports_events_metrics_candidates_and_publications(self):
        self.submit_ticket(at="2026-10-02T08:00:00Z")
        self.submit_meter(at="2026-10-02T09:00:00Z")
        self.release(day="2026-10-02")
        candidate = self.publication.prepare("ES", "2026-10-02", "2026-10-02", 1)
        report = self.lifecycle.impact("device-1", "2026-10-10T00:00:00Z", "2026-10-02", "2026-10-02")
        self.assertEqual(report["summary"]["tickets_affected"], 1)
        self.assertEqual(report["summary"]["telemetry_affected"], 1)
        self.assertEqual(report["summary"]["events_excluded"], 2)
        self.assertEqual(report["summary"]["days_changed"], 1)
        changed = report["metric_changes"][0]
        self.assertEqual(changed["before"]["ticket_opened"], 1)
        self.assertEqual(changed["after"]["ticket_opened"], 0)
        self.assertEqual(sorted(changed["before"]), sorted(changed["after"]))
        self.assertEqual(report["publications"][0]["version"], 1)
        self.assertTrue(report["publications"][0]["frozen"])
        self.assertIn(candidate["id"], [item["id"] for item in report["candidates"]])
        prepared = next(item for item in report["candidates"] if item["id"] == candidate["id"])
        self.assertTrue(prepared["ready_before_revision"])
        self.assertTrue(prepared["metrics_changed"])
        self.assertIn("digest", report)
        # 报告本身不改变任何状态，也不改变源修订。
        self.assertEqual(self.lifecycle.history("device-1")["revisions"][-1]["revision"], 1)
        self.assertEqual(self.analytics.live("ES", "2026-10-02", "2026-10-02")["rows"][0]["ticket_opened"], 1)

    def test_impact_requires_matching_range(self):
        with self.assertRaises(DomainError) as error:
            self.lifecycle.impact("device-1", "2026-10-10T00:00:00Z", "2026-10-02", None)
        self.assertEqual(error.exception.code, "invalid_range")

    def test_impact_rejects_stale_expected_revision(self):
        with self.assertRaises(DomainError) as error:
            self.lifecycle.impact("device-1", "2026-10-10T00:00:00Z", "2026-10-02", "2026-10-02", expected_revision=9)
        self.assertEqual(error.exception.code, "stale_device")

    def test_revision_invalidates_prepared_candidate(self):
        self.submit_ticket()
        candidate = self.publication.prepare("ES", "2026-10-21", "2026-10-21", 0)
        self.assertTrue(self.publication.candidate(candidate["id"])["ready"])
        self.revise("2026-10-21T12:00:00Z")
        snapshot = self.publication.candidate(candidate["id"])
        self.assertFalse(snapshot["ready"])
        with self.assertRaises(DomainError) as error:
            self.publication.publish(candidate["id"], 0, candidate["source_revision"])
        self.assertEqual(error.exception.code, "stale_source")

    def test_publication_history_stays_frozen_and_new_version_reflects_correction(self):
        self.submit_ticket(at="2026-10-02T08:00:00Z")
        self.submit_meter(at="2026-10-02T09:00:00Z")
        self.release(day="2026-10-02")
        frozen = self.publication.read("ES", 1)
        self.assertEqual(frozen["rows"][0]["ticket_opened"], 1)
        self.revise("2026-10-10T00:00:00Z")
        self.release(day="2026-10-02", expected_head=1)
        self.assertEqual(self.publication.read("ES", 1), frozen)
        self.assertTrue(self.publication.read("ES", 1)["digest_verified"])
        release = self.publication.read("ES", 2)
        self.assertEqual(release["rows"][0]["ticket_opened"], 0)
        self.assertEqual(release["rows"][0]["excluded_tickets"], 1)
        self.assertTrue(release["digest_verified"])
        diff = self.publication.diff("ES", 1, 2)
        self.assertTrue(diff["verifiable"])
        self.assertIn("ticket_opened", diff["changes"][0]["fields_changed"])
        self.assertEqual(diff["changes"][0]["before"]["ticket_opened"], 1)
        self.assertEqual(diff["changes"][0]["after"]["ticket_opened"], 0)
        csv_text = self.publication.export_csv("ES", 2)
        self.assertIn("excluded_tickets", csv_text)

    def test_lineage_at_old_revision_ignores_later_correction(self):
        self.submit_ticket(at="2026-10-02T08:00:00Z")
        self.release(day="2026-10-02")
        self.revise("2026-10-10T00:00:00Z")
        lineage = Lineage(self.store)
        old = lineage.explain("ES", 1, "2026-10-02", "dryer")
        self.assertEqual(old["devices"][0]["installed_at"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(old["devices"][0]["revision"], 1)
        self.assertEqual(len(old["source_rows"]), 1)
        self.assertEqual(old["excluded_source_rows"], [])
        self.release(day="2026-10-02", expected_head=1)
        new = lineage.explain("ES", 2, "2026-10-02", "dryer")
        self.assertEqual(new["devices"][0]["installed_at"], "2026-10-10T00:00:00+00:00")
        self.assertEqual(new["devices"][0]["revision"], 2)
        self.assertEqual(new["source_rows"], [])
        self.assertEqual([row["reason"] for row in new["excluded_source_rows"]], ["before_installation"])
        self.assertTrue(new["digest_verified"])

    def test_exclusions_list_is_auditable(self):
        self.submit_ticket(at="2026-10-02T08:00:00Z")
        self.submit_meter(at="2026-10-02T09:00:00Z")
        self.revise("2026-10-10T00:00:00Z")
        report = self.lifecycle.exclusions("ES", "2026-10-02", "2026-10-02")
        self.assertEqual(report["count"], 2)
        self.assertEqual({item["source"] for item in report["exclusions"]}, {"tickets", "telemetry"})
        self.assertTrue(all(item["reason"] == "before_installation" for item in report["exclusions"]))

    def test_exclusions_can_be_restored_from_source_revision(self):
        self.submit_ticket(at="2026-10-02T08:00:00Z")
        before = self.lifecycle.exclusions("ES", "2026-10-02", "2026-10-02")
        self.assertEqual(before["count"], 0)
        revised = self.revise("2026-10-10T00:00:00Z")
        after = self.lifecycle.exclusions("ES", "2026-10-02", "2026-10-02", source_revision=revised["source_revision"])
        self.assertEqual(after["count"], 1)
        original = self.lifecycle.exclusions("ES", "2026-10-02", "2026-10-02", source_revision=revised["source_revision"] - 1)
        self.assertEqual(original["count"], 0)

    def test_history_records_each_lifecycle_change(self):
        self.revise("2026-10-05T00:00:00Z", note="field ticket 4711")
        self.revise("2026-10-06T00:00:00Z", expected_revision=2, note="partner confirmed")
        history = self.lifecycle.history("device-1")
        self.assertEqual([item["action"] for item in history["revisions"]], ["installed", "installation_corrected", "installation_corrected"])
        self.assertEqual(history["revisions"][2]["previous_installed_at"], "2026-10-05T00:00:00+00:00")
        self.assertEqual(history["revisions"][2]["note"], "partner confirmed")

    def test_audit_trail_records_correction_and_dispositions(self):
        self.submit_ticket(at="2026-10-02T08:00:00Z")
        self.revise("2026-10-10T00:00:00Z")
        entries = self.store.audit_trail("device-1")
        revised = next(item for item in entries if item["action"] == "device_installation_revised")
        self.assertEqual(revised["details"]["excluded_events"], 1)
        self.assertEqual(revised["details"]["excluded"][0]["event_id"], "ticket-1")
        self.assertEqual(revised["details"]["note"], "install date corrected")

    def test_retirement_after_correction_still_bounds_events(self):
        self.submit_ticket(at="2026-10-21T10:00:00Z")
        self.revise("2026-10-02T00:00:00Z")
        self.catalog.retire("device-1", "2026-10-21T09:00:00Z", 2)
        row = self.analytics.live("ES", "2026-10-21", "2026-10-21")["rows"][0]
        self.assertEqual(row["ticket_opened"], 0)
        self.assertEqual(row["excluded_tickets"], 1)

    def test_export_and_investigation_still_reference_frozen_versions(self):
        self.submit_ticket(at="2026-10-02T08:00:00Z")
        self.release(day="2026-10-02")
        exports = Exports(self.store)
        created = exports.create("ES", 1, "json")
        self.revise("2026-10-10T00:00:00Z")
        self.release(day="2026-10-02", expected_head=1)
        exported = exports.read(created["id"])
        self.assertTrue(exported["verified"])
        self.assertEqual(exported["version"], 1)
        self.assertIn('"ticket_opened":1', exported["payload"])

    def test_impact_only_attributes_this_device_correction(self):
        self.revise("2026-10-04T00:00:00Z")
        self.submit_ticket(at="2026-10-04T08:00:00Z")
        self.catalog.install("device-2", "ES", "dryer", "2026-10-03T00:00:00Z")
        self.intake.submit("telemetry", "other", [{"event_key": "other-1", "id": "other-meter", "device_id": "device-2", "observed_at": "2026-10-03T09:00:00Z", "run_hours": 3, "downtime_hours": 0}])
        report = self.lifecycle.impact("device-1", "2026-10-10T00:00:00Z", "2026-10-02", "2026-10-04")
        changed_days = [item["day"] for item in report["metric_changes"]]
        # 10-03 只有 device-2 的计量数据，不属于本次修订的影响。
        self.assertEqual(changed_days, ["2026-10-04"])
        changed = report["metric_changes"][0]
        self.assertEqual(changed["before"]["active_devices"], 2)
        self.assertEqual(changed["after"]["active_devices"], 1)
        self.assertEqual(changed["before"]["telemetry_events"], changed["after"]["telemetry_events"])

    def test_reconciliation_surfaces_excluded_events(self):
        self.submit_ticket(at="2026-10-02T08:00:00Z")
        self.revise("2026-10-10T00:00:00Z")
        report = Reconciliation(self.store).inspect("ES", "2026-10-02", "2026-10-02")
        kinds = [item["kind"] for item in report["issues"]]
        self.assertIn("event_outside_lifecycle", kinds)
        item = next(item for item in report["issues"] if item["kind"] == "event_outside_lifecycle")
        self.assertEqual(item["event"], "ticket-1")
        self.assertEqual(item["reason"], "before_installation")

    def test_alert_severity_is_low_for_lifecycle_exclusions(self):
        self.submit_ticket(at="2026-10-02T08:00:00Z")
        self.revise("2026-10-10T00:00:00Z")
        revision = self.analytics.live("ES", "2026-10-02", "2026-10-02")["source_revision"]
        from brand_signal.alerts import Alerts
        result = Alerts(self.store).evaluate("ES", "2026-10-02", "2026-10-02", revision)
        lifecycle_alerts = [item for item in result["alerts"] if item["kind"] == "event_outside_lifecycle"]
        self.assertEqual([item["severity"] for item in lifecycle_alerts], ["low"])

    def test_rollup_sums_exclusion_audit_fields(self):
        self.submit_ticket(at="2026-10-02T08:00:00Z")
        self.revise("2026-10-10T00:00:00Z")
        self.release(day="2026-10-02")
        from brand_signal.rollups import Rollups
        rollups = Rollups(self.store)
        rollups.build("ES", 1)
        self.assertEqual(rollups.read("ES", 1)[0]["excluded_tickets"], 1)

    def test_concurrent_attempt_is_reported_and_audited(self):
        first = self.revise("2026-10-05T00:00:00Z")
        with self.assertRaises(DomainError) as error:
            self.revise("2026-10-06T00:00:00Z", expected_revision=1)
        self.assertEqual(error.exception.code, "stale_device")
        self.assertEqual(self.catalog.list_devices()[0]["installed_at"], first["installed_at"])
        entries = self.store.audit_trail("device-1")
        conflicts = [item for item in entries if item["action"] == "device_revision_conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["details"]["expected_revision"], 1)
        self.assertEqual(conflicts[0]["details"]["actual_revision"], 2)
        self.assertEqual(conflicts[0]["details"]["attempted_installed_at"], "2026-10-06T00:00:00+00:00")
