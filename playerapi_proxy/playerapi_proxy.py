"""
Tiny gateway that keeps the paid API's x-secret on YOUR server.

The desktop app calls   POST /v1/call  {"path": "/player/search", "body": {...}}
This service adds the secret, forwards to the upstream API, caches, rate
limits per client IP, and only allows the endpoints the app needs - so the
secret is never inside the app and a leaked build can't be used to hit
arbitrary endpoints.

Env vars (set them in Railway):
  PLAYERAPI_URL      e.g. http://46.105.28.236:5058
  PLAYERAPI_SECRET   the x-secret value
  CLIENT_TOKEN       optional shared token the app sends as x-client-token
  RATE_PER_MIN       per-IP limit (default 40)
  CACHE_TTL          seconds (default 120)

Run:  gunicorn playerapi_proxy:app   (Procfile: web: gunicorn playerapi_proxy:app)
"""
import json
import os
import threading
import time

import requests
from flask import Flask, jsonify, request

UPSTREAM = os.environ.get("PLAYERAPI_URL", "").rstrip("/")
SECRET = os.environ.get("PLAYERAPI_SECRET", "")
CLIENT_TOKEN = os.environ.get("CLIENT_TOKEN", "")
RATE = int(os.environ.get("RATE_PER_MIN", "40"))
TTL = int(os.environ.get("CACHE_TTL", "120"))

ALLOWED = {"/player/search", "/player/get/season", "/player/get/stats",
           "/player/get/status", "/player/get/info"}

app = Flask(__name__)
_lock = threading.Lock()
_cache, _hits = {}, {}


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
    try:
        r = requests.post(UPSTREAM + path, json=body,
                          headers={"x-secret": SECRET}, timeout=10)
    except requests.RequestException:
        return jsonify(error="upstream unreachable"), 502
    if r.status_code != 200:
        return jsonify(error="upstream error", status=r.status_code), 502
    data = r.json()
    with _lock:
        if len(_cache) > 5000:
            _cache.clear()
        _cache[key] = (time.time(), data)
    return jsonify(data)
