"""跨市场设备服务质量分析：lifecycle模块。

设备生命周期以登记安装时间（含）和退役时间（不含）为边界。安装时间修订后，
已接收的原始事件可能落到新的生命周期之外；本模块统一判断事件是否被计入指标，
并提供修订影响报告、事件处置记录和按源修订还原的历史生命周期快照。
"""
import hashlib
import json
from datetime import date, timedelta
from .catalog import Catalog, instant, iso, market_day
from .errors import DomainError, require
from .store import Store, now


# 旧发布版本没有这些字段；比较时补零，避免把“新增审计字段”误判为指标变化。
OPTIONAL_ZERO_FIELDS = ("excluded_tickets", "excluded_telemetry", "excluded_ticket_actions")


def lifecycle_bounds(device) -> tuple:
    """返回设备的 (安装时刻, 退役时刻或 None)。device 可以是行或映射。"""
    installed = instant(device["installed_at"])
    retired = instant(device["retired_at"]) if device["retired_at"] else None
    return installed, retired


def outside_reason(installed, retired, moment) -> str | None:
    """事件时刻落在生命周期外时返回稳定原因代码。"""
    if moment < installed:
        return "before_installation"
    if retired is not None and moment >= retired:
        return "after_retirement"
    return None


def event_reason(device, timestamp: str) -> str | None:
    installed, retired = lifecycle_bounds(device)
    return outside_reason(installed, retired, instant(timestamp))


def _reason_for_bounds(installed_at: str, retired_at: str | None, timestamp: str) -> str | None:
    installed = instant(installed_at)
    retired = instant(retired_at) if retired_at else None
    return outside_reason(installed, retired, instant(timestamp))


def _state(installed_at: str, retired_at: str | None, timestamp: str) -> dict:
    reason = _reason_for_bounds(installed_at, retired_at, timestamp)
    return {"included": reason is None, "reason": reason}


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _normalize_metric(row: dict) -> dict:
    return {**{field: 0 for field in OPTIONAL_ZERO_FIELDS}, **row}


def _metric_map(rows: list[dict], product: str) -> dict:
    return {row["day"]: _normalize_metric(row) for row in rows if row["product"] == product}


def _metric_changes(before: dict, after: dict, product: str) -> list[dict]:
    changes = []
    for day in sorted(before.keys() | after.keys()):
        left, right = before.get(day), after.get(day)
        if left == right:
            continue
        fields = sorted(field for field in (set(left or {}) | set(right or {})) if (left or {}).get(field) != (right or {}).get(field))
        changes.append({"day": day, "product": product, "fields_changed": fields, "before": left, "after": right})
    return changes


class DeviceLifecycle:
    def __init__(self, store: Store):
        self.store = store

    # -- 历史与快照 -------------------------------------------------------
    @staticmethod
    def resolved_devices(db, market: str, product: str | None = None, source_revision: int | None = None) -> dict:
        """返回设备生命周期视图；指定 source_revision 时按当时已确认的修订还原。

        在该源修订时尚未登记的设备不会被返回。
        """
        query = "SELECT d.*,m.timezone FROM devices d JOIN markets m ON m.code=d.market WHERE d.market=?"
        parameters: list = [market]
        if product is not None:
            query += " AND d.product=?"
            parameters.append(product)
        result = {}
        for row in db.execute(query + " ORDER BY d.id", parameters):
            device = dict(row)
            device["lifecycle_source_revision"] = None
            if source_revision is not None:
                revision = db.execute(
                    "SELECT * FROM device_revisions WHERE device_id=? AND source_revision<=? ORDER BY revision DESC LIMIT 1",
                    (device["id"], source_revision),
                ).fetchone()
                if revision is None:
                    continue
                device["installed_at"] = revision["installed_at"]
                device["retired_at"] = revision["retired_at"]
                device["revision"] = revision["revision"]
                device["lifecycle_source_revision"] = revision["source_revision"]
            result[device["id"]] = device
        return result

    @staticmethod
    def accepted_events(db, device_id: str) -> tuple[list[dict], list[dict]]:
        tickets = [dict(row) for row in db.execute("SELECT * FROM tickets WHERE device_id=? ORDER BY opened_at,id", (device_id,))]
        telemetry = [dict(row) for row in db.execute("SELECT * FROM telemetry WHERE device_id=? ORDER BY observed_at,id", (device_id,))]
        return tickets, telemetry

    @staticmethod
    def ticket_actions(db, device_id: str) -> list[dict]:
        # “open”动作就是工单本身，不重复计入处置动作。
        return [dict(row) for row in db.execute(
            "SELECT h.*,t.id AS ticket_id FROM ticket_history h JOIN tickets t ON t.id=h.ticket_id WHERE t.device_id=? AND h.action<>'open' ORDER BY h.ticket_id,h.revision",
            (device_id,),
        )]

    # -- 生命周期排除明细 -------------------------------------------------
    def exclusions(self, market: str, from_day: str, to_day: str, product: str | None = None, source_revision: int | None = None) -> dict:
        from .analytics import days_between
        days = set(days_between(from_day, to_day))
        with self.store.read() as db:
            require(db.execute("SELECT 1 FROM markets WHERE code=?", (market,)).fetchone(), "unknown_market", "market unknown", 404)
            devices = self.resolved_devices(db, market, product, source_revision)
            items = []
            for source, table, timestamp, identifier in (
                ("tickets", "tickets", "opened_at", "id"),
                ("telemetry", "telemetry", "observed_at", "id"),
            ):
                rows = db.execute(
                    f"SELECT * FROM {table} WHERE device_id IN (SELECT id FROM devices WHERE market=?) ORDER BY {timestamp},{identifier}",
                    (market,),
                ).fetchall()
                for row in rows:
                    device = devices.get(row["device_id"])
                    if device is None:
                        # 该设备在请求的源修订时尚未登记，不属于当时的口径。
                        continue
                    reason = event_reason(device, row[timestamp])
                    if reason is None:
                        continue
                    day = market_day(row[timestamp], device["timezone"])
                    if day not in days:
                        continue
                    items.append({
                        "source": source,
                        "event_id": row[identifier],
                        "device_id": row["device_id"],
                        "event_at": row[timestamp],
                        "day": day,
                        "reason": reason,
                        "origin_batch": row["origin_batch"],
                        "device_revision": device["revision"],
                        "lifecycle_source_revision": device["lifecycle_source_revision"],
                    })
            items.sort(key=lambda item: (item["day"], item["source"], item["event_at"], item["event_id"]))
            return {"market": market, "from_day": from_day, "to_day": to_day, "product": product, "source_revision": source_revision, "exclusions": items, "count": len(items)}

    # -- 影响报告 ---------------------------------------------------------
    def impact(self, device_id: str, installed_at: str, from_day: str | None = None, to_day: str | None = None, base_installed_at: str | None = None, expected_revision: int | None = None) -> dict:
        proposed = iso(installed_at)
        with self.store.read() as db:
            device = Catalog.device(db, device_id)
            if expected_revision is not None:
                require(type(expected_revision) is int and expected_revision >= 1, "invalid_revision", "expected device revision required")
                require(device["revision"] == expected_revision, "stale_device", "device revision changed", 409)
            base = iso(base_installed_at) if base_installed_at is not None else device["installed_at"]
            return self._impact(db, device, base, proposed, from_day, to_day)

    def _impact(self, db, device, base_installed_at: str, proposed_installed_at: str, from_day: str | None, to_day: str | None) -> dict:
        from .analytics import Analytics, days_between
        from .publication import Publication
        require((from_day is None) == (to_day is None), "invalid_range", "both from_day and to_day or neither")
        require(device["retired_at"] is None or instant(proposed_installed_at) < instant(device["retired_at"]), "invalid_lifecycle", "installation must precede retirement")
        if from_day is None:
            from_day, to_day = self._default_window(db, device, base_installed_at, proposed_installed_at)
        else:
            list(days_between(from_day, to_day))
        zone = device["timezone"]
        tickets, telemetry = self.accepted_events(db, device["id"])
        ticket_actions = self.ticket_actions(db, device["id"])
        retired_at = device["retired_at"]

        ticket_rows = []
        for ticket in tickets:
            before = _state(base_installed_at, retired_at, ticket["opened_at"])
            after = _state(proposed_installed_at, retired_at, ticket["opened_at"])
            ticket_rows.append({
                "id": ticket["id"],
                "device_id": ticket["device_id"],
                "opened_at": ticket["opened_at"],
                "local_day": market_day(ticket["opened_at"], zone),
                "state": ticket["state"],
                "severity": ticket["severity"],
                "origin_batch": ticket["origin_batch"],
                "before": before,
                "after": after,
                "affected": before != after,
            })
        telemetry_rows = []
        for event in telemetry:
            before = _state(base_installed_at, retired_at, event["observed_at"])
            after = _state(proposed_installed_at, retired_at, event["observed_at"])
            telemetry_rows.append({
                "id": event["id"],
                "device_id": event["device_id"],
                "observed_at": event["observed_at"],
                "local_day": market_day(event["observed_at"], zone),
                "run_hours": event["run_hours"],
                "downtime_hours": event["downtime_hours"],
                "origin_batch": event["origin_batch"],
                "before": before,
                "after": after,
                "affected": before != after,
            })
        tickets_by_id = {ticket["id"]: ticket for ticket in tickets}
        action_rows = []
        for action in ticket_actions:
            ticket = tickets_by_id[action["ticket_id"]]
            # 与 analytics 的口径一致：工单本身或处置时刻任一落在生命周期外，本次处置都不计入。
            before = _state(base_installed_at, retired_at, ticket["opened_at"])
            before = before if not before["included"] else _state(base_installed_at, retired_at, action["event_at"])
            after = _state(proposed_installed_at, retired_at, ticket["opened_at"])
            after = after if not after["included"] else _state(proposed_installed_at, retired_at, action["event_at"])
            if before == after:
                continue
            action_rows.append({
                "ticket_id": action["ticket_id"],
                "revision": action["revision"],
                "action": action["action"],
                "event_at": action["event_at"],
                "local_day": market_day(action["event_at"], zone),
                "before": before,
                "after": after,
            })

        analytics = Analytics(self.store)
        override = {device["id"]: {"installed_at": proposed_installed_at, "retired_at": retired_at}}
        metric_changes = _metric_changes(
            _metric_map(_calculate(analytics, db, device["market"], from_day, to_day), device["product"]),
            _metric_map(_calculate(analytics, db, device["market"], from_day, to_day, override), device["product"]),
            device["product"],
        )

        current_revision = self.store.revision(db)
        head = Publication.head(db, device["market"])
        candidates = []
        for candidate in db.execute("SELECT * FROM candidates WHERE market=? ORDER BY created_at,id", (device["market"],)).fetchall():
            if candidate["status"] == "discarded":
                continue
            ready_before = (
                candidate["status"] == "prepared"
                and candidate["source_revision"] == current_revision
                and candidate["expected_head"] == head
            )
            # 以候选自身的源修订为基准重算，只归因本次修订，不把之后的迟到数据算进来。
            changed = self._range_changes(
                db, candidate["market"], candidate["from_day"], candidate["to_day"],
                device, proposed_installed_at, candidate["source_revision"],
            )
            if candidate["status"] != "prepared" and not changed:
                continue
            candidates.append({
                "id": candidate["id"],
                "status": candidate["status"],
                "from_day": candidate["from_day"],
                "to_day": candidate["to_day"],
                "source_revision": candidate["source_revision"],
                "expected_head": candidate["expected_head"],
                "ready_before_revision": ready_before,
                "invalidated_by_revision": ready_before,
                "metrics_changed": bool(changed),
                "changed_days": changed,
            })

        publications = []
        for publication in db.execute("SELECT * FROM publications WHERE market=? ORDER BY version", (device["market"],)).fetchall():
            stored = {row["day"] for row in db.execute(
                "SELECT day FROM publication_rows WHERE market=? AND version=? AND product=?",
                (publication["market"], publication["version"], device["product"]),
            )}
            if not stored:
                continue
            changed = self._range_changes(
                db, publication["market"], min(stored), max(stored),
                device, proposed_installed_at, publication["source_revision"],
            )
            if not changed:
                continue
            publications.append({
                "version": publication["version"],
                "candidate_id": publication["candidate_id"],
                "source_revision": publication["source_revision"],
                "frozen": True,
                "superseded_by_correction": True,
                "changed_days": changed,
                "new_version_required": True,
            })

        summary = {
            "tickets_affected": sum(row["affected"] for row in ticket_rows),
            "telemetry_affected": sum(row["affected"] for row in telemetry_rows),
            "ticket_actions_affected": len(action_rows),
            "events_excluded": sum(row["after"]["reason"] is not None for row in ticket_rows + telemetry_rows),
            "events_restored": sum((not row["before"]["included"]) and row["after"]["included"] for row in ticket_rows + telemetry_rows),
            "days_changed": len(metric_changes),
            "prepared_candidates_invalidated": sum(candidate["invalidated_by_revision"] for candidate in candidates),
            "candidates_with_metric_changes": sum(candidate["metrics_changed"] for candidate in candidates),
            "publications_superseded": len(publications),
        }
        report = {
            "device": {key: device[key] for key in ("id", "market", "product", "installed_at", "retired_at", "revision")},
            "before": {"installed_at": base_installed_at, "retired_at": retired_at},
            "after": {"installed_at": proposed_installed_at, "retired_at": retired_at},
            "from_day": from_day,
            "to_day": to_day,
            "tickets": ticket_rows,
            "telemetry": telemetry_rows,
            "ticket_actions": action_rows,
            "metric_changes": metric_changes,
            "candidates": candidates,
            "publications": publications,
            "summary": summary,
        }
        report["digest"] = _digest({key: value for key, value in report.items() if key != "digest"})
        return report

    @staticmethod
    def _default_window(db, device, base_installed_at: str, proposed_installed_at: str) -> tuple[str, str]:
        zone = device["timezone"]
        moments = [base_installed_at, proposed_installed_at]
        if device["retired_at"]:
            moments.append(device["retired_at"])
        for table, field in (("tickets", "opened_at"), ("telemetry", "observed_at")):
            for row in db.execute(f"SELECT {field} FROM {table} WHERE device_id=?", (device["id"],)):
                moments.append(row[field])
        days = sorted({market_day(moment, zone) for moment in moments})
        start, stop = days[0], days[-1]
        if (date.fromisoformat(stop) - date.fromisoformat(start)).days > 365:
            start = (date.fromisoformat(stop) - timedelta(days=365)).isoformat()
        return start, stop

    def _range_changes(self, db, market: str, from_day: str, to_day: str, device, proposed_installed_at: str, source_revision: int) -> list[str]:
        """按给定源修订还原其他设备生命周期，只暴露本次设备修订带来的指标变化。"""
        from .analytics import Analytics
        historical = self.resolved_devices(db, market, None, source_revision)
        if device["id"] not in historical:
            # 该设备在版本冻结时尚未登记，修订不会改变这个版本。
            return []
        baseline = {key: {"installed_at": value["installed_at"], "retired_at": value["retired_at"]} for key, value in historical.items()}
        # 只改安装时间；退役时间是另一维修订，不能算到本次修订的头上。
        proposed = {**baseline, device["id"]: {"installed_at": proposed_installed_at, "retired_at": baseline[device["id"]]["retired_at"]}}
        analytics = Analytics(self.store)
        only = set(baseline)
        before = _metric_map(_calculate(analytics, db, market, from_day, to_day, baseline, only), device["product"])
        after = _metric_map(_calculate(analytics, db, market, from_day, to_day, proposed, only), device["product"])
        return [day for day in sorted(before) if before[day] != after.get(day)]

    # -- 修订 -------------------------------------------------------------
    def revise(self, device_id: str, installed_at: str, expected_revision: int, note: str = "") -> dict:
        proposed = iso(installed_at)
        require(type(expected_revision) is int and expected_revision >= 1, "invalid_revision", "expected device revision required")
        require(isinstance(note, str) and len(note) <= 1000, "invalid_note", "note too long")
        try:
            return self._revise(device_id, proposed, expected_revision, note)
        except DomainError as exc:
            if exc.code == "stale_device":
                # 事务已回滚，单独记录冲突，使并发修订的失败尝试也可追溯。
                with self.store.transaction() as db:
                    current = db.execute("SELECT revision,installed_at FROM devices WHERE id=?", (device_id,)).fetchone()
                    self.store.audit(db, "device_revision_conflict", device_id, {
                        "expected_revision": expected_revision,
                        "actual_revision": current["revision"] if current else None,
                        "current_installed_at": current["installed_at"] if current else None,
                        "attempted_installed_at": proposed,
                        "note": note,
                    })
            raise

    def _revise(self, device_id: str, proposed: str, expected_revision: int, note: str) -> dict:
        with self.store.transaction() as db:
            device = Catalog.device(db, device_id)
            require(device["revision"] == expected_revision, "stale_device", "device revision changed", 409)
            require(device["installed_at"] != proposed, "installation_unchanged", "installation time is unchanged", 409)
            require(device["retired_at"] is None or instant(proposed) < instant(device["retired_at"]), "invalid_lifecycle", "installation must precede retirement")
            previous = device["installed_at"]
            revision = expected_revision + 1
            db.execute("UPDATE devices SET installed_at=?,revision=? WHERE id=?", (proposed, revision, device_id))
            source_revision = self.store.bump(db)
            db.execute(
                "INSERT INTO device_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (device_id, revision, "installation_corrected", proposed, device["retired_at"], previous, device["retired_at"], note, source_revision, now()),
            )
            excluded, restored = self._record_dispositions(db, device, previous, proposed, revision, source_revision)
            self.store.audit(db, "device_installation_revised", device_id, {
                "previous_installed_at": previous,
                "installed_at": proposed,
                "revision": revision,
                "source_revision": source_revision,
                "note": note,
                "excluded_events": len(excluded),
                "restored_events": len(restored),
                "excluded": excluded,
                "restored": restored,
            })
            return {
                "id": device_id,
                "market": device["market"],
                "product": device["product"],
                "installed_at": proposed,
                "previous_installed_at": previous,
                "retired_at": device["retired_at"],
                "revision": revision,
                "source_revision": source_revision,
                "note": note,
                "dispositions": {"excluded": excluded, "restored": restored},
            }

    def _record_dispositions(self, db, device, previous_installed_at: str, proposed_installed_at: str, revision: int, source_revision: int) -> tuple[list[dict], list[dict]]:
        tickets, telemetry = self.accepted_events(db, device["id"])
        excluded, restored = [], []
        for source, events, field, identifier in (
            ("tickets", tickets, "opened_at", "id"),
            ("telemetry", telemetry, "observed_at", "id"),
        ):
            for event in events:
                before = _reason_for_bounds(previous_installed_at, device["retired_at"], event[field])
                after = _reason_for_bounds(proposed_installed_at, device["retired_at"], event[field])
                if after is not None:
                    disposition, reason = "excluded", after
                    excluded.append({"source": source, "event_id": event[identifier], "reason": after})
                elif before is not None:
                    disposition, reason = "restored", before
                    restored.append({"source": source, "event_id": event[identifier], "reason": before})
                else:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO lifecycle_dispositions(source,event_id,device_id,device_revision,source_revision,event_at,disposition,reason,installed_at,retired_at,at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (source, event[identifier], device["id"], revision, source_revision, event[field], disposition, reason, proposed_installed_at, device["retired_at"], now()),
                )
        actions = self.ticket_actions(db, device["id"])
        tickets_by_id = {ticket["id"]: ticket for ticket in tickets}
        for action in actions:
            ticket = tickets_by_id[action["ticket_id"]]
            # 工单处置指标只在工单本身及其处置时刻都落在生命周期内时计入。
            before = _reason_for_bounds(previous_installed_at, device["retired_at"], ticket["opened_at"]) or _reason_for_bounds(previous_installed_at, device["retired_at"], action["event_at"])
            after = _reason_for_bounds(proposed_installed_at, device["retired_at"], ticket["opened_at"]) or _reason_for_bounds(proposed_installed_at, device["retired_at"], action["event_at"])
            if after is not None:
                disposition, reason = "excluded", after
                excluded.append({"source": "ticket_actions", "event_id": f"{action['ticket_id']}#{action['revision']}", "reason": after})
            elif before is not None:
                disposition, reason = "restored", before
                restored.append({"source": "ticket_actions", "event_id": f"{action['ticket_id']}#{action['revision']}", "reason": before})
            else:
                continue
            db.execute(
                "INSERT OR REPLACE INTO lifecycle_dispositions(source,event_id,device_id,device_revision,source_revision,event_at,disposition,reason,installed_at,retired_at,at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("ticket_actions", f"{action['ticket_id']}#{action['revision']}", device["id"], revision, source_revision, action["event_at"], disposition, reason, proposed_installed_at, device["retired_at"], now()),
            )
        return excluded, restored

    # -- 修订历史与处置 ---------------------------------------------------
    def history(self, device_id: str) -> dict:
        with self.store.read() as db:
            device = Catalog.device(db, device_id)
            rows = [dict(row) for row in db.execute("SELECT * FROM device_revisions WHERE device_id=? ORDER BY revision", (device_id,))]
            return {"device": dict(device), "revisions": rows}

    def dispositions(self, device_id: str, revision: int | None = None) -> dict:
        with self.store.read() as db:
            device = Catalog.device(db, device_id)
            query = "SELECT * FROM lifecycle_dispositions WHERE device_id=?"
            parameters: list = [device_id]
            if revision is not None:
                query += " AND device_revision=?"
                parameters.append(revision)
            rows = [dict(row) for row in db.execute(query + " ORDER BY device_revision,source,event_at,event_id", parameters)]
            for item in rows:
                item["origin_batch"] = None
                if item["source"] == "tickets":
                    source = db.execute("SELECT origin_batch FROM tickets WHERE id=?", (item["event_id"],)).fetchone()
                elif item["source"] == "telemetry":
                    source = db.execute("SELECT origin_batch FROM telemetry WHERE id=?", (item["event_id"],)).fetchone()
                else:
                    source = db.execute(
                        "SELECT t.origin_batch FROM ticket_history h JOIN tickets t ON t.id=h.ticket_id WHERE h.ticket_id=? AND h.revision=?",
                        tuple(item["event_id"].rsplit("#", 1)),
                    ).fetchone()
                if source is not None:
                    item["origin_batch"] = source["origin_batch"]
                item["reason_detail"] = {
                    "before_installation": "accepted event precedes the corrected installation time",
                    "after_retirement": "accepted event is at or after device retirement",
                }.get(item["reason"], item["reason"])
            return {"device": dict(device), "revision": revision, "dispositions": rows, "count": len(rows)}


def _calculate(analytics, db, market: str, from_day: str, to_day: str, override: dict | None = None, only: set[str] | None = None) -> list[dict]:
    return analytics.calculate(db, market, from_day, to_day, overrides=override, only=only)
