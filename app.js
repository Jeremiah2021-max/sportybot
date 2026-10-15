// ======================
// Virtual Edge - SportyBet Instant Virtuals Predictor (Ghana)
// ======================

const STORAGE_KEY = "virtualEdge_data_v1";

// Backend URL (shared with push.js via window scope)
const BACKEND_URL = localStorage.getItem("ve_backend") || "http://localhost:8000";
window.BACKEND_URL = BACKEND_URL;

// State
let state = {
  bankroll: 0,
  bets: [],
  matches: [],   // { home, away, odd1, oddX, odd2 }
  sessionId: null
};

let selectedResult = null;
let currentImage = null;

// ---------- Init ----------
document.addEventListener("DOMContentLoaded", () => {
  loadState();
  setupTabs();
  setupUpload();
  setupBankroll();
  setupHistory();
  setupRealtimeFeed();
  renderAll();

  // Init WebSocket + push after UI is ready
  const sid = state.sessionId || (state.sessionId = crypto.randomUUID());
  saveState();
  if (window.VEPush) {
    VEPush.init(sid);
  }
});

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const data = JSON.parse(raw);
      state.bankroll  = data.bankroll  || 0;
      state.bets      = data.bets      || [];
      state.sessionId = data.sessionId || null;
    }
  } catch (e) {
    console.warn("Could not load saved data", e);
  }
}

function saveState() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    bankroll:  state.bankroll,
    bets:      state.bets,
    sessionId: state.sessionId,
  }));
}

function renderAll() {
  updateBankrollUI();
  renderHistory();
  updateStats();
}

// ---------- Tabs ----------
function setupTabs() {
  document.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
      document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(btn.dataset.tab).classList.add("active");
    });
  });
}

// ---------- Upload & OCR ----------
function setupUpload() {
  const zone = document.getElementById("uploadZone");
  const input = document.getElementById("fileInput");
  const placeholder = document.getElementById("uploadPlaceholder");
  const preview = document.getElementById("previewImage");
  const analyzeBtn = document.getElementById("analyzeBtn");

  zone.addEventListener("click", () => input.click());

  zone.addEventListener("dragover", e => {
    e.preventDefault();
    zone.classList.add("dragover");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
  zone.addEventListener("drop", e => {
    e.preventDefault();
    zone.classList.remove("dragover");
    if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
  });

  input.addEventListener("change", () => {
    if (input.files.length) handleFile(input.files[0]);
  });

  analyzeBtn.addEventListener("click", runOCR);
}

function handleFile(file) {
  if (!file.type.startsWith("image/")) {
    alert("Please upload an image file");
    return;
  }
  currentImage = file;
  const url = URL.createObjectURL(file);
  const preview = document.getElementById("previewImage");
  const placeholder = document.getElementById("uploadPlaceholder");
  
  preview.src = url;
  preview.classList.remove("hidden");
  placeholder.classList.add("hidden");
  document.getElementById("analyzeBtn").disabled = false;

  // Reset previous results
  document.getElementById("matchesSection").classList.add("hidden");
  document.getElementById("recommendations").classList.add("hidden");
  document.getElementById("ocrStatus").classList.add("hidden");
}

// Stores the last AI prediction response
let lastPrediction = null;

// ── Skeleton loader helpers ────────────────────────────────────────
function showSkeletons(count = 3) {
  const recsCard = document.getElementById("recommendations");
  const container = document.getElementById("recsContainer");
  container.innerHTML = Array.from({ length: count }, () => `
    <div class="skeleton-card">
      <div class="skel skel-badge"></div>
      <div class="skel skel-title"></div>
      <div class="skel skel-pick"></div>
      <div class="skel skel-meta"></div>
      <div class="skel-bars">
        <div class="skel skel-bar" style="width:62%"></div>
        <div class="skel skel-bar" style="width:43%"></div>
        <div class="skel skel-bar" style="width:35%"></div>
      </div>
    </div>
  `).join("");
  recsCard.classList.remove("hidden");
}

function hideSkeletons() {
  // Skeletons are replaced when renderAIPredictions() writes real content
}

// ── Step label map for the progress bar ──────────────────────────
const OCR_STEPS = [
  { pct: 5,  text: "Uploading screenshot…" },
  { pct: 15, text: "Reading screenshot with Gemini Vision…" },
  { pct: 40, text: "Extracting matches and odds…" },
  { pct: 55, text: "Running Poisson analysis…" },
  { pct: 70, text: "Running AI prediction engine…" },
  { pct: 85, text: "Applying Kelly staking model…" },
  { pct: 95, text: "Finalising recommendations…" },
  { pct: 100, text: "Done!" },
];
let _stepTimer = null;

function startStepAnimation(statusText, statusPercent, progressFill) {
  let i = 0;
  _stepTimer = setInterval(() => {
    if (i >= OCR_STEPS.length) { clearInterval(_stepTimer); return; }
    const s = OCR_STEPS[i++];
    statusText.textContent    = s.text;
    statusPercent.textContent = s.pct + "%";
    progressFill.style.width  = s.pct + "%";
  }, 600);
}

function stopStepAnimation() {
  if (_stepTimer) { clearInterval(_stepTimer); _stepTimer = null; }
}

async function runOCR() {
  if (!currentImage) return;

  const statusCard    = document.getElementById("ocrStatus");
  const statusText    = document.getElementById("statusText");
  const statusPercent = document.getElementById("statusPercent");
  const progressFill  = document.getElementById("progressFill");
  const analyzeBtn    = document.getElementById("analyzeBtn");
  const btnText       = analyzeBtn.querySelector(".btn-text");
  const spinner       = analyzeBtn.querySelector(".spinner");

  // Reset UI
  statusCard.classList.remove("hidden");
  analyzeBtn.disabled = true;
  btnText.textContent = "Analyzing…";
  spinner.classList.remove("hidden");
  lastPrediction = null;

  // Show skeletons immediately so the user sees activity
  // while the 2-3s backend round-trip is in flight
  showSkeletons(3);

  // Animated step labels
  startStepAnimation(statusText, statusPercent, progressFill);

  function setStatus(text, pct) {
    stopStepAnimation();
    statusText.textContent    = text;
    statusPercent.textContent = pct + "%";
    progressFill.style.width  = pct + "%";
  }

  try {
    // ── STEP 1: OCR ───────────────────────────────────────────────
    let usedBackend = false;

    try {
      const formData = new FormData();
      formData.append("image", currentImage);

      const controller = new AbortController();
      const timeout    = setTimeout(() => controller.abort(), 20000);

      const res = await fetch(`${window.BACKEND_URL}/api/ocr`, {
        method: "POST",
        body:   formData,
        signal: controller.signal,
      });
      clearTimeout(timeout);

      if (res.ok) {
        const data = await res.json();
        if (data.matches && data.matches.length > 0) {
          state.matches = data.matches;
          usedBackend   = true;
          setStatus(`OCR complete — ${data.matches.length} matches found (${data.engine})`, 40);
        }
      }
    } catch (backendErr) {
      console.log("Backend OCR unavailable, falling back to Tesseract:", backendErr.message);
    }

    // Fallback: local Tesseract
    if (!usedBackend) {
      setStatus("Using local OCR (Tesseract)…", 15);
      const worker = await Tesseract.createWorker("eng", 1, {
        logger: m => {
          if (m.status === "recognizing text") {
            const pct = Math.round(m.progress * 40) + 15;
            statusText.textContent    = "Reading screenshot…";
            statusPercent.textContent = pct + "%";
            progressFill.style.width  = pct + "%";
          }
        },
      });
      const { data: { text } } = await worker.recognize(currentImage);
      await worker.terminate();
      setStatus("Parsing SportyBet layout…", 40);
      state.matches = parseOCRText(text);
    }

    // Ensure editable rows even if OCR found nothing
    if (state.matches.length === 0) {
      for (let i = 0; i < 6; i++) {
        state.matches.push({ home: "", away: "", odd1: "", oddX: "", odd2: "" });
      }
    }

    renderMatchesEditor();
    document.getElementById("matchesSection").classList.remove("hidden");

    // ── STEP 2: AI + POISSON PREDICTION ──────────────────────────
    const validMatches = state.matches.filter(
      m => m.home && m.away && m.odd1 && m.oddX && m.odd2
    );

    if (validMatches.length > 0) {
      // Keep skeletons visible while AI call is in flight
      setStatus("Running AI + Poisson prediction engine…", 60);

      try {
        const controller2 = new AbortController();
        const timeout2    = setTimeout(() => controller2.abort(), 45000);

        const predRes = await fetch(`${window.BACKEND_URL}/api/predict`, {
          method:  "POST",
          headers: { "Content-Type": "application/json" },
          // Pass session_id so backend broadcast hits this tab's WS
          body:    JSON.stringify(validMatches, null, 0),
          signal:  controller2.signal,
        });
        clearTimeout(timeout2);

        if (predRes.ok) {
          lastPrediction = await predRes.json();
          // Sync session ID returned by backend
          if (lastPrediction.session_id) {
            state.sessionId = lastPrediction.session_id;
            saveState();
          }
          setStatus("Predictions ready!", 100);
        } else {
          const err = await predRes.json().catch(() => ({}));
          console.warn("Prediction API error:", err);
          setStatus("OCR done — AI prediction unavailable", 100);
        }
      } catch (predErr) {
        console.warn("Prediction request failed:", predErr.message);
        setStatus("OCR done — AI prediction timed out", 100);
      }
    } else {
      setStatus("OCR done — fill in odds to get predictions", 100);
    }

    // Render real cards — replaces skeleton placeholders
    if (lastPrediction) {
      renderAIPredictions(lastPrediction);
      document.getElementById("recommendations").classList.remove("hidden");
    } else {
      // Clear skeletons if no prediction came back
      document.getElementById("recsContainer").innerHTML = "";
      document.getElementById("recommendations").classList.add("hidden");
    }

    statusCard.classList.add("hidden");

  } catch (err) {
    console.error(err);
    stopStepAnimation();
    setStatus("OCR failed — enter matches manually.", 0);
    state.matches = [];
    for (let i = 0; i < 6; i++) {
      state.matches.push({ home: "", away: "", odd1: "", oddX: "", odd2: "" });
    }
    // Clear skeletons on error
    document.getElementById("recsContainer").innerHTML = "";
    document.getElementById("recommendations").classList.add("hidden");
    renderMatchesEditor();
    document.getElementById("matchesSection").classList.remove("hidden");
  } finally {
    stopStepAnimation();
    analyzeBtn.disabled = false;
    btnText.textContent = "Analyze Matches";
    spinner.classList.add("hidden");
  }
}

// ======================
// AI Prediction Renderer
// ======================

function confidenceTier(score) {
  if (score >= 80) return { label: "HIGH",   cls: "tier-high"   };
  if (score >= 66) return { label: "MEDIUM", cls: "tier-medium" };
  return              { label: "LOW",    cls: "tier-low"    };
}

function renderAIPredictions(prediction) {
  const container = document.getElementById("recsContainer");
  container.innerHTML = "";

  const bankroll    = state.bankroll || 0;
  const predictions = prediction.predictions || [];
  const acca        = prediction.accumulator || [];

  // Store session_id for feedback
  if (prediction.session_id) state.sessionId = prediction.session_id;

  // ── Header notice ────────────────────────────────────────────────
  const hasValue = predictions.some(p => p.value_bet);
  const notice   = document.createElement("div");
  notice.className = `rec-notice ${hasValue ? "value-notice" : "info-notice"}`;
  notice.innerHTML = hasValue
    ? `<strong>Value bets detected</strong> — AI + Poisson found picks where odds exceed fair probability. Ranked by edge.`
    : `<strong>AI + Poisson analysis complete</strong> — No strong value found. Picks shown by highest confidence. Bet small.`;
  container.appendChild(notice);

  // ── Per-match prediction cards ──────────────────────────────────
  predictions.forEach((p, i) => {
    const bp      = p.best_pick;
    const sp      = p.safe_pick;
    const tier    = confidenceTier(bp.confidence);
    const markets = p.markets || null;

    // Kelly stake
    const kellyStakeAmt = bankroll > 0
      ? Math.min(p.kelly_fraction * 0.25 * bankroll, bankroll * 0.15)
      : 0;

    const stakeHtml = bankroll > 0 && kellyStakeAmt > 0.01
      ? `<div class="kelly-stake">
           Suggested stake: <strong>GHS ${kellyStakeAmt.toFixed(2)}</strong>
           <span class="kelly-note">¼ Kelly on GHS ${bankroll.toFixed(2)} bankroll</span>
         </div>`
      : bankroll === 0
      ? `<div class="kelly-stake kelly-no-bankroll">Set your bankroll to get a suggested stake</div>`
      : `<div class="kelly-stake kelly-no-edge">Kelly says skip — no positive edge</div>`;

    // ── Poisson markets block ──────────────────────────────────────
    let marketsHtml = "";
    if (markets) {
      const over25Pct   = Math.round(markets.over25_prob  * 100);
      const under25Pct  = Math.round(markets.under25_prob * 100);
      const bttsPct     = Math.round(markets.btts_prob    * 100);
      const noBttsPct   = Math.round(markets.no_btts_prob * 100);
      const ou25HasVal  = markets.over25_edge  >= 3;
      const bttsHasVal  = markets.btts_edge    >= 3;

      // Correct scores
      const csRows = (markets.correct_scores || []).map((cs, idx) =>
        `<div class="cs-row ${idx === 0 ? "cs-top" : ""}">
           <span class="cs-label">${escapeHtml(cs.label)}</span>
           <span class="cs-prob">${Math.round(cs.prob * 100)}%</span>
           <span class="cs-odd">fair @ ${cs.fair_odd}</span>
         </div>`
      ).join("");

      // λ display
      const lhStr = markets.lambda_home ? markets.lambda_home.toFixed(2) : "—";
      const laStr = markets.lambda_away ? markets.lambda_away.toFixed(2) : "—";

      marketsHtml = `
        <div class="markets-block">
          <div class="markets-title">Poisson Markets</div>

          <div class="market-pills">
            <div class="market-pill ${ou25HasVal ? "pill-value" : ""}">
              <span class="pill-label">Over 2.5</span>
              <span class="pill-prob">${over25Pct}%</span>
              ${ou25HasVal ? '<span class="pill-edge-tag">VALUE</span>' : ""}
            </div>
            <div class="market-pill">
              <span class="pill-label">Under 2.5</span>
              <span class="pill-prob">${under25Pct}%</span>
            </div>
            <div class="market-pill ${bttsHasVal ? "pill-value" : ""}">
              <span class="pill-label">BTTS Yes</span>
              <span class="pill-prob">${bttsPct}%</span>
              ${bttsHasVal ? '<span class="pill-edge-tag">VALUE</span>' : ""}
            </div>
            <div class="market-pill">
              <span class="pill-label">BTTS No</span>
              <span class="pill-prob">${noBttsPct}%</span>
            </div>
          </div>

          <div class="cs-block">
            <div class="cs-heading">Top Correct Scores</div>
            ${csRows}
          </div>

          <div class="lambda-row">
            <span>xGoals: <strong>${escapeHtml(p.home)}</strong> ${lhStr}</span>
            <span class="lambda-sep">vs</span>
            <span><strong>${escapeHtml(p.away)}</strong> ${laStr}</span>
          </div>
        </div>`;
    }

    const card = document.createElement("div");
    card.className = `rec-card ${p.value_bet ? "value" : ""}`;
    card.innerHTML = `
      ${p.value_bet
        ? `<span class="value-badge">VALUE BET</span>`
        : `<span class="rank">#${i + 1}</span>`}

      <div class="match-name">${escapeHtml(p.home)} vs ${escapeHtml(p.away)}</div>

      <div class="ai-pick-row">
        <div class="ai-pick-main">
          <span class="pick-label-sm">Best pick</span>
          <span class="pick-text">${escapeHtml(bp.label)}</span>
          <span class="pick-odd">@ ${bp.odd}</span>
        </div>
        <span class="confidence-badge ${tier.cls}">${tier.label} ${bp.confidence}%</span>
      </div>

      <div class="ai-reasoning">"${escapeHtml(bp.reasoning)}"</div>

      <div class="meta">
        <span>Edge: <strong class="${bp.edge_pct >= 3 ? "edge-positive" : "edge-neutral"}">${bp.edge_pct.toFixed(1)}%</strong></span>
        <span>Overround: <strong>${p.overround_pct.toFixed(1)}%</strong></span>
      </div>

      <div class="odds-bar-wrap">
        <div class="odds-bar-row">
          <span>1</span>
          <div class="odds-bar"><div class="odds-fill home-fill" style="width:${Math.round(p.fair_p1*100)}%"></div></div>
          <span>${Math.round(p.fair_p1*100)}%</span>
        </div>
        <div class="odds-bar-row">
          <span>X</span>
          <div class="odds-bar"><div class="odds-fill draw-fill" style="width:${Math.round(p.fair_pX*100)}%"></div></div>
          <span>${Math.round(p.fair_pX*100)}%</span>
        </div>
        <div class="odds-bar-row">
          <span>2</span>
          <div class="odds-bar"><div class="odds-fill away-fill" style="width:${Math.round(p.fair_p2*100)}%"></div></div>
          <span>${Math.round(p.fair_p2*100)}%</span>
        </div>
      </div>

      ${marketsHtml}

      <div class="safe-pick-row">
        Safe: <strong>${escapeHtml(sp.pick)}</strong> — ${escapeHtml(sp.label)}
        <span class="safe-odd">@ ~${sp.odd}</span>
        <span class="safe-prob">(${sp.confidence}% conf)</span>
      </div>

      ${stakeHtml}

      <div class="feedback-row"
           data-home="${escapeHtml(p.home)}"
           data-away="${escapeHtml(p.away)}"
           data-pick="${escapeHtml(bp.pick)}"
           data-odd="${escapeHtml(bp.odd)}"
           data-fixture-id="${p.fixture_id || ""}">
        <span class="feedback-label">Log result:</span>
        <button class="fb-btn win"  onclick="submitFeedback(this,'win')">Won</button>
        <button class="fb-btn loss" onclick="submitFeedback(this,'loss')">Lost</button>
      </div>
    `;
    container.appendChild(card);
  });

  // ── Accumulator card ─────────────────────────────────────────────
  if (acca.length >= 2) {
    const accaCard   = document.createElement("div");
    accaCard.className = "rec-card acca-card";
    const legsHtml   = acca.map(leg =>
      `<div class="acca-leg">
        <span class="acca-teams">${escapeHtml(leg.home)} vs ${escapeHtml(leg.away)}</span>
        <span class="acca-pick">→ <strong>${escapeHtml(leg.pick)}</strong> @ ${leg.odd}</span>
        <span class="acca-reason">${escapeHtml(leg.reasoning)}</span>
       </div>`
    ).join("");

    accaCard.innerHTML = `
      <span class="rank acca-rank">ACCA</span>
      <div class="match-name" style="padding-right:70px">${acca.length}-Leg Accumulator</div>
      <div class="acca-legs">${legsHtml}</div>
      <div class="acca-footer">
        <span>Combined odds: <strong>@ ${prediction.acca_combined_odd}</strong></span>
      </div>
      <div class="ai-reasoning">"${escapeHtml(prediction.acca_reasoning)}"</div>
      <div class="margin-note">Accumulators are high-risk. Only stake what you can afford to lose entirely.</div>
    `;
    container.appendChild(accaCard);
  }

  // ── RNG disclaimer ───────────────────────────────────────────────
  const disc     = document.createElement("div");
  disc.className = "rec-disclaimer";
  disc.innerHTML = `Instant Virtuals are RNG-based. AI + Poisson analysis is odds-value based only — not real match prediction. Gamble responsibly. 18+`;
  container.appendChild(disc);

  document.getElementById("recommendations").classList.remove("hidden");
  document.getElementById("recommendations").scrollIntoView({ behavior: "smooth" });
}

// ── Feedback submission ─────────────────────────────────────────
async function submitFeedback(btn, result) {
  const row  = btn.closest(".feedback-row");
  const item = {
    home:       row.dataset.home,
    away:       row.dataset.away,
    pick:       row.dataset.pick,
    odd:        row.dataset.odd,
    result:     result,
    fixture_id: row.dataset.fixtureId ? parseInt(row.dataset.fixtureId) : null,
  };

  // Optimistic UI update
  row.innerHTML = `<span class="feedback-saved">${result === "win" ? "Logged as Won" : "Logged as Lost"}</span>`;

  // Log to bankroll history too
  state.bets.unshift({
    id:     Date.now(),
    match:  `${item.home} vs ${item.away} (${item.pick})`,
    stake:  0,
    odds:   parseFloat(item.odd) || 0,
    result: result,
    profit: 0,
    date:   new Date().toISOString()
  });
  saveState();
  renderHistory();
  updateStats();

  // Send to backend
  try {
    await fetch(`${window.BACKEND_URL}/api/feedback`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify([item])
    });
  } catch (e) {
    console.warn("Feedback sync failed (offline?):", e.message);
  }
}

// ======================
// Real-time Feed (WebSocket events from VEPush)
// ======================

// Live feed entry store — kept in memory, shown in the UI
const liveFeed = [];
const MAX_FEED = 20;

function setupRealtimeFeed() {
  // WS connection status dot + badge
  const dot   = document.getElementById("ws-status");
  const badge = document.getElementById("ws-badge");

  function setConnected(on) {
    if (dot)   dot.className   = on ? "connected" : "";
    if (badge) {
      badge.textContent  = on ? "● Live" : "○ Offline";
      badge.className    = `ws-badge ${on ? "ws-badge-on" : "ws-badge-off"}`;
    }
    // Pulse the Live tab dot
    document.querySelectorAll(".live-dot").forEach(d =>
      d.classList.toggle("live-dot-on", on)
    );
  }

  window.addEventListener("ve:ws_connected",    () => setConnected(true));
  window.addEventListener("ve:ws_disconnected", () => setConnected(false));

  // ── prediction_ready → re-render recommendations if same session ──
  window.addEventListener("ve:prediction_ready", e => {
    const { session_id, predictions, summary, engine } = e.detail;

    // Add to live feed
    _addFeedEntry({
      type:    "prediction",
      time:    new Date().toLocaleTimeString(),
      label:   `${predictions.length} match${predictions.length !== 1 ? "es" : ""} analysed`,
      sub:     summary?.best_pick_label || "",
      engine:  engine || "",
      value:   summary?.value_bets > 0,
    });

    // If we got a real server push (not our own request echo), show the alert
    if (session_id === state.sessionId && lastPrediction) {
      // Already rendered by runOCR — skip re-render to avoid flicker
      return;
    }
  });

  // ── value_alert → prominent live card ───────────────────────────
  window.addEventListener("ve:value_alert", e => {
    const d = e.detail;
    _addFeedEntry({
      type:  "value",
      time:  new Date().toLocaleTimeString(),
      label: `${d.home} vs ${d.away}`,
      sub:   `${d.pick} @ ${d.odd} — +${parseFloat(d.edge_pct || 0).toFixed(1)}% edge`,
      value: true,
    });
  });

  // ── analysis_progress → update OCR progress bar if visible ──────
  window.addEventListener("ve:analysis_progress", e => {
    const { step, pct, detail } = e.detail;
    const bar  = document.getElementById("progressFill");
    const text = document.getElementById("statusText");
    const pctEl = document.getElementById("statusPercent");
    if (bar)   bar.style.width   = pct + "%";
    if (text)  text.textContent  = step   || "Processing...";
    if (pctEl) pctEl.textContent = pct + "%";
  });

  // ── push_received (SW forwarded) → toast already shown by push.js ─
  window.addEventListener("ve:push_received", e => {
    const d = e.detail;
    _addFeedEntry({
      type:  "push",
      time:  new Date().toLocaleTimeString(),
      label: d.title || "Notification",
      sub:   d.body  || "",
      value: false,
    });
  });

  // ── Scheduler broadcasts ─────────────────────────────────────────
  // accuracy_update and pending_alert arrive via the generic ws_message
  // path — re-dispatch so setupHistory listeners can catch them
  window.addEventListener("ve:ws_message", e => {
    const msg = e.detail;
    if (msg.type === "accuracy_update") {
      window.dispatchEvent(new CustomEvent("ve:accuracy_update", { detail: msg }));
      _addFeedEntry({
        type:  "info",
        time:  new Date().toLocaleTimeString(),
        label: `Accuracy updated — ${msg.accuracy?.win_rate ?? 0}% win rate`,
        sub:   `${msg.accuracy?.wins ?? 0}W / ${msg.accuracy?.losses ?? 0}L`,
        value: false,
      });
    }
    if (msg.type === "pending_alert") {
      window.dispatchEvent(new CustomEvent("ve:pending_alert", { detail: msg }));
      _addFeedEntry({
        type:  "info",
        time:  new Date().toLocaleTimeString(),
        label: `📋 ${msg.count} prediction${msg.count !== 1 ? "s" : ""} need results`,
        sub:   "Open History tab → Results Checker",
        value: false,
      });
    }
  });

  // ── Enable push button ───────────────────────────────────────────
  const enableBtn  = document.getElementById("enablePushBtn");
  const pushStatus = document.getElementById("pushStatus");

  if (enableBtn) {
    // Show current permission state
    const perm = "Notification" in window ? Notification.permission : "unsupported";
    if (perm === "granted") {
      enableBtn.textContent = "Notifications enabled";
      enableBtn.disabled    = true;
    } else if (perm === "denied") {
      enableBtn.textContent = "Notifications blocked — allow in browser settings";
      enableBtn.disabled    = true;
    }

    enableBtn.addEventListener("click", async () => {
      if (!window.VEPush) return;
      enableBtn.disabled    = true;
      enableBtn.textContent = "Requesting permission…";
      const result = await VEPush.requestPermission();
      if (result === "granted") {
        enableBtn.textContent = "Notifications enabled";
        if (pushStatus) pushStatus.textContent = "You'll get alerts when value bets are found.";
      } else if (result === "denied") {
        enableBtn.textContent = "Permission denied";
        enableBtn.disabled    = false;
        if (pushStatus) pushStatus.textContent = "Allow notifications in your browser settings.";
      } else {
        enableBtn.disabled    = false;
        enableBtn.textContent = "Enable Push Notifications";
      }
    });
  }

  renderLiveFeed();
}

function _addFeedEntry(entry) {
  liveFeed.unshift(entry);
  if (liveFeed.length > MAX_FEED) liveFeed.pop();
  renderLiveFeed();
}

function renderLiveFeed() {
  const container = document.getElementById("liveFeedList");
  if (!container) return;

  if (liveFeed.length === 0) {
    container.innerHTML = `<p class="empty">Waiting for live events…</p>`;
    return;
  }

  container.innerHTML = liveFeed.map(e => `
    <div class="feed-item ${e.value ? "feed-value" : ""}">
      <div class="feed-left">
        <div class="feed-label">${escapeHtml(e.label)}</div>
        ${e.sub ? `<div class="feed-sub">${escapeHtml(e.sub)}</div>` : ""}
      </div>
      <div class="feed-right">
        <span class="feed-time">${escapeHtml(e.time)}</span>
        ${e.value ? `<span class="feed-badge">VALUE</span>` : ""}
      </div>
    </div>
  `).join("");
}

// ======================
// SportyBet Ghana Instant Virtuals – specialized OCR parser
// Handles short team codes (BRE, ARS, BMU, RMA...) and full names
// ======================
function parseOCRText(text) {
  const raw = text.replace(/\r/g, "\n");
  const lines = raw.split("\n").map(l => l.trim()).filter(l => l.length > 1);

  const matches = [];
  const seen = new Set();

  // Common patterns on SportyBet mobile:
  // "BRE vs ARS"   "TOT vs SUN"   "ESP vs JPN"   "Man Utd vs Liverpool"
  // Odds usually appear as groups of 3 numbers: 1.85  3.40  4.10

  const vsPatterns = [
    // 2-5 letter codes: BRE vs ARS
    /\b([A-Z]{2,5})\s*(?:vs|v\.?|VS|V)\s*([A-Z]{2,5})\b/g,
    // Mixed / longer names
    /([A-Za-z][A-Za-z0-9 .]{1,18}?)\s+(?:vs|v\.?|VS)\s+([A-Za-z][A-Za-z0-9 .]{1,18})/gi
  ];

  // Collect all "TEAM vs TEAM" occurrences with their line index
  const foundPairs = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    for (const pattern of vsPatterns) {
      pattern.lastIndex = 0;
      let m;
      while ((m = pattern.exec(line)) !== null) {
        let home = cleanTeam(m[1]);
        let away = cleanTeam(m[2]);
        if (home.length < 2 || away.length < 2) continue;
        if (home === away) continue;

        const key = home + "|" + away;
        if (seen.has(key)) continue;
        seen.add(key);

        foundPairs.push({ home, away, lineIdx: i });
      }
    }
  }

  // Extract all plausible odds from the whole text (decimal odds 1.01 – 25)
  const allOdds = [];
  const oddRegex = /\b(\d{1,2}\.\d{1,2})\b/g;
  let om;
  while ((om = oddRegex.exec(raw)) !== null) {
    const v = parseFloat(om[1]);
    if (v >= 1.01 && v <= 25.0) {
      allOdds.push(v);
    }
  }

  // Strategy A: if we have roughly 3 × number_of_matches odds, assign sequentially
  if (foundPairs.length > 0 && allOdds.length >= foundPairs.length * 2) {
    // Try to group odds in threes
    for (let i = 0; i < foundPairs.length; i++) {
      const base = i * 3;
      const o1 = allOdds[base];
      const oX = allOdds[base + 1];
      const o2 = allOdds[base + 2];

      matches.push({
        home: foundPairs[i].home,
        away: foundPairs[i].away,
        odd1: o1 ? o1.toFixed(2) : "",
        oddX: oX ? oX.toFixed(2) : "",
        odd2: o2 ? o2.toFixed(2) : ""
      });
    }
  } else {
    // Strategy B: for each pair look in nearby lines for 3 consecutive odds
    for (const pair of foundPairs) {
      let oddsFound = ["", "", ""];
      const searchStart = Math.max(0, pair.lineIdx - 1);
      const searchEnd = Math.min(lines.length - 1, pair.lineIdx + 3);
      let localText = "";
      for (let j = searchStart; j <= searchEnd; j++) localText += " " + lines[j];

      const localOdds = [];
      const localRegex = /\b(\d{1,2}\.\d{1,2})\b/g;
      let lm;
      while ((lm = localRegex.exec(localText)) !== null) {
        const v = parseFloat(lm[1]);
        if (v >= 1.01 && v <= 25) localOdds.push(v);
      }

      if (localOdds.length >= 3) {
        oddsFound = [
          localOdds[0].toFixed(2),
          localOdds[1].toFixed(2),
          localOdds[2].toFixed(2)
        ];
      }

      matches.push({
        home: pair.home,
        away: pair.away,
        odd1: oddsFound[0],
        oddX: oddsFound[1],
        odd2: oddsFound[2]
      });
    }
  }

  // Fallback: if still empty, try one more aggressive pass on whole text
  if (matches.length === 0) {
    const aggressive = raw.match(/\b([A-Z]{2,5})\s*(?:vs|v)\s*([A-Z]{2,5})\b/gi) || [];
    aggressive.forEach(str => {
      const parts = str.split(/\s*(?:vs|v)\s*/i);
      if (parts.length === 2) {
        matches.push({
          home: cleanTeam(parts[0]),
          away: cleanTeam(parts[1]),
          odd1: "",
          oddX: "",
          odd2: ""
        });
      }
    });
  }

  return matches.slice(0, 12);
}

function cleanTeam(name) {
  return name
    .replace(/[^a-zA-Z0-9\s]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toUpperCase()
    .slice(0, 18);
}

// ---------- Matches Editor ----------
function renderMatchesEditor() {
  const container = document.getElementById("matchesList");
  container.innerHTML = "";

  state.matches.forEach((m, idx) => {
    const row = document.createElement("div");
    row.className = "match-row";
    row.innerHTML = `
      <div class="teams">
        <input type="text" placeholder="Home team" value="${escapeHtml(m.home)}" data-idx="${idx}" data-field="home" />
        <span style="align-self:center;color:var(--text-muted)">vs</span>
        <input type="text" placeholder="Away team" value="${escapeHtml(m.away)}" data-idx="${idx}" data-field="away" />
        <button class="remove-btn" data-idx="${idx}" title="Remove">×</button>
      </div>
      <div class="odds-row">
        <label>1 (Home)
          <input type="number" step="0.01" min="1" value="${m.odd1}" data-idx="${idx}" data-field="odd1" placeholder="1.80" />
        </label>
        <label>X (Draw)
          <input type="number" step="0.01" min="1" value="${m.oddX}" data-idx="${idx}" data-field="oddX" placeholder="3.40" />
        </label>
        <label>2 (Away)
          <input type="number" step="0.01" min="1" value="${m.odd2}" data-idx="${idx}" data-field="odd2" placeholder="4.20" />
        </label>
      </div>
    `;
    container.appendChild(row);
  });

  // Bind events
  container.querySelectorAll("input").forEach(input => {
    input.addEventListener("input", e => {
      const idx = +e.target.dataset.idx;
      const field = e.target.dataset.field;
      state.matches[idx][field] = e.target.value;
    });
  });

  container.querySelectorAll(".remove-btn").forEach(btn => {
    btn.addEventListener("click", e => {
      const idx = +e.target.dataset.idx;
      state.matches.splice(idx, 1);
      renderMatchesEditor();
    });
  });

  document.getElementById("addMatchBtn").onclick = () => {
    state.matches.push({ home: "", away: "", odd1: "", oddX: "", odd2: "" });
    renderMatchesEditor();
  };

  document.getElementById("rankBtn").onclick = rankBets;
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------- Ranking Logic ----------
// Value bet threshold: if fair odds > bookmaker odds, there's potential value
const VALUE_THRESHOLD = 0.03; // 3% edge minimum to flag as value
// Kelly fraction – use fractional Kelly (0.25) to be conservative
const KELLY_FRACTION = 0.25;

function kellyStake(prob, odd, bankroll) {
  // Kelly formula: f = (b*p - q) / b  where b = odd-1, p = prob, q = 1-p
  const b = odd - 1;
  const q = 1 - prob;
  const f = (b * prob - q) / b;
  if (f <= 0) return 0; // no edge, don't bet
  const kelly = f * KELLY_FRACTION; // fractional Kelly
  return Math.max(0, Math.min(kelly * bankroll, bankroll * 0.15)); // cap at 15% of bankroll
}

function rankBets() {
  const valid = state.matches.filter(m => {
    const o1 = parseFloat(m.odd1);
    const oX = parseFloat(m.oddX);
    const o2 = parseFloat(m.odd2);
    return m.home && m.away && o1 > 1 && oX > 1 && o2 > 1;
  });

  if (valid.length === 0) {
    alert("Please enter at least one complete match with all three odds (1, X, 2).");
    return;
  }

  const bankroll = state.bankroll || 0;

  const ranked = valid.map(m => {
    const o1 = parseFloat(m.odd1);
    const oX = parseFloat(m.oddX);
    const o2 = parseFloat(m.odd2);

    // Raw implied probabilities (include bookmaker margin)
    const raw1 = 1 / o1;
    const rawX = 1 / oX;
    const raw2 = 1 / o2;
    const overround = raw1 + rawX + raw2; // e.g. 1.08 = 8% margin

    // Fair (true) probabilities after removing overround
    const p1 = raw1 / overround;
    const pX = rawX / overround;
    const p2 = raw2 / overround;

    // Bookmaker margin per match
    const margin = ((overround - 1) / overround * 100).toFixed(1);

    // Value = fair probability vs what the odds imply
    // A value bet exists when fair prob > implied prob by threshold
    const value1 = p1 - raw1;   // positive = value for home
    const valueX = pX - rawX;
    const value2 = p2 - raw2;

    // Build outcomes with value flags
    const outcomes = [
      { pick: "1", label: `HOME (${m.home})`, prob: p1, odd: o1, value: value1 },
      { pick: "X", label: "DRAW",              prob: pX, odd: oX, value: valueX },
      { pick: "2", label: `AWAY (${m.away})`,  prob: p2, odd: o2, value: value2 },
    ];

    // Best value outcome (highest edge first, then highest prob)
    const valueBets = outcomes.filter(o => o.value >= VALUE_THRESHOLD);
    valueBets.sort((a, b) => b.value - a.value);

    // Best probability outcome (safest pick)
    const byProb = [...outcomes].sort((a, b) => b.prob - a.prob);
    const bestProb = byProb[0];

    // Primary pick: value bet if available, otherwise safest
    const primaryPick = valueBets.length > 0 ? valueBets[0] : bestProb;
    const isValueBet = valueBets.length > 0;

    // Kelly suggested stake
    const suggestedStake = bankroll > 0
      ? kellyStake(primaryPick.prob, primaryPick.odd, bankroll)
      : null;

    // Double Chance (safer combo)
    const dc = [
      { combo: "1X", label: "Home or Draw", prob: p1 + pX },
      { combo: "X2", label: "Draw or Away", prob: pX + p2 },
      { combo: "12", label: "Home or Away", prob: p1 + p2 },
    ].sort((a, b) => b.prob - a.prob)[0];
    const dcOddApprox = (1 / (dc.prob / overround) * 0.92).toFixed(2);

    // Sort score: value bets ranked higher, then by edge size
    const score = (isValueBet ? 100 : 0) + primaryPick.value * 200 + primaryPick.prob * 10;

    return {
      ...m,
      p1, pX, p2,
      overround, margin,
      outcomes,
      primaryPick,
      isValueBet,
      valueBets,
      suggestedStake,
      dc,
      dcOddApprox,
      score
    };
  });

  // Sort: value bets first, then by edge
  ranked.sort((a, b) => b.score - a.score);

  renderRecommendations(ranked);
}

function renderRecommendations(ranked) {
  const container = document.getElementById("recsContainer");
  container.innerHTML = "";

  const bankroll = state.bankroll || 0;
  const hasValueBets = ranked.some(m => m.isValueBet);

  // --- Summary notice ---
  const notice = document.createElement("div");
  notice.className = "rec-notice";
  if (hasValueBets) {
    notice.innerHTML = `<strong>Value bets detected</strong> — odds appear generous vs fair probability. Ranked by edge size.`;
    notice.classList.add("value-notice");
  } else {
    notice.innerHTML = `No clear value bets found. Showing <strong>best odds probability</strong> only. These are RNG virtuals — bet small.`;
    notice.classList.add("info-notice");
  }
  container.appendChild(notice);

  // --- Top picks (max 4) ---
  const top = ranked.slice(0, 4);

  top.forEach((m, i) => {
    const pick = m.primaryPick;
    const probPct = Math.round(pick.prob * 100);
    const edgePct = (pick.value * 100).toFixed(1);
    const isValue = m.isValueBet;

    // Stake line
    let stakeLine = "";
    if (bankroll > 0 && m.suggestedStake > 0.01) {
      stakeLine = `
        <div class="kelly-stake">
          Suggested stake: <strong>GHS ${m.suggestedStake.toFixed(2)}</strong>
          <span class="kelly-note">(¼ Kelly of GHS ${bankroll.toFixed(2)} bankroll)</span>
        </div>`;
    } else if (bankroll === 0) {
      stakeLine = `<div class="kelly-stake kelly-no-bankroll">Set your bankroll to get a suggested stake</div>`;
    } else {
      stakeLine = `<div class="kelly-stake kelly-no-edge">No edge — Kelly says skip this one</div>`;
    }

    const card = document.createElement("div");
    card.className = `rec-card${isValue ? " value" : ""}`;
    card.innerHTML = `
      ${isValue ? `<span class="value-badge">VALUE BET</span>` : `<span class="rank">#${i + 1}</span>`}
      <div class="match-name">${escapeHtml(m.home)} vs ${escapeHtml(m.away)}</div>
      <div class="pick-label">Best pick: <span class="pick-text">${escapeHtml(pick.label)}</span></div>
      <div class="meta">
        <span>Odds: <strong>${pick.odd}</strong></span>
        <span>Fair prob: <strong>${probPct}%</strong></span>
        <span class="${isValue ? "edge-positive" : "edge-neutral"}">Edge: <strong>${edgePct}%</strong></span>
      </div>
      <div class="margin-note">Bookmaker margin on this match: ${m.margin}%</div>
      <div class="odds-bar-wrap">
        <div class="odds-bar-row">
          <span>1</span>
          <div class="odds-bar"><div class="odds-fill home-fill" style="width:${Math.round(m.p1*100)}%"></div></div>
          <span>${Math.round(m.p1*100)}%</span>
        </div>
        <div class="odds-bar-row">
          <span>X</span>
          <div class="odds-bar"><div class="odds-fill draw-fill" style="width:${Math.round(m.pX*100)}%"></div></div>
          <span>${Math.round(m.pX*100)}%</span>
        </div>
        <div class="odds-bar-row">
          <span>2</span>
          <div class="odds-bar"><div class="odds-fill away-fill" style="width:${Math.round(m.p2*100)}%"></div></div>
          <span>${Math.round(m.p2*100)}%</span>
        </div>
      </div>
      ${stakeLine}
    `;
    container.appendChild(card);
  });

  // --- Safest Double Chance ---
  const safest = [...ranked].sort((a, b) => b.dc.prob - a.dc.prob)[0];
  if (safest) {
    const dcProbPct = Math.round(safest.dc.prob * 100);
    const card = document.createElement("div");
    card.className = "rec-card safe";
    card.innerHTML = `
      <span class="rank safe-rank">SAFE</span>
      <div class="match-name">${escapeHtml(safest.home)} vs ${escapeHtml(safest.away)}</div>
      <div class="pick-label">Double Chance: <span class="pick-text gold">${safest.dc.combo} — ${safest.dc.label}</span></div>
      <div class="meta">
        <span>Est. Odds: <strong>~${safest.dcOddApprox}</strong></span>
        <span>Coverage: <strong>${dcProbPct}%</strong></span>
      </div>
      <div class="margin-note">Lower payout, higher coverage. Good for small stakes.</div>
      <div class="confidence-bar">
        <div class="confidence-fill" style="width:${dcProbPct}%;background:var(--gold)"></div>
      </div>
    `;
    container.appendChild(card);
  }

  // --- RNG disclaimer ---
  const disclaimer = document.createElement("div");
  disclaimer.className = "rec-disclaimer";
  disclaimer.innerHTML = `Instant Virtuals are RNG-based. No system guarantees a win. This tool shows odds value only — not real predictions. Always gamble within your means.`;
  container.appendChild(disclaimer);

  document.getElementById("recommendations").classList.remove("hidden");
  document.getElementById("recommendations").scrollIntoView({ behavior: "smooth" });
}

// ---------- Bankroll ----------
function setupBankroll() {
  document.getElementById("setBankrollBtn").addEventListener("click", () => {
    const val = parseFloat(document.getElementById("setBankrollInput").value);
    if (isNaN(val) || val < 0) {
      alert("Enter a valid amount");
      return;
    }
    state.bankroll = val;
    saveState();
    updateBankrollUI();
    document.getElementById("setBankrollInput").value = "";
  });

  document.querySelectorAll(".result-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".result-btn").forEach(b => b.classList.remove("selected"));
      btn.classList.add("selected");
      selectedResult = btn.dataset.result;
    });
  });

  document.getElementById("logBetBtn").addEventListener("click", logBet);
}

function logBet() {
  const match = document.getElementById("betMatch").value.trim();
  const stake = parseFloat(document.getElementById("betStake").value);
  const odds = parseFloat(document.getElementById("betOdds").value);

  if (!match || isNaN(stake) || stake <= 0 || isNaN(odds) || odds < 1 || !selectedResult) {
    alert("Please fill Match, Stake, Odds and select Won/Lost");
    return;
  }

  let profit = 0;
  if (selectedResult === "win") {
    profit = stake * (odds - 1);
    state.bankroll += profit;
  } else {
    profit = -stake;
    state.bankroll -= stake;
  }

  state.bets.unshift({
    id: Date.now(),
    match,
    stake,
    odds,
    result: selectedResult,
    profit,
    date: new Date().toISOString()
  });

  // Keep only last 100
  if (state.bets.length > 100) state.bets = state.bets.slice(0, 100);

  saveState();
  renderAll();

  // Reset form
  document.getElementById("betMatch").value = "";
  document.getElementById("betStake").value = "";
  document.getElementById("betOdds").value = "";
  selectedResult = null;
  document.querySelectorAll(".result-btn").forEach(b => b.classList.remove("selected"));

  alert(selectedResult === "win" ? `Nice! +GHS ${profit.toFixed(2)}` : `Lost GHS ${stake.toFixed(2)}`);
}

function updateBankrollUI() {
  const formatted = state.bankroll.toFixed(2);
  document.getElementById("headerBankroll").textContent = `GHS ${formatted}`;
  document.getElementById("currentBankroll").textContent = formatted;

  // Color the big number
  const el = document.getElementById("currentBankroll");
  el.style.color = state.bankroll >= 0 ? "var(--green)" : "var(--red)";
}

function updateStats() {
  const bets = state.bets;
  const total = bets.length;
  const wins = bets.filter(b => b.result === "win").length;
  const winRate = total ? Math.round((wins / total) * 100) : 0;
  const profit = bets.reduce((sum, b) => sum + b.profit, 0);
  const avgOdds = total ? (bets.reduce((s, b) => s + b.odds, 0) / total).toFixed(2) : "-";

  document.getElementById("winRate").textContent = winRate + "%";
  document.getElementById("totalBets").textContent = total;
  document.getElementById("profitLoss").textContent = (profit >= 0 ? "+" : "") + "GHS " + profit.toFixed(0);
  document.getElementById("profitLoss").style.color = profit >= 0 ? "var(--green)" : "var(--red)";
  document.getElementById("avgOdds").textContent = avgOdds;
}

// ---------- History + Results Checker ----------
function setupHistory() {
  document.getElementById("clearHistoryBtn").addEventListener("click", () => {
    if (confirm("Clear all bet history? This cannot be undone.")) {
      state.bets = [];
      saveState();
      renderAll();
    }
  });

  // Refresh pending predictions when the History tab is activated
  document.querySelectorAll('.tab[data-tab="history"]').forEach(btn => {
    btn.addEventListener("click", () => {
      loadPendingPredictions();
      loadAccuracy();
    });
  });

  // Manual refresh button
  const refreshBtn = document.getElementById("refreshPendingBtn");
  if (refreshBtn) refreshBtn.addEventListener("click", () => {
    loadPendingPredictions();
    loadAccuracy();
  });

  // WebSocket: accuracy updated by scheduler broadcast
  window.addEventListener("ve:ws_message", e => {
    const msg = e.detail;
    if (msg.type === "accuracy_update") renderAccuracyMeter(msg.accuracy);
  });
  window.addEventListener("ve:accuracy_update", e => {
    renderAccuracyMeter(e.detail.accuracy);
  });

  // WebSocket: pending alert from scheduler
  window.addEventListener("ve:pending_alert", e => {
    const badge = document.querySelector('.tab[data-tab="history"]');
    if (badge && e.detail.count > 0) {
      badge.dataset.badge = e.detail.count;
    }
    // Reload pending list if History tab is active
    const histTab = document.getElementById("history");
    if (histTab && histTab.classList.contains("active")) {
      loadPendingPredictions();
    }
  });

  // Initial load
  loadPendingPredictions();
  loadAccuracy();
}

// ── Accuracy meter ───────────────────────────────────────────────
async function loadAccuracy() {
  try {
    const res = await fetch(`${window.BACKEND_URL}/api/accuracy`);
    if (!res.ok) return;
    const acc = await res.json();
    renderAccuracyMeter(acc);
  } catch (e) {
    console.warn("Accuracy fetch failed:", e.message);
  }
}

function renderAccuracyMeter(acc) {
  if (!acc || acc.total === 0) return;

  const pct     = acc.win_rate  != null ? acc.win_rate  : 0;
  const total   = acc.total     != null ? acc.total     : 0;
  const wins    = acc.wins      != null ? acc.wins      : 0;
  const losses  = acc.losses    != null ? acc.losses    : 0;
  const profit  = acc.profit    != null ? acc.profit    : 0;

  // Ring (circumference of r=18 circle ≈ 113.1)
  const circ    = 113.1;
  const filled  = (pct / 100) * circ;
  const ringEl  = document.getElementById("ringFill");
  if (ringEl) {
    ringEl.style.strokeDasharray = `${filled.toFixed(1)} ${circ}`;
    ringEl.style.stroke = pct >= 60 ? "var(--green)" : pct >= 40 ? "var(--gold)" : "var(--red)";
  }

  const setText = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  setText("accuracyPct", pct.toFixed(0) + "%");
  setText("accTotal",    total);
  setText("accWins",     wins);
  setText("accLosses",   losses);
  setText("accProfit",   (profit >= 0 ? "+" : "") + "GHS " + profit.toFixed(0));

  // Bar
  const bar = document.getElementById("accuracyBar");
  if (bar) {
    bar.style.width      = pct + "%";
    bar.style.background = pct >= 60 ? "var(--green)" : pct >= 40 ? "var(--gold)" : "var(--red)";
  }

  const profEl = document.getElementById("accProfit");
  if (profEl) profEl.style.color = profit >= 0 ? "var(--green)" : "var(--red)";

  // Value bet sub-note
  const vbNote = document.getElementById("valueBetAccuracy");
  if (vbNote && acc.total_value_bets > 0) {
    vbNote.textContent  = `Value bets: ${acc.value_win_rate ?? 0}% win rate on ${acc.total_value_bets} picks`;
    vbNote.classList.remove("hidden");
  }
}

// ── Pending predictions list ─────────────────────────────────────
async function loadPendingPredictions() {
  const list = document.getElementById("pendingList");
  if (!list) return;
  list.innerHTML = `<p class="empty pending-loading">Loading pending predictions…</p>`;

  try {
    const res = await fetch(`${window.BACKEND_URL}/api/predictions/pending`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const { pending, accuracy } = await res.json();

    if (accuracy) renderAccuracyMeter(accuracy);
    renderPendingList(pending);
  } catch (e) {
    console.warn("Pending fetch failed:", e.message);
    list.innerHTML = `<p class="empty">Backend unavailable — log results manually below.</p>`;
  }
}

function renderPendingList(pending) {
  const list = document.getElementById("pendingList");
  if (!list) return;

  if (!pending || pending.length === 0) {
    list.innerHTML = `<p class="empty">All predictions have results logged.</p>`;
    return;
  }

  list.innerHTML = pending.map(p => {
    const age     = _timeSince(p.predicted_at);
    const isValue = p.value_bet ? `<span class="pending-value-tag">VALUE</span>` : "";
    const mktTag  = p.market !== "1X2"
      ? `<span class="pending-mkt-tag">${escapeHtml(p.market)}</span>` : "";

    return `
      <div class="pending-item" id="pending-${p.prediction_id}">
        <div class="pending-top">
          <span class="pending-match">${escapeHtml(p.home)} vs ${escapeHtml(p.away)}</span>
          ${isValue}${mktTag}
        </div>
        <div class="pending-pick">
          Pick: <strong>${escapeHtml(p.pick_label || p.pick)}</strong>
          @ <strong>${p.pick_odd}</strong>
          <span class="pending-conf">${p.confidence}% conf</span>
        </div>
        <div class="pending-reasoning">"${escapeHtml(p.reasoning || "")}"</div>
        <div class="pending-meta">${age} ago · ${escapeHtml(p.market || "1X2")}</div>
        <div class="pending-actions">
          <button class="resolve-btn win"
                  onclick="resolvePrediction(${p.prediction_id}, 'win', this)">
            Won
          </button>
          <button class="resolve-btn loss"
                  onclick="resolvePrediction(${p.prediction_id}, 'loss', this)">
            Lost
          </button>
        </div>
      </div>
    `;
  }).join("");
}

async function resolvePrediction(predictionId, result, btn) {
  // Optimistic UI
  const row     = document.getElementById(`pending-${predictionId}`);
  const actions = row?.querySelector(".pending-actions");
  if (actions) {
    actions.innerHTML = `<span class="resolve-saved">
      ${result === "win" ? "Logged as Won" : "Logged as Lost"}
    </span>`;
  }

  try {
    const res = await fetch(`${window.BACKEND_URL}/api/predictions/${predictionId}/resolve`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ result, stake: 0 }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const { accuracy } = await res.json();

    // Animate row out
    if (row) {
      row.style.opacity   = "0";
      row.style.transform = "translateX(40px)";
      setTimeout(() => row.remove(), 350);
    }

    // Update accuracy meter with server response
    if (accuracy) renderAccuracyMeter(accuracy);

    // Add to local bet history for the Bankroll tab
    const matchLabel = `${predictionId}`;  // placeholder — real label lives on server
    state.bets.unshift({
      id:     Date.now(),
      match:  `Prediction #${predictionId}`,
      stake:  0,
      odds:   0,
      result: result,
      profit: 0,
      date:   new Date().toISOString(),
    });
    saveState();
    renderHistory();
    updateStats();

  } catch (e) {
    console.error("Resolve failed:", e.message);
    // Restore buttons on failure
    if (actions) {
      actions.innerHTML = `
        <span class="resolve-error">Failed — try again</span>
        <button class="resolve-btn win"  onclick="resolvePrediction(${predictionId},'win',this)">Won</button>
        <button class="resolve-btn loss" onclick="resolvePrediction(${predictionId},'loss',this)">Lost</button>
      `;
    }
  }
}

function _timeSince(isoStr) {
  if (!isoStr) return "unknown";
  const diff = Math.floor((Date.now() - new Date(isoStr + "Z")) / 60000);
  if (diff <  1)  return "just now";
  if (diff <  60) return `${diff}m`;
  if (diff < 1440) return `${Math.floor(diff / 60)}h`;
  return `${Math.floor(diff / 1440)}d`;
}

function renderHistory() {
  const list = document.getElementById("historyList");
  if (!list) return;
  if (state.bets.length === 0) {
    list.innerHTML = `<p class="empty">No bets logged yet.</p>`;
    return;
  }
  list.innerHTML = state.bets.map(b => {
    const date      = new Date(b.date).toLocaleDateString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
    const profitStr = (b.profit >= 0 ? "+" : "") + "GHS " + b.profit.toFixed(2);
    return `
      <div class="history-item">
        <div class="left">
          <div class="match">${escapeHtml(b.match)}</div>
          <div class="details">${date} • Stake ${b.stake} @ ${b.odds}</div>
        </div>
        <div class="result ${b.result}">${profitStr}</div>
      </div>
    `;
  }).join("");
}
