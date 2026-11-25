import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class CertificationStore:
    """分拣记录与认证工作台状态的 SQLite 存储。

    记录以 ``(scope, record_id)`` 幂等。scope 通常是 ``global`` 或
    ``run:<run_id>``，使晚到记录既能独立登记，也能绑定某次批次运行。
    """

    def __init__(self, path=None):
        if path is None:
            from app.config import DB_PATH
            path = DB_PATH
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS certification_records(
                  scope TEXT NOT NULL,
                  record_id TEXT NOT NULL,
                  digest TEXT NOT NULL,
                  lot_id TEXT NOT NULL DEFAULT '',
                  payload TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(scope, record_id)
                );
                CREATE TABLE IF NOT EXISTS certification_state(
                  scope TEXT PRIMARY KEY,
                  payload TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                """
            )

    def add_record(self, scope, record_id, digest, lot_id, payload):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM certification_records WHERE scope=? AND record_id=?",
                (scope, record_id),
            ).fetchone()
            if row is not None:
                if row["digest"] != digest:
                    return "conflict", dict(row)
                return "duplicate", dict(row)
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                """INSERT INTO certification_records
                   (scope, record_id, digest, lot_id, payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (scope, record_id, digest, lot_id, json.dumps(payload, ensure_ascii=False), now),
            )
            row = db.execute(
                "SELECT * FROM certification_records WHERE scope=? AND record_id=?",
                (scope, record_id),
            ).fetchone()
            return "created", dict(row)

    def list_records(self, scope):
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM certification_records WHERE scope=? ORDER BY created_at, rowid",
                (scope,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_state(self, scope, payload):
        now = datetime.now(timezone.utc).isoformat()
        body = json.dumps(payload, ensure_ascii=False)
        with self.connect() as db:
            db.execute(
                """INSERT INTO certification_state(scope,payload,updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(scope) DO UPDATE SET payload=excluded.payload,
                                                    updated_at=excluded.updated_at""",
                (scope, body, now),
            )
        return payload

    def get_state(self, scope):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM certification_state WHERE scope=?", (scope,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def new_scope(self):
        return uuid.uuid4().hex[:12]
