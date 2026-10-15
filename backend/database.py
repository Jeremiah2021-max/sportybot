"""
database.py – Persistence layer for Virtual Edge
=================================================
Supports two database backends determined by DATABASE_URL:

  SQLite   (default)   – zero config, file-based, perfect for local dev
                         and single-instance cloud deployments (Render free tier)
  PostgreSQL           – set DATABASE_URL=postgresql://... for production
                         (Render managed DB, Railway, Supabase, etc.)

TWO interfaces are provided:
  1. SQLAlchemy ORM  – engine + SessionLocal + get_db() FastAPI dependency
                       Used by auth routes and ORM-based queries.
  2. execute() / fetchall() helpers  – thin wrapper over psycopg2 / sqlite3
                       Used by the predict/feedback/stats/monitoring pipeline.
"""

import os
import uuid
import pathlib
import json as _json
from contextlib import contextmanager
from typing import Optional, Generator, List

from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import sessionmaker, Session as OrmSession

from models import Base

# ──────────────────────────────────────────────────────────────────
# DB URL resolution
# ──────────────────────────────────────────────────────────────────
DB_PATH = pathlib.Path(__file__).parent / "virtualedge.db"

# Render / Railway inject DATABASE_URL automatically for PostgreSQL add-ons.
# If not set → fall back to local SQLite file.
_raw_url: str = os.getenv("DATABASE_URL", "")

# Render sometimes provides "postgres://..." — SQLAlchemy needs "postgresql://"
if _raw_url.startswith("postgres://"):
    _raw_url = _raw_url.replace("postgres://", "postgresql://", 1)

# Force psycopg2 dialect so SQLAlchemy 2.x doesn't try to load psycopg3
if _raw_url.startswith("postgresql://") and "+psycopg" not in _raw_url:
    _raw_url = _raw_url.replace("postgresql://", "postgresql+psycopg2://", 1)

IS_POSTGRES = _raw_url.startswith("postgresql")
DB_URL      = _raw_url if IS_POSTGRES else f"sqlite:///{DB_PATH}"


# ──────────────────────────────────────────────────────────────────
# SQLAlchemy engine + session factory
# ──────────────────────────────────────────────────────────────────
_connect_args = {"check_same_thread": False} if not IS_POSTGRES else {}

engine = create_engine(
    DB_URL,
    connect_args  = _connect_args,
    echo          = False,
    # PostgreSQL pool settings
    pool_pre_ping = True,
    pool_recycle  = 300 if IS_POSTGRES else -1,
)

# Enable WAL mode for SQLite connections (better concurrent read performance)
if not IS_POSTGRES:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[OrmSession, None, None]:
    """FastAPI dependency — yields an ORM session, closes on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────
# Unified raw query helper
# Abstracts the SQLite / psycopg2 parameter style difference:
#   SQLite uses  ?      placeholders
#   psycopg2 uses %s    placeholders
# ──────────────────────────────────────────────────────────────────
def _adapt_sql(sql: str) -> str:
    """Convert ? placeholders → %s when running against PostgreSQL."""
    if IS_POSTGRES:
        return sql.replace("?", "%s")
    return sql


@contextmanager
def get_conn():
    """
    Yields a raw DB-API 2.0 connection (sqlite3 or psycopg2).
    Commits on success, rolls back on exception.
    """
    if IS_POSTGRES:
        import psycopg2
        import psycopg2.extras
        # psycopg2.connect needs plain postgresql:// not postgresql+psycopg2://
        _pg_url = _raw_url.replace("postgresql+psycopg2://", "postgresql://", 1)
        conn = psycopg2.connect(_pg_url, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        import sqlite3
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _execute(conn, sql: str, params: tuple = ()):
    """Execute a statement and return the cursor (works for both backends)."""
    cur = conn.cursor()
    cur.execute(_adapt_sql(sql), params)
    return cur


def _fetchall(conn, sql: str, params: tuple = ()) -> List[dict]:
    """Execute a SELECT and return all rows as plain dicts."""
    cur = _execute(conn, sql, params)
    rows = cur.fetchall()
    if not rows:
        return []
    # psycopg2 RealDictCursor → already dict-like; sqlite3 Row → needs dict()
    return [dict(r) for r in rows]


def _fetchone(conn, sql: str, params: tuple = ()) -> Optional[dict]:
    """Execute a SELECT and return one row as a plain dict, or None."""
    cur = _execute(conn, sql, params)
    row = cur.fetchone()
    return dict(row) if row else None


def _lastrowid(conn, cur) -> Optional[int]:
    """Get last inserted row ID in a backend-agnostic way."""
    if IS_POSTGRES:
        # psycopg2: use RETURNING id or lastrowid is None for some drivers
        row = cur.fetchone()
        return row["id"] if row else None
    return cur.lastrowid


# ──────────────────────────────────────────────────────────────────
# Schema helpers
# ──────────────────────────────────────────────────────────────────
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT    NOT NULL UNIQUE,
    full_name     TEXT,
    inferred_atk  REAL    DEFAULT 1.0,
    inferred_def  REAL    DEFAULT 1.0,
    total_matches INTEGER DEFAULT 0,
    wins          INTEGER DEFAULT 0,
    draws         INTEGER DEFAULT 0,
    losses        INTEGER DEFAULT 0,
    created_at    TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    bankroll   REAL    DEFAULT 0,
    created_at TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS fixtures (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL,
    home_code     TEXT    NOT NULL,
    away_code     TEXT    NOT NULL,
    odd1          REAL    NOT NULL,
    oddX          REAL    NOT NULL,
    odd2          REAL    NOT NULL,
    fair_p1       REAL,
    fair_pX       REAL,
    fair_p2       REAL,
    lambda_home   REAL,
    lambda_away   REAL,
    overround_pct REAL,
    created_at    TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id      INTEGER NOT NULL,
    pick            TEXT    NOT NULL,
    pick_label      TEXT,
    pick_odd        REAL,
    confidence      INTEGER,
    edge_pct        REAL,
    kelly_fraction  REAL,
    value_bet       INTEGER DEFAULT 0,
    reasoning       TEXT,
    market          TEXT    DEFAULT '1X2',
    predicted_at    TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (fixture_id) REFERENCES fixtures(id)
);
CREATE TABLE IF NOT EXISTS market_predictions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id    INTEGER NOT NULL UNIQUE,
    over25_prob   REAL,
    under25_prob  REAL,
    btts_prob     REAL,
    no_btts_prob  REAL,
    cs_1_0        REAL,
    cs_0_0        REAL,
    cs_1_1        REAL,
    cs_2_0        REAL,
    cs_0_1        REAL,
    cs_2_1        REAL,
    cs_top1_label TEXT,
    cs_top2_label TEXT,
    cs_top3_label TEXT,
    created_at    TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (fixture_id) REFERENCES fixtures(id)
);
CREATE TABLE IF NOT EXISTS outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id    INTEGER NOT NULL,
    prediction_id INTEGER,
    actual_result TEXT,
    home_goals    INTEGER,
    away_goals    INTEGER,
    is_correct    INTEGER,
    stake         REAL    DEFAULT 0,
    profit_loss   REAL    DEFAULT 0,
    logged_at     TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (fixture_id)    REFERENCES fixtures(id),
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);
CREATE TABLE IF NOT EXISTS bet_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT,
    prediction_id INTEGER,
    match_label   TEXT,
    stake         REAL,
    odds          REAL,
    result        TEXT,
    profit_loss   REAL,
    logged_at     TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (session_id)    REFERENCES sessions(id),
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);
CREATE VIEW IF NOT EXISTS performance_summary AS
SELECT
    COUNT(*)                                                        AS total_predictions,
    SUM(o.is_correct)                                               AS wins,
    COUNT(*) - SUM(o.is_correct)                                    AS losses,
    ROUND(AVG(o.is_correct) * 100, 1)                              AS win_rate_pct,
    ROUND(SUM(o.profit_loss), 2)                                    AS total_profit,
    ROUND(AVG(p.edge_pct), 2)                                       AS avg_edge_pct,
    ROUND(AVG(CASE WHEN p.value_bet=1 THEN o.is_correct END)*100,1) AS value_bet_win_rate
FROM outcomes o
JOIN predictions p ON o.prediction_id = p.id;
"""

# PostgreSQL equivalent (uses SERIAL, TIMESTAMPTZ, no AUTOINCREMENT keyword)
_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id            SERIAL PRIMARY KEY,
    code          TEXT    NOT NULL UNIQUE,
    full_name     TEXT,
    inferred_atk  REAL    DEFAULT 1.0,
    inferred_def  REAL    DEFAULT 1.0,
    total_matches INTEGER DEFAULT 0,
    wins          INTEGER DEFAULT 0,
    draws         INTEGER DEFAULT 0,
    losses        INTEGER DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    bankroll   REAL    DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS fixtures (
    id            SERIAL PRIMARY KEY,
    session_id    TEXT    NOT NULL REFERENCES sessions(id),
    home_code     TEXT    NOT NULL,
    away_code     TEXT    NOT NULL,
    odd1          REAL    NOT NULL,
    oddX          REAL    NOT NULL,
    odd2          REAL    NOT NULL,
    fair_p1       REAL,
    fair_pX       REAL,
    fair_p2       REAL,
    lambda_home   REAL,
    lambda_away   REAL,
    overround_pct REAL,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS predictions (
    id              SERIAL PRIMARY KEY,
    fixture_id      INTEGER NOT NULL REFERENCES fixtures(id),
    pick            TEXT    NOT NULL,
    pick_label      TEXT,
    pick_odd        REAL,
    confidence      INTEGER,
    edge_pct        REAL,
    kelly_fraction  REAL,
    value_bet       BOOLEAN DEFAULT FALSE,
    reasoning       TEXT,
    market          TEXT    DEFAULT '1X2',
    predicted_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS market_predictions (
    id            SERIAL PRIMARY KEY,
    fixture_id    INTEGER NOT NULL UNIQUE REFERENCES fixtures(id),
    over25_prob   REAL,
    under25_prob  REAL,
    btts_prob     REAL,
    no_btts_prob  REAL,
    cs_1_0        REAL, cs_0_0 REAL, cs_1_1 REAL,
    cs_2_0        REAL, cs_0_1 REAL, cs_2_1 REAL,
    cs_top1_label TEXT,
    cs_top2_label TEXT,
    cs_top3_label TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS outcomes (
    id            SERIAL PRIMARY KEY,
    fixture_id    INTEGER NOT NULL REFERENCES fixtures(id),
    prediction_id INTEGER REFERENCES predictions(id),
    actual_result TEXT,
    home_goals    INTEGER,
    away_goals    INTEGER,
    is_correct    BOOLEAN,
    stake         REAL    DEFAULT 0,
    profit_loss   REAL    DEFAULT 0,
    logged_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS bet_log (
    id            SERIAL PRIMARY KEY,
    session_id    TEXT REFERENCES sessions(id),
    prediction_id INTEGER REFERENCES predictions(id),
    match_label   TEXT,
    stake         REAL,
    odds          REAL,
    result        TEXT,
    profit_loss   REAL,
    logged_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE OR REPLACE VIEW performance_summary AS
SELECT
    COUNT(*)                                                              AS total_predictions,
    SUM(o.is_correct::int)                                                AS wins,
    COUNT(*) - SUM(o.is_correct::int)                                     AS losses,
    ROUND(AVG(o.is_correct::int) * 100, 1)                               AS win_rate_pct,
    ROUND(CAST(SUM(o.profit_loss) AS numeric), 2)                         AS total_profit,
    ROUND(CAST(AVG(p.edge_pct) AS numeric), 2)                            AS avg_edge_pct,
    ROUND(AVG(CASE WHEN p.value_bet THEN o.is_correct::int END)*100, 1)   AS value_bet_win_rate
FROM outcomes o
JOIN predictions p ON o.prediction_id = p.id;
"""


def _init_raw_schema():
    """Create raw tables/views not managed by SQLAlchemy ORM."""
    if IS_POSTGRES:
        with get_conn() as conn:
            cur = conn.cursor()
            for stmt in _POSTGRES_SCHEMA.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
    else:
        import sqlite3
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SQLITE_SCHEMA)
        conn.commit()
        conn.close()


def init_db():
    """Create all tables. Called at app startup."""
    Base.metadata.create_all(bind=engine)
    _init_raw_schema()
    backend = "PostgreSQL" if IS_POSTGRES else f"SQLite @ {DB_PATH}"
    print(f"[DB] Initialised — backend: {backend}")


# ──────────────────────────────────────────────────────────────────
# INSERT helpers — PostgreSQL uses RETURNING id; SQLite uses lastrowid
# ──────────────────────────────────────────────────────────────────
def _insert_returning(conn, sql: str, params: tuple) -> int:
    """
    Execute an INSERT and return the new row's id.
    Automatically appends RETURNING id for PostgreSQL.
    """
    if IS_POSTGRES:
        cur = _execute(conn, sql + " RETURNING id", params)
        row = cur.fetchone()
        return row["id"] if row else None
    else:
        cur = _execute(conn, sql, params)
        return cur.lastrowid


# ──────────────────────────────────────────────────────────────────
# Teams
# ──────────────────────────────────────────────────────────────────
def upsert_team(code: str, full_name: str = ""):
    with get_conn() as conn:
        if IS_POSTGRES:
            _execute(conn, """
                INSERT INTO teams (code, full_name)
                VALUES (%s, %s)
                ON CONFLICT (code) DO UPDATE SET
                    full_name = CASE WHEN EXCLUDED.full_name != '' THEN EXCLUDED.full_name ELSE teams.full_name END
            """, (code.upper(), full_name))
        else:
            _execute(conn, """
                INSERT INTO teams (code, full_name) VALUES (?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    full_name = CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE full_name END
            """, (code.upper(), full_name))


def update_team_record(code: str, result: str):
    col = {"win": "wins", "draw": "draws", "loss": "losses"}.get(result)
    if not col:
        return
    with get_conn() as conn:
        _execute(conn,
            f"UPDATE teams SET total_matches = total_matches + 1, {col} = {col} + 1 WHERE code = ?",
            (code.upper(),)
        )


def get_team_stats(code: str) -> Optional[dict]:
    with get_conn() as conn:
        return _fetchone(conn, "SELECT * FROM teams WHERE code = ?", (code.upper(),))


# ──────────────────────────────────────────────────────────────────
# Sessions
# ──────────────────────────────────────────────────────────────────
def create_session(bankroll: float = 0.0) -> str:
    sid = str(uuid.uuid4())
    with get_conn() as conn:
        _execute(conn, "INSERT INTO sessions (id, bankroll) VALUES (?, ?)", (sid, bankroll))
    return sid


def ensure_session(sid: str, bankroll: float = 0.0) -> str:
    with get_conn() as conn:
        row = _fetchone(conn, "SELECT id FROM sessions WHERE id = ?", (sid,))
        if row:
            return sid
    return create_session(bankroll)


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────
def insert_fixture(
    session_id: str,
    home: str, away: str,
    odd1: float, oddX: float, odd2: float,
    fair_p1: float, fair_pX: float, fair_p2: float,
    lambda_home: float, lambda_away: float,
    overround_pct: float,
) -> int:
    upsert_team(home)
    upsert_team(away)
    with get_conn() as conn:
        return _insert_returning(conn, """
            INSERT INTO fixtures
              (session_id, home_code, away_code,
               odd1, oddX, odd2,
               fair_p1, fair_pX, fair_p2,
               lambda_home, lambda_away, overround_pct)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            session_id, home.upper(), away.upper(),
            odd1, oddX, odd2,
            fair_p1, fair_pX, fair_p2,
            lambda_home, lambda_away, overround_pct,
        ))


# ──────────────────────────────────────────────────────────────────
# Predictions
# ──────────────────────────────────────────────────────────────────
def insert_prediction(
    fixture_id: int,
    pick: str, pick_label: str,
    pick_odd: float, confidence: int,
    edge_pct: float, kelly_fraction: float,
    value_bet: bool, reasoning: str,
    market: str = "1X2",
) -> int:
    with get_conn() as conn:
        return _insert_returning(conn, """
            INSERT INTO predictions
              (fixture_id, pick, pick_label, pick_odd, confidence,
               edge_pct, kelly_fraction, value_bet, reasoning, market)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            fixture_id, pick, pick_label, pick_odd, confidence,
            edge_pct, kelly_fraction, int(value_bet), reasoning, market,
        ))


def insert_market_prediction(fixture_id: int, markets: dict):
    with get_conn() as conn:
        if IS_POSTGRES:
            _execute(conn, """
                INSERT INTO market_predictions
                  (fixture_id, over25_prob, under25_prob, btts_prob, no_btts_prob,
                   cs_1_0, cs_0_0, cs_1_1, cs_2_0, cs_0_1, cs_2_1,
                   cs_top1_label, cs_top2_label, cs_top3_label)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (fixture_id) DO UPDATE SET
                    over25_prob=EXCLUDED.over25_prob,
                    under25_prob=EXCLUDED.under25_prob,
                    btts_prob=EXCLUDED.btts_prob,
                    no_btts_prob=EXCLUDED.no_btts_prob
            """, (
                fixture_id,
                markets.get("over25_prob"),  markets.get("under25_prob"),
                markets.get("btts_prob"),    markets.get("no_btts_prob"),
                markets.get("cs_1_0"),       markets.get("cs_0_0"),
                markets.get("cs_1_1"),       markets.get("cs_2_0"),
                markets.get("cs_0_1"),       markets.get("cs_2_1"),
                markets.get("cs_top1_label"), markets.get("cs_top2_label"),
                markets.get("cs_top3_label"),
            ))
        else:
            _execute(conn, """
                INSERT OR REPLACE INTO market_predictions
                  (fixture_id, over25_prob, under25_prob, btts_prob, no_btts_prob,
                   cs_1_0, cs_0_0, cs_1_1, cs_2_0, cs_0_1, cs_2_1,
                   cs_top1_label, cs_top2_label, cs_top3_label)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                fixture_id,
                markets.get("over25_prob"),  markets.get("under25_prob"),
                markets.get("btts_prob"),    markets.get("no_btts_prob"),
                markets.get("cs_1_0"),       markets.get("cs_0_0"),
                markets.get("cs_1_1"),       markets.get("cs_2_0"),
                markets.get("cs_0_1"),       markets.get("cs_2_1"),
                markets.get("cs_top1_label"), markets.get("cs_top2_label"),
                markets.get("cs_top3_label"),
            ))


# ──────────────────────────────────────────────────────────────────
# Outcomes
# ──────────────────────────────────────────────────────────────────
def insert_outcome(
    fixture_id: int,
    prediction_id: Optional[int],
    actual_result: str,
    is_correct: bool,
    stake: float = 0.0,
    profit_loss: float = 0.0,
):
    with get_conn() as conn:
        _execute(conn, """
            INSERT INTO outcomes
              (fixture_id, prediction_id, actual_result, is_correct, stake, profit_loss)
            VALUES (?,?,?,?,?,?)
        """, (fixture_id, prediction_id, actual_result, int(is_correct), stake, profit_loss))


def log_bet(
    session_id: str, prediction_id: Optional[int],
    match_label: str, stake: float, odds: float,
    result: str, profit_loss: float,
):
    with get_conn() as conn:
        _execute(conn, """
            INSERT INTO bet_log
              (session_id, prediction_id, match_label, stake, odds, result, profit_loss)
            VALUES (?,?,?,?,?,?,?)
        """, (session_id, prediction_id, match_label, stake, odds, result, profit_loss))


# ──────────────────────────────────────────────────────────────────
# Stats queries
# ──────────────────────────────────────────────────────────────────
def get_performance_summary() -> dict:
    with get_conn() as conn:
        row = _fetchone(conn, "SELECT * FROM performance_summary")
    if not row:
        return {"total_predictions": 0, "wins": 0, "losses": 0,
                "win_rate_pct": 0, "total_profit": 0,
                "avg_edge_pct": 0, "value_bet_win_rate": 0}
    return row


def get_team_leaderboard(limit: int = 20) -> list:
    with get_conn() as conn:
        return _fetchall(conn, """
            SELECT code, full_name, total_matches, wins, draws, losses,
                   ROUND(CAST(wins AS REAL) / GREATEST(total_matches, 1) * 100, 1) AS win_pct
            FROM teams
            WHERE total_matches > 0
            ORDER BY win_pct DESC, total_matches DESC
            LIMIT ?
        """, (limit,))


def get_recent_predictions(limit: int = 50) -> list:
    with get_conn() as conn:
        return _fetchall(conn, """
            SELECT p.id, f.home_code, f.away_code, p.pick, p.pick_odd,
                   p.confidence, p.edge_pct, p.value_bet,
                   o.is_correct, o.profit_loss, p.predicted_at
            FROM predictions p
            JOIN fixtures f ON p.fixture_id = f.id
            LEFT JOIN outcomes o ON o.prediction_id = p.id
            ORDER BY p.predicted_at DESC
            LIMIT ?
        """, (limit,))


def get_value_bet_stats() -> dict:
    with get_conn() as conn:
        row = _fetchone(conn, """
            SELECT
                COUNT(*)                                    AS total_value_bets,
                SUM(o.is_correct)                           AS value_wins,
                ROUND(AVG(o.is_correct) * 100, 1)          AS value_win_rate,
                ROUND(SUM(o.profit_loss), 2)                AS value_profit
            FROM predictions p
            JOIN outcomes o ON o.prediction_id = p.id
            WHERE p.value_bet = 1
        """)
    return row or {}


# ──────────────────────────────────────────────────────────────────
# Feedback JSONL helpers
# ──────────────────────────────────────────────────────────────────
FEEDBACK_FILE = pathlib.Path(__file__).parent / "feedback_store.jsonl"


def save_feedback(entry: dict):
    with FEEDBACK_FILE.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(entry) + "\n")


def load_recent_feedback(n: int = 20) -> list:
    if not FEEDBACK_FILE.exists():
        return []
    lines = FEEDBACK_FILE.read_text(encoding="utf-8").strip().splitlines()
    entries = []
    for line in lines[-n:]:
        try:
            entries.append(_json.loads(line))
        except Exception:
            pass
    return entries
