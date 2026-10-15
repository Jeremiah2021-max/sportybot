"""
models.py – SQLAlchemy ORM models for Virtual Edge
===================================================
Tables: users, sessions, fixtures, predictions,
        market_predictions, outcomes, bet_log
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean,
    DateTime, ForeignKey, Text, UniqueConstraint
)
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


# ──────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    username      = Column(String(50),  nullable=False, unique=True, index=True)
    email         = Column(String(120), nullable=False, unique=True, index=True)
    password_hash = Column(String(256), nullable=False)
    is_active     = Column(Boolean, default=True)
    bankroll      = Column(Float, default=0.0)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sessions    = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")
    bet_logs    = relationship("BetLog",      back_populates="user", cascade="all, delete-orphan")


# ──────────────────────────────────────────────────────────────────
class UserSession(Base):
    """
    Tracks an analysis session (one screenshot upload = one session).
    Linked to a User if authenticated, anonymous otherwise.
    """
    __tablename__ = "sessions"

    id         = Column(String(36), primary_key=True)   # UUID
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=True)
    bankroll   = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)

    user     = relationship("User",    back_populates="sessions")
    fixtures = relationship("Fixture", back_populates="session", cascade="all, delete-orphan")


# ──────────────────────────────────────────────────────────────────
class Team(Base):
    __tablename__ = "teams"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    code          = Column(String(18), nullable=False, unique=True, index=True)
    full_name     = Column(String(60), nullable=True)
    inferred_atk  = Column(Float, default=1.0)
    inferred_def  = Column(Float, default=1.0)
    total_matches = Column(Integer, default=0)
    wins          = Column(Integer, default=0)
    draws         = Column(Integer, default=0)
    losses        = Column(Integer, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)


# ──────────────────────────────────────────────────────────────────
class Fixture(Base):
    __tablename__ = "fixtures"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    session_id     = Column(String(36), ForeignKey("sessions.id"), nullable=False)
    home_code      = Column(String(18), nullable=False)
    away_code      = Column(String(18), nullable=False)

    # Raw bookmaker odds
    odd1           = Column(Float, nullable=False)
    oddX           = Column(Float, nullable=False)
    odd2           = Column(Float, nullable=False)

    # Poisson-derived fair probabilities
    fair_p1        = Column(Float)
    fair_pX        = Column(Float)
    fair_p2        = Column(Float)

    # Poisson parameters
    lambda_home    = Column(Float)
    lambda_away    = Column(Float)
    overround_pct  = Column(Float)
    created_at     = Column(DateTime, default=datetime.utcnow)

    session            = relationship("UserSession",       back_populates="fixtures")
    predictions        = relationship("Prediction",        back_populates="fixture", cascade="all, delete-orphan")
    market_prediction  = relationship("MarketPrediction",  back_populates="fixture", uselist=False, cascade="all, delete-orphan")
    outcomes           = relationship("Outcome",           back_populates="fixture", cascade="all, delete-orphan")


# ──────────────────────────────────────────────────────────────────
class Prediction(Base):
    __tablename__ = "predictions"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    fixture_id     = Column(Integer, ForeignKey("fixtures.id"), nullable=False)

    pick           = Column(String(4),   nullable=False)   # "1","X","2","1X","X2","12"
    pick_label     = Column(String(60))
    pick_odd       = Column(Float)
    confidence     = Column(Integer)                        # 0–100
    edge_pct       = Column(Float)
    kelly_fraction = Column(Float)
    value_bet      = Column(Boolean, default=False)
    reasoning      = Column(Text)
    market         = Column(String(10), default="1X2")      # "1X2","OU25","BTTS","CS"
    predicted_at   = Column(DateTime, default=datetime.utcnow)

    fixture  = relationship("Fixture", back_populates="predictions")
    outcomes = relationship("Outcome", back_populates="prediction")
    bet_logs = relationship("BetLog",  back_populates="prediction")


# ──────────────────────────────────────────────────────────────────
class MarketPrediction(Base):
    """Poisson-derived market probabilities for a fixture."""
    __tablename__ = "market_predictions"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    fixture_id     = Column(Integer, ForeignKey("fixtures.id"), nullable=False, unique=True)

    over25_prob    = Column(Float)
    under25_prob   = Column(Float)
    over25_edge    = Column(Float)
    btts_prob      = Column(Float)
    no_btts_prob   = Column(Float)
    btts_edge      = Column(Float)

    # Top 3 correct scores
    cs_top1_label  = Column(String(10))
    cs_top1_prob   = Column(Float)
    cs_top1_odd    = Column(Float)
    cs_top2_label  = Column(String(10))
    cs_top2_prob   = Column(Float)
    cs_top2_odd    = Column(Float)
    cs_top3_label  = Column(String(10))
    cs_top3_prob   = Column(Float)
    cs_top3_odd    = Column(Float)

    created_at     = Column(DateTime, default=datetime.utcnow)

    fixture = relationship("Fixture", back_populates="market_prediction")


# ──────────────────────────────────────────────────────────────────
class Outcome(Base):
    """Stores the actual result after a user logs it."""
    __tablename__ = "outcomes"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    fixture_id    = Column(Integer, ForeignKey("fixtures.id"), nullable=False)
    prediction_id = Column(Integer, ForeignKey("predictions.id"), nullable=True)

    actual_result = Column(String(4))    # "1", "X", "2"
    home_goals    = Column(Integer)
    away_goals    = Column(Integer)
    is_correct    = Column(Boolean)
    stake         = Column(Float, default=0.0)
    profit_loss   = Column(Float, default=0.0)
    logged_at     = Column(DateTime, default=datetime.utcnow)

    fixture    = relationship("Fixture",    back_populates="outcomes")
    prediction = relationship("Prediction", back_populates="outcomes")


# ──────────────────────────────────────────────────────────────────
class BetLog(Base):
    """User-facing bet history."""
    __tablename__ = "bet_log"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=True)
    prediction_id = Column(Integer, ForeignKey("predictions.id"), nullable=True)

    match_label   = Column(String(100))
    stake         = Column(Float)
    odds          = Column(Float)
    result        = Column(String(10))   # "win" | "loss" | "pending"
    profit_loss   = Column(Float)
    logged_at     = Column(DateTime, default=datetime.utcnow)

    user       = relationship("User",       back_populates="bet_logs")
    prediction = relationship("Prediction", back_populates="bet_logs")
