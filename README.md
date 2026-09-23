# Delta Force Tracker — community leaderboard backend

A small FastAPI service: an opt-in stats leaderboard for Delta Force
Tracker users. One Python file, one SQLite database, one environment
variable. No Discord app, no OAuth, no client secret to protect — see
"Why no Discord/OAuth" below for why that's a deliberate choice, not a
missing feature.

## How players get identified

There's no separate account system here. The desktop app already
requires a real, verified login to the game's own DfTools backend before
it can do anything - so a player's `openid` (captured from that login,
never typed in by hand) is what identifies them. Joining the leaderboard
is one direct API call: `POST /register {openid, nickname}` → the server
hashes the openid (SHA-256, never stored raw) and hands back a bearer
`api_key` for all future sync/opt-out/delete calls.

## 1. Run it locally first

```
cd server
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --reload
```

Visit http://localhost:8000/ — you should see `{"ok":true,...}`. Visit
http://localhost:8000/docs for interactive API docs (FastAPI generates
this automatically from `app.py`).

Try it end to end with curl before touching the desktop app:

```
curl -X POST http://localhost:8000/register \
  -H "Content-Type: application/json" \
  -d '{"openid": "test-123", "nickname": "TestPlayer"}'
# -> {"api_key": "...", "opted_in": true}

curl -X POST http://localhost:8000/stats/sync \
  -H "Authorization: Bearer <api_key from above>" \
  -H "Content-Type: application/json" \
  -d '{"net_income_all_time": 15000, "matches": 20, "wins": 12, "losses": 8, "win_rate": 60.0, "best_map": "Zero Dam", "best_operator": "Recon"}'

curl http://localhost:8000/leaderboard
```

## 2. Deploy it somewhere with a stable URL

Pricing on all of these shifts often, so this is current as of when this
was last checked (Sept 2026) — verify against the platform's own pricing
page before committing, especially the free-tier claims.

**Render** (https://render.com) — the easiest genuinely-free path, no
card required. Push this repo to GitHub, then on Render: New -> Web
Service -> connect the repo -> set Root Directory to `server` -> it
detects the `Dockerfile` automatically -> pick the Free instance type.
You'll get a `*.onrender.com` URL.

The tradeoff: Render's free tier has **no persistent disk**, so the
SQLite file can be wiped on redeploys or restarts — fine for trying
this out, less fine if losing everyone's registration would actually be
annoying. Two ways around that once it matters: upgrade just that
service to Render's Starter tier (~$7/mo at time of writing) for
persistent disk with no code changes, or use Fly.io from the start
(below).

**Fly.io** (https://fly.io) — no free tier anymore (a 2-hour trial, then
a card is required), but genuinely cheap and gives real persistent
storage immediately: a small always-on machine plus a 1GB volume runs
roughly $2-3/month. `fly launch` in this folder picks up the
`Dockerfile`; `fly volumes create data --size 1` then mount it at
`/data` in the generated `fly.toml`; `fly deploy`.

**Your own VPS** (DigitalOcean, Hetzner, etc., roughly $4-6/mo) —
`docker build -t df-community . && docker run -d -p 8000:8000 -v $(pwd)/data:/data df-community`,
then put a reverse proxy (Caddy or nginx) in front if you want HTTPS.
(Unlike an OAuth-based setup, HTTPS isn't strictly required for this to
function — there's no redirect URI that has to match a registered value
— but it's still good practice for anything reachable from the internet.)
Comparable cost to Fly.io, more manual setup, full control.

## 3. Point the desktop app at it

In `delta_force_community.py` (in the main app folder, not here), set:

```python
SERVER_URL = "https://your-backend.example.com"
```

to your deployed URL, then rebuild the desktop app (see `BUILD.md`).

## Why no Discord/OAuth

An earlier version of this used Discord OAuth for identity. It works,
but it's a lot of moving parts (a registered Discord application, a
protected client secret, a redirect URI that has to match exactly, a
local loopback HTTP server in the desktop app to catch the callback) for
what this leaderboard actually needs: knowing which stats belong to
which player.

The desktop app already has that — `openid`, captured the moment someone
completes a real DfTools login. Using it means joining the leaderboard
is one direct API call with no browser popup, and this backend has zero
external dependencies to configure.

The tradeoff, to be upfront about it: Discord OAuth gives a live,
third-party-verified "this is a currently authenticated session" check
at login time. This approach doesn't - it trusts that the desktop app
only ever sends the openid it captured from the user's own login (never
a free-text field), which holds as long as that rule in
`delta_force_community.py` isn't changed. That's a real difference from
a cryptographically-verified identity provider, just one that's a
reasonable trade for a hobby leaderboard's actual threat model.

## What this does and doesn't store

Stores, per opted-in player: an in-game nickname, a SHA-256 hash of their
openid (not the raw value), and the same aggregate numbers already shown
in the app's Overview tab (all-time net income, matches, win/loss, best
map, best operator, rank label).

Never stores: raw openid, DfTools/game login credentials, raw match
history.

`DELETE /me` (wired up to "Leave Leaderboard" in the desktop app) removes
a player's row and their stats entirely.

## Running this for a group of friends vs. the general public

Nothing here limits who can register — anyone running your build of the
desktop app can opt in. If you want it scoped to a specific friend group
rather than open to whoever you hand the build to, the straightforward
option is: don't publish the build widely. There's no invite-code or
allowlist system here; that's a reasonable next feature if you outgrow
"just don't share the download link," but it's not built in yet.
