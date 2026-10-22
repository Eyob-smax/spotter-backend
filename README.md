# Spotter Route API (backend)

A Django REST API that finds 3 truck routes, checks the weather forecast at the time the truck will reach each checkpoint, classifies the risk with the assessment's rules, and recommends the safest route.

- **Routing and geocoding:** TomTom, in truck mode with the load weight.
- **Weather:** Open-Meteo hourly forecasts.
- **State:** none in a database. Plans and forecasts live in Redis.

## Run locally

With Docker:
```bash
cp .env.example .env            # set TOMTOM_API_KEY; DEBUG/REDIS_URL are set by compose
docker compose up --build       # API on http://localhost:8000, docs on http://localhost:8000/api/docs
```

Or with a virtualenv (uses an in-memory cache, no Redis needed):
```bash
python -m venv venv && . venv/bin/activate
pip install -r requirements-dev.txt
DEBUG=1 python manage.py runserver
```

## Tests
```bash
pytest                                   # ~280 tests, no network access needed
pytest --cov=planner --cov-report=term   # 99% coverage; 100% on risk, ranking, sampling, forecast alignment
ruff check . && ruff format --check .
```

- External APIs are faked at the HTTP level (`planner/tests/fakes.py`), so the full request path runs in tests: views, services, domain logic and caching.
- The risk engine has boundary tests for every threshold and every load rule.

## API

| Method & path | Purpose |
|---|---|
| `GET /api/meta` | Risk thresholds, load rules, ranking order and input limits; the frontend reads these instead of hard-coding them |
| `GET /api/geocode/search?q=` | Address and place autocomplete |
| `GET /api/geocode/reverse?lat=&lon=` | Coordinates to an address, for map clicks and "use my location" |
| `POST /api/routes` | Finds 3 truck routes and returns a `plan_id` |
| `POST /api/plans/{id}/analysis` | Checkpoints every 10/25/50 mi, the weather at each ETA, risk, and the recommendation |
| `GET /api/plans/{id}/heatmap` | Corridor grid of risk scores, hourly for 0–48 h from departure |
| `GET /api/health` | Liveness and cache check |
| `GET /api/docs` | Interactive OpenAPI documentation |

Errors always have the shape `{"error": {"code", "message", ...}}`. Full request and response examples are in the interactive docs at `/api/docs`. Design notes, verified provider facts and the test plan are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## How it works
```
planner/
  domain/     pure logic, no I/O: risk.py, recommend.py, sampling.py, forecast.py, geo.py, heatgrid.py
  services/   I/O: tomtom.py, weather.py, routes.py (3-route orchestration), plans.py (caching + flows)
  api/        serializers (validation), views, error envelope, request-id middleware
```

1. **Routes.**
   - We ask TomTom for the fastest truck route plus 2 alternatives, passing the load weight in kg.
   - If fewer than 3 distinct routes come back (NYC → Chicago sometimes returns 2), the missing ones are built by sending the trip through a waypoint offset sideways from the fastest route. These are labelled as detour routes.
   - Near-duplicate routes (≥ 90% overlap) are removed.
   - Routes are named after their main road ("via I-80") and simplified to at most 1,500 points.
2. **Checkpoints and ETAs.**
   - Checkpoints are placed at mile 0, every 10/25/50 mi, and at the destination.
   - Each ETA comes from TomTom's own cumulative travel time along the route, not a constant-speed guess.
3. **Weather at the ETA.**
   - Checkpoints are snapped to 0.1° cells and fetched in one batched, cached Open-Meteo request.
   - Each checkpoint also reports temperature, visibility, chance of precipitation and a conditions description (from the WMO weather code). These are shown to the driver but don't change the risk, which follows the brief's wind/rain/snow rules.
   - Wind uses the forecast hour nearest the ETA. Precipitation uses the hour whose window contains the ETA, because Open-Meteo totals cover the hour before each timestamp.
4. **Risk and recommendation.** See the assumptions below.

## Assumptions and interpretations
1. **Wind** means the sustained 10 m wind speed. Gusts are returned and shown, but not scored.
2. **Rain rate** is `rain + showers` per hour; Open-Meteo's `rain` leaves out convective showers. **Snow** is inches of snow depth per hour, not water equivalent.
3. **Band boundaries:**
   - Every band includes its lower bound. Where the table repeats a value (rain 0.25, snow 1.0), the higher band wins.
   - No Travel is `≥ 55 mph` for wind, but strictly `> 1.00 in/hr` for rain and `> 3.0 in/hr` for snow, as written in the brief.
4. **Load rules** use strict comparison on weight ("> 30,000 lb"), and wind bands `45 ≤ w < 55` and `35 ≤ w < 45`. A load rule can raise a checkpoint's level but never lower it.
5. **Each checkpoint covers the stretch halfway to its neighbours on each side.** That's how "Severe miles" and "High miles" are measured, and these stretches always add up to the route length.
6. **Ranking:**
   - Routes are ordered by fewest No-Travel miles first (not in the brief, but a No-Travel route shouldn't win on time), then fewest Severe miles, fewest High miles, lowest mile-weighted average risk, and shortest travel time. Average risk uses each checkpoint's continuous 0–4 score (its integer part is the level: Low 0 … No Travel 4), so routes that are all Low still differ by how close their weather comes to the Moderate thresholds.
   - Miles are compared at 0.1 mi and average risk at 0.01, so floating-point noise never decides.
7. **Explanation:** the reason compares the winner with the fastest route, or with the runner-up when the winner is the fastest, and states the extra driving time.
8. **Departure** can be up to 7 days ahead. Up to 15 minutes in the past is treated as now. The heatmap covers 48 h from the departure hour.
9. **Missing forecasts:** checkpoints are marked `unknown` and excluded from the average, the response includes a notice, and the result isn't cached.

## Design decisions

### What affects the risk level, and what doesn't
Risk follows the brief's table exactly: each checkpoint takes the **worst of wind, rain and snow**, and the load rules can raise it. For example:
- 0.17 in/hr of rain → Moderate
- 0.60 in/hr → Severe
- more than 3 in/hr of snow → No Travel

The forecast also provides **temperature, visibility, chance of precipitation and a conditions description**. These are returned and shown to the driver but **don't change the level**, for three reasons:
1. **The brief defines thresholds only** for wind, rain and snow, plus the load rules. Scoring other fields would invent rules the assessment didn't specify.
2. **No double counting.** Rain and snow are measured amounts. Chance of precipitation and the conditions text describe the same weather, so scoring them would count it twice.
3. **They still help.** The frontend shows them, including a freezing line on the temperature chart.

### Why each checkpoint uses a different forecast hour per field
Open-Meteo reports different fields in different ways:
- **Wind speed is a snapshot** at the timestamp. A truck arriving at 14:20 uses the **nearest** snapshot, 14:00.
- **Rain, showers, snowfall and gusts are totals for the preceding hour.** The value stamped 15:00 covers 14:00–15:00. A truck at 14:20 is inside that window, so it uses **15:00**.

**Why this matters:** using the nearest hour for everything would read the 14:00 total, which is the rain from 13:00–14:00, an hour already over. If a storm arrives at 14:15, the 14:00 value says 0.00 in/hr ("dry") while the 15:00 value says 0.60 in/hr ("Severe"). The hour-ending rule picks the hour the truck is actually in. It also makes the hourly total match the brief's **in/hr** thresholds directly. Unit tests cover ETAs on the hour, just after it, and just before it.

### Why detour waypoints (the "Detour" routes)
- **The brief requires 3 route options,** but TomTom doesn't guarantee 3 alternatives. Live, NYC → Chicago returned only 2. Asking for more is slow (about 21 s for 4 alternatives cross-country) and still not guaranteed.
- **When fewer than 3 distinct routes come back,** the backend creates the missing ones:
  1. Take a point at 50% of the fastest route's length, then 33% or 67% if needed.
  2. Move it **sideways**, at a right angle to the road, by 20% of the straight-line trip distance, clamped between 15 km and 120 km.
  3. Ask TomTom for a truck route forced through that point.
  4. Keep it only if it's **distinct** (less than 90% overlap with the others) and **reasonable** (no more than 1.6× the fastest route's time).
- **The point of this project is comparing weather.** Separate corridors can meet different weather; near-copies of the same highway can't. Detour routes are labelled as such in the UI.

### Why Redis and not Postgres
Nothing needs to be kept permanently: there are no accounts or trip history. Everything is temporary and expires: trip plans after 1 h, forecasts per map cell after 30 min, geocoding after 24 h, rate-limit counters after a minute. Redis keeps this in memory, expires it automatically, and is shared by every gunicorn worker. Postgres would add tables, migrations, backups and another service to secure, for data we throw away. It would be the right addition if saved trips or accounts were added, alongside Redis as the cache.

### Why gunicorn
Django's `runserver` is for development only. Gunicorn is the standard production server for Django: it manages worker processes, restarts crashed ones, and enforces timeouts. It uses **threaded workers (`gthread`)** because the API mostly *waits* on TomTom and Open-Meteo, up to about 20 s for a cross-country route, and threads keep serving other requests meanwhile. nginx sits in front and handles HTTPS.

### Why Docker and docker compose
- **One image everywhere:** CI tests the code and builds a single image; production runs exactly that image.
- **Isolation on a shared server:** compose runs the backend and its own Redis as project `spotter`, published only on `127.0.0.1`. It can't conflict with the server's other apps.
- **Easy rollback:** every commit has its own image tag, and the deploy rolls back automatically if the new image isn't healthy.
- **Local development:** `docker compose up` starts everything with one command.

### Why GitHub Actions
The code is on GitHub, and Actions is built in. Every push runs lint, the tests (failing below 95% coverage) and Django's deploy checks. A push to `master` then builds the image to GHCR and deploys it over SSH, with health checks and automatic rollback. Nobody runs commands on the server by hand.

### Why reverse geocoding goes through the backend
Map clicks and "Use my location" produce coordinates; reverse geocoding turns them into a readable address for the form and the route header. It runs through `/api/geocode/reverse`, so the TomTom key never reaches the browser, and results are cached for 24 h. Where there's no address, such as open water, the label falls back to the coordinates.

## Operational notes
- **Weather quota.** The free Open-Meteo tier allows 600 locations per minute, so one search is capped at about 340 weather lookups: up to 120 for checkpoints and 220 for the heatmap. Long trips use a coarser grid, with a notice.
  - On HTTP 429 the API stops calling Open-Meteo for 60 s and returns `retry_after`.
  - A paid Open-Meteo key can be set with `OPEN_METEO_API_KEY`.
- **TomTom latency.** Requesting more alternatives makes TomTom much slower: about 12 s for 2 versus 21 s for 4 on LA → NYC. The gunicorn timeout is 90 s, and repeated searches come from cache in about 10 ms.
- **Rate limits** are per client IP and configurable through `THROTTLE_*`. Every response carries an `X-Request-ID`, which also appears in the logs.
- **Deployment:** see [Deployment](#deployment) below.

## Deployment
```
push to master ─▶ GitHub Actions (.github/workflows/ci-cd.yml)
                   ├─ test    ruff, tests (≥95% coverage), Django deploy checks   ← every push / PR
                   ├─ build   image ─▶ ghcr.io/eyob-smax/spotter-backend:sha-<commit>, :latest
                   └─ deploy  SSH to the VPS: pull, restart, wait for health checks,
                              roll back to the previous image if unhealthy, then check /api/health over HTTPS

VPS (shared with other apps):  nginx (HTTPS via certbot) ─▶ 127.0.0.1:8010 ─▶ compose project "spotter": backend + redis
```

**On the server**, in `/opt/spotter`, owned by user `deploy`, which is in the `docker` group:
- **`.env`:** the production settings. See [`.env.example`](.env.example) for the variables, with `DEBUG=0`, `ALLOWED_HOSTS=api.spotter.wearecerta.app`, `NUM_PROXIES=1` and `BACKEND_PORT=8010`. It's mode 600 and never committed. The frontend origins allowed by CORS (the Vercel URL and local dev) are set in `config/settings.py`, so changing them is a normal code change and deploy.
- **`docker-compose.prod.yml`:** uploaded by CI on every deploy. The backend is published on loopback only, and Redis stays internal.
- **The nginx site for `api.spotter.wearecerta.app`:**
  ```nginx
  server {
      listen 80;
      server_name api.spotter.wearecerta.app;
      proxy_read_timeout 90s;   # cross-country route lookups can take ~20 s upstream
      location / {
          proxy_pass http://127.0.0.1:8010;
          proxy_set_header Host $host;
          proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;   # rate limits use the real client IP
          proxy_set_header X-Forwarded-Proto $scheme;
      }
  }
  ```
  certbot adds the HTTPS block with `certbot --nginx -d api.spotter.wearecerta.app --redirect`.

**GitHub settings** (*Settings → Secrets and variables → Actions*):
- **Secrets:** `DEPLOY_HOST`, `DEPLOY_USER` (`deploy`), `DEPLOY_SSH_KEY` (the private key whose public half is in `/home/deploy/.ssh/authorized_keys`), and `DEPLOY_KNOWN_HOSTS` (output of `ssh-keyscan`).
- **Variable:** `API_DOMAIN`.

**Operating it**, as `deploy` in `/opt/spotter`:
```bash
docker compose -f docker-compose.prod.yml ps                 # health
docker compose -f docker-compose.prod.yml logs -f backend    # request ids appear in [brackets]
# manual rollback to an earlier commit's image:
BACKEND_IMAGE=ghcr.io/eyob-smax/spotter-backend:sha-<commit> docker compose -f docker-compose.prod.yml up -d --wait
```
