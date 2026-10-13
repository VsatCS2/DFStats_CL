"""
Tiny gateway that keeps the paid API's x-secret on YOUR server.

The desktop app calls   POST /v1/call  {"path": "/player/search", "body": {...}}
This service adds the secret, forwards to the upstream API, caches, rate
limits per client IP, and only allows the endpoints the app needs - so the
secret is never inside the app and a leaked build can't be used to hit
arbitrary endpoints.

Env vars (set them in Railway):
  PLAYERAPI_URL      RapidAPI:  https://delta-force-api.p.rapidapi.com
                     (a *.rapidapi.com URL automatically uses RapidAPI auth
                      headers; any other URL uses the x-secret header)
  PLAYERAPI_SECRET   your RapidAPI key (or the x-secret for a self-hosted API)
  CLIENT_TOKEN       optional shared token the app sends as x-client-token
  RATE_PER_MIN       per-IP limit (default 30)
  CACHE_TTL          seconds (default 600 - every upstream call costs money)
  DAILY_CAP          max upstream calls per UTC day, 0 = unlimited (default 0).
                     Set this! It stops a bug or abuse from burning your quota.

Run:  gunicorn playerapi_proxy:app   (Procfile: web: gunicorn playerapi_proxy:app)
"""
import json
import os
import threading
import time

from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request

UPSTREAM = os.environ.get("PLAYERAPI_URL", "").rstrip("/")
SECRET = os.environ.get("PLAYERAPI_SECRET", "")
CLIENT_TOKEN = os.environ.get("CLIENT_TOKEN", "")
RATE = int(os.environ.get("RATE_PER_MIN", "30"))
TTL = int(os.environ.get("CACHE_TTL", "600"))
DAILY_CAP = int(os.environ.get("DAILY_CAP", "0"))

ALLOWED = {"/player/search", "/player/get/season", "/player/get/stats",
           "/player/get/status", "/player/get/info"}

app = Flask(__name__)
_lock = threading.Lock()
_cache, _hits = {}, {}
_daily = {"day": "", "n": 0}


def auth_headers(base_url: str, secret: str) -> dict:
    """RapidAPI needs x-rapidapi-key + x-rapidapi-host; a self-hosted API
    uses x-secret."""
    host = urlparse(base_url).netloc
    if host.endswith("rapidapi.com"):
        return {"x-rapidapi-key": secret, "x-rapidapi-host": host}
    return {"x-secret": secret}


def _over_daily_cap() -> bool:
    """Counts an upstream call; True if the cap is already used up."""
    if not DAILY_CAP:
        return False
    today = time.strftime("%Y-%m-%d", time.gmtime())
    with _lock:
        if _daily["day"] != today:
            _daily.update(day=today, n=0)
        if _daily["n"] >= DAILY_CAP:
            return True
        _daily["n"] += 1
        return False


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "?"


def _limited(ip) -> bool:
    now = time.time()
    with _lock:
        window = [t for t in _hits.get(ip, []) if now - t < 60]
        window.append(now)
        _hits[ip] = window
        return len(window) > RATE


@app.get("/health")
def health():
    return jsonify(ok=True, configured=bool(UPSTREAM and SECRET))


@app.post("/v1/call")
def call():
    if CLIENT_TOKEN and request.headers.get("x-client-token") != CLIENT_TOKEN:
        return jsonify(error="forbidden"), 403
    if not (UPSTREAM and SECRET):
        return jsonify(error="not configured"), 503
    if _limited(_client_ip()):
        return jsonify(error="rate limited"), 429
    payload = request.get_json(silent=True) or {}
    path, body = payload.get("path"), payload.get("body") or {}
    if path not in ALLOWED or not isinstance(body, dict):
        return jsonify(error="endpoint not allowed"), 400

    key = path + "|" + json.dumps(body, sort_keys=True)
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return jsonify(hit[1])
    if _over_daily_cap():
        return jsonify(error="daily upstream cap reached"), 429
    try:
        r = requests.post(UPSTREAM + path, json=body,
                          headers=auth_headers(UPSTREAM, SECRET), timeout=10)
    except requests.RequestException:
        return jsonify(error="upstream unreachable"), 502
    if r.status_code != 200:
        out = {"error": "upstream error", "status": r.status_code}
        if r.status_code in (401, 403, 429):
            out["detail"] = r.text[:200]      # usually says why (bad key / IP blocked)
        return jsonify(out), 502
    data = r.json()
    with _lock:
        if len(_cache) > 5000:
            _cache.clear()
        _cache[key] = (time.time(), data)
    return jsonify(data)
