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
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE TABLE IF NOT EXISTS port_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS waiting_queue (
                    record_id INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    vessel TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    start_hour INTEGER NOT NULL,
                    end_hour INTEGER NOT NULL,
                    pilot_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS berth_occupancy (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    berth TEXT NOT NULL,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    vessel TEXT NOT NULL,
                    start_hour INTEGER NOT NULL,
                    end_hour INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
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

    def list_by_states(self, states: List[str]) -> List[Dict[str, Any]]:
        if not states:
            return []
        marks = ",".join("?" for _ in states)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM records WHERE state IN (%s) ORDER BY id" % marks, list(states)).fetchall()
        return [self._row(row) for row in rows]

    def add_port_event(self, event_type: str, reason: str, actor_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO port_events(event_type,reason,actor_id,created_at) VALUES(?,?,?,?)",
                (event_type, reason, actor_id, _now()),
            )

    def latest_port_event(self) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM port_events ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def port_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM port_events ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 100)),)).fetchall()
        return [dict(row) for row in rows]

    def replace_waiting(self, entries: List[Dict[str, Any]]) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM waiting_queue")
            for entry in entries:
                connection.execute(
                    "INSERT INTO waiting_queue(record_id,position,reason,updated_at) VALUES(?,?,?,?)",
                    (int(entry["record_id"]), int(entry["position"]), entry["reason"], now),
                )
            connection.commit()

    def waiting_list(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT w.record_id, w.position, w.reason AS queue_reason, w.updated_at AS queued_at, r.* "
                "FROM waiting_queue w JOIN records r ON r.id = w.record_id "
                "WHERE r.state = 'waiting' ORDER BY w.position"
            ).fetchall()
        return [self._row(row) for row in rows]

    def add_channel_slot(self, record_id: int, vessel: str, sequence: int, start_hour: int, end_hour: int, pilot_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO channel_slots(record_id,vessel,sequence,start_hour,end_hour,pilot_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (record_id, vessel, int(sequence), int(start_hour), int(end_hour), pilot_id, _now()),
            )

    def channel_slots(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM channel_slots ORDER BY start_hour, id").fetchall()
        return [dict(row) for row in rows]

    def delete_channel_slots(self, record_ids: List[int]) -> None:
        if not record_ids:
            return
        marks = ",".join("?" for _ in record_ids)
        with self._connect() as connection:
            connection.execute("DELETE FROM channel_slots WHERE record_id IN (%s)" % marks, list(record_ids))

    def add_berth_occupancy(self, berth: str, record_id: int, vessel: str, start_hour: int, end_hour: int, source: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO berth_occupancy(berth,record_id,vessel,start_hour,end_hour,source,created_at) VALUES(?,?,?,?,?,?,?)",
                (berth, record_id, vessel, int(start_hour), int(end_hour), source, _now()),
            )

    def berth_occupancy(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM berth_occupancy ORDER BY berth, start_hour, id").fetchall()
        return [dict(row) for row in rows]

    def berth_free_at(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT berth, MAX(end_hour) AS free_at FROM berth_occupancy GROUP BY berth").fetchall()
        return {str(row["berth"]): int(row["free_at"]) for row in rows}

    def occupancy_for_record(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM berth_occupancy WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def delete_berth_occupancy(self, record_ids: List[int], source: Optional[str] = None) -> None:
        if not record_ids:
            return
        marks = ",".join("?" for _ in record_ids)
        sql = "DELETE FROM berth_occupancy WHERE record_id IN (%s)" % marks
        params: List[Any] = list(record_ids)
        if source:
            sql += " AND source=?"
            params.append(source)
        with self._connect() as connection:
            connection.execute(sql, params)

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
