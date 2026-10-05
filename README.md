# ✈️ Skyguard Radar — Flight Timetable

A read-only timetable for every aircraft in the Aviabit (АвиаБит) flight plan.
One tab per tail, one row per leg, planned time against actual time.

## What it shows

- **A separate timetable for every tail** that appears in the plan, switched
  from a bar pinned to the bottom of the screen. Real registrations come first,
  planning placeholders (VFlyAir, UKreserv, …) last and dimmed. Nothing to
  configure: a tail that starts flying shows up on its own, and one that stops
  flying disappears.
- **Planned vs. actual.** Every leg shows the scheduled time struck through, the
  actual (or Aviabit's own estimated) time below it, and a colour-coded delay
  chip: green ≤15 min, amber ≤60 min, red beyond that.
- **Crew**, read straight off the flight record, so it always matches the date.
- **Airport names, not bare codes.** `airports.py` carries hand-checked Russian
  names for the network this fleet flies, backed by a generated table covering
  every airport worldwide with scheduled service — so a brand-new destination
  reads as a city the first time it appears in the plan.
- **History.** The «История» switch in the header shows what each tail actually
  flew over any past range — yesterday, 7 days, 30 days, or two dates you pick
  (up to 62 days at once). Newest day first, with a summary per tail: flights
  planned and flown, block hours, on-time share and average departure delay.
  Past days are fetched from Aviabit on demand, so nothing has to be stored.
  `/#history` opens straight into it.
- All times **UTC**.

Not included by design: live position tracking, METAR/NOTAM, Telegram alerts.

## Setup

```bash
pip install -r requirements.txt
cp config.example.json config.json    # then fill in your Aviabit login
python app.py
```

Open <http://localhost:5050>.

Instead of `config.json` you can set `AVIABIT_USERNAME` and `AVIABIT_PASSWORD`
as environment variables — which is what you want on a hosted deployment.

## Configuration

| Key / env var | Default | Meaning |
|---|---|---|
| `days_back` / `DAYS_BACK` | 1 | how far back the timetable reaches |
| `days_ahead` / `DAYS_AHEAD` | 21 | how far ahead the timetable reaches |
| `cache_ttl` / `CACHE_TTL` | 300 | seconds before Aviabit is queried again |
| `history_max_days` / `HISTORY_MAX_DAYS` | 62 | longest range the history view will fetch at once |

## Endpoints

| Route | Purpose |
|---|---|
| `/` | the timetable |
| `/api/timetable` | tails and legs as JSON (served from cache) |
| `/api/refresh` | force a re-fetch from Aviabit |
| `/api/history?from=YYYY-MM-DD&to=YYYY-MM-DD` | past flights for a date range (add `&force=1` to skip the 15-min cache) |
| `/api/health` | login state, last error, cache age |

## Deploying (Render)

`render.yaml` holds the whole configuration. On an existing service, check that
**Settings → Start Command** is:

```
gunicorn app:app --workers 1 --threads 4 --timeout 120
```

Both of those matter. One worker means one shared cache, so page loads cost
Aviabit nothing most of the time. The 120-second timeout covers a cold start,
where the first request fans out into several Aviabit calls — Gunicorn's
30-second default would kill the worker mid-wake.

Required environment variables: `AVIABIT_USERNAME`, `AVIABIT_PASSWORD`.
Optional: `DAYS_BACK`, `DAYS_AHEAD`, `CACHE_TTL`, `PYTHONUNBUFFERED=1`.

There is no database and no background thread, so nothing is lost when the host
restarts.

### On the free tier

The service sleeps after 15 minutes idle and takes 30–60 seconds to wake. The
app handles this itself: the first open shows a wake-up screen with a progress
bar and retries until the server answers, rather than failing on a blank page.

## On an iPhone

Open the URL in Safari → Share → **Add to Home Screen**. It installs as a
standalone app: its own icon, no Safari chrome, and the status bar blends into
the header. `manifest.webmanifest` and the icons under `static/` do this; the
layout is built for a phone-width screen and the tables scroll on their own
without the page moving sideways.

## Notes

- `/api/plan-flight` returns at most ~400 records per call, **oldest first**, so a
  wide date range silently loses the newest flights. `fetch_plan()` works around
  this by requesting the window in 7-day slices and merging them.
- Aviabit publishes `dateTakeoffReal` / `dateLandingReal` alongside the planned
  times. This app reads them; anything that only reads the planned times will
  report an aircraft airborne while it is still on stand.
- The airport fallback table in `airports.py` is generated from OurAirports
  (public domain). To refresh it, rebuild from `airports.csv` at
  <https://davidmegginson.github.io/ourairports-data/> — keep `NAMES_RU`, which
  is hand-written and takes priority.
