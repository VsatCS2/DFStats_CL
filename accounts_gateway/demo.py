"""Public "try it" player lookup for the website (POST /v1/demo/lookup).

No sign-in, so it is locked down hard:
  * 3 successful lookups per visitor address per UTC day (DEMO_PER_IP), plus a
    separate cap on total attempts so typos can't be used to hammer the API
  * a global daily cap on upstream-backed lookups (DEMO_DAILY_CAP)
  * only the website's own origins may call it from a browser (DEMO_ORIGINS)
  * returns a small, trimmed summary - never the raw upstream payload, and no
    stash value
Visitor addresses are kept only as a salted hash, in memory, for the day.
Nothing is written to the database.

Env vars (all optional):
  DEMO_ENABLED     "0" switches the endpoint off            (default on)
  DEMO_PER_IP      successful lookups per address per day   (default 3)
  DEMO_MAX_TRIES   all attempts per address per day         (default 8)
  DEMO_DAILY_CAP   upstream-backed lookups per UTC day      (default 300)
  DEMO_ORIGINS     comma list of allowed browser origins
                   (default https://deltaforcetracker.com,https://www.deltaforcetracker.com)
"""
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time

import httpx
from fastapi import Body, Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("demo")
log.setLevel(logging.INFO)
if not log.handlers:
    log.addHandler(logging.StreamHandler())      # shows up in Railway logs

SOL_MODES = ({}, {"game_mode": 1, "game_rule": 4})
NAME_RE = re.compile(r"^[\w .\-\[\]]{2,24}$", re.UNICODE)
DEFAULT_ORIGINS = "https://deltaforcetracker.com,https://www.deltaforcetracker.com"
MESSAGES = {
    "bad_name": "Enter a player name of 2 to 24 characters.",
    "not_found": "No player found with that name. Check the spelling and try again.",
    "no_stats": "That player was found, but their career stats aren't available.",
    "limit": "You've used all your free demo lookups for today. Come back tomorrow, or download DF Tracker for more.",
    "busy": "The demo is busy right now. Please try again later.",
    "unavailable": "The demo is unavailable right now. Please try again later.",
    "disabled": "The demo is switched off at the moment.",
}


def _i(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _ok(payload):
    """Upstream body is usable when its error code is absent or 0."""
    return isinstance(payload, dict) and payload.get("result", 0) in (0, None)


def summarize(info, career):
    """The handful of fields the website shows."""
    sol = (career or {}).get("sol_data") or {}
    fights = _i(sol.get("total_fight") or (career or {}).get("total_fight"))
    escaped = _i(sol.get("total_escape") or (career or {}).get("total_escaped"))
    kills, deaths = _i(sol.get("total_kill")), _i(sol.get("total_killed"))
    kd = _f((career or {}).get("kd")) or (round(kills / max(deaths, 1), 2) if kills else 0.0)
    secs = _i(sol.get("total_game_time") or (career or {}).get("total_game_time"))
    return {
        "name": str(info.get("nick_name") or "")[:40],
        "level_operations": _i(info.get("season_lvl")),
        "level_warfare": _i(info.get("level") or info.get("account_lvl")),
        "matches": fights,
        "extraction_rate": round(100.0 * escaped / fights, 1) if fights else 0.0,
        "kd": round(kd, 2),
        "kills": kills,
        "deaths": deaths,
        "ranked_points": _i(sol.get("rank_score") or info.get("sol_rank_score")),
        "hours_played": round(secs / 3600.0, 1),
    }


def register_demo(app, settings, upstream, client_ip, clock=time.time):
    enabled = os.environ.get("DEMO_ENABLED", "1") not in ("0", "false", "no")
    per_ip = int(os.environ.get("DEMO_PER_IP", "3"))
    max_tries = int(os.environ.get("DEMO_MAX_TRIES", "8"))
    daily_cap = int(os.environ.get("DEMO_DAILY_CAP", "300"))
    origins = {o.strip().rstrip("/") for o in
               os.environ.get("DEMO_ORIGINS", DEFAULT_ORIGINS).split(",") if o.strip()}
    lock = threading.Lock()
    salt = secrets.token_hex(8)
    state = {"day": "", "ips": {}, "global": 0}
    cache = {}                                    # name -> (time, summary)

    def day():
        return time.strftime("%Y-%m-%d", time.gmtime(clock()))

    def roll():
        d = day()
        if state["day"] != d:
            state.update(day=d, ips={}, **{"global": 0})
            cache.clear()

    def cors(request):
        origin = (request.headers.get("origin") or "").rstrip("/")
        h = {"Cache-Control": "no-store", "Vary": "Origin"}
        if origin in origins:
            h["Access-Control-Allow-Origin"] = origin
        return h

    def reply(request, status, body, remaining=None):
        if remaining is not None:
            body["remaining"], body["limit"] = max(remaining, 0), per_ip
        return JSONResponse(body, status, headers=cors(request))

    def err(request, status, code, remaining=None):
        return reply(request, status, {"ok": False, "error": code,
                                       "message": MESSAGES.get(code, "")}, remaining)

    @app.options("/v1/demo/lookup")
    def demo_preflight(request: Request):
        h = cors(request)
        h.update({"Access-Control-Allow-Methods": "POST, OPTIONS",
                  "Access-Control-Allow-Headers": "Content-Type",
                  "Access-Control-Max-Age": "86400"})
        return Response(status_code=204, headers=h)

    @app.get("/v1/demo/status")
    def demo_status(request: Request):
        """Lets the page show how many tries this visitor has left."""
        ip = hashlib.sha256((salt + client_ip(request)).encode()).hexdigest()[:20]
        with lock:
            roll()
            used = state["ips"].get(ip, {"ok": 0})["ok"]
        return reply(request, 200, {"ok": True, "enabled": enabled and bool(upstream)},
                     per_ip - used)

    @app.post("/v1/demo/lookup")
    def demo_lookup(request: Request, data: dict = Body(...)):
        if not enabled:
            return err(request, 503, "disabled")
        name = " ".join(str(data.get("name") or "").split())
        ip = hashlib.sha256((salt + client_ip(request)).encode()).hexdigest()[:20]
        with lock:
            roll()
            rec = state["ips"].setdefault(ip, {"ok": 0, "tries": 0})
            left = per_ip - rec["ok"]
            if left <= 0 or rec["tries"] >= max_tries:
                return err(request, 429, "limit", 0)
            if not NAME_RE.match(name):
                return err(request, 400, "bad_name", left)     # free: never reaches the API
            rec["tries"] += 1
            hit = cache.get(name.lower())
            if hit and clock() - hit[0] < 1800:
                rec["ok"] += 1
                return reply(request, 200, {"ok": True, "player": hit[1], "cached": True},
                             per_ip - rec["ok"])
            if not upstream:
                rec["tries"] -= 1
                log.error("demo: no upstream configured (set PLAYERAPI_URL and PLAYERAPI_SECRET)")
                return err(request, 503, "unavailable", left)
            if state["global"] >= daily_cap:
                rec["tries"] -= 1
                return err(request, 503, "busy", left)
            state["global"] += 1

        try:
            status, payload = upstream.post("/player/search", {"search_player_nick": name})
            players = (payload or {}).get("player_info") if status == 200 and _ok(payload) else None
            if status != 200:
                log.error("demo: /player/search returned HTTP %s: %s", status, json.dumps(payload)[:300])
                return err(request, 502, "unavailable", left)
            if not players:
                return err(request, 404, "not_found", left)
            info = next((p for p in players
                         if str(p.get("nick_name") or "").lower() == name.lower()), players[0])
            pid = _i(info.get("player_id"))
            career = None
            for mode in SOL_MODES:
                st, body = upstream.post("/player/get/season",
                                         {"player_id": pid, "is_career": True, "mode": mode})
                if st == 200 and _ok(body) and (body.get("sol_data") or {}).get("total_fight"):
                    career = body
                    break
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
            log.exception("demo: upstream call failed (%s)", type(exc).__name__)
            return err(request, 502, "unavailable", left)

        if career is None:
            log.warning("demo: found player %s but no career stats", pid)
            return err(request, 404, "no_stats", left)
        player = summarize(info, career)
        with lock:
            rec = state["ips"].setdefault(ip, {"ok": 0, "tries": 0})
            rec["ok"] += 1
            cache[name.lower()] = (clock(), player)
            if len(cache) > 1000:
                cache.clear()
            left = per_ip - rec["ok"]
        return reply(request, 200, {"ok": True, "player": player}, left)
