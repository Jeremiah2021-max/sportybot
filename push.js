/**
 * push.js – Web Push + WebSocket client for Virtual Edge
 * ========================================================
 * Responsibilities:
 *   1. Register service worker (sw.js)
 *   2. Request notification permission from user
 *   3. Subscribe to Web Push via VAPID public key from backend
 *   4. Send subscription to /api/push/subscribe
 *   5. Open and maintain WebSocket connection to /ws/{session_id}
 *   6. Dispatch custom DOM events for the rest of app.js to react to
 *
 * Public API (called by app.js):
 *   VEPush.init(sessionId)       – call once on page load
 *   VEPush.requestPermission()   – call after user interaction
 *   VEPush.isConnected()         – WebSocket status
 */

const BACKEND_WS  = (() => {
  const stored = localStorage.getItem("ve_backend") || "http://localhost:8000";
  return stored.replace(/^http/, "ws");  // http→ws, https→wss
})();

// BACKEND_URL is defined in app.js, use it from global scope

const VEPush = (() => {
  let _ws         = null;
  let _sessionId  = null;
  let _reconnectN = 0;
  let _swReg      = null;

  // ── Service worker registration ─────────────────────────────────
  async function _registerSW() {
    if (!("serviceWorker" in navigator)) return null;
    try {
      const reg = await navigator.serviceWorker.register("./sw.js", { scope: "./" });
      console.log("[VEPush] Service worker registered:", reg.scope);
      _swReg = reg;
      return reg;
    } catch (e) {
      console.warn("[VEPush] SW registration failed:", e);
      return null;
    }
  }

  // ── VAPID key helper ─────────────────────────────────────────────
  function _urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - base64String.length % 4) % 4);
    const base64  = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw     = atob(base64);
    return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
  }

  // ── Subscribe to Web Push ────────────────────────────────────────
  async function _subscribePush(reg) {
    try {
      // Get VAPID public key from backend
      const res = await fetch(`${window.BACKEND_URL}/api/push/vapid-public-key`);
      if (!res.ok) {
        console.warn("[VEPush] No VAPID key on server yet — push disabled");
        return;
      }
      const { public_key } = await res.json();

      // Check if already subscribed
      let sub = await reg.pushManager.getSubscription();
      if (!sub) {
        sub = await reg.pushManager.subscribe({
          userVisibleOnly:      true,
          applicationServerKey: _urlBase64ToUint8Array(public_key),
        });
      }

      // Send subscription to backend
      await fetch(`${window.BACKEND_URL}/api/push/subscribe`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          endpoint:   sub.endpoint,
          keys: {
            p256dh: btoa(String.fromCharCode(...new Uint8Array(sub.getKey("p256dh")))),
            auth:   btoa(String.fromCharCode(...new Uint8Array(sub.getKey("auth")))),
          },
          session_id:  _sessionId,
          user_agent:  navigator.userAgent.slice(0, 80),
        }),
      });
      console.log("[VEPush] Push subscription saved");
    } catch (e) {
      console.warn("[VEPush] Push subscribe failed:", e);
    }
  }

  // ── Permission request ────────────────────────────────────────────
  async function requestPermission() {
    if (!("Notification" in window)) {
      console.warn("[VEPush] Notifications not supported");
      return "denied";
    }
    if (Notification.permission === "granted") {
      if (_swReg) await _subscribePush(_swReg);
      return "granted";
    }
    if (Notification.permission === "denied") return "denied";

    const result = await Notification.requestPermission();
    if (result === "granted" && _swReg) {
      await _subscribePush(_swReg);
    }
    return result;
  }

  // ── WebSocket ────────────────────────────────────────────────────
  function _connectWS() {
    if (!_sessionId) return;
    const url = `${BACKEND_WS}/ws/${_sessionId}`;

    try {
      _ws = new WebSocket(url);
    } catch (e) {
      console.warn("[VEPush] WS connect error:", e);
      _scheduleReconnect();
      return;
    }

    _ws.onopen = () => {
      console.log("[VEPush] WebSocket connected");
      _reconnectN = 0;
      _dispatch("ws_connected", {});
    };

    _ws.onmessage = event => {
      try {
        const msg = JSON.parse(event.data);
        _handleServerMsg(msg);
      } catch (e) {
        console.warn("[VEPush] Bad WS message:", event.data);
      }
    };

    _ws.onerror = e => console.warn("[VEPush] WS error:", e);

    _ws.onclose = () => {
      console.log("[VEPush] WS closed");
      _dispatch("ws_disconnected", {});
      _scheduleReconnect();
    };

    // Client-side keepalive (ping every 20s)
    const pingTimer = setInterval(() => {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(JSON.stringify({ type: "ping" }));
      } else {
        clearInterval(pingTimer);
      }
    }, 20000);
  }

  function _scheduleReconnect() {
    const delay = Math.min(1000 * 2 ** _reconnectN, 30000); // exp backoff, max 30s
    _reconnectN++;
    console.log(`[VEPush] Reconnecting in ${delay}ms (attempt ${_reconnectN})`);
    setTimeout(_connectWS, delay);
  }

  // ── Handle incoming server messages ─────────────────────────────
  function _handleServerMsg(msg) {
    switch (msg.type) {
      case "connected":
        console.log("[VEPush] Server ack:", msg.message);
        break;

      case "prediction_ready":
        // Let app.js render the new predictions
        _dispatch("prediction_ready", msg);
        _showInAppBanner(`Analysis complete — ${msg.predictions?.length || 0} matches`);
        break;

      case "value_alert":
        _dispatch("value_alert", msg);
        _showInAppBanner(`${msg.title}: ${msg.body}`, "value");
        break;

      case "analysis_progress":
        _dispatch("analysis_progress", msg);
        break;

      case "ping":
        if (_ws && _ws.readyState === WebSocket.OPEN) {
          _ws.send(JSON.stringify({ type: "ping" }));
        }
        break;

      default:
        _dispatch("ws_message", msg);
    }
  }

  // ── In-app toast banner ──────────────────────────────────────────
  function _showInAppBanner(text, type = "info") {
    // Remove existing
    document.querySelectorAll(".ve-toast").forEach(el => el.remove());

    const toast = document.createElement("div");
    toast.className = `ve-toast ve-toast-${type}`;
    toast.textContent = text;
    document.body.appendChild(toast);

    // Animate in
    requestAnimationFrame(() => toast.classList.add("ve-toast-show"));

    // Auto-dismiss after 4s
    setTimeout(() => {
      toast.classList.remove("ve-toast-show");
      setTimeout(() => toast.remove(), 400);
    }, 4000);
  }

  // ── DOM event dispatcher ────────────────────────────────────────
  function _dispatch(name, detail) {
    window.dispatchEvent(new CustomEvent(`ve:${name}`, { detail }));
  }

  // ── Permission prompt banner ─────────────────────────────────────
  function _showPermissionPrompt() {
    if (Notification.permission !== "default") return;
    if (document.getElementById("ve-push-prompt")) return;

    const banner = document.createElement("div");
    banner.id        = "ve-push-prompt";
    banner.className = "push-prompt";
    banner.innerHTML = `
      <span>Get notified when value bets are found</span>
      <button id="ve-push-allow" class="push-allow-btn">Allow</button>
      <button id="ve-push-deny"  class="push-deny-btn">Not now</button>
    `;
    document.body.prepend(banner);

    document.getElementById("ve-push-allow").onclick = async () => {
      banner.remove();
      const result = await requestPermission();
      if (result === "granted") {
        _showInAppBanner("Notifications enabled!", "info");
      }
    };
    document.getElementById("ve-push-deny").onclick = () => banner.remove();
  }

  // ── Public init ──────────────────────────────────────────────────
  async function init(sessionId) {
    _sessionId = sessionId;

    // Register SW
    const reg = await _registerSW();

    // Connect WebSocket
    _connectWS();

    // Show push permission prompt after 3s (after user has seen the UI)
    setTimeout(_showPermissionPrompt, 3000);

    // Listen for messages from SW (push_received forwarded to app)
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.addEventListener("message", event => {
        if (event.data?.type === "push_received") {
          _dispatch("push_received", event.data.data);
        }
      });
    }
  }

  return {
    init,
    requestPermission,
    isConnected: () => _ws && _ws.readyState === WebSocket.OPEN,
    getSessionId: () => _sessionId,
  };
})();

// Export for use by app.js
window.VEPush = VEPush;
