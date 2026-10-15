"""
monitoring.py – Accuracy Iteration Tracker for Virtual Edge
=============================================================
Answers Step 5.2: "Track your bot's true win-rate over 1,000 virtual games.
Adjust algorithm weightings if specific variables shift."

Produces a structured monitoring report with these sections:

  overall          – total resolved, win-rate, profit, ROI
  by_confidence    – win-rate split across LOW / MEDIUM / HIGH tiers
  by_market        – win-rate per market type (1X2, OU25, BTTS, CS)
  by_value_bet     – value-bet picks vs non-value picks compared
  trend            – rolling 20-prediction win-rate over time
  overround        – average bookmaker margin on resolved fixtures
  alerts           – auto-generated flags when something needs attention
  recommendations  – plain-English action items based on the data

All queries run directly against the DB via database.get_conn() so they
work with both SQLite and PostgreSQL.
"""

import os
from datetime import datetime, timezone
from typing import List, Optional

from database import get_conn, _fetchall, _fetchone, IS_POSTGRES

# ── Thresholds (can be overridden via .env) ─────────────────────────
MIN_SAMPLE          = int(os.getenv("MONITOR_MIN_SAMPLE",       50))
WIN_RATE_ALERT_PCT  = float(os.getenv("MONITOR_WIN_RATE_ALERT", 45))
VALUE_EDGE_FLOOR    = 3.0    # minimum edge% for a pick to be "value"
HIGH_CONF_FLOOR     = 80     # confidence score threshold for HIGH tier
MED_CONF_FLOOR      = 66     # confidence score threshold for MEDIUM tier
TREND_WINDOW        = 20     # rolling window for trend calculation
ROI_ALERT_PCT       = -10.0  # flag if ROI drops below this %


# ──────────────────────────────────────────────────────────────────
# Section 1 – Overall stats
# ──────────────────────────────────────────────────────────────────
def _overall(conn) -> dict:
    row = _fetchone(conn, """
        SELECT
            COUNT(*)                            AS total,
            COALESCE(SUM(o.is_correct::int), 0) AS wins,
            COUNT(*) - COALESCE(SUM(o.is_correct::int), 0) AS losses,
            ROUND(AVG(o.is_correct::int) * 100, 1)  AS win_rate,
            ROUND(SUM(o.profit_loss), 2)             AS total_profit,
            ROUND(SUM(o.stake), 2)                   AS total_staked,
            ROUND(
                CASE WHEN SUM(o.stake) > 0
                     THEN SUM(o.profit_loss) / SUM(o.stake) * 100
                     ELSE 0 END, 1)                  AS roi_pct
        FROM outcomes o
        JOIN predictions p ON o.prediction_id = p.id
    """) if IS_POSTGRES else _fetchone(conn, """
        SELECT
            COUNT(*)                           AS total,
            COALESCE(SUM(o.is_correct), 0)     AS wins,
            COUNT(*) - COALESCE(SUM(o.is_correct), 0) AS losses,
            ROUND(AVG(o.is_correct) * 100, 1)  AS win_rate,
            ROUND(SUM(o.profit_loss), 2)        AS total_profit,
            ROUND(SUM(o.stake), 2)              AS total_staked,
            ROUND(
                CASE WHEN SUM(o.stake) > 0
                     THEN SUM(o.profit_loss) / SUM(o.stake) * 100
                     ELSE 0 END, 1)             AS roi_pct
        FROM outcomes o
        JOIN predictions p ON o.prediction_id = p.id
    """)
    return row or {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                   "total_profit": 0, "total_staked": 0, "roi_pct": 0}


# ──────────────────────────────────────────────────────────────────
# Section 2 – Win-rate by confidence tier
# ──────────────────────────────────────────────────────────────────
def _by_confidence(conn) -> List[dict]:
    cast = "o.is_correct::int" if IS_POSTGRES else "o.is_correct"
    rows = _fetchall(conn, f"""
        SELECT
            CASE
                WHEN p.confidence >= {HIGH_CONF_FLOOR} THEN 'HIGH (80+)'
                WHEN p.confidence >= {MED_CONF_FLOOR}  THEN 'MEDIUM (66-79)'
                ELSE                                        'LOW (50-65)'
            END                                    AS tier,
            COUNT(*)                               AS total,
            COALESCE(SUM({cast}), 0)               AS wins,
            ROUND(AVG({cast}) * 100, 1)            AS win_rate,
            ROUND(AVG(p.confidence), 0)            AS avg_confidence,
            ROUND(SUM(o.profit_loss), 2)           AS profit
        FROM outcomes o
        JOIN predictions p ON o.prediction_id = p.id
        GROUP BY tier
        ORDER BY
            CASE tier
                WHEN 'HIGH (80+)'     THEN 1
                WHEN 'MEDIUM (66-79)' THEN 2
                ELSE                       3
            END
    """)
    return rows


# ──────────────────────────────────────────────────────────────────
# Section 3 – Win-rate by market type
# ──────────────────────────────────────────────────────────────────
def _by_market(conn) -> List[dict]:
    cast = "o.is_correct::int" if IS_POSTGRES else "o.is_correct"
    rows = _fetchall(conn, f"""
        SELECT
            p.market                              AS market,
            COUNT(*)                              AS total,
            COALESCE(SUM({cast}), 0)              AS wins,
            ROUND(AVG({cast}) * 100, 1)           AS win_rate,
            ROUND(AVG(p.pick_odd), 2)             AS avg_odd,
            ROUND(SUM(o.profit_loss), 2)          AS profit
        FROM outcomes o
        JOIN predictions p ON o.prediction_id = p.id
        GROUP BY p.market
        ORDER BY win_rate DESC
    """)
    return rows


# ──────────────────────────────────────────────────────────────────
# Section 4 – Value bets vs non-value bets
# ──────────────────────────────────────────────────────────────────
def _by_value_bet(conn) -> dict:
    cast = "o.is_correct::int" if IS_POSTGRES else "o.is_correct"
    vb_bool = "TRUE" if IS_POSTGRES else "1"
    rows = _fetchall(conn, f"""
        SELECT
            CASE WHEN p.value_bet = {vb_bool}
                 THEN 'Value Bet (edge ≥ {VALUE_EDGE_FLOOR}%)'
                 ELSE 'Standard Pick'
            END                                   AS category,
            COUNT(*)                              AS total,
            COALESCE(SUM({cast}), 0)              AS wins,
            ROUND(AVG({cast}) * 100, 1)           AS win_rate,
            ROUND(AVG(p.edge_pct), 2)             AS avg_edge_pct,
            ROUND(AVG(p.pick_odd), 2)             AS avg_odd,
            ROUND(SUM(o.profit_loss), 2)          AS profit
        FROM outcomes o
        JOIN predictions p ON o.prediction_id = p.id
        GROUP BY p.value_bet
        ORDER BY p.value_bet DESC
    """)
    return {r["category"]: r for r in rows}


# ──────────────────────────────────────────────────────────────────
# Section 5 – Rolling trend (last TREND_WINDOW resolved predictions)
# ──────────────────────────────────────────────────────────────────
def _trend(conn) -> dict:
    cast = "o.is_correct::int" if IS_POSTGRES else "o.is_correct"
    recent = _fetchall(conn, f"""
        SELECT
            {cast}               AS correct,
            o.profit_loss        AS profit,
            p.confidence         AS confidence,
            p.edge_pct           AS edge_pct,
            o.logged_at          AS logged_at
        FROM outcomes o
        JOIN predictions p ON o.prediction_id = p.id
        ORDER BY o.logged_at DESC
        LIMIT {TREND_WINDOW}
    """)

    if not recent:
        return {"window": TREND_WINDOW, "wins": 0, "win_rate": 0,
                "profit": 0, "direction": "insufficient_data", "data_points": []}

    wins   = sum(1 for r in recent if r.get("correct"))
    profit = sum(r.get("profit", 0) or 0 for r in recent)
    rate   = round(wins / len(recent) * 100, 1)

    # Compare against overall to determine trend direction
    direction = "stable"
    overall = _fetchone(conn, f"""
        SELECT ROUND(AVG({cast}) * 100, 1) AS win_rate
        FROM outcomes o JOIN predictions p ON o.prediction_id = p.id
    """)
    overall_rate = overall.get("win_rate") or 0 if overall else 0
    if rate > (overall_rate + 5):
        direction = "improving"
    elif rate < (overall_rate - 5):
        direction = "declining"

    # Reverse so oldest first for charting
    recent.reverse()
    data_points = [
        {
            "result":     "win" if r.get("correct") else "loss",
            "profit":     round(r.get("profit", 0) or 0, 2),
            "confidence": r.get("confidence"),
            "logged_at":  str(r.get("logged_at", "")),
        }
        for r in recent
    ]

    return {
        "window":      TREND_WINDOW,
        "wins":        wins,
        "total":       len(recent),
        "win_rate":    rate,
        "profit":      round(profit, 2),
        "direction":   direction,
        "overall_rate": overall_rate,
        "data_points": data_points,
    }


# ──────────────────────────────────────────────────────────────────
# Section 6 – Overround analysis
# ──────────────────────────────────────────────────────────────────
def _overround(conn) -> dict:
    row = _fetchone(conn, """
        SELECT
            ROUND(AVG(f.overround_pct), 2)  AS avg_overround,
            ROUND(MIN(f.overround_pct), 2)  AS min_overround,
            ROUND(MAX(f.overround_pct), 2)  AS max_overround,
            COUNT(DISTINCT f.id)             AS fixtures_analysed
        FROM fixtures f
        JOIN predictions p ON p.fixture_id = f.id
        JOIN outcomes o     ON o.prediction_id = p.id
    """)
    return row or {"avg_overround": 0, "min_overround": 0,
                   "max_overround": 0, "fixtures_analysed": 0}


# ──────────────────────────────────────────────────────────────────
# Section 7 – Auto-generated alerts + recommendations
# ──────────────────────────────────────────────────────────────────
def _alerts_and_recommendations(
    overall: dict,
    by_confidence: List[dict],
    by_value: dict,
    trend: dict,
) -> tuple[List[str], List[str]]:
    alerts  = []
    recs    = []
    total   = overall.get("total", 0)
    wr      = overall.get("win_rate") or 0
    roi     = overall.get("roi_pct")  or 0

    if total < MIN_SAMPLE:
        alerts.append(
            f"Only {total} resolved predictions — need {MIN_SAMPLE} for reliable stats. "
            f"Keep logging results."
        )
        recs.append(
            f"Log at least {MIN_SAMPLE - total} more results before drawing conclusions."
        )
        return alerts, recs   # not enough data for further analysis

    # Win-rate alert
    if wr < WIN_RATE_ALERT_PCT:
        alerts.append(
            f"Overall win-rate {wr}% is below the alert threshold of {WIN_RATE_ALERT_PCT}%. "
            f"Review your picks."
        )

    # ROI alert
    if roi < ROI_ALERT_PCT:
        alerts.append(
            f"ROI is {roi}% — negative returns. Consider reducing stake sizes."
        )
        recs.append("Apply a stricter edge filter (raise VALUE_THRESHOLD to 5%) to reduce bet count.")

    # Trend
    if trend.get("direction") == "declining":
        alerts.append(
            f"Recent {trend['window']}-prediction win-rate ({trend['win_rate']}%) "
            f"is declining vs overall ({trend['overall_rate']}%)."
        )
        recs.append("Check if the virtual engine was recently updated — odds patterns may have shifted.")

    if trend.get("direction") == "improving":
        alerts.append(
            f"Recent {trend['window']}-prediction win-rate ({trend['win_rate']}%) "
            f"is improving vs overall ({trend['overall_rate']}%). Keep the current settings."
        )

    # Confidence tier analysis
    for tier in by_confidence:
        tier_total = tier.get("total", 0)
        tier_wr    = tier.get("win_rate") or 0
        tier_name  = tier.get("tier", "")
        if tier_total >= 20 and "HIGH" in tier_name and tier_wr < 55:
            alerts.append(
                f"HIGH confidence picks win at only {tier_wr}% ({tier_total} predictions). "
                f"Confidence scoring may need recalibration."
            )
            recs.append(
                "Investigate HIGH confidence picks — if win-rate is below MEDIUM tier, "
                "the confidence model is over-scoring. Reduce HIGH threshold to 85+."
            )

    # Value-bet analysis
    vb_key = next((k for k in by_value if "Value" in k), None)
    std_key = next((k for k in by_value if "Standard" in k), None)
    if vb_key and std_key:
        vb_wr  = by_value[vb_key].get("win_rate")  or 0
        std_wr = by_value[std_key].get("win_rate") or 0
        if vb_wr < std_wr and by_value[vb_key].get("total", 0) >= 20:
            alerts.append(
                f"Value bets ({vb_wr}%) are underperforming standard picks ({std_wr}%). "
                f"Edge detection may need recalibration."
            )
            recs.append(
                "Raise the VALUE_THRESHOLD in analytics.py from 3% to 5% to only flag "
                "picks with stronger edge signals."
            )
        elif vb_wr > std_wr + 5:
            recs.append(
                f"Value bets outperform standard picks by {round(vb_wr - std_wr, 1)}pp. "
                f"Prioritise value bets in your staking."
            )

    if not alerts:
        alerts.append("All metrics within normal range.")
    if not recs:
        recs.append("No immediate action needed. Continue accumulating results.")

    return alerts, recs


# ──────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────
def generate_report() -> dict:
    """
    Run the full monitoring pipeline and return a structured report dict.
    Called by the /api/monitoring endpoint.
    """
    with get_conn() as conn:
        overall     = _overall(conn)
        by_conf     = _by_confidence(conn)
        by_market   = _by_market(conn)
        by_value    = _by_value_bet(conn)
        trend       = _trend(conn)
        overround   = _overround(conn)

    alerts, recs = _alerts_and_recommendations(overall, by_conf, by_value, trend)

    return {
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "overall":           overall,
        "by_confidence":     by_conf,
        "by_market":         by_market,
        "by_value_bet":      by_value,
        "trend":             trend,
        "overround":         overround,
        "alerts":            alerts,
        "recommendations":   recs,
        "thresholds": {
            "min_sample":         MIN_SAMPLE,
            "win_rate_alert_pct": WIN_RATE_ALERT_PCT,
            "value_edge_floor":   VALUE_EDGE_FLOOR,
            "high_conf_floor":    HIGH_CONF_FLOOR,
            "trend_window":       TREND_WINDOW,
        },
    }
