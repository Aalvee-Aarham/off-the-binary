"""Decision + alert audit trail. ponytail: sync sqlite3 on the event loop (sub-ms writes); move to a thread
or Postgres if the load test shows event-loop stalls."""
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, created_at REAL, tick INTEGER, status TEXT, actor TEXT,
  regime TEXT, algorithm TEXT, gate TEXT, body TEXT);
CREATE INDEX IF NOT EXISTS decisions_status ON decisions(status);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, tick INTEGER, kind TEXT, type TEXT,
  severity TEXT, entity TEXT, message TEXT);
"""


class Audit:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def ping(self):
        try:
            with self.lock:
                self.db.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    def save(self, d):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO decisions(id, created_at, tick, status, actor, regime, algorithm, gate, body) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (d["id"], d["created_at"], d["tick"], d["status"], d.get("actor"), d["router"]["regime"],
                 d["algorithm"], "auto" if d["gate"]["auto"] else "human", json.dumps(d, default=str)))

    def get(self, did):
        with self.lock:
            r = self.db.execute("SELECT body FROM decisions WHERE id=?", (did,)).fetchone()
        return json.loads(r["body"]) if r else None

    def list(self, status=None, limit=50):
        q, args = "SELECT body FROM decisions", []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self.lock:
            return [json.loads(r["body"]) for r in self.db.execute(q, args)]

    def alert(self, kind, a):
        with self.lock, self.db:
            self.db.execute("INSERT INTO alerts(created_at, tick, kind, type, severity, entity, message) VALUES (?,?,?,?,?,?,?)",
                            (time.time(), a.get("tick"), kind, a["type"], a["severity"], a["entity"], a["message"]))

    def alerts(self, limit=100):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (int(limit),))]
