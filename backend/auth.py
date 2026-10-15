"""
auth.py – JWT authentication for Virtual Edge
==============================================
Provides:
  - Password hashing / verification (bcrypt via passlib)
  - JWT access token creation and validation (python-jose)
  - FastAPI dependency: get_current_user (required)
                        get_optional_user (anonymous-friendly)
  - CRUD: register_user, authenticate_user

Tokens expire after ACCESS_TOKEN_EXPIRE_MINUTES (default 60 * 24 = 24h).
Set SECRET_KEY in your .env file — never use the default in production.
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session as OrmSession

from models import User

# ──────────────────────────────────────────────────────────────────
# Config  (override via environment / .env)
# ──────────────────────────────────────────────────────────────────
SECRET_KEY  = os.getenv("SECRET_KEY", "change-this-secret-in-production-abc123xyz")
ALGORITHM   = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("TOKEN_EXPIRE_MINUTES", 60 * 24))

# passlib 1.7.4 + bcrypt ≥4.0 compatibility fix
import bcrypt as _bcrypt_mod
if not hasattr(_bcrypt_mod, "__about__"):
    _bcrypt_mod.__about__ = type("_about", (), {"__version__": _bcrypt_mod.__version__})()

pwd_context   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


# ──────────────────────────────────────────────────────────────────
# Pydantic schemas (request / response)
# ──────────────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    username: str
    email:    str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    username:     str
    user_id:      int


class UserPublic(BaseModel):
    id:         int
    username:   str
    email:      str
    bankroll:   float
    created_at: datetime

    class Config:
        from_attributes = True


# ──────────────────────────────────────────────────────────────────
# Password helpers
# ──────────────────────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ──────────────────────────────────────────────────────────────────
# JWT helpers
# ──────────────────────────────────────────────────────────────────
def create_access_token(user_id: int, username: str) -> str:
    expire  = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub":      str(user_id),
        "username": username,
        "exp":      expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """Returns payload dict or None if token is invalid/expired."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") is None:
            return None
        return payload
    except JWTError:
        return None


# ──────────────────────────────────────────────────────────────────
# FastAPI dependencies
# ──────────────────────────────────────────────────────────────────
def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db:    OrmSession    = None,   # injected via Depends in routes
) -> User:
    """Strict dependency — raises 401 if token missing or invalid."""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalid or expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = int(payload["sub"])
    if db is None:
        raise HTTPException(status_code=500, detail="DB session not available")
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or deactivated",
        )
    return user


def get_optional_user(
    token: Optional[str] = Depends(oauth2_scheme),
) -> Optional[dict]:
    """
    Lenient dependency — returns token payload dict if valid, else None.
    Use for endpoints that work for both authenticated and anonymous users.
    """
    if not token:
        return None
    return decode_token(token)


# ──────────────────────────────────────────────────────────────────
# CRUD helpers
# ──────────────────────────────────────────────────────────────────
def register_user(db: OrmSession, req: RegisterRequest) -> User:
    """
    Create a new user. Raises HTTPException on duplicate username/email.
    """
    # Validate input length
    if len(req.username.strip()) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if "@" not in req.email:
        raise HTTPException(400, "Invalid email address")

    # Check uniqueness
    if db.query(User).filter(User.username == req.username.strip()).first():
        raise HTTPException(409, "Username already taken")
    if db.query(User).filter(User.email == req.email.strip().lower()).first():
        raise HTTPException(409, "Email already registered")

    user = User(
        username      = req.username.strip(),
        email         = req.email.strip().lower(),
        password_hash = hash_password(req.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: OrmSession, username: str, password: str) -> Optional[User]:
    """Returns the User if credentials are valid, else None."""
    user = db.query(User).filter(User.username == username.strip()).first()
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        return None
    return user


def update_bankroll(db: OrmSession, user_id: int, bankroll: float) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    user.bankroll  = bankroll
    user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return user
