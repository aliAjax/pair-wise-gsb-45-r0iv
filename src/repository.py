"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    at_hour REAL,
                    batch_id INTEGER,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedule_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    params TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS waiting_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES schedule_batches(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    record_id INTEGER NOT NULL,
                    reference TEXT NOT NULL,
                    vessel TEXT NOT NULL,
                    berth TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    dangerous_goods INTEGER NOT NULL,
                    dangerous_class TEXT NOT NULL,
                    eta_hour INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    since_hour REAL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES schedule_batches(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    record_id INTEGER NOT NULL,
                    reference TEXT NOT NULL,
                    vessel TEXT NOT NULL,
                    berth TEXT NOT NULL,
                    start_hour REAL,
                    end_hour REAL,
                    pilot_id TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS berth_occupancy (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES schedule_batches(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL,
                    reference TEXT NOT NULL,
                    vessel TEXT NOT NULL,
                    berth TEXT NOT NULL,
                    start_hour REAL NOT NULL,
                    end_hour REAL NOT NULL,
                    source TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_channel_events ON channel_events(id);
                CREATE INDEX IF NOT EXISTS idx_queue_batch ON waiting_queue(batch_id, position);
                CREATE INDEX IF NOT EXISTS idx_slots_batch ON channel_slots(batch_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_occupancy_batch ON berth_occupancy(batch_id, berth, start_hour);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _plain_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item.pop("created_at", None)
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def bulk_transition(
        self,
        states: Dict[str, str],
        new_state: str,
        mutate_payload,
        actor_id: str,
        action: str,
        details_builder,
    ) -> List[Dict[str, Any]]:
        """在单事务内把指定状态的全部记录整体迁移并逐条写审计。"""
        now = _now()
        updated: List[Dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in states)
            rows = connection.execute(
                "SELECT * FROM records WHERE state IN (%s) ORDER BY id" % placeholders,
                tuple(states),
            ).fetchall()
            for row in rows:
                record = self._row(row)
                payload = mutate_payload(record)
                version = int(record["version"]) + 1
                connection.execute(
                    "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                    (new_state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record["id"]),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        record["id"],
                        action,
                        actor_id,
                        version,
                        json.dumps(details_builder(record, payload), ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                result = connection.execute("SELECT * FROM records WHERE id=?", (record["id"],)).fetchone()
                updated.append(self._row(result))
            connection.commit()
        return updated

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- 航道事件 ----

    def add_channel_event(self, kind: str, actor_id: str, details: Dict[str, Any], at_hour: Optional[float] = None, batch_id: Optional[int] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO channel_events(kind,at_hour,batch_id,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                (kind, at_hour, batch_id, actor_id, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            event_id = int(cursor.lastrowid)
            row = connection.execute("SELECT * FROM channel_events WHERE id=?", (event_id,)).fetchone()
        item = self._plain_row(row)
        item["details"] = json.loads(item["details"])
        return item

    def last_channel_event(self) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM channel_events ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        item = self._plain_row(row)
        item["details"] = json.loads(item["details"])
        return item

    def channel_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM channel_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = self._plain_row(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    # ---- 复航重排计划 ----

    def save_plan(self, kind: str, params: Dict[str, Any], actor_id: str, plan: Dict[str, Any]) -> int:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT INTO schedule_batches(kind,params,actor_id,created_at) VALUES(?,?,?,?)",
                (kind, json.dumps(params, ensure_ascii=False, sort_keys=True), actor_id, now),
            )
            batch_id = int(cursor.lastrowid)
            for item in plan["queue"]:
                connection.execute(
                    """
                    INSERT INTO waiting_queue(batch_id,position,record_id,reference,vessel,berth,risk_level,
                        dangerous_goods,dangerous_class,eta_hour,reason,detail,since_hour,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        batch_id, item["position"], item["record_id"], item["reference"], item["vessel"],
                        item["berth"], item["risk_level"], 1 if item["dangerous_goods"] else 0,
                        item.get("dangerous_class", ""), item["eta_hour"], item["reason"], item.get("detail", ""),
                        item.get("since_hour"), now,
                    ),
                )
            for item in plan["slots"]:
                connection.execute(
                    """
                    INSERT INTO channel_slots(batch_id,sequence,record_id,reference,vessel,berth,start_hour,end_hour,
                        pilot_id,status,reason,detail,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        batch_id, item["sequence"], item["record_id"], item["reference"], item["vessel"],
                        item["berth"], item.get("start_hour"), item.get("end_hour"), item.get("pilot_id"),
                        item["status"], item.get("reason"), item.get("detail", ""), now,
                    ),
                )
            for item in plan["occupancy"]:
                connection.execute(
                    """
                    INSERT INTO berth_occupancy(batch_id,record_id,reference,vessel,berth,start_hour,end_hour,
                        source,detail,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        batch_id, item["record_id"], item["reference"], item["vessel"], item["berth"],
                        item["start_hour"], item["end_hour"], item["source"], item.get("detail", ""), now,
                    ),
                )
            connection.commit()
        return batch_id

    def latest_batch_id(self, kind: Optional[str] = None) -> Optional[int]:
        with self._connect() as connection:
            if kind:
                row = connection.execute("SELECT MAX(id) AS id FROM schedule_batches WHERE kind=?", (kind,)).fetchone()
            else:
                row = connection.execute("SELECT MAX(id) AS id FROM schedule_batches").fetchone()
        if row is None or row["id"] is None:
            return None
        return int(row["id"])

    def _load_batch(self, connection: sqlite3.Connection, batch_id: Optional[int]) -> Optional[Dict[str, Any]]:
        if batch_id is None:
            return None
        batch = connection.execute("SELECT * FROM schedule_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            return None
        queue_rows = connection.execute("SELECT * FROM waiting_queue WHERE batch_id=? ORDER BY position", (batch_id,)).fetchall()
        slot_rows = connection.execute("SELECT * FROM channel_slots WHERE batch_id=? ORDER BY sequence", (batch_id,)).fetchall()
        occupancy_rows = connection.execute(
            "SELECT * FROM berth_occupancy WHERE batch_id=? ORDER BY start_hour, id", (batch_id,)
        ).fetchall()
        return {
            "batch_id": batch_id,
            "kind": batch["kind"],
            "params": json.loads(batch["params"]),
            "queue": [self._plain_row(row) for row in queue_rows],
            "slots": [self._plain_row(row) for row in slot_rows],
            "occupancy": [self._plain_row(row) for row in occupancy_rows],
        }

    def load_latest_plan(self, kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
        batch_id = self.latest_batch_id(kind)
        if batch_id is None:
            return None
        with self._connect() as connection:
            return self._load_batch(connection, batch_id)

    def load_plan(self, batch_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            return self._load_batch(connection, batch_id)
