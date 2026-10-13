# Player API proxy (Railway)

Holds the paid API's x-secret so it never ships inside the desktop app.

Railway variables (Service -> Variables):
  PLAYERAPI_URL     http://46.105.28.236:5058
  PLAYERAPI_SECRET  <the x-secret>        <- set ONLY here, never commit it
  CLIENT_TOKEN      <any random string>   (optional; also put it in the app's config)
  RATE_PER_MIN      40   (optional)
  CACHE_TTL         120  (optional)

Check it works:  GET https://<your-domain>/health  ->  {"configured": true, "ok": true}
