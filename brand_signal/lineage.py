"""跨市场设备服务质量分析：lineage模块。"""
import json
from .catalog import market_day
from .errors import require
from .lifecycle import DeviceLifecycle, event_reason
from .publication import Publication
from .store import Store


class Lineage:
    def __init__(self, store: Store):
        self.store = store
        self.publication = Publication(store)

    def explain(self, market: str, version: int, day: str, product: str) -> dict:
        release = self.publication.read(market, version)
        metric = next((r for r in release["rows"] if r["day"] == day and r["product"] == product), None)
        require(metric is not None, "unknown_metric", "published metric unknown", 404)
        with self.store.read() as db:
            zone = db.execute("SELECT timezone FROM markets WHERE code=?", (market,)).fetchone()[0]
            # 按发布时的源修订还原生命周期，使历史版本的“为什么计入/不计入”可复验。
            resolved = DeviceLifecycle.resolved_devices(db, market, product, release["source_revision"])
            devices = [resolved[key] for key in sorted(resolved)]
            source_rows = []
            excluded_rows = []
            for device in devices:
                for table, timestamp in (("tickets", "opened_at"), ("telemetry", "observed_at")):
                    for row in db.execute(f"SELECT * FROM {table} WHERE device_id=? ORDER BY {timestamp},id", (device["id"],)):
                        if market_day(row[timestamp], zone) != day:
                            continue
                        item = {"source": table, "id": row["id"], "device_id": device["id"], "origin_batch": row["origin_batch"], "at": row[timestamp]}
                        reason = event_reason(device, row[timestamp])
                        if reason is None:
                            source_rows.append(item)
                        else:
                            excluded_rows.append({**item, "reason": reason, "device_revision": device["revision"]})
            history = []
            for row in source_rows:
                if row["source"] != "tickets":
                    continue
                for item in db.execute("SELECT * FROM ticket_history WHERE ticket_id=? ORDER BY revision", (row["id"],)):
                    history.append(dict(item))
            dispositions = [dict(row) for row in db.execute(
                "SELECT * FROM lifecycle_dispositions WHERE source_revision<=? AND device_id IN (SELECT id FROM devices WHERE market=? AND product=?) ORDER BY device_revision,event_at,source,event_id",
                (release["source_revision"], market, product),
            )]
            return {
                "market": market,
                "version": version,
                "day": day,
                "product": product,
                "source_revision": release["source_revision"],
                "digest": release["digest"],
                "computed_digest": release["computed_digest"],
                "digest_verified": release["digest_verified"],
                "metric": metric,
                "devices": devices,
                "source_rows": source_rows,
                "excluded_source_rows": excluded_rows,
                "ticket_history": history,
                "lifecycle_dispositions": dispositions,
            }

    def batch_trace(self, batch_id: str) -> dict:
        with self.store.read() as db:
            batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            require(batch is not None, "unknown_batch", "batch unknown", 404)
            rows = [dict(r) for r in db.execute("SELECT * FROM batch_rows WHERE batch_id=? ORDER BY row_no", (batch_id,))]
            for row in rows:
                row["payload"] = json.loads(row["payload"])
            return {"batch": dict(batch), "rows": rows}
