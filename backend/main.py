"""
Virtual Edge – Backend  v3.0
=============================
Engines:   Groq (Llama/Mixtral) → Gemini 1.5-pro/flash → OpenAI GPT-4o-mini → Google Cloud Vision → Tesseract fallback
Analytics: analytics.py  – Poisson + Kelly + value-bet engine (runs locally, no API cost)
Auth:      auth.py       – JWT registration / login
DB:        database.py   – SQLAlchemy ORM + raw SQLite helpers
Models:    models.py     – ORM table definitions

Endpoints
─────────
POST /auth/register          – create account
POST /auth/login             – get JWT token
GET  /auth/me                – current user info  [auth required]
PUT  /auth/bankroll          – update bankroll    [auth required]

POST /api/ocr                – upload screenshot → extract matches
POST /api/predict            – matches → AI + Poisson predictions   [auth optional]
POST /api/analyse            – matches → pure local Poisson engine   [no AI, instant]
POST /api/feedback           – log win/loss result
GET  /api/stats              – performance dashboard
GET  /api/feedback/stats     – feedback win-rate summary

GET  /health                 – service status

Run:
  pip install -r requirements.txt
  $env:GOOGLE_AI_STUDIO_KEY = "AIza..."
  $env:SECRET_KEY           = "your-secret"   # optional, has default
  uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import os
import re
import base64
import io
import json
import uuid
import asyncio
import pathlib
from contextlib import asynccontextmanager
from typing import List, Optional
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, File, UploadFile, HTTPException, Depends, status, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session as OrmSession

# Internal modules
from database import (
    init_db, get_db,
    insert_fixture, insert_prediction, insert_market_prediction,
    insert_outcome, save_feedback, load_recent_feedback,
    get_performance_summary, get_team_leaderboard,
    get_recent_predictions, get_value_bet_stats,
    ensure_session, upsert_team, update_team_record,
)
from models import User
from auth import (
    RegisterRequest, LoginRequest, TokenResponse, UserPublic,
    register_user, authenticate_user, create_access_token,
    get_current_user, get_optional_user, update_bankroll,
)
from analytics import run_analytics
from poisson import analyse_match
from websocket_manager import manager as ws_manager, msg_progress, msg_prediction_ready
from scheduler import create_scheduler, get_pending_predictions, get_accuracy_snapshot, resolve_prediction
from monitoring import generate_report

FEEDBACK_FILE    = pathlib.Path(__file__).parent / "feedback_store.jsonl"
PUSH_SUBS_FILE   = pathlib.Path(__file__).parent / "push_subscriptions.jsonl"
VAPID_FILE       = pathlib.Path(__file__).parent / "vapid_keys.json"


# ──────────────────────────────────────────────────────────────────
# App lifecycle
# ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # WebSocket keepalive
    ws_task = asyncio.create_task(ws_manager.keepalive_loop(25))
    # APScheduler results-checker (runs every 5 min)
    scheduler = create_scheduler()
    scheduler.start()
    yield
    ws_task.cancel()
    scheduler.shutdown(wait=False)

app = FastAPI(title="Virtual Edge", version="3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────
# Pydantic request / response schemas
# ──────────────────────────────────────────────────────────────────
class Match(BaseModel):
    home: str
    away: str
    odd1: str = ""
    oddX: str = ""
    odd2: str = ""


class OCRResponse(BaseModel):
    matches:  List[Match]
    engine:   str
    raw_text: Optional[str] = None


class MatchInput(BaseModel):
    home: str
    away: str
    odd1: str
    oddX: str
    odd2: str


class CorrectScore(BaseModel):
    label:    str
    prob:     float
    fair_odd: float


class MarketData(BaseModel):
    over25_prob:    float
    under25_prob:   float
    over25_edge:    float
    btts_prob:      float
    no_btts_prob:   float
    btts_edge:      float
    correct_scores: List[CorrectScore]
    lambda_home:    float
    lambda_away:    float


class PickDetail(BaseModel):
    pick:       str
    label:      str
    odd:        str
    confidence: int
    edge_pct:   float
    reasoning:  str


class MatchPrediction(BaseModel):
    home:           str
    away:           str
    # Standard analytics payload fields
    prediction:     str          # canonical pick label e.g. "HOME WIN (ARS)"
    pick:           str          # "1","X","2"
    confidence:     str          # "78%"
    confidence_int: int
    edge_pct:       float
    odd:            str
    value_bet:      bool
    kelly_stake:    float        # GHS amount (0 if no bankroll)
    kelly_fraction: float
    # Probabilities
    fair_p1:        float
    fair_pX:        float
    fair_p2:        float
    overround_pct:  float
    # Full pick objects
    best_pick:      PickDetail
    safe_pick:      PickDetail
    # Poisson markets
    markets:        Optional[MarketData] = None
    # DB reference
    fixture_id:     Optional[int] = None


class AccumulatorLeg(BaseModel):
    home:      str
    away:      str
    pick:      str
    odd:       str
    reasoning: str


class AnalyticsSummary(BaseModel):
    total:            int
    value_bets:       int
    best_pick_label:  str
    best_confidence:  str
    best_edge_pct:    float
    has_value:        bool


class AnalyticsResponse(BaseModel):
    """Returned by both /api/analyse (pure local) and /api/predict (AI-enhanced)."""
    predictions:       List[MatchPrediction]
    accumulator:       dict
    summary:           AnalyticsSummary
    acca_combined_odd: Optional[str] = None
    acca_reasoning:    Optional[str] = None
    engine:            str
    session_id:        Optional[str] = None


class FeedbackItem(BaseModel):
    home:          str
    away:          str
    pick:          str
    odd:           str
    result:        str            # "win" | "loss"
    fixture_id:    Optional[int] = None
    prediction_id: Optional[int] = None
    stake:         Optional[float] = None
    timestamp:     Optional[str]  = None


class BankrollUpdate(BaseModel):
    bankroll: float


class PushSubscription(BaseModel):
    endpoint:    str
    keys:        dict   # {"p256dh": "...", "auth": "..."}
    session_id:  Optional[str] = None
    user_agent:  Optional[str] = None


# ──────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────
def clean_team(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9\s]", "", name)
    return re.sub(r"\s+", " ", name).strip().upper()[:18]


def safe_float(val: str) -> float:
    try:
        f = float(str(val).strip())
        return f if 1.05 <= f <= 20.0 else 0.0
    except Exception:
        return 0.0


def build_few_shot_block(feedback: List[dict]) -> str:
    if not feedback:
        return ""
    wins   = [f for f in feedback if f.get("result") == "win"][-5:]
    losses = [f for f in feedback if f.get("result") == "loss"][-5:]
    block  = "\nPAST RESULTS (learn from these):\n"
    for fb in wins:
        block += f"  {fb['home']} vs {fb['away']} → pick={fb['pick']} odd={fb['odd']} WON\n"
    for fb in losses:
        block += f"  {fb['home']} vs {fb['away']} → pick={fb['pick']} odd={fb['odd']} LOST\n"
    return block


def parse_sportybet_text(text: str) -> List[dict]:
    lines = [l.strip() for l in text.replace("\r", "\n").split("\n") if len(l.strip()) > 1]
    matches, seen, found_pairs = [], set(), []
    vs_patterns = [
        re.compile(r"\b([A-Z]{2,5})\s*(?:vs|v\.?|VS|V)\s*([A-Z]{2,5})\b"),
        re.compile(r"([A-Za-z][A-Za-z0-9 .]{1,18}?)\s+(?:vs|v\.?|VS)\s+([A-Za-z][A-Za-z0-9 .]{1,18})", re.I),
    ]
    for i, line in enumerate(lines):
        for pat in vs_patterns:
            for m in pat.finditer(line):
                home, away = clean_team(m.group(1)), clean_team(m.group(2))
                if len(home) < 2 or len(away) < 2 or home == away:
                    continue
                key = f"{home}|{away}"
                if key in seen:
                    continue
                seen.add(key)
                found_pairs.append({"home": home, "away": away, "line": i})
    all_odds = [float(x) for x in re.findall(r"\b(\d{1,2}\.\d{1,2})\b", text)
                if 1.01 <= float(x) <= 25.0]
    if found_pairs and len(all_odds) >= len(found_pairs) * 2:
        for idx, pair in enumerate(found_pairs):
            base = idx * 3
            o1 = all_odds[base]     if base     < len(all_odds) else None
            oX = all_odds[base + 1] if base + 1 < len(all_odds) else None
            o2 = all_odds[base + 2] if base + 2 < len(all_odds) else None
            matches.append({"home": pair["home"], "away": pair["away"],
                            "odd1": f"{o1:.2f}" if o1 else "",
                            "oddX": f"{oX:.2f}" if oX else "",
                            "odd2": f"{o2:.2f}" if o2 else ""})
    else:
        for pair in found_pairs:
            matches.append({"home": pair["home"], "away": pair["away"],
                            "odd1": "", "oddX": "", "odd2": ""})
    return matches[:12]


# ──────────────────────────────────────────────────────────────────
# OCR engines
# ──────────────────────────────────────────────────────────────────
OCR_PROMPT = """You are a specialist OCR agent for SportyBet Ghana Instant Virtuals betting slips.

CONTEXT — SportyBet Instant Virtuals layout:
- Each match row: Match number, Home Team, "vs", Away Team, then THREE decimal odds
- Odds order: [Home Win]  [Draw]  [Away Win]  (left to right)
- Team names: short 3-5 letter codes — ARS, BRE, TOT, MCI, LIV, MUN, CHE, NEW, etc.
- Odds range: 1.10 – 20.00, always 2 decimal places
- Usually 6–12 matches per screen, dark background

INSTRUCTIONS:
1. Scan every match row TOP to BOTTOM — do not skip any
2. Extract home team, away team, and all three odds
3. Reject odds outside 1.05–20.00 range
4. Do NOT invent matches — only extract what is visible
5. Each match must have home != away and all three odds

Return ONLY a valid JSON array. Zero markdown, zero explanation.
Keys: "home", "away", "odd1", "oddX", "odd2", "match_num" (int or 0)
"""


def _validate_ocr(data: list) -> list:
    cleaned, seen = [], set()
    for item in data:
        home = clean_team(str(item.get("home", "")))
        away = clean_team(str(item.get("away", "")))
        if not home or not away or home == away or len(home) < 2:
            continue
        key = f"{home}|{away}"
        if key in seen:
            continue
        seen.add(key)

        def so(v):
            try:
                f = float(str(v).strip())
                return f"{f:.2f}" if 1.05 <= f <= 20.0 else ""
            except Exception:
                return ""

        cleaned.append({
            "home": home, "away": away,
            "odd1": so(item.get("odd1")), "oddX": so(item.get("oddX")),
            "odd2": so(item.get("odd2")),
            "match_num": int(item.get("match_num", 0) or 0),
        })
    cleaned.sort(key=lambda x: x["match_num"] if x["match_num"] > 0 else 999)
    for m in cleaned:
        m.pop("match_num", None)
    return cleaned


async def _ocr_gemini(image_bytes: bytes) -> tuple[list, str]:
    import google.generativeai as genai
    import PIL.Image
    api_key = os.getenv("GOOGLE_AI_STUDIO_KEY") or os.getenv("GEMINI_API_KEY")
    genai.configure(api_key=api_key)
    img = PIL.Image.open(io.BytesIO(image_bytes))
    content = ""
    for model_name in ["gemini-1.5-pro", "gemini-1.5-flash"]:
        try:
            model   = genai.GenerativeModel(model_name,
                          generation_config={"temperature": 0, "max_output_tokens": 2048})
            content = model.generate_content([OCR_PROMPT, img]).text.strip()
            break
        except Exception as e:
            print(f"{model_name} OCR failed: {e}")
    if not content:
        return [], ""
    content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.MULTILINE)
    content = re.sub(r"\s*```$",           "", content, flags=re.MULTILINE).strip()
    m = re.search(r"\[.*\]", content, re.DOTALL)
    if m:
        content = m.group(0)
    try:
        return _validate_ocr(json.loads(content)), content
    except Exception:
        return parse_sportybet_text(content), content


async def _ocr_openai(image_bytes: bytes) -> tuple[list, str]:
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    b64    = base64.b64encode(image_bytes).decode("utf-8")
    prompt = ('Extract every SportyBet Instant Virtuals match as JSON array. '
              'Keys: home, away, odd1, oddX, odd2. No markdown.')
    resp   = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}}
        ]}], max_tokens=1500, temperature=0)
    content = resp.choices[0].message.content.strip()
    content = re.sub(r"^```json\s*", "", content)
    content = re.sub(r"\s*```$",     "", content)
    try:
        data = json.loads(content)
        return [{
            "home": clean_team(str(i.get("home", ""))),
            "away": clean_team(str(i.get("away", ""))),
            "odd1": str(i.get("odd1", "")), "oddX": str(i.get("oddX", "")),
            "odd2": str(i.get("odd2", "")),
        } for i in data], content
    except Exception:
        return parse_sportybet_text(content), content


# ──────────────────────────────────────────────────────────────────
# AI prediction engine (Gemini, AI-layer on top of Poisson)
# ──────────────────────────────────────────────────────────────────
PREDICT_SYSTEM = """You are an expert betting analyst for SportyBet Ghana Instant Virtuals.
The odds encode the bookmaker's Poisson probability model. Your job:
1. Strip overround → true fair probability.
2. Identify VALUE picks (edge > 3%).
3. Suggest a SAFE double-chance (1X/X2/12).
4. Recommend a 2-3 leg accumulator, combined prob > 45%.
5. One clear reasoning sentence per pick.
Rules: no negative Kelly picks; confidence 50-65=low, 66-79=medium, 80+=high."""


async def _run_ai_prediction(matches: List[dict], feedback: List[dict]) -> dict:
    # Try Groq first, fall back to Gemini
    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key:
        try:
            return await _run_groq_prediction(matches, feedback)
        except Exception as e:
            print(f"Groq prediction failed: {e}, trying Gemini")

    # Fallback to Gemini
    import google.generativeai as genai
    api_key = os.getenv("GOOGLE_AI_STUDIO_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("No AI key configured — set GROQ_API_KEY or GOOGLE_AI_STUDIO_KEY")
    genai.configure(api_key=api_key)

    block = "".join(
        f"Match {i}: {m['home']} vs {m['away']} | "
        f"odd1={m['odd1']}  oddX={m['oddX']}  odd2={m['odd2']}\n"
        for i, m in enumerate(matches, 1)
    )

    prompt = f"""Analyse these SportyBet Instant Virtuals matches.

MATCHES:
{block}
{build_few_shot_block(feedback)}

Return ONLY a valid JSON object (no markdown):
{{
  "predictions": [{{
    "home":"TEAM","away":"TEAM",
    "best_pick":{{"pick":"1|X|2","label":"HOME WIN (T)|DRAW|AWAY WIN (T)",
                 "odd":"2.10","confidence":72,"edge_pct":4.2,
                 "reasoning":"One sentence."}},
    "safe_pick":{{"pick":"1X|X2|12","label":"Home or Draw",
                 "odd":"1.45","confidence":85,"edge_pct":2.1,
                 "reasoning":"One sentence."}},
    "fair_p1":0.48,"fair_pX":0.28,"fair_p2":0.24,
    "overround_pct":7.2,"kelly_fraction":0.062,"value_bet":true
  }}],
  "accumulator":[
    {{"home":"T","away":"T","pick":"1","odd":"2.10","reasoning":"reason"}},
    {{"home":"T","away":"T","pick":"X","odd":"3.40","reasoning":"reason"}}
  ],
  "acca_combined_odd":"7.14",
  "acca_reasoning":"One sentence."
}}"""

    content = ""
    for model_name in ["gemini-1.5-pro", "gemini-1.5-flash"]:
        try:
            model   = genai.GenerativeModel(
                model_name,
                system_instruction=PREDICT_SYSTEM,
                generation_config={"temperature": 0.2, "max_output_tokens": 3000})
            content = model.generate_content(prompt).text.strip()
            break
        except Exception as e:
            print(f"Prediction {model_name} failed: {e}")

    if not content:
        raise RuntimeError("All AI prediction models failed")

    content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.MULTILINE)
    content = re.sub(r"\s*```$",           "", content, flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        content = m.group(0)
    return json.loads(content)


async def _run_groq_prediction(matches: List[dict], feedback: List[dict]) -> dict:
    from groq import Groq
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    block = "".join(
        f"Match {i}: {m['home']} vs {m['away']} | "
        f"odd1={m['odd1']}  oddX={m['oddX']}  odd2={m['odd2']}\n"
        for i, m in enumerate(matches, 1)
    )

    prompt = f"""Analyse these SportyBet Instant Virtuals matches.

MATCHES:
{block}
{build_few_shot_block(feedback)}

Return ONLY a valid JSON object (no markdown):
{{
  "predictions": [{{
    "home":"TEAM","away":"TEAM",
    "best_pick":{{"pick":"1|X|2","label":"HOME WIN (T)|DRAW|AWAY WIN (T)",
                 "odd":"2.10","confidence":72,"edge_pct":4.2,
                 "reasoning":"One sentence."}},
    "safe_pick":{{"pick":"1X|X2|12","label":"Home or Draw",
                 "odd":"1.45","confidence":85,"edge_pct":2.1,
                 "reasoning":"One sentence."}},
    "fair_p1":0.48,"fair_pX":0.28,"fair_p2":0.24,
    "overround_pct":7.2,"kelly_fraction":0.062,"value_bet":true
  }}],
  "accumulator":[
    {{"home":"T","away":"T","pick":"1","odd":"2.10","reasoning":"reason"}},
    {{"home":"T","away":"T","pick":"X","odd":"3.40","reasoning":"reason"}}
  ],
  "acca_combined_odd":"7.14",
  "acca_reasoning":"One sentence."
}}"""

    # Try multiple Groq models in order of preference
    models = ["llama-3.3-70b-versatile", "llama-3.1-70b-versatile", "mixtral-8x7b-32768"]

    for model_name in models:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": PREDICT_SYSTEM},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2,
                max_tokens=3000,
                response_format={"type": "json_object"}
            )
            content = response.choices[0].message.content.strip()
            if content:
                return json.loads(content)
        except Exception as e:
            print(f"Groq {model_name} failed: {e}")
            continue

    raise RuntimeError("All Groq prediction models failed")


def _poisson_market_data(o1: float, oX: float, o2: float) -> Optional[MarketData]:
    try:
        a = analyse_match(o1, oX, o2)
        return MarketData(
            over25_prob   = a["over25_prob"],
            under25_prob  = a["under25_prob"],
            over25_edge   = a["over25_edge"]["edge_pct"],
            btts_prob     = a["btts_prob"],
            no_btts_prob  = a["no_btts_prob"],
            btts_edge     = a["btts_edge"]["edge_pct"],
            correct_scores=[CorrectScore(**cs) for cs in a["correct_scores"]],
            lambda_home   = a["lambda_home"],
            lambda_away   = a["lambda_away"],
        )
    except Exception as e:
        print(f"Poisson market data failed: {e}")
        return None


def _persist_fixture(sid: str, m: dict, pa: dict) -> Optional[int]:
    try:
        fid = insert_fixture(
            session_id    = sid,
            home          = m["home"], away = m["away"],
            odd1          = float(m["odd1"]),
            oddX          = float(m["oddX"]),
            odd2          = float(m["odd2"]),
            fair_p1       = pa["fair_p1"],
            fair_pX       = pa["fair_pX"],
            fair_p2       = pa["fair_p2"],
            lambda_home   = pa["lambda_home"],
            lambda_away   = pa["lambda_away"],
            overround_pct = pa["overround_pct"],
        )
        cs = pa.get("correct_scores", [])
        insert_market_prediction(fid, {
            "over25_prob":   pa["over25_prob"],
            "under25_prob":  pa["under25_prob"],
            "btts_prob":     pa["btts_prob"],
            "no_btts_prob":  pa["no_btts_prob"],
            "cs_top1_label": cs[0]["label"] if len(cs) > 0 else None,
            "cs_top2_label": cs[1]["label"] if len(cs) > 1 else None,
            "cs_top3_label": cs[2]["label"] if len(cs) > 2 else None,
        })
        upsert_team(m["home"])
        upsert_team(m["away"])
        return fid
    except Exception as e:
        print(f"Fixture persist failed: {e}")
        return None


def _build_match_prediction(p: dict, fixture_id: Optional[int],
                             poisson_data: Optional[MarketData],
                             bankroll: float) -> Optional[MatchPrediction]:
    """Convert analytics engine output dict → MatchPrediction Pydantic model."""
    try:
        kelly_stake = p.get("kelly_stake", 0.0)
        # Recalculate with actual bankroll if provided
        if bankroll > 0 and p.get("kelly_fraction", 0) > 0:
            from analytics import _kelly_stake as ks
            pick_prob = (p["fair_p1"] if p["pick"] == "1"
                         else (p["fair_pX"] if p["pick"] == "X" else p["fair_p2"]))
            kelly_stake = ks(pick_prob, float(p["odd"]), bankroll)

        return MatchPrediction(
            home           = p["home"],
            away           = p["away"],
            prediction     = p["prediction"],
            pick           = p["pick"],
            confidence     = p["confidence"],
            confidence_int = p["confidence_int"],
            edge_pct       = p["edge_pct"],
            odd            = p["odd"],
            value_bet      = p["value_bet"],
            kelly_stake    = kelly_stake,
            kelly_fraction = p["kelly_fraction"],
            fair_p1        = p["fair_p1"],
            fair_pX        = p["fair_pX"],
            fair_p2        = p["fair_p2"],
            overround_pct  = p["overround_pct"],
            best_pick      = PickDetail(**p["best_pick"]),
            safe_pick      = PickDetail(**p["safe_pick"]),
            markets        = poisson_data,
            fixture_id     = fixture_id,
        )
    except Exception as e:
        print(f"MatchPrediction build failed: {e}")
        return None


# ──────────────────────────────────────────────────────────────────
# AUTH ROUTES
# ──────────────────────────────────────────────────────────────────
@app.post("/auth/register", response_model=TokenResponse, status_code=201)
def auth_register(req: RegisterRequest, db: OrmSession = Depends(get_db)):
    user  = register_user(db, req)
    token = create_access_token(user.id, user.username)
    return TokenResponse(access_token=token, username=user.username, user_id=user.id)


@app.post("/auth/login", response_model=TokenResponse)
def auth_login(req: LoginRequest, db: OrmSession = Depends(get_db)):
    user = authenticate_user(db, req.username, req.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    token = create_access_token(user.id, user.username)
    return TokenResponse(access_token=token, username=user.username, user_id=user.id)


@app.post("/auth/login/form", response_model=TokenResponse)
def auth_login_form(
    form: OAuth2PasswordRequestForm = Depends(),
    db:   OrmSession                = Depends(get_db),
):
    """OAuth2 form login — compatible with Swagger UI 'Authorize' button."""
    user = authenticate_user(db, form.username, form.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(user.id, user.username)
    return TokenResponse(access_token=token, username=user.username, user_id=user.id)


@app.get("/auth/me", response_model=UserPublic)
def auth_me(
    token_payload: Optional[dict] = Depends(get_optional_user),
    db:            OrmSession     = Depends(get_db),
):
    if not token_payload:
        raise HTTPException(401, "Not authenticated")
    user = db.query(User).filter(User.id == int(token_payload["sub"])).first()
    if not user:
        raise HTTPException(404, "User not found")
    return user


@app.put("/auth/bankroll", response_model=UserPublic)
def auth_update_bankroll(
    body:          BankrollUpdate,
    token_payload: Optional[dict] = Depends(get_optional_user),
    db:            OrmSession     = Depends(get_db),
):
    if not token_payload:
        raise HTTPException(401, "Not authenticated")
    return update_bankroll(db, int(token_payload["sub"]), body.bankroll)


# ──────────────────────────────────────────────────────────────────
# OCR ENDPOINT
# ──────────────────────────────────────────────────────────────────
@app.post("/api/ocr", response_model=OCRResponse)
async def ocr_endpoint(image: UploadFile = File(...)):
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")
    image_bytes = await image.read()
    if len(image_bytes) > 8 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 8 MB)")

    engine, matches, raw_text = "none", [], None

    gemini_key = os.getenv("GOOGLE_AI_STUDIO_KEY") or os.getenv("GEMINI_API_KEY")
    if gemini_key:
        try:
            matches, raw_text = await _ocr_gemini(image_bytes)
            engine = "gemini-1.5-pro"
        except Exception as e:
            print("Gemini OCR failed:", e)

    if not matches and os.getenv("OPENAI_API_KEY"):
        try:
            matches, raw_text = await _ocr_openai(image_bytes)
            engine = "openai-gpt4o-mini"
        except Exception as e:
            print("OpenAI OCR failed:", e)

    if not matches:
        engine = "fallback-empty"

    return OCRResponse(matches=[Match(**m) for m in matches],
                       engine=engine, raw_text=raw_text)


# ──────────────────────────────────────────────────────────────────
# PURE LOCAL ANALYTICS  (/api/analyse — instant, no AI API call)
# ──────────────────────────────────────────────────────────────────
@app.post("/api/analyse", response_model=AnalyticsResponse)
async def analyse_endpoint(
    matches:       List[MatchInput],
    session_id:    Optional[str]  = None,
    token_payload: Optional[dict] = Depends(get_optional_user),
):
    """
    Pure Poisson + Kelly analytics — no Gemini API call.
    Returns instantly. Use when you need fast results or AI quota is exhausted.
    """
    if not matches:
        raise HTTPException(400, "No matches provided")

    # Determine bankroll
    bankroll = 0.0
    if token_payload:
        # We don't have db here — bankroll passed from frontend state
        pass

    match_dicts = [m.model_dump() for m in matches]
    result      = run_analytics(match_dicts, bankroll)

    sid = ensure_session(session_id or str(uuid.uuid4()))

    predictions: List[MatchPrediction] = []
    for p in result["predictions"]:
        o1 = safe_float(p.get("odd1", "") or
                        next((m["odd1"] for m in match_dicts
                              if clean_team(m["home"]) == p["home"]), "0"))
        # Use data from the analytics dict directly
        poisson_data = _poisson_market_data(
            safe_float(next((m["odd1"] for m in match_dicts
                             if clean_team(m["home"]) == p["home"]), "0")),
            safe_float(next((m["oddX"] for m in match_dicts
                             if clean_team(m["home"]) == p["home"]), "0")),
            safe_float(next((m["odd2"] for m in match_dicts
                             if clean_team(m["home"]) == p["home"]), "0")),
        )
        mp = _build_match_prediction(p, None, poisson_data, bankroll)
        if mp:
            predictions.append(mp)

    acca = result["accumulator"]
    return AnalyticsResponse(
        predictions       = predictions,
        accumulator       = acca,
        summary           = AnalyticsSummary(**result["summary"]),
        acca_combined_odd = acca.get("combined_odd"),
        acca_reasoning    = acca.get("reasoning"),
        engine            = "poisson-local",
        session_id        = sid,
    )


# ──────────────────────────────────────────────────────────────────
# AI-ENHANCED PREDICT  (/api/predict — Gemini + Poisson)
# ──────────────────────────────────────────────────────────────────
@app.post("/api/predict", response_model=AnalyticsResponse)
async def predict_endpoint(
    matches:       List[MatchInput],
    session_id:    Optional[str]  = None,
    token_payload: Optional[dict] = Depends(get_optional_user),
):
    """
    Two-layer pipeline:
      1. run_analytics() — local Poisson engine (always runs, no API cost)
      2. _run_ai_prediction() — Gemini refines reasoning + accumulator
    AI result overlays reasoning onto Poisson probabilities.
    Falls back gracefully to pure Poisson if AI fails.
    """
    if not matches:
        raise HTTPException(400, "No matches provided")
    if len(matches) > 12:
        raise HTTPException(400, "Maximum 12 matches per request")

    groq_key = os.getenv("GROQ_API_KEY")
    gemini_key = os.getenv("GOOGLE_AI_STUDIO_KEY") or os.getenv("GEMINI_API_KEY")
    if not groq_key and not gemini_key:
        raise HTTPException(503, "No AI key configured — set GROQ_API_KEY or GOOGLE_AI_STUDIO_KEY")

    # Bankroll from auth user if logged in
    bankroll = 0.0

    sid         = ensure_session(session_id or str(uuid.uuid4()))
    match_dicts = [m.model_dump() for m in matches]

    # ── Step 1: Local Poisson analytics (ground truth) ──────────────
    local_result = run_analytics(match_dicts, bankroll)

    # ── Step 2: AI reasoning overlay ────────────────────────────────
    ai_result    = None
    feedback     = load_recent_feedback(20)
    try:
        ai_result = await _run_ai_prediction(match_dicts, feedback)
    except Exception as e:
        print(f"AI prediction failed, using pure Poisson: {e}")

    # ── Step 3: Merge — Poisson probs + AI reasoning ─────────────────
    predictions: List[MatchPrediction] = []

    for p in local_result["predictions"]:
        home = p["home"]
        away = p["away"]

        # Find matching input odds
        odds = next((m for m in match_dicts
                     if clean_team(m["home"]) == home), None)
        o1 = safe_float(odds["odd1"]) if odds else 0.0
        oX = safe_float(odds["oddX"]) if odds else 0.0
        o2 = safe_float(odds["odd2"]) if odds else 0.0

        # Poisson market data
        poisson_data = _poisson_market_data(o1, oX, o2) if o1 else None

        # Overlay AI reasoning if available
        if ai_result:
            ai_p = next(
                (ap for ap in ai_result.get("predictions", [])
                 if clean_team(str(ap.get("home", ""))) == home),
                None
            )
            if ai_p:
                # Keep Poisson probabilities, overlay AI reasoning text
                p["best_pick"]["reasoning"] = ai_p.get("best_pick", {}).get(
                    "reasoning", p["best_pick"]["reasoning"])
                p["safe_pick"]["reasoning"] = ai_p.get("safe_pick", {}).get(
                    "reasoning", p["safe_pick"]["reasoning"])

        # Persist fixture to DB
        fixture_id = None
        if o1 and oX and o2:
            try:
                pa = analyse_match(o1, oX, o2)
                fixture_id = _persist_fixture(sid, {"home": home, "away": away,
                                                     "odd1": str(o1), "oddX": str(oX),
                                                     "odd2": str(o2)}, pa)
                if fixture_id:
                    insert_prediction(
                        fixture_id     = fixture_id,
                        pick           = p["pick"],
                        pick_label     = p["prediction"],
                        pick_odd       = float(p["odd"]),
                        confidence     = p["confidence_int"],
                        edge_pct       = p["edge_pct"],
                        kelly_fraction = p["kelly_fraction"],
                        value_bet      = p["value_bet"],
                        reasoning      = p["best_pick"]["reasoning"],
                        market         = "1X2",
                    )
            except Exception as db_err:
                print(f"DB persist failed: {db_err}")

        mp = _build_match_prediction(p, fixture_id, poisson_data, bankroll)
        if mp:
            predictions.append(mp)

    # ── Accumulator: prefer AI version, fall back to Poisson ─────────
    acca = local_result["accumulator"]
    acca_odd = acca.get("combined_odd", "—")
    acca_rsn = acca.get("reasoning", "")

    if ai_result:
        acca_odd = str(ai_result.get("acca_combined_odd", acca_odd))
        acca_rsn = str(ai_result.get("acca_reasoning",    acca_rsn))

    # Build AccumulatorLeg list from local result legs
    acca_legs = []
    for leg in acca.get("legs", []):
        acca_legs.append({
            "home":      leg["home"],
            "away":      leg["away"],
            "pick":      leg["pick"],
            "odd":       str(leg["odd"]),
            "reasoning": leg["reasoning"],
        })
    acca["legs_formatted"] = acca_legs

    response = AnalyticsResponse(
        predictions       = predictions,
        accumulator       = acca,
        summary           = AnalyticsSummary(**local_result["summary"]),
        acca_combined_odd = acca_odd,
        acca_reasoning    = acca_rsn,
        engine            = "gemini+poisson" if ai_result else "poisson-local",
        session_id        = sid,
    )

    # ── Broadcast via WebSocket to this session's open tabs ──────────
    pred_dicts = [p.model_dump() for p in predictions]
    await ws_manager.send_to_session(sid, msg_prediction_ready(
        session_id  = sid,
        predictions = pred_dicts,
        summary     = local_result["summary"],
        engine      = response.engine,
    ))

    # Emit individual value_alert messages per value bet
    await ws_manager.broadcast_value_alerts(
        [p.model_dump() for p in predictions], sid
    )

    # ── Web Push notification for value bets ─────────────────────────
    asyncio.create_task(_notify_value_bets(
        [p.model_dump() for p in predictions], sid
    ))

    return response


# ──────────────────────────────────────────────────────────────────
# WEBSOCKET ENDPOINT
# ──────────────────────────────────────────────────────────────────
@app.websocket("/ws/{session_id}")
async def websocket_endpoint(ws: WebSocket, session_id: str):
    """
    Persistent real-time connection per session.
    - Sends prediction_ready, value_alert, analysis_progress events.
    - Keeps alive with ping every 25s.
    - Accepts {"type":"ping"} heartbeat from client.
    """
    await ws_manager.connect(ws, session_id)
    try:
        while True:
            # Wait for any client message (heartbeat or command)
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            except Exception:
                pass
    except WebSocketDisconnect:
        ws_manager.disconnect(ws, session_id)


@app.get("/ws/stats")
def ws_stats():
    return ws_manager.stats()


# ──────────────────────────────────────────────────────────────────
# WEB PUSH ENDPOINTS
# ──────────────────────────────────────────────────────────────────

def _load_vapid() -> Optional[dict]:
    if VAPID_FILE.exists():
        return json.loads(VAPID_FILE.read_text())
    return None


def _load_subscriptions() -> List[dict]:
    if not PUSH_SUBS_FILE.exists():
        return []
    subs = []
    for line in PUSH_SUBS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            subs.append(json.loads(line))
        except Exception:
            pass
    return subs


def _save_subscription(sub: dict):
    # Deduplicate by endpoint
    existing = _load_subscriptions()
    endpoints = {s["endpoint"] for s in existing}
    if sub["endpoint"] not in endpoints:
        with PUSH_SUBS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(sub) + "\n")


@app.get("/api/push/vapid-public-key")
def get_vapid_public_key():
    """Returns the VAPID public key for the frontend to subscribe with."""
    vapid = _load_vapid()
    if not vapid:
        raise HTTPException(503, "VAPID keys not generated yet. Run: python generate_vapid.py")
    return {"public_key": vapid["public_key"]}


@app.post("/api/push/subscribe")
async def push_subscribe(sub: PushSubscription):
    """Store a browser push subscription."""
    entry = sub.model_dump()
    entry["subscribed_at"] = datetime.utcnow().isoformat()
    _save_subscription(entry)
    return {"status": "subscribed", "endpoint": sub.endpoint[:60] + "..."}


@app.post("/api/push/notify")
async def push_notify_endpoint(
    token_payload: Optional[dict] = Depends(get_optional_user)
):
    """Manually trigger a push to all subscribers (admin use)."""
    sent, failed = await _send_push_to_all({
        "title": "Virtual Edge — New Analysis Ready",
        "body":  "Open the app to see the latest predictions.",
        "url":   "/",
    })
    return {"sent": sent, "failed": failed}


async def _send_push_to_all(payload: dict) -> tuple[int, int]:
    """Send Web Push notification to all stored subscriptions."""
    vapid = _load_vapid()
    if not vapid:
        return 0, 0

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("pywebpush not installed — push disabled")
        return 0, 0

    subs  = _load_subscriptions()
    sent  = 0
    failed = 0

    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys":     sub["keys"],
                },
                data=json.dumps({
                    "title": payload.get("title", "Virtual Edge"),
                    "body":  payload.get("body", ""),
                    "url":   payload.get("url", "/"),
                }),
                vapid_private_key=vapid["private_key"],
                vapid_claims={
                    "sub": f"mailto:{os.getenv('VAPID_EMAIL','admin@virtualedge.app')}",
                },
            )
            sent += 1
        except Exception as e:
            print(f"Push failed for {sub['endpoint'][:40]}: {e}")
            failed += 1

    return sent, failed


async def _notify_value_bets(predictions: list, session_id: str):
    """Push browser notification for every value bet found."""
    value_bets = [p for p in predictions if p.get("value_bet")]
    if not value_bets:
        return
    best = value_bets[0]
    await _send_push_to_all({
        "title": f"Value Bet: {best['home']} vs {best['away']}",
        "body":  (f"{best['prediction']} @ {best['odd']} — "
                  f"{best['confidence']} confidence, +{best['edge_pct']:.1f}% edge"),
        "url":   "/",
    })


# ──────────────────────────────────────────────────────────────────
# FEEDBACK
# ──────────────────────────────────────────────────────────────────
@app.post("/api/feedback")
async def feedback_endpoint(
    items:         List[FeedbackItem],
    token_payload: Optional[dict] = Depends(get_optional_user),
):
    if not items:
        raise HTTPException(400, "No feedback items provided")
    saved = 0
    for item in items:
        ts    = item.timestamp or datetime.utcnow().isoformat()
        entry = item.model_dump()
        entry["timestamp"] = ts
        save_feedback(entry)

        if item.fixture_id:
            try:
                is_correct  = item.result == "win"
                stake       = item.stake or 0.0
                profit      = stake * (float(item.odd) - 1) if is_correct else -stake
                insert_outcome(
                    fixture_id    = item.fixture_id,
                    prediction_id = item.prediction_id,
                    actual_result = item.pick if is_correct else "?",
                    is_correct    = is_correct,
                    stake         = stake,
                    profit_loss   = profit,
                )
                if is_correct:
                    if item.pick == "1":
                        update_team_record(item.home, "win")
                        update_team_record(item.away, "loss")
                    elif item.pick == "2":
                        update_team_record(item.home, "loss")
                        update_team_record(item.away, "win")
                    elif item.pick == "X":
                        update_team_record(item.home, "draw")
                        update_team_record(item.away, "draw")
            except Exception as e:
                print(f"Feedback DB persist failed: {e}")
        saved += 1

    total = 0
    if FEEDBACK_FILE.exists():
        with FEEDBACK_FILE.open() as f:
            total = sum(1 for _ in f)
    return {"saved": saved, "total_feedback": total}


@app.get("/api/feedback/stats")
def feedback_stats(token_payload: Optional[dict] = Depends(get_optional_user)):
    feedback = load_recent_feedback(500)
    if not feedback:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0}
    wins   = sum(1 for f in feedback if f.get("result") == "win")
    losses = sum(1 for f in feedback if f.get("result") == "loss")
    return {
        "total":    len(feedback),
        "wins":     wins,
        "losses":   losses,
        "win_rate": round(wins / len(feedback) * 100, 1),
    }


# ──────────────────────────────────────────────────────────────────
# PREDICTIONS — PENDING & RESOLVE (Results Checker)
# ──────────────────────────────────────────────────────────────────
class ResolveRequest(BaseModel):
    result: str          # "win" | "loss"
    stake:  float = 0.0  # optional — used for profit calculation


@app.get("/api/predictions/pending")
def predictions_pending(
    token_payload: Optional[dict] = Depends(get_optional_user),
):
    """
    Returns all predictions older than STALE_AFTER minutes
    that have no recorded outcome yet.
    Feeds the Results Checker UI in the History tab.
    """
    pending  = get_pending_predictions()
    accuracy = get_accuracy_snapshot()
    return {
        "pending":  pending,
        "count":    len(pending),
        "accuracy": accuracy,
    }


@app.post("/api/predictions/{prediction_id}/resolve")
async def prediction_resolve(
    prediction_id: int,
    body:          ResolveRequest,
    token_payload: Optional[dict] = Depends(get_optional_user),
):
    """
    Mark a single prediction as won or lost.
    Updates the DB outcome, recalculates accuracy, and broadcasts
    the updated accuracy to all connected WebSocket clients.
    """
    if body.result not in ("win", "loss"):
        raise HTTPException(400, "result must be 'win' or 'loss'")

    try:
        accuracy = resolve_prediction(
            prediction_id = prediction_id,
            result        = body.result,
            stake         = body.stake,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"Resolve failed: {e}")

    # Broadcast updated accuracy to all tabs immediately
    await ws_manager.broadcast({
        "type":     "accuracy_update",
        "accuracy": accuracy,
        "ts":       datetime.utcnow().isoformat(),
    })

    return {
        "status":   "resolved",
        "result":   body.result,
        "accuracy": accuracy,
    }


@app.get("/api/accuracy")
def accuracy_endpoint(token_payload: Optional[dict] = Depends(get_optional_user)):
    """Instant accuracy snapshot — no scheduler needed."""
    return get_accuracy_snapshot()


# ──────────────────────────────────────────────────────────────────
# STATS DASHBOARD
# ──────────────────────────────────────────────────────────────────
@app.get("/api/stats")
def stats_endpoint(token_payload: Optional[dict] = Depends(get_optional_user)):
    return {
        "performance":        get_performance_summary(),
        "value_bet_stats":    get_value_bet_stats(),
        "team_leaderboard":   get_team_leaderboard(20),
        "recent_predictions": get_recent_predictions(20),
    }


# ──────────────────────────────────────────────────────────────────
# MONITORING  (Step 5.2 — accuracy iteration tracker)
# ──────────────────────────────────────────────────────────────────
@app.get("/api/monitoring")
def monitoring_endpoint(token_payload: Optional[dict] = Depends(get_optional_user)):
    """
    Full accuracy monitoring report:
      • overall win-rate, ROI, profit
      • win-rate by confidence tier (LOW / MEDIUM / HIGH)
      • win-rate by market type (1X2, OU25, BTTS, CS)
      • value bets vs standard picks compared
      • rolling trend over last 20 resolved predictions
      • bookmaker overround analysis
      • auto-generated alerts and plain-English recommendations
    """
    try:
        return generate_report()
    except Exception as e:
        raise HTTPException(500, f"Monitoring report failed: {e}")


# ──────────────────────────────────────────────────────────────────
# HEALTH
# ──────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    groq_key = os.getenv("GROQ_API_KEY")
    gemini_key = os.getenv("GOOGLE_AI_STUDIO_KEY") or os.getenv("GEMINI_API_KEY")
    return {
        "status":        "ok",
        "version":       "5.0",
        "groq":          bool(groq_key),
        "gemini":        bool(gemini_key),
        "openai":        bool(os.getenv("OPENAI_API_KEY")),
        "google_vision": bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS")),
        "db":            "PostgreSQL" if __import__("database").IS_POSTGRES else str(pathlib.Path(__file__).parent / "virtualedge.db"),
        "endpoints": {
            "register":       "POST /auth/register",
            "login":          "POST /auth/login",
            "ocr":            "POST /api/ocr",
            "analyse":        "POST /api/analyse  (instant, no AI)",
            "predict":        "POST /api/predict  (AI + Poisson)",
            "feedback":       "POST /api/feedback",
            "pending":        "GET  /api/predictions/pending",
            "resolve":        "POST /api/predictions/{id}/resolve",
            "accuracy":       "GET  /api/accuracy",
            "stats":          "GET  /api/stats",
            "monitoring":     "GET  /api/monitoring",
            "websocket":      "WS   /ws/{session_id}",
            "docs":           "GET  /docs",
        },
    }
