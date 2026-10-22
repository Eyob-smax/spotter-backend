# Backend plan: Weather-Aware Truck Routing API

This is the build specification for the Django backend. Every behaviour here is covered by tests. The data contract is shared with the frontend spec in the frontend design spec (§12, kept with the frontend) and the live OpenAPI docs at `/api/docs`.

## 1. Facts verified against live APIs (2026-10-22)

| Fact | Consequence |
|---|---|
| TomTom NYC→Chicago with `maxAlternatives=4` returned **2** routes; Denver→SLC returned 4 | 3 routes are not guaranteed, so a detour-waypoint fallback is mandatory |
| TomTom LA→NYC took 4.8 s with 0 alternatives, 12.2 s with 2, and 21 s with 4 | Request `maxAlternatives=2`. Set the read timeout to 45 s and gunicorn's timeout to 90 s. Cache route results |
| TomTom geometry is about 13,000 points per long route | Simplify to ≤1,500 points before storing and sending |
| TomTom `extendedRouteRepresentation=travelTime,distance` gives a `progress[]` list of (pointIndex, cumulative distance, cumulative time) | ETAs come from TomTom's own timing, interpolated between progress entries |
| TomTom returns HTTP 400 `detailedError.code = MAP_MATCHING_FAILURE`, naming "Origin" or "Destination", for unroutable points | Map this to `point_not_routable` with the offending field |
| TomTom accepts a `departAt` in the past | Validate on our side: clamp up to 15 min in the past to "now", and reject anything older |
| TomTom `instructionsType=coded` instructions carry `roadNumbers` with route offsets | Route names like "via I-80" |
| Open-Meteo: `rain` excludes `showers`; `snowfall` is in inches when `precipitation_unit=inch`; the value at T covers T−1h…T; wind is instantaneous | Rain rate = `rain + showers`. Precipitation and gusts use the hour ending at or after the ETA; wind uses the nearest hour |
| Open-Meteo: 150 locations per GET is a 2.5 KB URL and takes about 2 s; each location counts as one call (600/min, 10,000/day) | Batch 150 points per request, fetch batches in parallel, cache per 0.1° cell, dedupe cells |

## 2. Module layout

```
backend/
  config/            settings (env-driven), urls, wsgi
  planner/
    domain/          pure logic: no Django, no I/O; 100% unit-tested
      geo.py         haversine, bearing, destination point, interpolation along a polyline, RDP simplify, overlap
      risk.py        thresholds, load rules, classify() -> Assessment(level, score, factors)
      sampling.py    checkpoints every N miles with ETA, progress and segment miles
      recommend.py   summarize route risk, rank routes, explain the decision
      heatgrid.py    corridor grid cell generation
      timealign.py   picks the forecast hour for an ETA
    services/        I/O adapters
      http.py        shared requests.Session with retries and timeouts
      tomtom.py      calculate_route(), search(), reverse(); maps TomTom errors to domain errors
      routes.py      orchestrates: alternatives, dedupe, detour fallback, naming, simplification
      weather.py     batched Open-Meteo fetch with a per-cell cache
      plans.py       plan store (cache) plus analysis and heatmap computation
    api/
      serializers.py, views.py, urls.py, errors.py (exception handler), throttles.py
    tests/
```

## 3. Flows

### 3.1 `GET /api/geocode/search?q&lat&lon`
1. Validate: `q` is 2–120 characters after trimming; `lat` and `lon` are optional but must come together and be in range.
2. Check the cache key `geo:s:{q.lower()}:{lat:.1f}:{lon:.1f}` (24 h).
3. Call TomTom `search/2/search/{q}.json?typeahead=true&limit=6[&lat&lon]`.
4. Map each result to `{id, label, secondary, lat, lon, type}`. For a POI the label is the POI name; otherwise it's the freeform address.

### 3.2 `GET /api/geocode/reverse?lat&lon`
1. Validate the range, then check the cache key `geo:r:{lat:.4f}:{lon:.4f}` (24 h).
2. Call TomTom reverseGeocode. If there's no address, the label falls back to `"lat, lon"`.

### 3.3 `POST /api/routes`
1. **Validate:**
   - origin and destination coordinates are in range and are not the same point (at least 0.5 km apart)
   - `load_weight_lb` is between 1,000 and 120,000
   - `departure_time` is timezone-aware, no more than 15 min in the past (clamped to now), and at most 7 days ahead
2. **Cache:** look up `routes:{sha1(o,d rounded 4dp, weight, departure rounded to 15 min)}` (15 min).
3. **Request routes:** TomTom truck route with `maxAlternatives=2`, `vehicleWeight` in kg, `vehicleCommercial=true`, `departAt`, `traffic=true`, `instructionsType=coded`, and `extendedRouteRepresentation` set to both `travelTime` and `distance`.
4. **Parse each route:**
   - full-resolution coordinates
   - the progress list, interpolated into per-vertex cumulative distance and time (distance comes from TomTom's progress, split within each gap by haversine proportion)
   - summary and road names
5. **Dedupe:** drop any route whose overlap with a kept route is ≥ 0.9 (§4.4).
6. **Detour fallback** if fewer than 3 routes remain:
   - Generate waypoints perpendicular to the fastest route: at 50% of its distance, then at 33% and 67%, on both sides.
   - Offset distance D = clamp(0.2 × straight-line distance, 15 km, 120 km).
   - Request 2 candidates at a time in parallel (`maxAlternatives=0`, origin:via:destination).
   - Accept a candidate if it is distinct (overlap < 0.9) and its duration is ≤ 1.6 × the fastest.
   - Stop once there are 3 routes or after 6 candidates.
   - If still short, return what we have with a notice.
7. **Order and name:** sort by duration and assign ids A, B, C. Name each route by its longest road that the other routes don't share, e.g. "via I-80", falling back to "Route X".
8. **Simplify** each geometry (RDP with adaptive tolerance, ≤1,500 points), keeping the cumulative arrays aligned with the kept vertices.
9. **Store the plan:** save `plan:{id}` (1 h TTL) and return the response in §12 of the design spec.

### 3.4 `POST /api/plans/{id}/analysis {interval_mi}`
1. **Load the plan.** If it's missing, return 404 `plan_expired`.
2. **Check the analysis cache:** `plan:{id}:analysis:{interval}`.
3. **Place checkpoints:** for each route, at miles 0, N, 2N, … plus the destination. Each checkpoint has its position (interpolated along the geometry), ETA (departure plus interpolated time), progress, and `segment_mi` (the midpoint split between neighbours, so the segments sum to the route length).
4. **Fetch weather:** collect the unique 0.1° cells across all routes and fetch them in one batched weather request (§4.6).
5. **Classify** each checkpoint (§4.1). Weather values are chosen with the time alignment in §1. A missing value gives `unknown`.
6. **Summarize** each route: miles by level, the four ranking numbers, blocked, and the first blocked mile.
7. **Rank and explain** (§4.3), then cache and return.

### 3.5 `GET /api/plans/{id}/heatmap`
1. **Load the plan;** check `plan:{id}:heatmap`.
2. **Build the corridor grid** (§4.5), at most 220 cells.
3. **Fetch weather** for those cells, for hours departure_hour … +48 h.
4. **Score** each cell for each hour, using the plan's load weight. Missing values become `null`.
5. Cache and return the compact arrays.

### 3.6 `GET /api/meta` and `GET /api/health`
- `/api/meta` returns the rule tables straight from `risk.py`, so the frontend never duplicates the thresholds.
- `/api/health` returns `{status, cache}`, checks only Redis, and makes no third-party calls.

## 4. Algorithms

### 4.1 Risk classification (`risk.py`)
Bands use **lower-bound-inclusive** thresholds. Where the table repeats a boundary value, the higher, safer band wins.

| Metric | Moderate | High | Severe | No Travel |
|---|---|---|---|---|
| Wind (mph, sustained) | ≥ 25 | ≥ 35 | ≥ 45 | **≥ 55** |
| Rain (in/hr, rain + showers) | ≥ 0.10 | ≥ 0.25 | ≥ 0.50 | **> 1.00** |
| Snow (in/hr, snow depth) | ≥ 0.5 | ≥ 1.0 | ≥ 2.0 | **> 3.0** |

Load rules (strict ">" on weight, as written in the brief):
- wind ≥ 55 → No Travel for any load (already covered by the table)
- 45 ≤ wind < 55 and load > 30,000 lb → No Travel
- 35 ≤ wind < 45 and load > 40,000 lb → Severe

The level is the maximum of the wind, rain, snow and load results.

`score` (0.0–4.0) is a continuous value used for heatmap smoothness and for tie-breaking:
- Within a level, it's level + the fractional position inside that band, capped at level + 0.99.
- Load rules set the floor at 3.0 or 4.0.
- No Travel is exactly 4.0.

`floor(score) == level` always holds.

Every factor carries a human-readable message, such as "Wind 47 mph with 42,000 lb load (> 30,000 lb) → No Travel".

### 4.2 Checkpoint risk aggregation (`recommend.summarize`)
- `miles_by_risk[level] += segment_mi`.
- Risk scores: low 0, moderate 1, high 2, severe 3, No Travel 4.
- `avg_risk` is the mile-weighted mean of those scores, excluding unknown miles.
- `blocked` is true if there are any No Travel miles.

### 4.3 Ranking (`recommend.rank`)
- Sort key: `(no_travel_mi, severe_mi, high_mi, avg_risk, duration_s)`.
- Miles are rounded to 0.1 and `avg_risk` to 0.01 before comparing, so float noise never decides.
- **No Travel miles rank first.** This is an assumption: the brief doesn't list them.
- **Reason:** the winner is compared with the route a driver would otherwise pick, the **fastest** one. If the winner is the fastest, it is compared with the runner-up. The reason names the first criterion where they differ, and `compared_to` says which route was used. Examples:
  - "Fewest severe-risk miles: 0 mi vs 12 mi on Route A (fastest); adds 26 min"
  - "Same weather risk as Route B, and 42 min faster"

  (Implemented after an end-to-end test showed that comparing only with the runner-up hid why the fastest route was rejected.)
- `all_blocked` is true when every route has No Travel miles. The winner is still the best by the key.

### 4.4 Route overlap (`geo.overlap`)
- Resample each route every 1 km and hash the points into a 0.01° grid, including the 8 neighbouring cells.
- overlap(A, B) is the share of A's samples that land in B's cells.
- Two routes are duplicates if the mean of overlap(A, B) and overlap(B, A) is ≥ 0.9.

### 4.5 Heatmap corridor (`heatgrid.py`)
- The cell size starts at the smallest of [0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0] degrees that keeps the count ≤ 220. Every size is a multiple of 0.1°, so cells share the weather cache with checkpoints.
- Sample all routes every half a cell. Add the cell containing each sample plus its neighbours within 1 cell, which gives about a ±1 cell corridor. Dedupe.

### 4.6 Weather fetch (`weather.py`)
- Each location snaps to a 0.1° cell.
- **Cache key** `wx:v1:{lat:.1f}:{lon:.1f}:d{days}`, where `days` is the smallest of the buckets 3, 7, 10 or 16 that covers the needed end time. Lookups also accept any larger cached bucket. TTL 30 min.
- Cache misses are fetched in batches of 150, with up to 4 batches in parallel.
- Request: `hourly=wind_speed_10m,wind_gusts_10m,rain,showers,snowfall&wind_speed_unit=mph&precipitation_unit=inch&timeformat=unixtime&timezone=GMT&forecast_days={days}`.
- **On failure** (timeout or 5xx after retries), those cells return `None` and their checkpoints become `unknown`, with a notice. The request itself does not fail unless every cell failed.
- **Rate limit (HTTP 429):** this is a per-minute quota, so a retry wouldn't help. We record a 60 s cooldown in the cache and stop calling Open-Meteo until it ends; cached cells are still served.
  - If nothing can be served, the response is `upstream_unavailable` with `retry_after: 60`.
  - Results with missing forecasts are **never cached**, so a retry fills them in.
- **Budget:** the free tier allows 600 locations per minute (verified live: two quick long-haul searches triggered a 429). An analysis uses at most 120 weather cells, snapping checkpoints to a coarser 0.2–0.5° grid on long trips, with a notice. The heatmap uses at most 220 cells.

## 5. Errors
- Every error has the shape `{"error": {"code", "message", "field"?, "fields"?, "retry_after"?}}`. Codes: `validation_error` 400, `point_not_routable` 422, `no_route_found` 422, `plan_expired` 404, `rate_limited` 429, `upstream_timeout` 504, `upstream_unavailable` 502, `internal_error` 500.
- Unhandled exceptions are logged with the request id and returned as `internal_error`. Stack traces are never returned.

## 6. Configuration (env)
- Core: `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`, `REDIS_URL` (falls back to locmem if absent), `TOMTOM_API_KEY`, `LOG_LEVEL`.
- `NUM_PROXIES`: the number of proxies in front of the backend, so rate limits use the real client IP.
- Throttle rates (`THROTTLE_*`).
- There is no database. `DATABASES = {}`, and auth, admin and sessions are removed.

## 7. Tests

| Area | What's covered |
|---|---|
| risk | Every boundary for each metric (24.99/25, 34.99/35, 44.99/45, 54.99/55, 0.0999/0.10, 0.25, 0.4999/0.50, 1.00/1.0001, 2.0/3.0/3.0001…). Each load rule at 30,000/30,001 and 40,000/40,001 and at wind edges 34.99/35/44.99/45/54.99/55. Combined worst-of. Score invariants (floor(score) == level, monotonic in each input) |
| timealign | ETA on the hour, just after, just before; wind nearest-hour versus precipitation hour-ending |
| sampling | Interval spacing, destination included, segment miles sum to length, ETA monotonic, ETA interpolation against a known progress list, routes shorter than the interval |
| recommend | Each ranking criterion decides on its own; ties fall through to duration; float-noise rounding; all blocked; reason strings; unknown miles excluded from the average |
| geo | haversine against known distances; simplification keeps endpoints and stays within tolerance and the point budget; overlap of identical, disjoint and partial routes |
| heatgrid | Cell budget respected; cells on the 0.1 grid; covers all route samples |
| tomtom (mocked HTTP) | Request params (kg conversion, departAt, alternatives); parsing progress and road names; MAP_MATCHING_FAILURE becomes `point_not_routable` with the right field; NO_ROUTE_FOUND; timeout; 403 |
| routes service | Dedupe; detour fallback until 3 routes; notice when it stays short; ordering and naming |
| weather (mocked) | Batching at 150; cache hits avoid HTTP; partial failure becomes unknown; unit parsing |
| API | Validation errors per field, happy paths end to end with mocked upstreams, plan expiry, error envelope shape, throttling, health, meta |

Tools: pytest, pytest-django, `responses` to mock HTTP, pytest-cov. Target over 90% coverage of `domain/` and `services/`.

## 8. Deployment
- **VPS (shared with other apps):** `docker compose -f docker-compose.prod.yml up -d` runs the **backend** (gunicorn gthread with 2 workers and 4 threads, 90 s timeout), published only on `127.0.0.1:8010`, and **Redis** (`maxmemory 256mb`, `allkeys-lru`, no persistence needed). The server's existing **nginx** proxies the API domain to it, and certbot provides HTTPS.
- **Frontend on Vercel:** calls `https://<api-domain>/api` directly. CORS is limited to the Vercel origin and `NUM_PROXIES=1`. A Vercel rewrite was rejected for two reasons: nginx would only see Vercel's IPs, which breaks per-client rate limiting, and Vercel's proxy timeout is below the slowest route lookups (about 20 s). See `backend/deploy/DEPLOY.md`.
- **CI:** GitHub Actions runs ruff, pytest and `manage.py check --deploy`.

## 9. Assumptions to document in the README
1. Wind rules use **sustained** 10 m wind. Gusts are shown but not scored.
2. Rain rate = rain + showers per hour. Snow is in inches of snow depth per hour.
3. Boundary convention follows §4.1.
4. No Travel miles rank above Severe miles.
5. Each checkpoint represents the stretch halfway to its neighbours on each side.
6. Departure is allowed up to 7 days ahead. The heatmap covers 48 h from the departure hour.
7. If TomTom returns fewer than 3 distinct routes, the missing ones are built through detour waypoints and labelled as such.
