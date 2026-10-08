import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path


def fingerprint(text: str) -> str:
    return hashlib.sha256(" ".join(text.casefold().split()).encode()).hexdigest()


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[\w@]+", text.casefold()))


class DB:
    """Small synchronous, transactional SQLite operations; no await inside transactions."""

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn = sqlite3.connect(path)
        Path(path).chmod(0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS grants (user_id INTEGER PRIMARY KEY, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS groups (chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL,
          approved_by INTEGER NOT NULL, title TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
          user_id INTEGER, username TEXT, name TEXT, text TEXT NOT NULL, media TEXT NOT NULL,
          via_bot TEXT, sender_chat INTEGER, date REAL NOT NULL, edited REAL,
          deleted INTEGER DEFAULT 0, PRIMARY KEY(chat_id,message_id));
        CREATE INDEX IF NOT EXISTS messages_history ON messages(chat_id,message_id DESC);
        CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
          creator INTEGER NOT NULL, after_id INTEGER NOT NULL, created REAL NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
          chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, message_id INTEGER, rule_id INTEGER,
          text TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          created REAL NOT NULL, log_id INTEGER, state TEXT NOT NULL DEFAULT '{}', actor INTEGER);
        CREATE INDEX IF NOT EXISTS request_user ON requests(chat_id,user_id,status);
        CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
          rule_id INTEGER NOT NULL, request_id INTEGER NOT NULL UNIQUE, text TEXT NOT NULL,
          hash TEXT NOT NULL, lesson TEXT NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS feedback_group ON feedback(chat_id,rule_id,id DESC);
        CREATE TABLE IF NOT EXISTS events (chat_id INTEGER, message_id INTEGER, rule_id INTEGER,
          hash TEXT, outcome TEXT NOT NULL, created REAL NOT NULL,
          PRIMARY KEY(chat_id,message_id,rule_id,hash));
        CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
          message_id INTEGER NOT NULL, hash TEXT NOT NULL, item TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          UNIQUE(chat_id,message_id,hash));
        CREATE INDEX IF NOT EXISTS job_pending ON jobs(status,id);
        CREATE TABLE IF NOT EXISTS violations (chat_id INTEGER NOT NULL, rule_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL, message_id INTEGER NOT NULL, created REAL NOT NULL,
          PRIMARY KEY(chat_id,rule_id,user_id,message_id));
        """)
        self.conn.commit()

    def execute(self, sql, args=()):
        with self.conn:
            return self.conn.execute(sql, args)

    def one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(row) for row in self.conn.execute(sql, args).fetchall()]

    def get(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def enabled(self, chat):
        row = self.one("SELECT enabled FROM groups WHERE chat_id=?", (chat,))
        return bool(row and row["enabled"])

    def enable(self, chat, by, title, enabled=True):
        self.execute("INSERT OR REPLACE INTO groups VALUES (?,?,?,?)", (chat, enabled, by, title))

    def allowed(self, user):
        return user == self.get("owner_id") or bool(self.one("SELECT 1 FROM grants WHERE user_id=?", (user,)))

    def remember_user(self, user):
        self.execute("INSERT OR REPLACE INTO users VALUES (?,?,?)", (user.id, user.username, user.full_name))

    def resolve_user(self, chat, username):
        # Latest username stored per numeric ID, scoped to people actually observed in this group.
        rows = self.all(
            """SELECT DISTINCT u.user_id FROM users u JOIN messages m ON m.user_id=u.user_id
                         WHERE m.chat_id=? AND lower(u.username)=?""",
            (chat, username.lstrip("@").lower()),
        )
        return rows[0]["user_id"] if len(rows) == 1 else None

    def save_message(self, item):
        old = self.one(
            "SELECT text,media,via_bot FROM messages WHERE chat_id=? AND message_id=?",
            (item["chat_id"], item["message_id"]),
        )
        changed = not old or any(old[k] != item[k] for k in ("text", "media", "via_bot"))
        self.execute(
            """INSERT INTO messages(chat_id,message_id,user_id,username,name,text,media,via_bot,
          sender_chat,date,edited) VALUES (:chat_id,:message_id,:user_id,:username,:name,:text,:media,
          :via_bot,:sender_chat,:date,:edited) ON CONFLICT(chat_id,message_id) DO UPDATE SET
          text=excluded.text,media=excluded.media,via_bot=excluded.via_bot,edited=excluded.edited""",
            item,
        )
        return changed

    def history(self, chat, count, before=None):
        if before is None:
            before = 2**63 - 1
        return list(
            reversed(
                self.all(
                    "SELECT * FROM messages WHERE chat_id=? AND message_id<? "
                    "ORDER BY message_id DESC LIMIT ?",
                    (chat, before, count),
                )
            )
        )

    def add_rule(self, chat, creator, after, body, created=None):
        return self.execute(
            "INSERT INTO rules(chat_id,creator,after_id,created,body) VALUES (?,?,?,?,?)",
            (chat, creator, after, time.time() if created is None else created, json.dumps(body)),
        ).lastrowid

    def rules(self, chat):
        rows = self.all("SELECT * FROM rules WHERE chat_id=? AND active=1 ORDER BY id", (chat,))
        for row in rows:
            row["body"] = json.loads(row["body"])
        return rows

    def event(self, chat, message, rule, text, outcome):
        return (
            self.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                (chat, message, rule, fingerprint(text), outcome, time.time()),
            ).rowcount
            == 1
        )

    def new_request(self, kind, chat, user, message=0, rule=0, text="", evidence="", state=None):
        return self.execute(
            """INSERT INTO requests(kind,chat_id,user_id,message_id,rule_id,text,evidence,
          created,state) VALUES (?,?,?,?,?,?,?,?,?)""",
            (kind, chat, user, message, rule, text, evidence, time.time(), json.dumps(state or {})),
        ).lastrowid

    def request(self, request_id):
        row = self.one("SELECT * FROM requests WHERE id=?", (request_id,))
        if row:
            row["state"] = json.loads(row["state"])
        return row

    def claim(self, request, actor):
        return (
            self.execute(
                "UPDATE requests SET status='processing',actor=? WHERE id=? AND status='pending'",
                (actor, request),
            ).rowcount
            == 1
        )

    def update_request(self, request, **fields):
        assert set(fields) <= {"status", "log_id", "state", "actor"}
        if "state" in fields:
            fields["state"] = json.dumps(fields["state"])
        self.execute(
            "UPDATE requests SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
            (*fields.values(), request),
        )

    def feedback(self, request, lesson):
        self.execute(
            "INSERT OR IGNORE INTO feedback(chat_id,rule_id,request_id,text,hash,lesson,created) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                request["chat_id"],
                request["rule_id"],
                request["id"],
                request["text"],
                fingerprint(request["text"]),
                lesson,
                time.time(),
            ),
        )

    def recalled(self, chat, rule, text):
        # All examples persist; retrieval scans them to select relevant examples, not just a rolling window.
        rows = self.all("SELECT * FROM feedback WHERE chat_id=? AND rule_id=? ORDER BY id DESC", (chat, rule))
        query = tokens(text)
        ranked = sorted(
            rows,
            key=lambda r: len(query & tokens(r["text"])) / max(1, len(query | tokens(r["text"]))),
            reverse=True,
        )
        chosen = {r["id"]: r for r in rows[:4] + ranked[:8]}
        return list(chosen.values())

    def exact_exception(self, chat, rule, text):
        return bool(
            self.one(
                "SELECT 1 FROM feedback WHERE chat_id=? AND rule_id=? AND hash=?",
                (chat, rule, fingerprint(text)),
            )
        )

    def close(self):
        self.conn.close()

    def enqueue(self, item):
        self.execute(
            "INSERT OR IGNORE INTO jobs(chat_id,message_id,hash,item) VALUES (?,?,?,?)",
            (item["chat_id"], item["message_id"], fingerprint(json.dumps(item)), json.dumps(item)),
        )

    def claim_job(self):
        with self.conn:
            row = self.one("SELECT * FROM jobs WHERE status='pending' ORDER BY id LIMIT 1")
            if row:
                self.conn.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
                row["item"] = json.loads(row["item"])
            return row
