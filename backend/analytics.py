"""
analytics.py – Analytics Engine for Virtual Edge
=================================================
Takes a list of OCR-extracted matches and produces a fully standardised
prediction payload for every match, combining:

  1. Poisson goal model  (from poisson.py)
     → fair 1X2 probabilities, Over/Under 2.5, BTTS, correct scores,
       expected goals (λ), Asian Handicap

  2. Kelly Criterion staking
     → optimal stake fraction for each outcome

  3. Value detection
     → any pick where fair_prob > implied_prob by VALUE_THRESHOLD

  4. Confidence scoring
     → tiered 0–100 score based on edge size and probability

  5. Standardised output payload (matches the /api/predict response schema)

Public API:
    run_analytics(matches: list[dict], bankroll: float = 0) -> AnalyticsResult
"""

import math
from typing import List, Dict, Optional

from poisson import analyse_match

# ──────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────
VALUE_THRESHOLD   = 0.03   # minimum edge % to flag as value bet
KELLY_FRACTION    = 0.25   # fractional Kelly (conservative)
MAX_KELLY_PCT     = 0.15   # never stake more than 15% of bankroll
MIN_CONFIDENCE    = 50     # floor confidence score
ACCA_MIN_LEGS     = 2
ACCA_MAX_LEGS     = 3
ACCA_MIN_COMBINED_PROB = 0.40   # combined probability floor for accumulator


# ──────────────────────────────────────────────────────────────────
# Data classes (plain dicts for JSON serialisation)
# ──────────────────────────────────────────────────────────────────
def _pick_confidence(edge_pct: float, fair_prob: float) -> int:
    """
    Maps edge size and probability into a 50–100 confidence score.
    High edge + high probability = high confidence.
    """
    edge_score = min(edge_pct * 6, 30)           # max +30 from edge
    prob_score = fair_prob * 70                   # max +70 from probability
    raw        = MIN_CONFIDENCE + edge_score + (prob_score - 35)
    return max(MIN_CONFIDENCE, min(100, round(raw)))


def _kelly_stake(fair_prob: float, book_odd: float, bankroll: float) -> float:
    """Full Kelly × KELLY_FRACTION × bankroll, capped at MAX_KELLY_PCT."""
    if bankroll <= 0 or book_odd <= 1.0:
        return 0.0
    b = book_odd - 1.0
    q = 1.0 - fair_prob
    f = (b * fair_prob - q) / b
    if f <= 0:
        return 0.0
    stake = f * KELLY_FRACTION * bankroll
    return round(min(stake, bankroll * MAX_KELLY_PCT), 2)


def _edge(fair_prob: float, book_odd: float) -> float:
    """
    Edge in percentage points.
    fair_prob  = probability after stripping overround (true probability)
    implied    = 1/odd (bookmaker's stated probability, includes margin)
    Positive edge means fair_prob > what bookie implies → value exists.
    """
    implied = 1.0 / book_odd if book_odd > 0 else 0.0
    # fair_prob is already de-margined, so compare against raw implied
    return round((fair_prob - implied) * 100, 2)


def _best_1x2_pick(
    fair_p1: float, fair_pX: float, fair_p2: float,
    o1: float, oX: float, o2: float,
    home: str, away: str,
) -> dict:
    """Return the best-value single 1X2 pick."""
    outcomes = [
        {"pick": "1", "label": f"HOME WIN ({home})", "fair_prob": fair_p1, "odd": o1},
        {"pick": "X", "label": "DRAW",                "fair_prob": fair_pX, "odd": oX},
        {"pick": "2", "label": f"AWAY WIN ({away})",  "fair_prob": fair_p2, "odd": o2},
    ]
    for o in outcomes:
        o["edge_pct"]   = _edge(o["fair_prob"], o["odd"])
        o["confidence"] = _pick_confidence(max(o["edge_pct"], 0), o["fair_prob"])

    # Sort: value bets first (edge ≥ threshold), then by confidence
    value = [o for o in outcomes if o["edge_pct"] >= VALUE_THRESHOLD * 100]
    value.sort(key=lambda x: (x["edge_pct"], x["confidence"]), reverse=True)
    by_conf = sorted(outcomes, key=lambda x: x["confidence"], reverse=True)

    best = value[0] if value else by_conf[0]
    return {
        "pick":       best["pick"],
        "label":      best["label"],
        "odd":        f"{best['odd']:.2f}",
        "confidence": best["confidence"],
        "edge_pct":   best["edge_pct"],
        "reasoning":  _reasoning(best["pick"], best["edge_pct"], best["fair_prob"],
                                 best["odd"], home, away),
    }


def _best_double_chance(
    fair_p1: float, fair_pX: float, fair_p2: float,
    o1: float, oX: float, o2: float,
    home: str, away: str,
) -> dict:
    """Return the safest double-chance pick with explicit team names."""
    dc_options = [
        {"pick": "1X", "label": f"{home} or Draw (Home/Draw)", "prob": fair_p1 + fair_pX},
        {"pick": "X2", "label": f"Draw or {away} (Draw/Away)", "prob": fair_pX + fair_p2},
        {"pick": "12", "label": f"{home} or {away} (Home/Away)", "prob": fair_p1 + fair_p2},
    ]
    best = max(dc_options, key=lambda x: x["prob"])

    # Approximate DC odd (remove overround from combined prob)
    approx_odd = round(1.0 / (best["prob"] * 0.92), 2)
    conf       = _pick_confidence(0, best["prob"])

    return {
        "pick":       best["pick"],
        "label":      f"Double Chance {best['pick']}: {best['label']}",
        "odd":        f"{approx_odd:.2f}",
        "confidence": conf,
        "edge_pct":   0.0,
        "reasoning":  (
            f"Double chance {best['pick']} ({best['label']}) covers {round(best['prob']*100)}% "
            f"of outcomes — safest option for {home} vs {away}."
        ),
    }


def _reasoning(pick: str, edge: float, fair_prob: float, odd: float,
               home: str, away: str) -> str:
    """Generate a one-sentence reasoning string from stats."""
    team  = home if pick == "1" else (away if pick == "2" else "either side")
    trend = "value" if edge >= VALUE_THRESHOLD * 100 else "probability"
    return (
        f"Pick {pick} ({team}) has a fair probability of {round(fair_prob*100)}% "
        f"with {edge:+.1f}% edge — selected by {trend}."
    )


def _ou25_pick(over_prob: float, under_prob: float,
               over_edge: float, under_edge: float) -> dict:
    """Return the better-value Over/Under 2.5 pick."""
    if over_edge >= under_edge and over_edge >= 0:
        return {
            "pick": "Over 2.5", "prob": over_prob,
            "edge_pct": over_edge, "has_value": over_edge >= VALUE_THRESHOLD * 100,
            "confidence": _pick_confidence(max(over_edge, 0), over_prob),
        }
    return {
        "pick": "Under 2.5", "prob": under_prob,
        "edge_pct": under_edge, "has_value": under_edge >= VALUE_THRESHOLD * 100,
        "confidence": _pick_confidence(max(under_edge, 0), under_prob),
    }


def _btts_pick(btts_prob: float, no_prob: float,
               btts_edge: float, no_edge: float) -> dict:
    if btts_edge >= no_edge and btts_edge >= 0:
        return {
            "pick": "BTTS Yes", "prob": btts_prob,
            "edge_pct": btts_edge, "has_value": btts_edge >= VALUE_THRESHOLD * 100,
            "confidence": _pick_confidence(max(btts_edge, 0), btts_prob),
        }
    return {
        "pick": "BTTS No", "prob": no_prob,
        "edge_pct": no_edge, "has_value": no_edge >= VALUE_THRESHOLD * 100,
        "confidence": _pick_confidence(max(no_edge, 0), no_prob),
    }


# ──────────────────────────────────────────────────────────────────
# Accumulator builder
# ──────────────────────────────────────────────────────────────────
def _build_accumulator(analysed: list) -> dict:
    """
    Select 2-3 legs with positive edge and highest combined probability.
    Returns accumulator dict with legs, combined_odd, combined_prob.
    """
    # Candidates: best pick per match that has positive edge
    candidates = []
    for m in analysed:
        bp = m["best_pick"]
        if bp["edge_pct"] > 0:
            candidates.append({
                "home":      m["home"],
                "away":      m["away"],
                "pick":      bp["pick"],
                "odd":       float(bp["odd"]),
                "prob":      m["fair_p1"] if bp["pick"] == "1"
                             else (m["fair_pX"] if bp["pick"] == "X" else m["fair_p2"]),
                "edge_pct":  bp["edge_pct"],
                "reasoning": bp["reasoning"],
            })

    # Sort by edge descending, take top ACCA_MAX_LEGS
    candidates.sort(key=lambda x: x["edge_pct"], reverse=True)
    legs = candidates[:ACCA_MAX_LEGS]

    if len(legs) < ACCA_MIN_LEGS:
        return {"legs": [], "combined_odd": "—", "combined_prob": 0, "reasoning": ""}

    combined_odd  = round(math.prod(l["odd"]  for l in legs), 2)
    combined_prob = round(math.prod(l["prob"] for l in legs), 4)

    if combined_prob < ACCA_MIN_COMBINED_PROB:
        # Try 2-leg version
        legs = legs[:2]
        combined_odd  = round(math.prod(l["odd"]  for l in legs), 2)
        combined_prob = round(math.prod(l["prob"] for l in legs), 4)

    return {
        "legs":          legs,
        "combined_odd":  f"{combined_odd:.2f}",
        "combined_prob": combined_prob,
        "reasoning": (
            f"{len(legs)}-leg acca with {round(combined_prob*100)}% combined probability "
            f"at odds {combined_odd:.2f} — all legs have positive edge."
        ),
    }


# ──────────────────────────────────────────────────────────────────
# Main public function
# ──────────────────────────────────────────────────────────────────
def run_analytics(matches: List[dict], bankroll: float = 0.0) -> dict:
    """
    Core analytics engine.

    Args:
        matches:  list of dicts with keys: home, away, odd1, oddX, odd2
        bankroll: current user bankroll in GHS (0 = skip Kelly staking)

    Returns:
        {
          "predictions": [ MatchAnalysis, ... ],
          "accumulator": { legs, combined_odd, combined_prob, reasoning },
          "summary": { total, value_bets, best_pick_label, best_edge_pct }
        }

    Each MatchAnalysis dict has this standard payload:
        {
          "home": "ARS", "away": "BRE",
          "prediction": "HOME WIN (ARS)",   ← primary pick label
          "pick": "1",
          "confidence": "78%",
          "edge_pct": 4.2,
          "odd": "2.10",
          "value_bet": True,
          "kelly_stake": 12.50,             ← GHS amount (0 if no bankroll)
          "fair_p1": 0.48, "fair_pX": 0.28, "fair_p2": 0.24,
          "overround_pct": 8.1,
          "lambda_home": 1.42, "lambda_away": 1.08,
          "best_pick":  { pick, label, odd, confidence, edge_pct, reasoning },
          "safe_pick":  { pick, label, odd, confidence, edge_pct, reasoning },
          "markets": {
            "over25":  { pick, prob, edge_pct, has_value, confidence },
            "btts":    { pick, prob, edge_pct, has_value, confidence },
            "correct_scores": [ {label, prob, fair_odd}, ... ],
            "asian_handicap": { home_ah_prob, away_ah_prob }
          }
        }
    """
    results = []

    for m in matches:
        home = str(m.get("home", "")).strip().upper()
        away = str(m.get("away", "")).strip().upper()

        try:
            o1 = float(m.get("odd1", 0))
            oX = float(m.get("oddX", 0))
            o2 = float(m.get("odd2", 0))
        except (ValueError, TypeError):
            continue

        if not (1.05 <= o1 <= 20.0 and 1.05 <= oX <= 20.0 and 1.05 <= o2 <= 20.0):
            continue
        if not home or not away or home == away:
            continue

        # ── Poisson analysis ──────────────────────────────────────
        try:
            pa = analyse_match(o1, oX, o2)
        except Exception as e:
            print(f"Poisson failed for {home} vs {away}: {e}")
            continue

        fair_p1 = pa["fair_p1"]
        fair_pX = pa["fair_pX"]
        fair_p2 = pa["fair_p2"]

        # ── Best 1X2 pick ─────────────────────────────────────────
        best_pick = _best_1x2_pick(fair_p1, fair_pX, fair_p2, o1, oX, o2, home, away)
        safe_pick = _best_double_chance(fair_p1, fair_pX, fair_p2, o1, oX, o2, home, away)

        # ── Value flag ────────────────────────────────────────────
        is_value = best_pick["edge_pct"] >= VALUE_THRESHOLD * 100

        # ── Kelly stake ───────────────────────────────────────────
        pick_odd_f = float(best_pick["odd"])
        pick_prob  = (fair_p1 if best_pick["pick"] == "1"
                      else (fair_pX if best_pick["pick"] == "X" else fair_p2))
        kelly_stake = _kelly_stake(pick_prob, pick_odd_f, bankroll)

        # ── Derived markets ───────────────────────────────────────
        over_edge  = pa["over25_edge"]["edge_pct"]
        under_edge = -over_edge * 0.6   # rough mirror
        btts_edge  = pa["btts_edge"]["edge_pct"]
        nobt_edge  = -btts_edge * 0.6

        ou_pick   = _ou25_pick(pa["over25_prob"], pa["under25_prob"],
                                over_edge, under_edge)
        btts_p    = _btts_pick(pa["btts_prob"], pa["no_btts_prob"],
                                btts_edge, nobt_edge)

        markets = {
            "over25":          ou_pick,
            "btts":            btts_p,
            "correct_scores":  pa["correct_scores"],
            "asian_handicap":  pa["asian_handicap"],
        }

        results.append({
            # Standard payload fields
            "home":            home,
            "away":            away,
            "prediction":      best_pick["label"],       # ← canonical field
            "pick":            best_pick["pick"],
            "confidence":      f"{best_pick['confidence']}%",
            "confidence_int":  best_pick["confidence"],
            "edge_pct":        best_pick["edge_pct"],
            "odd":             best_pick["odd"],
            "value_bet":       is_value,
            "kelly_stake":     kelly_stake,
            "kelly_fraction":  round(pa["kelly_1"] if best_pick["pick"] == "1"
                                     else (pa["kelly_X"] if best_pick["pick"] == "X"
                                           else pa["kelly_2"]), 4),

            # Probability breakdown
            "fair_p1":         fair_p1,
            "fair_pX":         fair_pX,
            "fair_p2":         fair_p2,
            "overround_pct":   pa["overround_pct"],

            # Poisson parameters
            "lambda_home":     pa["lambda_home"],
            "lambda_away":     pa["lambda_away"],

            # Full pick objects
            "best_pick":       best_pick,
            "safe_pick":       safe_pick,

            # All markets
            "markets":         markets,
        })

    # ── Sort: value bets first, then by confidence ────────────────
    results.sort(
        key=lambda x: (int(x["value_bet"]), x["edge_pct"]),
        reverse=True
    )

    # ── Accumulator ───────────────────────────────────────────────
    acca = _build_accumulator(results)

    # ── Summary ───────────────────────────────────────────────────
    value_bets = [r for r in results if r["value_bet"]]
    best       = results[0] if results else None

    summary = {
        "total":            len(results),
        "value_bets":       len(value_bets),
        "best_pick_label":  best["prediction"] if best else "—",
        "best_confidence":  best["confidence"] if best else "—",
        "best_edge_pct":    best["edge_pct"]   if best else 0.0,
        "has_value":        len(value_bets) > 0,
    }

    return {
        "predictions": results,
        "accumulator": acca,
        "summary":     summary,
    }
