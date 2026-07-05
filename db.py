"""SQLite 저장소 — 스키마 + 커넥션 헬퍼."""
import pathlib
import sqlite3
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
DB_PATH = HERE / "data" / "pnl_coach.db"

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS exchange_keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  exchange TEXT NOT NULL,
  api_key_enc TEXT NOT NULL,
  secret_enc TEXT NOT NULL,
  passphrase_enc TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE(user_id, exchange)
);
CREATE TABLE IF NOT EXISTS llm_keys(
  user_id INTEGER NOT NULL,
  provider TEXT NOT NULL,
  key_enc TEXT NOT NULL,
  model TEXT,
  PRIMARY KEY(user_id, provider)
);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  exchange TEXT NOT NULL,
  ext_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT,
  open_time INTEGER,
  close_time INTEGER NOT NULL,
  volume REAL,
  leverage REAL,
  open_price REAL,
  close_price REAL,
  realized REAL NOT NULL,
  fee REAL DEFAULT 0,
  funding REAL DEFAULT 0,
  UNIQUE(user_id, exchange, ext_id)
);
CREATE INDEX IF NOT EXISTS idx_pos_user_time ON positions(user_id, close_time);
CREATE TABLE IF NOT EXISTS cashflows(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  exchange TEXT NOT NULL,
  ext_id TEXT NOT NULL,
  kind TEXT NOT NULL,               -- fee | funding
  symbol TEXT,
  amount REAL NOT NULL,
  time INTEGER NOT NULL,
  UNIQUE(user_id, exchange, ext_id)
);
CREATE TABLE IF NOT EXISTS sync_log(
  user_id INTEGER NOT NULL,
  exchange TEXT NOT NULL,
  last_sync INTEGER,
  status TEXT,
  message TEXT,
  PRIMARY KEY(user_id, exchange)
);
CREATE TABLE IF NOT EXISTS advice(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  provider TEXT,
  model TEXT,
  content TEXT NOT NULL
);
"""


def get_db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.executescript(SCHEMA)
        _local.conn = conn
    return conn


def now_ms() -> int:
    return int(time.time() * 1000)


def upsert_positions(user_id: int, exchange: str, rows: list[dict]) -> int:
    """정규화된 청산 포지션 목록 저장. 신규 건수 리턴."""
    db = get_db()
    new = 0
    for r in rows:
        cur = db.execute(
            """INSERT OR IGNORE INTO positions
               (user_id, exchange, ext_id, symbol, side, open_time, close_time,
                volume, leverage, open_price, close_price, realized, fee, funding)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, exchange, str(r["ext_id"]), r["symbol"], r.get("side"),
             r.get("open_time"), r["close_time"], r.get("volume"),
             r.get("leverage"), r.get("open_price"), r.get("close_price"),
             float(r["realized"]), float(r.get("fee") or 0),
             float(r.get("funding") or 0)),
        )
        new += cur.rowcount
    db.commit()
    return new


def upsert_cashflows(user_id: int, exchange: str, rows: list[dict]) -> int:
    db = get_db()
    new = 0
    for r in rows:
        cur = db.execute(
            """INSERT OR IGNORE INTO cashflows
               (user_id, exchange, ext_id, kind, symbol, amount, time)
               VALUES (?,?,?,?,?,?,?)""",
            (user_id, exchange, str(r["ext_id"]), r["kind"], r.get("symbol"),
             float(r["amount"]), r["time"]),
        )
        new += cur.rowcount
    db.commit()
    return new


def set_sync_log(user_id: int, exchange: str, status: str, message: str = ""):
    get_db().execute(
        """INSERT INTO sync_log(user_id, exchange, last_sync, status, message)
           VALUES (?,?,?,?,?)
           ON CONFLICT(user_id, exchange) DO UPDATE
           SET last_sync=excluded.last_sync, status=excluded.status,
               message=excluded.message""",
        (user_id, exchange, now_ms(), status, message[:500]),
    )
    get_db().commit()
