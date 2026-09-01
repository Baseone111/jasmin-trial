"""Small durable outbox. One worker; SQLite transactions serialize state changes."""
import hashlib
from contextlib import contextmanager
import json
import sqlite3
import time
import uuid
from pathlib import Path

FINAL = {"DELIVERED", "UNDELIVERABLE", "REJECTED", "EXPIRED", "DELETED"}


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, client_ref TEXT UNIQUE NOT NULL,
                    destination TEXT NOT NULL, content TEXT NOT NULL,
                    status TEXT NOT NULL, gateway_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_key TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                    received_at REAL NOT NULL, payload TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, client_ref, destination, content):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM messages WHERE client_ref=?", (client_ref,)).fetchone()
            if row:
                if (row["destination"], row["content"]) != (destination, content):
                    raise ValueError("client_ref already belongs to a different message")
                return dict(row)
            now = time.time()
            mid = str(uuid.uuid4())
            db.execute("INSERT INTO messages (id,client_ref,destination,content,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                       (mid, client_ref, destination, content, "PENDING", now, now))
        return self.get(mid)

    def get(self, mid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            if not row:
                return None
            data = dict(row)
            data["events"] = [dict(r) for r in db.execute(
                "SELECT received_at,payload FROM events WHERE message_id=? ORDER BY received_at", (mid,))]
            for event in data["events"]:
                event["payload"] = json.loads(event["payload"])
            return data

    def list(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM messages ORDER BY created_at DESC LIMIT 50")]

    def recover(self):
        # A process may have died after sending, before recording the response.
        # Do not resend these ambiguous submissions automatically.
        with self.connect() as db:
            db.execute("UPDATE messages SET status='UNKNOWN', error='Worker restarted during submission; inspect before resending', updated_at=? WHERE status='SUBMITTING'", (time.time(),))

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM messages WHERE status='PENDING' AND next_attempt<=? ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return None
            db.execute("UPDATE messages SET status='SUBMITTING', attempts=attempts+1, updated_at=? WHERE id=?", (time.time(), row["id"]))
            return dict(db.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone())

    def submission_result(self, mid, status, gateway_id=None, error=None, delay=0):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            # Callback may arrive before the submit HTTP response.
            if row["status"] in FINAL or row["status"] == "ACCEPTED":
                return
            db.execute("UPDATE messages SET status=?, gateway_id=COALESCE(?,gateway_id), error=?,next_attempt=?,updated_at=? WHERE id=?",
                       (status, gateway_id, error, time.time()+delay, time.time(), mid))

    def receipt(self, mid, payload):
        status = payload["message_status"]
        level = payload["level"]
        if level == "1":
            state = "ACCEPTED" if status == "ESME_ROK" else "REJECTED"
        else:
            state = {"DELIVRD": "DELIVERED", "UNDELIV": "UNDELIVERABLE",
                     "REJECTD": "REJECTED", "EXPIRED": "EXPIRED", "DELETED": "DELETED",
                     "ACCEPTD": "ACCEPTED", "ENROUTE": "ACCEPTED", "UNKNOWN": "UNKNOWN"}.get(status)
        key = hashlib.sha256(json.dumps([mid,payload["id"],level,status]).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            if not row:
                raise KeyError(mid)
            if row["gateway_id"] and row["gateway_id"] != payload["id"]:
                raise ValueError("Gateway message ID mismatch")
            inserted = db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
                                  (key,mid,time.time(),json.dumps(payload))).rowcount
            if inserted:
                # Preserve terminal states if a late acceptance arrives.
                next_state = row["status"] if row["status"] in FINAL or state is None else state
                db.execute("UPDATE messages SET status=?,gateway_id=?,updated_at=?,error=NULL WHERE id=?",
                           (next_state,payload["id"],time.time(),mid))
