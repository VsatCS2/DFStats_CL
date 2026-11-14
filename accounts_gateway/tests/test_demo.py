import sys, types
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gateway_app import Settings, create_app
from store import MemoryStore
from auth import Verifier

SITE = "https://deltaforcetracker.com"


class Up:
    def __init__(self):
        self.calls, self.found, self.stats, self.boom = [], True, True, False

    def post(self, path, body):
        self.calls.append(path)
        if self.boom:
            raise httpx.ConnectError("down")
        if path == "/player/search":
            if not self.found:
                return 200, {"result": 0, "player_info": []}
            return 200, {"result": 0, "player_info": [
                {"player_id": "77", "nick_name": body["search_player_nick"], "level": 60, "season_lvl": 50}]}
        if not self.stats:
            return 200, {"result": 0, "sol_data": {}}
        return 200, {"result": 0, "kd": 2.5, "sol_data": {
            "total_fight": 200, "total_escape": 100, "total_kill": 500, "total_killed": 200,
            "rank_score": 6100, "total_game_time": 7200 * 50, "current_asset": 999}}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("DEMO_ORIGINS", raising=False)
    up = Up()
    s = Settings(supabase_url="https://p.supabase.co", store="memory", jwt_secret="x" * 40,
                 playerapi_url="x", playerapi_secret="y")
    app = create_app(s, store=MemoryStore(), verifier=Verifier("https://p.supabase.co", "x" * 40), upstream=up)
    return types.SimpleNamespace(c=TestClient(app), up=up)


def go(env, name="Alpha", ip="1.1.1.1", origin=SITE):
    return env.c.post("/v1/demo/lookup", json={"name": name},
                      headers={"x-forwarded-for": ip, "origin": origin})


def test_success_is_trimmed_and_counts_down(env):
    r = go(env)
    j = r.json()
    assert r.status_code == 200 and j["ok"] and j["remaining"] == 2 and j["limit"] == 3
    p = j["player"]
    assert p["name"] == "Alpha" and p["matches"] == 200 and p["extraction_rate"] == 50.0
    assert p["kd"] == 2.5 and p["ranked_points"] == 6100 and p["hours_played"] == 100.0
    assert "999" not in r.text and "stash" not in r.text       # no stash value, no raw payload


def test_three_per_ip_then_blocked_and_other_ip_unaffected(env):
    for n in range(3):
        assert go(env, f"P{n}").status_code == 200
    r = go(env, "P9")
    assert r.status_code == 429 and r.json()["error"] == "limit" and r.json()["remaining"] == 0
    assert go(env, "P9", ip="2.2.2.2").status_code == 200


def test_cached_name_costs_no_api_call_but_still_counts_for_visitor(env):
    go(env, "Same"); n = len(env.up.calls)
    r = go(env, "same", ip="3.3.3.3")
    assert r.json()["cached"] and len(env.up.calls) == n and r.json()["remaining"] == 2


def test_spoofed_xff_prefix_cannot_dodge_limit(env):
    for n in range(3):
        env.c.post("/v1/demo/lookup", json={"name": f"Q{n}"},
                   headers={"x-forwarded-for": f"9.9.9.{n}, 4.4.4.4", "origin": SITE})
    r = env.c.post("/v1/demo/lookup", json={"name": "Q9"},
                   headers={"x-forwarded-for": "8.8.8.8, 4.4.4.4", "origin": SITE})
    assert r.status_code == 429


@pytest.mark.parametrize("name", ["", "a", "x" * 30, "<script>", "a/b", "ab\ncd;"])
def test_bad_names_never_reach_api(env, name):
    r = go(env, name)
    assert r.status_code in (400,) and r.json()["error"] == "bad_name" and env.up.calls == []
    assert r.json()["remaining"] == 3                          # a rejected name is free


def test_not_found_does_not_use_a_lookup_but_tries_are_capped(env):
    env.up.found = False
    for n in range(8):
        r = go(env, f"Nobody{n}")
        assert r.status_code == 404 and r.json()["remaining"] == 3
    assert go(env, "Nobody9").status_code == 429               # attempts capped at 8


def test_no_stats_and_upstream_down(env):
    env.up.stats = False
    assert go(env, "Priv").json()["error"] == "no_stats"
    env.up.boom = True
    r = go(env, "Down")
    assert r.status_code == 502 and r.json()["error"] == "unavailable"


def test_cors_only_for_the_site(env):
    ok = go(env)
    assert ok.headers["access-control-allow-origin"] == SITE
    evil = go(env, "Beta", origin="https://evil.example")
    assert "access-control-allow-origin" not in evil.headers
    pre = env.c.options("/v1/demo/lookup", headers={"origin": SITE, "access-control-request-method": "POST"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == SITE


def test_global_cap(env, monkeypatch):
    monkeypatch.setenv("DEMO_DAILY_CAP", "2")
    up = Up()
    s = Settings(supabase_url="https://p.supabase.co", store="memory", jwt_secret="x" * 40,
                 playerapi_url="x", playerapi_secret="y")
    app = create_app(s, store=MemoryStore(), verifier=Verifier("https://p.supabase.co", "x" * 40), upstream=up)
    c = TestClient(app)
    post = lambda n, ip: c.post("/v1/demo/lookup", json={"name": n}, headers={"x-forwarded-for": ip})
    assert post("Aa", "1.0.0.1").status_code == 200 and post("Bb", "1.0.0.2").status_code == 200
    r = post("Cc", "1.0.0.3")
    assert r.status_code == 503 and r.json()["error"] == "busy"


def test_status_and_kill_switch(env, monkeypatch):
    assert env.c.get("/v1/demo/status", headers={"x-forwarded-for": "5.5.5.5"}).json()["remaining"] == 3
    monkeypatch.setenv("DEMO_ENABLED", "0")
    s = Settings(supabase_url="https://p.supabase.co", store="memory", jwt_secret="x" * 40)
    app = create_app(s, store=MemoryStore(), verifier=Verifier("https://p.supabase.co", "x" * 40), upstream=Up())
    r = TestClient(app).post("/v1/demo/lookup", json={"name": "Zed"})
    assert r.status_code == 503 and r.json()["error"] == "disabled"
