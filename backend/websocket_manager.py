"""
websocket_manager.py – WebSocket connection registry for Virtual Edge
======================================================================
Manages all active browser connections.
Supports:
  • Per-session targeting  (send to one user's browser)
  • Broadcast to all      (send to every connected client)
  • Typed message protocol (JSON envelopes with "type" field)

Message types emitted by the server:
  prediction_ready   – full prediction payload after /api/predict
  analysis_progress  – OCR / analysis step updates  (0-100%)
  value_alert        – high-confidence value bet found
  ping               – keepalive (every 25s)
"""

import asyncio
import json
from typing import Dict, List, Optional
from fastapi import WebSocket


# ──────────────────────────────────────────────────────────────────
# Message builders
# ──────────────────────────────────────────────────────────────────
def msg_prediction_ready(session_id: str, predictions: list,
                         summary: dict, engine: str) -> dict:
    return {
        "type":       "prediction_ready",
        "session_id": session_id,
        "predictions": predictions,
        "summary":    summary,
        "engine":     engine,
    }


def msg_progress(step: str, pct: int, detail: str = "") -> dict:
    return {
        "type":   "analysis_progress",
        "step":   step,
        "pct":    pct,
        "detail": detail,
    }


def msg_value_alert(home: str, away: str, pick: str,
                    odd: str, confidence: int, edge_pct: float,
                    market: str = "1X2") -> dict:
    return {
        "type":       "value_alert",
        "home":       home,
        "away":       away,
        "pick":       pick,
        "odd":        odd,
        "confidence": confidence,
        "edge_pct":   edge_pct,
        "market":     market,
        "title":      f"🔥 Value Bet: {home} vs {away}",
        "body":       (f"{pick} @ {odd} — {confidence}% confidence, "
                       f"+{edge_pct:.1f}% edge"),
    }


def msg_ping() -> dict:
    return {"type": "ping"}


# ──────────────────────────────────────────────────────────────────
# Connection manager
# ──────────────────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        # session_id → list of WebSocket connections
        # (one user can have multiple tabs open)
        self._connections: Dict[str, List[WebSocket]] = {}

    # ── Connect / disconnect ──────────────────────────────────────
    async def connect(self, ws: WebSocket, session_id: str):
        await ws.accept()
        self._connections.setdefault(session_id, []).append(ws)
        # Confirm connection
        await self._send(ws, {
            "type":       "connected",
            "session_id": session_id,
            "message":    "Real-time feed active",
        })

    def disconnect(self, ws: WebSocket, session_id: str):
        conns = self._connections.get(session_id, [])
        if ws in conns:
            conns.remove(ws)
        if not conns:
            self._connections.pop(session_id, None)

    # ── Send helpers ──────────────────────────────────────────────
    async def _send(self, ws: WebSocket, data: dict):
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            pass   # stale connection — will be cleaned up on next receive

    async def send_to_session(self, session_id: str, data: dict):
        """Send to all tabs open for a specific session."""
        conns = self._connections.get(session_id, [])
        dead  = []
        for ws in conns:
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, session_id)

    async def broadcast(self, data: dict):
        """Send to every connected client."""
        for session_id, conns in list(self._connections.items()):
            dead = []
            for ws in conns:
                try:
                    await ws.send_text(json.dumps(data))
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.disconnect(ws, session_id)

    async def broadcast_value_alerts(self, predictions: list, session_id: str):
        """
        After a prediction run, push value_alert messages for every
        pick that has a positive edge ≥ 3%.
        """
        for p in predictions:
            edge = p.get("edge_pct", 0)
            if edge >= 3.0:
                alert = msg_value_alert(
                    home       = p.get("home", ""),
                    away       = p.get("away", ""),
                    pick       = p.get("pick", ""),
                    odd        = str(p.get("odd", "")),
                    confidence = p.get("confidence_int", p.get("confidence", 0)),
                    edge_pct   = edge,
                )
                await self.send_to_session(session_id, alert)
                await asyncio.sleep(0.05)  # small gap between alerts

    # ── Keepalive ─────────────────────────────────────────────────
    async def keepalive_loop(self, interval: int = 25):
        """Background task — ping all clients every `interval` seconds."""
        while True:
            await asyncio.sleep(interval)
            if self._connections:
                await self.broadcast(msg_ping())

    # ── Stats ─────────────────────────────────────────────────────
    def stats(self) -> dict:
        return {
            "total_sessions":    len(self._connections),
            "total_connections": sum(len(v) for v in self._connections.values()),
        }


# Singleton instance — imported by main.py
manager = ConnectionManager()
