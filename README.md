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

## 2. Deploy it on Railway (what this project uses)

Railway (https://railway.com) has no sleeping and real persistent volumes,
which is exactly what a SQLite-backed leaderboard needs. Pricing shifts, so
check railway.com/pricing; at time of writing the Hobby plan is $5/month
and includes $5 of usage, which a service this small should stay inside.

1. Put `app.py`, `requirements.txt` and `railway.toml` at the **root** of a
   GitHub repo.
2. Railway -> New Project -> Deploy from GitHub repo -> pick that repo.
   `railway.toml` supplies the start command
   (`uvicorn app:app --host 0.0.0.0 --port $PORT`), so nothing else to set.
3. Add a **Volume** to the service (right-click the project canvas, or
   Ctrl/Cmd+K -> Volume), attach it to the service, mount path `/data`.
   The server finds it automatically through `RAILWAY_VOLUME_MOUNT_PATH`
   - no environment variables needed.
4. Service -> Settings -> Networking -> **Generate Domain**.
5. Open `https://<your-domain>/` in a browser. You want to see
   `"database_on_volume": true`. If it says `false`, the volume isn't
   attached and everything will be erased on the next deploy (the deploy
   log says so loudly too).

Test persistence once before you rely on it: join the leaderboard from the
desktop app, trigger a redeploy from the Railway dashboard, and confirm you
are still on the leaderboard afterwards.

Other hosts work too (any host with a persistent disk): set `DATABASE_PATH`
to a file on that disk. Render's free tier does **not** keep the disk,
which is why this project moved off it.

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
