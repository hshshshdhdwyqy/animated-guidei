"""SQLite storage: users, settings, chat history, groups."""
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

TEHRAN = timezone(timedelta(hours=3, minutes=30))


def today() -> str:
    return datetime.now(TEHRAN).strftime("%Y-%m-%d")


def today_jalali() -> str:
    try:
        import jdatetime
        return jdatetime.datetime.fromgregorian(datetime=datetime.now(TEHRAN)).strftime("%Y/%m/%d")
    except ImportError:
        return today()


class DB:
    def __init__(self, path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init()

    # ------------------------------------------------------------------ schema
    def _init(self):
        with self.lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT
                );
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT, first_name TEXT,
                    joined REAL, last_seen REAL,
                    banned INTEGER DEFAULT 0,
                    blocked INTEGER DEFAULT 0,
                    persona TEXT DEFAULT 'default',
                    web INTEGER DEFAULT 0,
                    day TEXT, msgs_today INTEGER DEFAULT 0, imgs_today INTEGER DEFAULT 0,
                    total_msgs INTEGER DEFAULT 0, total_imgs INTEGER DEFAULT 0,
                    personal_prompt TEXT DEFAULT '', response_language TEXT DEFAULT 'auto',
                    custom_daily_msgs INTEGER DEFAULT 0, custom_daily_imgs INTEGER DEFAULT 0,
                    referred_by INTEGER DEFAULT NULL, referral_code TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER, role TEXT, text TEXT, ts REAL
                );
                CREATE INDEX IF NOT EXISTS idx_history_chat ON history(chat_id, id);
                CREATE TABLE IF NOT EXISTS referrals (
                    inviter_id INTEGER NOT NULL, invited_id INTEGER PRIMARY KEY,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY, added REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY, title TEXT, enabled INTEGER DEFAULT 1,
                    added REAL, active INTEGER DEFAULT 1
                );
                """
            )
            existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)").fetchall()}
            migrations = {
                "personal_prompt": "TEXT DEFAULT ''",
                "response_language": "TEXT DEFAULT 'auto'",
                "custom_daily_msgs": "INTEGER DEFAULT 0",
                "custom_daily_imgs": "INTEGER DEFAULT 0",
                "referred_by": "INTEGER DEFAULT NULL",
                "referral_code": "TEXT DEFAULT ''",
            }
            for name, declaration in migrations.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE users ADD COLUMN {name} {declaration}")

    # ---------------------------------------------------------------- settings
    def get_setting(self, key, default=None):
        with self.lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row and row["value"] is not None else default

    def set_setting(self, key, value):
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def del_setting(self, key):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM settings WHERE key=?", (key,))

    def get_int(self, key, default=0):
        try:
            return int(self.get_setting(key, default))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------- users
    def upsert_user(self, uid, username, first_name):
        now = time.time()
        d = today()
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO users(id, username, first_name, joined, last_seen, day)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    username=excluded.username, first_name=excluded.first_name,
                    last_seen=excluded.last_seen, blocked=0
                """,
                (uid, username, first_name, now, now, d),
            )
            # daily counter reset
            self.conn.execute(
                "UPDATE users SET day=?, msgs_today=0, imgs_today=0 WHERE id=? AND (day IS NULL OR day<>?)",
                (d, uid, d),
            )
        return self.get_user(uid)

    def get_user(self, uid):
        with self.lock:
            return self.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def find_user(self, query: str):
        q = query.strip().lstrip("@")
        with self.lock:
            if q.lstrip("-").isdigit():
                return self.conn.execute("SELECT * FROM users WHERE id=?", (int(q),)).fetchone()
            return self.conn.execute(
                "SELECT * FROM users WHERE lower(username)=lower(?)", (q,)
            ).fetchone()

    def set_user_field(self, uid, field, value):
        if field not in {"banned", "blocked", "persona", "web", "personal_prompt", "response_language", "custom_daily_msgs", "custom_daily_imgs"}:
            raise ValueError(field)
        with self.lock, self.conn:
            self.conn.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, uid))

    def incr(self, uid, kind):
        col, total = ("msgs_today", "total_msgs") if kind == "msgs" else ("imgs_today", "total_imgs")
        with self.lock, self.conn:
            self.conn.execute(
                f"UPDATE users SET {col}={col}+1, {total}={total}+1 WHERE id=?", (uid,)
            )

    def reset_today(self, uid):
        with self.lock, self.conn:
            self.conn.execute("UPDATE users SET msgs_today=0, imgs_today=0 WHERE id=?", (uid,))

    def broadcast_targets(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT id FROM users WHERE banned=0 AND blocked=0"
            ).fetchall()
        return [r["id"] for r in rows]

    def recent_users(self, limit=10):
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM users ORDER BY last_seen DESC LIMIT ?", (limit,)
            ).fetchall()

    def top_users(self, limit=10):
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM users ORDER BY (total_msgs+total_imgs) DESC LIMIT ?", (limit,)
            ).fetchall()

    def stats(self):
        d = today()
        with self.lock:
            c = self.conn
            one = lambda sql, *a: c.execute(sql, a).fetchone()[0] or 0  # noqa: E731
            return {
                "users": one("SELECT COUNT(*) FROM users"),
                "active_today": one("SELECT COUNT(*) FROM users WHERE day=?", d),
                "new_today": one(
                    "SELECT COUNT(*) FROM users WHERE joined>=?",
                    datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TEHRAN).timestamp(),
                ),
                "banned": one("SELECT COUNT(*) FROM users WHERE banned=1"),
                "blocked": one("SELECT COUNT(*) FROM users WHERE blocked=1"),
                "msgs_today": one("SELECT SUM(msgs_today) FROM users WHERE day=?", d),
                "imgs_today": one("SELECT SUM(imgs_today) FROM users WHERE day=?", d),
                "total_msgs": one("SELECT SUM(total_msgs) FROM users"),
                "total_imgs": one("SELECT SUM(total_imgs) FROM users"),
                "groups": one("SELECT COUNT(*) FROM groups WHERE active=1"),
            }

    # ----------------------------------------------------------------- history
    def add_history(self, chat_id, role, text, keep=60):
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO history(chat_id, role, text, ts) VALUES(?,?,?,?)",
                (chat_id, role, text[:8000], time.time()),
            )
            self.conn.execute(
                "DELETE FROM history WHERE chat_id=? AND id NOT IN "
                "(SELECT id FROM history WHERE chat_id=? ORDER BY id DESC LIMIT ?)",
                (chat_id, chat_id, keep),
            )

    def get_history(self, chat_id, limit=24):
        with self.lock:
            rows = self.conn.execute(
                "SELECT role, text FROM history WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [(r["role"], r["text"]) for r in reversed(rows)]

    def clear_history(self, chat_id):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM history WHERE chat_id=?", (chat_id,))

    # ------------------------------------------------------------------ groups
    def upsert_group(self, gid, title):
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO groups(id, title, added, active) VALUES(?,?,?,1)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, active=1
                """,
                (gid, title, time.time()),
            )
        return self.get_group(gid)

    def get_group(self, gid):
        with self.lock:
            return self.conn.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()

    def list_groups(self, limit=20):
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM groups WHERE active=1 ORDER BY added DESC LIMIT ?", (limit,)
            ).fetchall()

    def set_group_enabled(self, gid, enabled):
        with self.lock, self.conn:
            self.conn.execute("UPDATE groups SET enabled=? WHERE id=?", (1 if enabled else 0, gid))

    def deactivate_group(self, gid):
        with self.lock, self.conn:
            self.conn.execute("UPDATE groups SET active=0 WHERE id=?", (gid,))

    # --------------------------------------------------------- free user features
    def set_personal_setting(self, uid, field, value):
        allowed = {"personal_prompt", "response_language", "custom_daily_msgs", "custom_daily_imgs"}
        if field not in allowed:
            raise ValueError(field)
        with self.lock, self.conn:
            self.conn.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, uid))

    def set_referral_code(self, uid, code):
        with self.lock, self.conn:
            self.conn.execute("UPDATE users SET referral_code=? WHERE id=?", (code, uid))

    def add_referral(self, inviter_id, invited_id):
        if inviter_id == invited_id:
            return False
        with self.lock, self.conn:
            exists = self.conn.execute("SELECT referred_by FROM users WHERE id=?", (invited_id,)).fetchone()
            if not exists or exists["referred_by"] is not None:
                return False
            self.conn.execute("UPDATE users SET referred_by=? WHERE id=?", (inviter_id, invited_id))
            self.conn.execute("INSERT OR IGNORE INTO referrals(inviter_id, invited_id, created) VALUES(?,?,?)",
                              (inviter_id, invited_id, time.time()))
            return True

    def referral_count(self, uid):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id=?", (uid,)).fetchone()[0]

    def add_admin(self, uid):
        with self.lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO admins(user_id, added) VALUES(?,?)", (uid, time.time()))

    def remove_admin(self, uid):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM admins WHERE user_id=?", (uid,))

    def is_db_admin(self, uid):
        with self.lock:
            return self.conn.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None

    def list_admins(self):
        with self.lock:
            return [r["user_id"] for r in self.conn.execute("SELECT user_id FROM admins ORDER BY added")]

    # ------------------------------------------------------------------ backup
    def backup_to(self, dest_path):
        with self.lock:
            dest = sqlite3.connect(str(dest_path))
            try:
                self.conn.backup(dest)
            finally:
                dest.close()
