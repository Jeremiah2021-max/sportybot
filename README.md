# Virtual Edge — SportyBet Instant Virtuals Predictor

> AI-powered screenshot analysis, Poisson goal modelling, Kelly staking, real-time WebSocket feed, and Web Push notifications for SportyBet Ghana Instant Virtuals.

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [Architecture](#architecture)
3. [Local Development](#local-development)
4. [Environment Variables](#environment-variables)
5. [Deploy: Backend → Render](#deploy-backend--render)
6. [Deploy: Frontend → Vercel](#deploy-frontend--vercel)
7. [Deploy: Frontend → Netlify](#deploy-frontend--netlify-alternative)
8. [PostgreSQL Setup](#postgresql-setup)
9. [API Reference](#api-reference)
10. [Monitoring & Accuracy Iteration](#monitoring--accuracy-iteration)
11. [File Structure](#file-structure)
12. [Important Disclaimer](#important-disclaimer)

---

## What It Does

| Feature | Detail |
|---|---|
| **Screenshot OCR** | Upload a SportyBet Instant Virtuals screenshot → Gemini 1.5-pro reads every match and odds |
| **Poisson model** | Strips bookmaker overround, infers expected goals (λ), derives Over/Under 2.5, BTTS, correct scores |
| **AI predictions** | Gemini overlays reasoning on Poisson probabilities to produce ranked picks with confidence tiers |
| **Value detection** | Flags picks where fair probability > implied probability by ≥ 3% (genuine edge) |
| **Kelly staking** | ¼ Kelly Criterion suggests optimal stake per pick based on your bankroll |
| **Real-time feed** | WebSocket broadcasts predictions and value alerts to all open browser tabs instantly |
| **Web Push** | Native browser/Android notifications when a high-confidence value bet is found |
| **Results Checker** | APScheduler polls every 5 min for unresolved predictions, prompts manual result logging |
| **Monitoring** | Full accuracy report: win-rate by confidence tier, market, value-bet flag, rolling trend, auto-alerts |
| **Auth** | JWT registration and login — results and bankroll tied to your account |

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  Browser (index.html + app.js + push.js)             │
│  Served by Vercel / Netlify CDN                      │
│                                                      │
│  ┌──────────┐  HTTPS/WSS  ┌──────────────────────┐  │
│  │  Upload  │ ──────────► │  FastAPI Backend      │  │
│  │  Screen- │             │  (Render / Railway /  │  │
│  │  shot    │ ◄────────── │   Docker VPS)         │  │
│  └──────────┘  JSON/WS    └──────────┬───────────┘  │
└─────────────────────────────────────┼───────────────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                   │
              ┌─────▼─────┐   ┌───────▼──────┐   ┌───────▼──────┐
              │ Gemini API │   │  PostgreSQL  │   │  APScheduler │
              │ (Google AI │   │  (sportybot_ │   │  Results     │
              │  Studio)   │   │   db)        │   │  Checker     │
              └───────────┘   └──────────────┘   └──────────────┘
```

---

## Local Development

### Prerequisites

- Python 3.11+
- PostgreSQL 14+ running locally (or skip — SQLite works out of the box)
- A Google AI Studio API key (free): https://aistudio.google.com/app/apikey

### 1. Clone and set up the backend

```powershell
cd C:\Users\HP\Desktop\sportybot\backend

# Create virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Generate VAPID keys for Web Push (run once)
python generate_vapid.py
```

### 2. Configure environment

```powershell
# Copy the example file
Copy-Item .env.example .env
```

Edit `.env` and fill in at minimum:

```env
GOOGLE_AI_STUDIO_KEY=AIza-your-key-here
DATABASE_URL=postgresql://postgres:yourpassword@localhost:5432/sportybot_db
SECRET_KEY=generate-a-long-random-string-here
```

Generate a secret key:
```powershell
.\venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

### 3. Start the backend

```powershell
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

You should see:
```
[DB] Initialised — backend: PostgreSQL
INFO: Application startup complete.
```

### 4. Open the frontend

Double-click `index.html` — or open `http://localhost:8000/docs` to explore the API.

Set the backend URL in your browser console once:
```js
localStorage.setItem("ve_backend", "http://localhost:8000")
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_AI_STUDIO_KEY` | ✅ Yes | — | Gemini OCR + prediction engine |
| `DATABASE_URL` | No | SQLite file | `postgresql://user:pass@host:5432/dbname` |
| `SECRET_KEY` | ✅ Yes (prod) | weak default | JWT signing key — change in production |
| `TOKEN_EXPIRE_MINUTES` | No | `1440` | JWT lifetime in minutes |
| `OPENAI_API_KEY` | No | — | OpenAI fallback OCR engine |
| `GOOGLE_APPLICATION_CREDENTIALS` | No | — | Path to Google Cloud Vision JSON key |
| `VAPID_EMAIL` | No | `admin@virtualedge.app` | Contact email for Web Push VAPID |
| `PORT` | No | `8000` | Injected automatically by Render/Railway |
| `ALLOWED_ORIGINS` | No | `*` | Comma-separated CORS origins |
| `MONITOR_MIN_SAMPLE` | No | `50` | Min predictions before monitoring alerts fire |
| `MONITOR_WIN_RATE_ALERT` | No | `45` | Win-rate % below which an alert is raised |

---

## Deploy: Backend → Render

Render is the recommended platform — it provides free PostgreSQL and a free web service tier.

### Step 1 — Create a PostgreSQL database on Render

1. Go to https://dashboard.render.com → **New** → **PostgreSQL**
2. Name it `virtualedge-db`, choose the free plan
3. After creation, go to **Connect** → copy the **External Database URL**
   ```
   postgresql://user:password@dpg-xxxxxxx.oregon-postgres.render.com/virtualedge_db
   ```

### Step 2 — Push your code to GitHub

```powershell
cd C:\Users\HP\Desktop\sportybot
git init
git add .
git commit -m "Virtual Edge v5.0"
# Create a repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/virtual-edge.git
git push -u origin main
```

> **Important:** Add `.env` to `.gitignore` so your credentials are never committed:
> ```
> echo "backend/.env" >> .gitignore
> echo "backend/vapid_keys.json" >> .gitignore
> echo "backend/feedback_store.jsonl" >> .gitignore
> echo "backend/virtualedge.db*" >> .gitignore
> ```

### Step 3 — Create a Web Service on Render

1. **New** → **Web Service** → connect your GitHub repo
2. Set these fields:

| Field | Value |
|---|---|
| **Root Directory** | `backend` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt && python generate_vapid.py` |
| **Start Command** | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| **Plan** | Free (or Starter for always-on) |

3. Add **Environment Variables** (Render dashboard → Environment tab):

| Key | Value |
|---|---|
| `GOOGLE_AI_STUDIO_KEY` | Your Gemini API key |
| `DATABASE_URL` | The External URL from Step 1 |
| `SECRET_KEY` | A long random string |
| `VAPID_EMAIL` | Your email address |

4. Click **Deploy** — Render will build and start the service.
5. Your backend URL will be: `https://your-service-name.onrender.com`

### Step 4 — Point the frontend to the deployed backend

In `app.js`, update the default:
```js
const BACKEND_URL = localStorage.getItem("ve_backend") || "https://your-service-name.onrender.com";
```

Or users can set it in their browser:
```js
localStorage.setItem("ve_backend", "https://your-service-name.onrender.com")
```

---

## Deploy: Frontend → Vercel

The frontend is a static HTML/CSS/JS app — no build step needed.

### Option A — Vercel CLI

```powershell
npm install -g vercel
cd C:\Users\HP\Desktop\sportybot
vercel --prod
```

Follow the prompts. Your site will be live at `https://your-project.vercel.app`.

### Option B — Vercel GitHub integration

1. Go to https://vercel.com/new
2. Import your GitHub repository
3. Set **Root Directory** to `/` (the repo root, not `backend/`)
4. Framework preset: **Other**
5. Click **Deploy**

The `vercel.json` already in the repo handles all routing, caching, and security headers automatically.

---

## Deploy: Frontend → Netlify (alternative)

```powershell
npm install -g netlify-cli
cd C:\Users\HP\Desktop\sportybot
netlify deploy --prod --dir .
```

Or drag-and-drop the `sportybot` folder onto https://app.netlify.com/drop.

The `netlify.toml` in the repo configures:
- Service worker delivered with `no-cache` headers (required by browsers)
- Long cache for JS/CSS assets
- SPA fallback so all routes serve `index.html`
- Security headers (X-Frame-Options, X-Content-Type-Options, etc.)

---

## PostgreSQL Setup

### Local (already done)

Your local database `sportybot_db` is already connected via:
```
postgresql://postgres:****@localhost:5432/sportybot_db
```

Tables are created automatically by `init_db()` on first startup.

### Production (Render managed DB)

Same process — just set `DATABASE_URL` to the Render external URL and the app handles everything else.

### Migrations

There is no separate migration runner. `init_db()` uses `CREATE TABLE IF NOT EXISTS` so it is safe to run repeatedly. To add a new column in production:

```sql
-- Connect to your DB and run manually:
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS new_column TEXT;
```

---

## API Reference

Base URL: `http://localhost:8000` (local) or `https://your-service.onrender.com` (production)

Interactive docs: `GET /docs` (Swagger UI)

### Auth

| Method | Endpoint | Body | Description |
|---|---|---|---|
| `POST` | `/auth/register` | `{username, email, password}` | Create account → returns JWT |
| `POST` | `/auth/login` | `{username, password}` | Login → returns JWT |
| `GET` | `/auth/me` | — | Current user info |
| `PUT` | `/auth/bankroll` | `{bankroll: 200.0}` | Update bankroll |

### Analysis

| Method | Endpoint | Body | Description |
|---|---|---|---|
| `POST` | `/api/ocr` | `form-data: image` | Screenshot → extracted matches |
| `POST` | `/api/analyse` | `[{home, away, odd1, oddX, odd2}]` | Instant local Poisson (no AI cost) |
| `POST` | `/api/predict` | `[{home, away, odd1, oddX, odd2}]` | Full AI + Poisson pipeline |

### Results & Stats

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/feedback` | Log win/loss result |
| `GET` | `/api/predictions/pending` | Predictions awaiting results |
| `POST` | `/api/predictions/{id}/resolve` | Mark prediction won/lost |
| `GET` | `/api/accuracy` | Live accuracy snapshot |
| `GET` | `/api/stats` | Full dashboard data |
| `GET` | `/api/monitoring` | Accuracy iteration report (see below) |

### Real-time

| Protocol | Endpoint | Description |
|---|---|---|
| `WebSocket` | `/ws/{session_id}` | Real-time prediction and alert feed |
| `POST` | `/api/push/subscribe` | Register browser for Web Push |
| `GET` | `/api/push/vapid-public-key` | VAPID public key for subscription |

### Example: Full predict flow

```powershell
# 1. Upload screenshot → get matches
$form = @{ image = Get-Item "screenshot.png" }
$ocr  = Invoke-RestMethod -Uri "http://localhost:8000/api/ocr" -Method POST -Form $form

# 2. Send matches to predict endpoint
$body = $ocr.matches | ConvertTo-Json
$pred = Invoke-RestMethod -Uri "http://localhost:8000/api/predict" `
        -Method POST -ContentType "application/json" -Body $body

# 3. View top pick
$pred.predictions[0] | Select-Object home, away, prediction, confidence, edge_pct, odd
```

---

## Monitoring & Accuracy Iteration

Call `GET /api/monitoring` to get a full accuracy report.

### Sample response structure

```json
{
  "generated_at": "2026-10-15T14:32:00Z",
  "overall": {
    "total": 312,
    "wins": 178,
    "losses": 134,
    "win_rate": 57.1,
    "total_profit": 42.50,
    "roi_pct": 8.3
  },
  "by_confidence": [
    { "tier": "HIGH (80+)",     "total": 88, "wins": 58, "win_rate": 65.9 },
    { "tier": "MEDIUM (66-79)", "total": 142, "wins": 80, "win_rate": 56.3 },
    { "tier": "LOW (50-65)",    "total": 82, "wins": 40, "win_rate": 48.8 }
  ],
  "by_market": [
    { "market": "1X2",  "total": 240, "win_rate": 58.3, "profit": 38.20 },
    { "market": "OU25", "total": 52,  "win_rate": 53.8, "profit": 4.30  },
    { "market": "BTTS", "total": 20,  "win_rate": 50.0, "profit": 0.00  }
  ],
  "by_value_bet": {
    "Value Bet (edge ≥ 3%)": { "total": 94, "win_rate": 63.8, "avg_edge_pct": 4.9 },
    "Standard Pick":         { "total": 218, "win_rate": 53.7, "avg_edge_pct": 1.2 }
  },
  "trend": {
    "window": 20, "wins": 13, "win_rate": 65.0, "direction": "improving"
  },
  "alerts": ["✅ All metrics within normal range."],
  "recommendations": ["✅ Value bets outperform standard picks by 10.1pp. Prioritise value bets."]
}
```

### How to iterate on accuracy

After 50+ resolved predictions, check the monitoring report and act on these signals:

| Signal | Action |
|---|---|
| Value bet win-rate **below** standard picks | Raise `VALUE_THRESHOLD` in `analytics.py` from `0.03` to `0.05` |
| HIGH confidence win-rate **below** 55% | Raise `HIGH_CONF_FLOOR` in `monitoring.py` from `80` to `85` |
| Trend **declining** vs overall | Check if SportyBet updated their engine — odds patterns may have shifted |
| ROI **negative** | Lower `KELLY_FRACTION` in `analytics.py` from `0.25` to `0.15` |
| MEDIUM tier wins more than HIGH tier | Increase `KELLY_FRACTION` only for MEDIUM picks |
| OU25 / BTTS win-rates higher than 1X2 | Shift staking focus to those markets |

The APScheduler checks for pending predictions every 5 minutes and broadcasts `accuracy_update` via WebSocket — the accuracy ring in the History tab updates live as you log results.

---

## File Structure

```
sportybot/
├── index.html              ← App shell
├── style.css               ← All styles (dark violet theme)
├── app.js                  ← Frontend logic, OCR, predictions, results checker
├── push.js                 ← WebSocket client, Web Push subscription
├── sw.js                   ← Service worker (offline cache, push events)
├── manifest.json           ← PWA manifest (Add to Home Screen)
├── vercel.json             ← Vercel deployment config
├── netlify.toml            ← Netlify deployment config
│
└── backend/
    ├── main.py             ← FastAPI app v5.0 — all endpoints
    ├── analytics.py        ← Poisson + Kelly engine (no API cost)
    ├── poisson.py          ← Goal model, OU2.5, BTTS, correct scores
    ├── monitoring.py       ← Accuracy iteration tracker
    ├── scheduler.py        ← APScheduler results-checker (every 5 min)
    ├── database.py         ← SQLAlchemy ORM + raw helpers (SQLite & PostgreSQL)
    ├── models.py           ← ORM table definitions
    ├── auth.py             ← JWT auth, bcrypt hashing
    ├── websocket_manager.py← WebSocket connection registry + broadcaster
    ├── generate_vapid.py   ← One-time VAPID key generator
    ├── requirements.txt    ← All Python dependencies
    ├── Dockerfile          ← Container build for VPS / Docker deployment
    ├── Procfile            ← Render / Railway process entry point
    ├── .env                ← Your local secrets (never commit)
    └── .env.example        ← Template — copy to .env and fill in
```

---

## Important Disclaimer

SportyBet Instant Virtuals are **RNG-based** (powered by Kiron Interactive).
Each match outcome is computer-generated and independent of all previous results.

This tool:
- ✅ Strips bookmaker overround to find **fair probabilities**
- ✅ Detects **value bets** where odds are more generous than the probability warrants
- ✅ Applies **Kelly Criterion** for mathematically sound staking
- ❌ Cannot predict RNG outcomes
- ❌ Does not guarantee wins

**Gamble responsibly. Only stake what you can afford to lose entirely. 18+**
#   s p o r t y b o t  
 