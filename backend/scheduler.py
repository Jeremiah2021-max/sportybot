"""
scheduler.py – APScheduler Results-Checker for Virtual Edge
============================================================
Runs a background job every POLL_INTERVAL minutes that:

  1. Queries the DB for predictions created > STALE_AFTER minutes ago
     with no recorded outcome (is_correct IS NULL).

  2. Computes a live accuracy snapshot from all resolved outcomes.

  3. Broadcasts two WebSocket messages to all connected clients:
       • accuracy_update  – latest win rate, profit, value-bet stats
       • pending_alert    – how many predictions still need results logged

  4. Does NOT auto-resolve predictions (virtual results are not
     publicly available via API) — instead nudges the user via WS
     to log them manually through the Results Checker UI.

APScheduler uses the AsyncIOScheduler so it shares the FastAPI
event loop and can await WebSocket broadcasts.
"""

import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import get_conn
from websocket_manager import manager as ws_manager

log = logging.getLogger("scheduler")

# ── Tuneable constants ─────────────────────────────────────────────
POLL_INTERVAL = 5      # minutes between each check
STALE_AFTER   = 10     # minutes after prediction before it's "pending"
MAX_PENDING   = 50     # cap returned in the pending list


# ──────────────────────────────────────────────────────────────────
# DB helpers (raw SQLite — no ORM overhead in background jobs)
# ──────────────────────────────────────────────────────────────────
def get_pending_predictions(limit: int = MAX_PENDING) -> list:
    """
    Returns predictions that are older than STALE_AFTER minutes
    and have no outcome row (i.e., is_correct IS NULL).
    """
    cutoff = (datetime.utcnow() - timedelta(minutes=STALE_AFTER)).isoformat()
    from database import IS_POSTGRES

    with get_conn() as conn:
        if IS_POSTGRES:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    p.id            AS prediction_id,
                    p.fixture_id,
                    f.home_code     AS home,
                    f.away_code     AS away,
                    f.odd1, f.oddX, f.odd2,
                    p.pick,
                    p.pick_label,
                    p.pick_odd,
                    p.confidence,
                    p.edge_pct,
                    p.value_bet,
                    p.reasoning,
                    p.market,
                    p.predicted_at,
                    o.is_correct
                FROM predictions p
                JOIN fixtures f ON p.fixture_id = f.id
                LEFT JOIN outcomes o ON o.prediction_id = p.id
                WHERE o.id IS NULL
                  AND p.predicted_at < %s
                ORDER BY p.predicted_at DESC
                LIMIT %s
            """, (cutoff, limit))
            rows = cursor.fetchall()
        else:
            rows = conn.execute("""
                SELECT
                    p.id            AS prediction_id,
                    p.fixture_id,
                    f.home_code     AS home,
                    f.away_code     AS away,
                    f.odd1, f.oddX, f.odd2,
                    p.pick,
                    p.pick_label,
                    p.pick_odd,
                    p.confidence,
                    p.edge_pct,
                    p.value_bet,
                    p.reasoning,
                    p.market,
                    p.predicted_at,
                    o.is_correct
                FROM predictions p
                JOIN fixtures f ON p.fixture_id = f.id
                LEFT JOIN outcomes o ON o.prediction_id = p.id
                WHERE o.id IS NULL
                  AND p.predicted_at < ?
                ORDER BY p.predicted_at DESC
                LIMIT ?
            """, (cutoff, limit)).fetchall()
    return [dict(r) for r in rows]


def get_accuracy_snapshot() -> dict:
    """Fast accuracy summary from all resolved outcomes."""
    from database import IS_POSTGRES

    with get_conn() as conn:
        if IS_POSTGRES:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*)                                     AS total,
                    COALESCE(SUM(o.is_correct), 0)               AS wins,
                    COUNT(*) - COALESCE(SUM(o.is_correct), 0)    AS losses,
                    ROUND(AVG(o.is_correct) * 100, 1)            AS win_rate,
                    ROUND(SUM(o.profit_loss), 2)                  AS profit,
                    COUNT(CASE WHEN p.value_bet=1 THEN 1 END)    AS total_value_bets,
                    ROUND(AVG(CASE WHEN p.value_bet=1
                                   THEN o.is_correct END)*100,1) AS value_win_rate
                FROM outcomes o
                JOIN predictions p ON o.prediction_id = p.id
            """)
            row = cursor.fetchone()
        else:
            row = conn.execute("""
                SELECT
                    COUNT(*)                                     AS total,
                    COALESCE(SUM(o.is_correct), 0)               AS wins,
                    COUNT(*) - COALESCE(SUM(o.is_correct), 0)    AS losses,
                    ROUND(AVG(o.is_correct) * 100, 1)            AS win_rate,
                    ROUND(SUM(o.profit_loss), 2)                  AS profit,
                    COUNT(CASE WHEN p.value_bet=1 THEN 1 END)    AS total_value_bets,
                    ROUND(AVG(CASE WHEN p.value_bet=1
                                   THEN o.is_correct END)*100,1) AS value_win_rate
                FROM outcomes o
                JOIN predictions p ON o.prediction_id = p.id
            """).fetchone()
    if not row:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "profit": 0, "total_value_bets": 0, "value_win_rate": 0}
    return dict(row)


def resolve_prediction(prediction_id: int, result: str,
                       stake: float = 0.0) -> dict:
    """
    Mark a prediction as won or lost.
    result: "win" | "loss"
    Returns the updated accuracy snapshot.
    """
    from database import IS_POSTGRES

    is_correct = result == "win"
    with get_conn() as conn:
        if IS_POSTGRES:
            cursor = conn.cursor()
            # Check prediction exists and isn't already resolved
            cursor.execute(
                "SELECT id, fixture_id, pick_odd FROM predictions WHERE id = %s",
                (prediction_id,)
            )
            pred = cursor.fetchone()
            if not pred:
                raise ValueError(f"Prediction {prediction_id} not found")

            cursor.execute(
                "SELECT id FROM outcomes WHERE prediction_id = %s",
                (prediction_id,)
            )
            existing = cursor.fetchone()
            if existing:
                raise ValueError(f"Prediction {prediction_id} already resolved")

            profit = stake * (float(pred["pick_odd"]) - 1) if is_correct else -stake

            cursor.execute("""
                INSERT INTO outcomes
                  (fixture_id, prediction_id, actual_result, is_correct, stake, profit_loss)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                pred["fixture_id"],
                prediction_id,
                result,
                int(is_correct),
                stake,
                profit,
            ))
        else:
            # Check prediction exists and isn't already resolved
            pred = conn.execute(
                "SELECT id, fixture_id, pick_odd FROM predictions WHERE id = ?",
                (prediction_id,)
            ).fetchone()
            if not pred:
                raise ValueError(f"Prediction {prediction_id} not found")

            existing = conn.execute(
                "SELECT id FROM outcomes WHERE prediction_id = ?",
                (prediction_id,)
            ).fetchone()
            if existing:
                raise ValueError(f"Prediction {prediction_id} already resolved")

            profit = stake * (float(pred["pick_odd"]) - 1) if is_correct else -stake

            conn.execute("""
                INSERT INTO outcomes
                  (fixture_id, prediction_id, actual_result, is_correct, stake, profit_loss)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                pred["fixture_id"],
                prediction_id,
                result,
                int(is_correct),
                stake,
                profit,
            ))

    return get_accuracy_snapshot()


# ──────────────────────────────────────────────────────────────────
# Scheduled job
# ──────────────────────────────────────────────────────────────────
async def results_checker_job():
    """
    Runs every POLL_INTERVAL minutes.
    Broadcasts current accuracy + pending count to all open browser tabs.
    """
    log.info("[Scheduler] Running results checker…")

    pending  = get_pending_predictions()
    accuracy = get_accuracy_snapshot()

    # Broadcast accuracy update to every connected client
    if accuracy["total"] > 0:
        await ws_manager.broadcast({
            "type":     "accuracy_update",
            "accuracy": accuracy,
            "ts":       datetime.utcnow().isoformat(),
        })

    # Broadcast pending alert if there are unresolved predictions
    if pending:
        await ws_manager.broadcast({
            "type":    "pending_alert",
            "count":   len(pending),
            "message": (
                f"📋 {len(pending)} prediction"
                f"{'s' if len(pending) != 1 else ''} waiting for results. "
                f"Log them in the Results Checker to track accuracy."
            ),
            "ts": datetime.utcnow().isoformat(),
        })
        log.info(f"[Scheduler] {len(pending)} pending predictions broadcast")
    else:
        log.info("[Scheduler] No pending predictions")


# ──────────────────────────────────────────────────────────────────
# Scheduler factory
# ──────────────────────────────────────────────────────────────────
def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        results_checker_job,
        trigger=IntervalTrigger(minutes=POLL_INTERVAL),
        id="results_checker",
        name="Results Checker",
        replace_existing=True,
        misfire_grace_time=60,
    )
    return scheduler
