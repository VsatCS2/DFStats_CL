"""
Delta Force Tracker — community leaderboard backend.

A small FastAPI service you host yourself. It does two things:
  1. Registers a player (one direct API call, no browser/OAuth involved -
     see module note below on why) and hands back a bearer api_key.
  2. Accepts opt-in stat uploads and serves a leaderboard of everyone
     who's opted in.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env
    uvicorn app:app --reload

See README.md for deploying it somewhere real.

Why there's no Discord/OAuth here:
    The desktop app already requires a real, verified login to the game's
    own DfTools backend before it can do anything (that's the browser
    login flow in delta_force_login.py) - so by the time a player opts
    into this leaderboard, the desktop app already holds their `openid`,
    captured from that authenticated session, not typed in by hand. That
    openid is what identifies a player here, instead of a separate
    Discord account. See delta_force_community.py's module docstring for
    the full reasoning and the one rule that keeps this trustworthy (the
    desktop app only ever sends the openid IT captured, never a
    user-editable field).

Security notes (read this before deploying):
  - openid is hashed (SHA-256) before it's ever stored or looked up here.
    This service never sees or stores the raw value, only a one-way
    digest of it - even a full database leak wouldn't reveal it.
  - api_key is a per-player bearer credential (like a password): treat it
    as one. It's generated with secrets.token_urlsafe - a capability
    token for THIS leaderboard only.
  - Stats stored here are exactly the aggregate numbers the desktop app
    already shows locally (net income, win/loss, best map/operator) -
    never raw match history, never DfTools/game credentials.
"""

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

DATABASE_PATH = os.environ.get("DATABASE_PATH", "./community.db")

app = FastAPI(title="Delta Force Tracker Community API")


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_key TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                api_key TEXT UNIQUE NOT NULL,
                opted_in INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                net_income_all_time INTEGER NOT NULL DEFAULT 0,
                matches INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                win_rate REAL NOT NULL DEFAULT 0,
                best_map TEXT,
                best_operator TEXT,
                rank_label TEXT,
                updated_at TEXT NOT NULL
            )
        """)


init_db()


def _hash_openid(openid: str) -> str:
    return hashlib.sha256(openid.strip().encode("utf-8")).hexdigest()


def upsert_player(openid: str, display_name: str, opt_in: bool) -> dict:
    """Create the player if new, or refresh their display name (and,
    only on their very first registration, their opt-in choice) if they
    already registered before. Re-registering always returns the SAME
    api_key, so a reinstall or a second opt-in click never orphans an
    existing leaderboard entry."""
    player_key = _hash_openid(openid)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with db() as conn:
        row = conn.execute(
            "SELECT api_key, opted_in FROM users WHERE player_key = ?",
            (player_key,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET display_name=?, updated_at=? WHERE player_key=?",
                (display_name, now, player_key),
            )
            return {"api_key": row["api_key"], "opted_in": bool(row["opted_in"])}

        api_key = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO users (player_key, display_name, api_key, opted_in, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (player_key, display_name, api_key, int(opt_in), now, now),
        )
        return {"api_key": api_key, "opted_in": opt_in}


def get_user_by_api_key(api_key: str):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE api_key = ?", (api_key,)
        ).fetchone()


def require_user(authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing or malformed Authorization header")
    api_key = authorization.split(" ", 1)[1].strip()
    user = get_user_by_api_key(api_key)
    if not user:
        raise HTTPException(401, "Invalid API key")
    return user


# ---------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------
class RegisterPayload(BaseModel):
    openid: str
    nickname: str = "Player"
    opt_in: bool = True


@app.post("/register")
def register(payload: RegisterPayload):
    if not payload.openid or not payload.openid.strip():
        raise HTTPException(400, "openid is required")
    result = upsert_player(payload.openid, payload.nickname or "Player",
                            payload.opt_in)
    return result


# ---------------------------------------------------------------------
# Stats + leaderboard
# ---------------------------------------------------------------------
class StatsPayload(BaseModel):
    net_income_all_time: int = 0
    matches: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    best_map: str = ""
    best_operator: str = ""
    rank_label: str = ""


@app.get("/me")
def me(authorization: str = Header(None)):
    u = require_user(authorization)
    with db() as conn:
        stats = conn.execute(
            "SELECT * FROM stats WHERE user_id = ?", (u["id"],)
        ).fetchone()
    return {
        "display_name": u["display_name"],
        "opted_in": bool(u["opted_in"]),
        "stats": dict(stats) if stats else None,
    }


@app.post("/stats/sync")
def sync_stats(payload: StatsPayload, authorization: str = Header(None)):
    u = require_user(authorization)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with db() as conn:
        conn.execute(
            """
            INSERT INTO stats (user_id, net_income_all_time, matches, wins,
                                losses, win_rate, best_map, best_operator,
                                rank_label, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                net_income_all_time=excluded.net_income_all_time,
                matches=excluded.matches,
                wins=excluded.wins,
                losses=excluded.losses,
                win_rate=excluded.win_rate,
                best_map=excluded.best_map,
                best_operator=excluded.best_operator,
                rank_label=excluded.rank_label,
                updated_at=excluded.updated_at
            """,
            (u["id"], payload.net_income_all_time, payload.matches,
             payload.wins, payload.losses, payload.win_rate,
             payload.best_map, payload.best_operator, payload.rank_label, now),
        )
    return {"ok": True}


@app.post("/opt-in")
def opt_in(authorization: str = Header(None)):
    u = require_user(authorization)
    with db() as conn:
        conn.execute("UPDATE users SET opted_in=1 WHERE id=?", (u["id"],))
    return {"opted_in": True}


@app.post("/opt-out")
def opt_out(authorization: str = Header(None)):
    u = require_user(authorization)
    with db() as conn:
        conn.execute("UPDATE users SET opted_in=0 WHERE id=?", (u["id"],))
    return {"opted_in": False}


@app.delete("/me")
def delete_me(authorization: str = Header(None)):
    """Right-to-be-forgotten: wipes this player's account and stats
    entirely. The desktop app calls this and then deletes its own local
    api_key file - after this, nothing about the player remains here."""
    u = require_user(authorization)
    with db() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (u["id"],))
    return {"deleted": True}


_SORT_COLUMNS = {
    "net_income": "s.net_income_all_time",
    "matches": "s.matches",
    "win_rate": "s.win_rate",
}


@app.get("/leaderboard")
def leaderboard(sort: str = "net_income", limit: int = 50):
    sort_col = _SORT_COLUMNS.get(sort, _SORT_COLUMNS["net_income"])
    limit = max(1, min(limit, 200))
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT u.display_name, s.net_income_all_time,
                   s.matches, s.wins, s.losses, s.win_rate, s.best_map,
                   s.best_operator, s.rank_label, s.updated_at
            FROM users u
            JOIN stats s ON s.user_id = u.id
            WHERE u.opted_in = 1
            ORDER BY {sort_col} DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"sort": sort, "players": [dict(r) for r in rows]}


@app.get("/")
def health():
    return {"ok": True, "service": "delta-force-tracker-community"}
