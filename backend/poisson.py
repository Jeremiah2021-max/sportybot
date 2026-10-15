"""
poisson.py – Poisson goal model for Virtual Edge
=================================================
Pipeline:
  1. Strip bookmaker overround from 1X2 odds → fair probabilities
  2. Solve for λ_home, λ_away (expected goals) that reproduce those probabilities
  3. Build NxN score probability matrix (Dixon-Coles low-score correction)
  4. Derive all markets from the matrix:
       • Over/Under 2.5 goals
       • Both Teams to Score (BTTS)
       • Top-3 correct scores
       • Asian Handicap ±0.5
  5. Return Kelly-ready edge figures for each market
"""

import math
from typing import Dict, List, Tuple


# ──────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────
MAX_GOALS   = 7      # score matrix dimension (0..MAX_GOALS each side)
RHO         = -0.13  # Dixon-Coles low-score dependency correction
BOOKMAKER_MARGIN_OU   = 0.08   # assumed OU2.5 overround (8%)
BOOKMAKER_MARGIN_BTTS = 0.08   # assumed BTTS overround


# ──────────────────────────────────────────────────────────────────
# Step 1: Overround removal
# ──────────────────────────────────────────────────────────────────
def remove_overround(o1: float, oX: float, o2: float) -> Tuple[float, float, float, float]:
    """
    Convert raw bookmaker 1X2 odds to fair probabilities.
    Returns (fair_p1, fair_pX, fair_p2, overround_pct).
    """
    raw1 = 1.0 / o1
    rawX = 1.0 / oX
    raw2 = 1.0 / o2
    margin = raw1 + rawX + raw2          # e.g. 1.082 → 8.2% margin
    fair_p1 = raw1 / margin
    fair_pX = rawX / margin
    fair_p2 = raw2 / margin
    overround_pct = (margin - 1.0) / margin * 100.0
    return fair_p1, fair_pX, fair_p2, overround_pct


# ──────────────────────────────────────────────────────────────────
# Step 2: Poisson PMF + Dixon-Coles correction
# ──────────────────────────────────────────────────────────────────
def poisson_pmf(k: int, lam: float) -> float:
    """P(X = k) for Poisson(λ)."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def dixon_coles_tau(home: int, away: int, lh: float, la: float, rho: float) -> float:
    """
    Multiplicative correction for low-scoring cells (0-0, 1-0, 0-1, 1-1).
    Introduced by Dixon & Coles (1997) to correct Poisson independence assumption.
    """
    if home == 0 and away == 0:
        return 1.0 - lh * la * rho
    if home == 1 and away == 0:
        return 1.0 + la * rho
    if home == 0 and away == 1:
        return 1.0 + lh * rho
    if home == 1 and away == 1:
        return 1.0 - rho
    return 1.0


# ──────────────────────────────────────────────────────────────────
# Step 3: Score probability matrix
# ──────────────────────────────────────────────────────────────────
def score_matrix(lh: float, la: float, n: int = MAX_GOALS) -> List[List[float]]:
    """
    Returns matrix[home_goals][away_goals] = P(score = h:a).
    Rows = home goals (0..n), columns = away goals (0..n).
    """
    mat = []
    for h in range(n + 1):
        row = []
        for a in range(n + 1):
            p = (poisson_pmf(h, lh) *
                 poisson_pmf(a, la) *
                 dixon_coles_tau(h, a, lh, la, RHO))
            row.append(p)
        mat.append(row)

    # Normalise so matrix sums to 1.0
    total = sum(p for row in mat for p in row)
    if total > 0:
        mat = [[p / total for p in row] for row in mat]
    return mat


# ──────────────────────────────────────────────────────────────────
# Step 4a: Derive 1X2 from matrix (used for λ solver)
# ──────────────────────────────────────────────────────────────────
def matrix_to_1x2(mat: List[List[float]]) -> Tuple[float, float, float]:
    p1, pX, p2 = 0.0, 0.0, 0.0
    n = len(mat) - 1
    for h in range(n + 1):
        for a in range(n + 1):
            if h > a:
                p1 += mat[h][a]
            elif h == a:
                pX += mat[h][a]
            else:
                p2 += mat[h][a]
    return p1, pX, p2


# ──────────────────────────────────────────────────────────────────
# Step 4b: Solve for λ_home, λ_away via grid search
# ──────────────────────────────────────────────────────────────────
def _loss(lh: float, la: float, target_p1: float, target_p2: float, target_pX: float) -> float:
    mat = score_matrix(lh, la)
    mp1, mpX, mp2 = matrix_to_1x2(mat)
    return (mp1 - target_p1) ** 2 + (mpX - target_pX) ** 2 + (mp2 - target_p2) ** 2


def solve_lambdas(
    fair_p1: float, fair_pX: float, fair_p2: float,
    steps: int = 30,
) -> Tuple[float, float]:
    """
    Grid-search for (λ_home, λ_away) that best reproduce the fair 1X2 probabilities.
    Range 0.3 – 4.0 goals per team covers all realistic virtual match scenarios.
    """
    best_loss = float("inf")
    best_lh, best_la = 1.2, 1.0  # sensible defaults

    lh_range = [0.3 + i * (3.7 / steps) for i in range(steps + 1)]
    la_range = [0.3 + i * (3.7 / steps) for i in range(steps + 1)]

    for lh in lh_range:
        for la in la_range:
            loss = _loss(lh, la, fair_p1, fair_p2, fair_pX)
            if loss < best_loss:
                best_loss = loss
                best_lh, best_la = lh, la

    # Refine with finer grid around best point
    fine_step = 3.7 / steps / 5
    for lh in [best_lh + fine_step * i for i in range(-4, 5)]:
        for la in [best_la + fine_step * i for i in range(-4, 5)]:
            if lh <= 0 or la <= 0:
                continue
            loss = _loss(lh, la, fair_p1, fair_p2, fair_pX)
            if loss < best_loss:
                best_loss = loss
                best_lh, best_la = lh, la

    return round(best_lh, 4), round(best_la, 4)


# ──────────────────────────────────────────────────────────────────
# Step 4c: Derived markets
# ──────────────────────────────────────────────────────────────────
def over_under_lines(mat: List[List[float]]) -> Dict[str, Tuple[float, float]]:
    """Returns (P_over, P_under) for lines 0.5, 1.5, 2.5, 3.5."""
    n = len(mat) - 1
    p_o05, p_o15, p_o25, p_o35 = 0.0, 0.0, 0.0, 0.0
    for h in range(n + 1):
        for a in range(n + 1):
            tot = h + a
            if tot > 0: p_o05 += mat[h][a]
            if tot > 1: p_o15 += mat[h][a]
            if tot > 2: p_o25 += mat[h][a]
            if tot > 3: p_o35 += mat[h][a]
    return {
        "0.5": (round(p_o05, 4), round(max(0.0, 1.0 - p_o05), 4)),
        "1.5": (round(p_o15, 4), round(max(0.0, 1.0 - p_o15), 4)),
        "2.5": (round(p_o25, 4), round(max(0.0, 1.0 - p_o25), 4)),
        "3.5": (round(p_o35, 4), round(max(0.0, 1.0 - p_o35), 4)),
    }


def over_under_25(mat: List[List[float]]) -> Tuple[float, float]:
    """Returns (P_over_2.5, P_under_2.5)."""
    p_over = 0.0
    n = len(mat) - 1
    for h in range(n + 1):
        for a in range(n + 1):
            if h + a > 2:
                p_over += mat[h][a]
    return round(p_over, 4), round(1.0 - p_over, 4)


def btts(mat: List[List[float]]) -> Tuple[float, float]:
    """Returns (P_both_score, P_not_both_score)."""
    p_yes = 0.0
    n = len(mat) - 1
    for h in range(1, n + 1):
        for a in range(1, n + 1):
            p_yes += mat[h][a]
    return round(p_yes, 4), round(1.0 - p_yes, 4)


def top_correct_scores(mat: List[List[float]], top_n: int = 3) -> List[Dict]:
    """Returns top_n most likely scorelines with probability and fair odds."""
    scores = []
    n = len(mat) - 1
    for h in range(n + 1):
        for a in range(n + 1):
            p = mat[h][a]
            fair_odd = round(1.0 / p, 2) if p > 0 else 999.0
            scores.append({
                "label":    f"{h}-{a}",
                "home_g":   h,
                "away_g":   a,
                "prob":     round(p, 4),
                "fair_odd": fair_odd,
            })
    scores.sort(key=lambda x: x["prob"], reverse=True)
    return scores[:top_n]


def asian_handicap(mat: List[List[float]]) -> Dict:
    """
    Returns P(home -0.5 AH win) = P(home wins by 1+)
    and     P(away -0.5 AH win) = P(away wins by 1+)
    These are the same as P(home win) and P(away win) from the matrix.
    """
    p_home_ah, p_away_ah = 0.0, 0.0
    n = len(mat) - 1
    for h in range(n + 1):
        for a in range(n + 1):
            if h > a:
                p_home_ah += mat[h][a]
            elif a > h:
                p_away_ah += mat[h][a]
    return {
        "home_ah_prob": round(p_home_ah, 4),
        "away_ah_prob": round(p_away_ah, 4),
    }


# ──────────────────────────────────────────────────────────────────
# Step 5: Edge calculation for derived markets
# ──────────────────────────────────────────────────────────────────
def market_edge(fair_prob: float, bookmaker_margin: float) -> Dict:
    """
    Given a fair probability and assumed bookmaker margin,
    estimate the implied bookmaker probability and compute edge.
    Returns implied_odd, fair_odd, edge_pct, kelly_fraction.
    """
    # Bookmaker's implied probability includes the margin
    implied_prob = fair_prob * (1.0 - bookmaker_margin)
    implied_odd  = round(1.0 / implied_prob, 2) if implied_prob > 0 else 0.0
    fair_odd     = round(1.0 / fair_prob, 2) if fair_prob > 0 else 0.0
    edge_pct     = round((fair_prob - implied_prob) * 100, 2)

    # Kelly fraction (raw — caller multiplies by bankroll × Kelly fraction)
    b = implied_odd - 1.0
    q = 1.0 - fair_prob
    kelly = (b * fair_prob - q) / b if b > 0 else 0.0
    kelly = max(0.0, round(kelly, 4))

    return {
        "implied_odd":  implied_odd,
        "fair_odd":     fair_odd,
        "edge_pct":     edge_pct,
        "kelly":        kelly,
        "has_value":    edge_pct >= 3.0,
    }


# ──────────────────────────────────────────────────────────────────
# Main public function
# ──────────────────────────────────────────────────────────────────
def analyse_match(o1: float, oX: float, o2: float) -> Dict:
    """
    Full Poisson analysis for a single match given 1X2 bookmaker odds.

    Returns a dict with:
      fair_p1, fair_pX, fair_p2, overround_pct,
      lambda_home, lambda_away,
      over25, under25, btts_yes, btts_no,
      correct_scores (top 3),
      asian_handicap,
      markets (edge data for each derived market),
      score_matrix (full NxN, for advanced use)
    """
    # 1. Fair probabilities
    fair_p1, fair_pX, fair_p2, overround_pct = remove_overround(o1, oX, o2)

    # 2. Solve lambdas
    lh, la = solve_lambdas(fair_p1, fair_pX, fair_p2)

    # 3. Score matrix
    mat = score_matrix(lh, la)

    # 4. Markets
    p_over, p_under   = over_under_25(mat)
    ou_lines          = over_under_lines(mat)
    p_btts,  p_nobtts = btts(mat)
    top_cs            = top_correct_scores(mat, top_n=3)
    ah                = asian_handicap(mat)

    # 5. Edge data (using estimated bookmaker margins)
    ou_edge   = market_edge(p_over,  BOOKMAKER_MARGIN_OU)
    btts_edge = market_edge(p_btts,  BOOKMAKER_MARGIN_BTTS)

    # Kelly for 1X2 picks
    def kelly_1x2(fair_p: float, book_odd: float) -> float:
        b = book_odd - 1.0
        q = 1.0 - fair_p
        f = (b * fair_p - q) / b if b > 0 else 0.0
        return max(0.0, round(f, 4))

    return {
        # Core probabilities
        "fair_p1":       round(fair_p1, 4),
        "fair_pX":       round(fair_pX, 4),
        "fair_p2":       round(fair_p2, 4),
        "overround_pct": round(overround_pct, 2),

        # Poisson parameters
        "lambda_home":    lh,
        "lambda_away":    la,
        "expected_goals": round(lh + la, 2),

        # Over/Under
        "over25_prob":   p_over,
        "under25_prob":  p_under,
        "over25_edge":   ou_edge,
        "ou_lines":      ou_lines,

        # BTTS
        "btts_prob":     p_btts,
        "no_btts_prob":  p_nobtts,
        "btts_edge":     btts_edge,

        # Correct scores
        "correct_scores": top_cs,

        # Asian Handicap
        "asian_handicap": ah,

        # 1X2 Kelly fractions
        "kelly_1":  kelly_1x2(fair_p1, o1),
        "kelly_X":  kelly_1x2(fair_pX, oX),
        "kelly_2":  kelly_1x2(fair_p2, o2),

        # 1X2 edges
        "edge_p1":  round((fair_p1 - 1/o1) * 100, 2),
        "edge_pX":  round((fair_pX - 1/oX) * 100, 2),
        "edge_p2":  round((fair_p2 - 1/o2) * 100, 2),
    }
