"""Quick smoke test for analytics + poisson + database modules."""
import sys

# ── Analytics engine ──────────────────────────────────────────────
print("=" * 55)
print("TEST 1: Analytics engine")
print("=" * 55)
from analytics import run_analytics

matches = [
    {"home": "ARS", "away": "BRE", "odd1": "2.10", "oddX": "3.40", "odd2": "3.25"},
    {"home": "MCI", "away": "LIV", "odd1": "1.90", "oddX": "3.50", "odd2": "3.80"},
    {"home": "TOT", "away": "CHE", "odd1": "2.50", "oddX": "3.20", "odd2": "2.80"},
]
result = run_analytics(matches, bankroll=100.0)

for p in result["predictions"]:
    print(
        f"  {p['home']:4} vs {p['away']:4}"
        f"  pick={p['pick']}  odd={p['odd']:5}"
        f"  edge={p['edge_pct']:+.1f}%"
        f"  conf={p['confidence']:4}"
        f"  kelly=GHS{p['kelly_stake']:.2f}"
        f"  value={'YES' if p['value_bet'] else 'no'}"
    )

acca = result["accumulator"]
print(f"\n  Accumulator: {len(acca.get('legs', []))} legs"
      f" @ {acca.get('combined_odd', '-')}"
      f" | prob={round(acca.get('combined_prob', 0) * 100)}%")
print(f"  Summary: {result['summary']}")

# ── Poisson model ─────────────────────────────────────────────────
print("\n" + "=" * 55)
print("TEST 2: Poisson model")
print("=" * 55)
from poisson import analyse_match

pa = analyse_match(2.10, 3.40, 3.25)
print(f"  ARS vs BRE fair probs:  1={pa['fair_p1']:.3f}  X={pa['fair_pX']:.3f}  2={pa['fair_p2']:.3f}")
print(f"  Lambda: home={pa['lambda_home']}  away={pa['lambda_away']}")
print(f"  Over 2.5: {pa['over25_prob']*100:.1f}%   BTTS: {pa['btts_prob']*100:.1f}%")
print(f"  Top scores: {[cs['label'] for cs in pa['correct_scores']]}")
print(f"  Overround: {pa['overround_pct']:.2f}%")

# ── Database ──────────────────────────────────────────────────────
print("\n" + "=" * 55)
print("TEST 3: Database init + ORM")
print("=" * 55)
from database import init_db, get_performance_summary, get_team_leaderboard
init_db()
perf = get_performance_summary()
print(f"  Performance summary: {perf}")
teams = get_team_leaderboard(5)
print(f"  Team leaderboard ({len(teams)} teams): {[t['code'] for t in teams]}")

# ── Auth module ───────────────────────────────────────────────────
print("\n" + "=" * 55)
print("TEST 4: Auth module")
print("=" * 55)
from auth import hash_password, verify_password, create_access_token, decode_token

hashed = hash_password("testpassword123")
assert verify_password("testpassword123", hashed), "Password verify failed"
assert not verify_password("wrongpassword", hashed), "Wrong password should fail"
print("  Password hash/verify: OK")

token   = create_access_token(user_id=1, username="testuser")
payload = decode_token(token)
assert payload is not None, "Token decode failed"
assert payload["sub"] == "1", "Token subject mismatch"
assert payload["username"] == "testuser", "Token username mismatch"
print(f"  JWT create/decode: OK  (sub={payload['sub']} username={payload['username']})")

print("\n" + "=" * 55)
print("ALL TESTS PASSED")
print("=" * 55)
