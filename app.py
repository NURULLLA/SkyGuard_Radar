"""Skyguard Radar — per-aircraft flight timetable from Aviabit.

One job: show what every tail is scheduled to fly, and how far off schedule
it is running. No live tracking, no weather, no notifications.
"""

import json
import logging
import logging.handlers
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template

from airports import airport_name
from schedule_service import AviabitSchedule

# ── logging ──────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_file = logging.handlers.RotatingFileHandler(
    "logs/skyguard.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
_file.setFormatter(_fmt)
_console = logging.StreamHandler()
_console.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_file, _console])
logger = logging.getLogger(__name__)

# ── config ───────────────────────────────────────────────────────────────────
_cfg = {}
try:
    with open("config.json", encoding="utf-8") as fh:
        _cfg = json.load(fh)
    logger.info("config.json loaded")
except FileNotFoundError:
    logger.info("no config.json — falling back to environment variables")
except Exception as exc:
    logger.warning("config.json unreadable (%s) — using environment variables", exc)

_av = _cfg.get("aviabit", {})
USERNAME = os.environ.get("AVIABIT_USERNAME") or _av.get("username", "")
PASSWORD = os.environ.get("AVIABIT_PASSWORD") or _av.get("password", "")
BASE_URL = os.environ.get("AVIABIT_BASE_URL") or _av.get(
    "base_url", "https://ab-web.aviastartu.ru")

DAYS_BACK = int(os.environ.get("DAYS_BACK") or _cfg.get("days_back", 1))
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD") or _cfg.get("days_ahead", 21))
CACHE_TTL = int(os.environ.get("CACHE_TTL") or _cfg.get("cache_ttl", 300))

# A real registration looks like UK75058. Anything else in the "pln" field
# (VFlyAir, UKreserv, UKres2 ...) is a planning placeholder: still shown, but
# sorted last and labelled, so an empty slot is never read as a live aircraft.
REGISTRATION = re.compile(r"^[A-Z]{2}\d{4,}$", re.I)

if not USERNAME or not PASSWORD:
    logger.error("AVIABIT_USERNAME / AVIABIT_PASSWORD are not set — "
                 "add them to config.json or the environment")

app = Flask(__name__)
aviabit = AviabitSchedule(USERNAME, PASSWORD, BASE_URL)

# ── crew ─────────────────────────────────────────────────────────────────────
CREW_ROLES = {
    "КВ": "Командир ВС", "КВС": "Командир ВС",
    "2П": "Второй пилот", "ВП": "Второй пилот",
    "БП": "Бортпроводник", "БИ": "Бортинженер",
    "ШТ": "Штурман", "ДПП": "Доп. пилот", "ПИ": "Пилот-инструктор",
}


def parse_crew(xml_str):
    """Crew comes attached to the flight record, so it always matches the date."""
    if not xml_str or xml_str.strip() in ("", "<crew/>"):
        return []
    try:
        members = []
        for emp in ET.fromstring(xml_str).findall("employee"):
            role = emp.get("armChair", "—")
            members.append({
                "name": (emp.text or "—").strip(),
                "role": role,
                "role_full": CREW_ROLES.get(role, role),
                "order": int(emp.get("orderNumber", 99)),
            })
        return sorted(members, key=lambda m: m["order"])
    except Exception as exc:
        logger.warning("crew xml: %s", exc)
        return []


# ── leg shaping ──────────────────────────────────────────────────────────────
def _dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _minutes(later, earlier):
    if not later or not earlier:
        return None
    return int(round((later - earlier).total_seconds() / 60))


def build_leg(rec, now):
    std = _dt(rec.get("dateTakeoff"))
    sta = _dt(rec.get("dateLanding"))

    # Actual first, then Aviabit's own estimate, then nothing.
    atd = _dt(rec.get("dateTakeoffReal"))
    ata = _dt(rec.get("dateLandingReal"))
    etd = atd or _dt(rec.get("dateTakeoffCalculation"))
    eta = ata or _dt(rec.get("dateLandingCalculation"))

    if ata:
        state = "completed"
    elif atd:
        state = "airborne"
    elif std and std < now:
        state = "due"          # past its slot, no departure recorded yet
    else:
        state = "scheduled"

    origin = (rec.get("airPortTOCode") or "—").upper()
    dest = (rec.get("airPortLACode") or "—").upper()

    return {
        "id": rec.get("recordID"),
        "pf_id": rec.get("pfRecordId"),
        "flight": rec.get("flight") or "—",
        "origin": origin,
        "destination": dest,
        "origin_name": airport_name(origin),
        "dest_name": airport_name(dest),
        "std": std.isoformat() if std else None,
        "sta": sta.isoformat() if sta else None,
        "etd": etd.isoformat() if etd else None,
        "eta": eta.isoformat() if eta else None,
        "atd": atd.isoformat() if atd else None,
        "ata": ata.isoformat() if ata else None,
        "dep_delay": _minutes(etd, std),
        "arr_delay": _minutes(eta, sta),
        "block_planned": _minutes(sta, std),
        "block_actual": _minutes(ata, atd),
        "state": state,
        "day": std.strftime("%Y-%m-%d") if std else "—",
        "crew": parse_crew(rec.get("crew")),
    }


def build_timetable(records):
    now = datetime.now(timezone.utc)
    by_tail = {}

    for rec in records:
        tail = (rec.get("pln") or "—").strip() or "—"
        entry = by_tail.setdefault(tail, {
            "tail": tail,
            "type": rec.get("plnType") or "",
            "placeholder": not REGISTRATION.match(tail),
            "legs": [],
        })
        entry["legs"].append(build_leg(rec, now))

    tails = []
    for entry in by_tail.values():
        legs = sorted(entry["legs"], key=lambda l: l["std"] or "")
        entry["legs"] = legs

        flown = [l for l in legs if l["state"] == "completed"]
        delays = [l["dep_delay"] for l in flown if l["dep_delay"] is not None]
        upcoming = [l for l in legs if l["state"] in ("scheduled", "due")]
        airborne = next((l for l in legs if l["state"] == "airborne"), None)

        entry["stats"] = {
            "total": len(legs),
            "completed": len(flown),
            "upcoming": len(upcoming),
            "avg_dep_delay": int(round(sum(delays) / len(delays))) if delays else None,
            "on_time_pct": (round(100 * sum(1 for d in delays if d <= 15) / len(delays))
                            if delays else None),
        }
        entry["now_flying"] = airborne
        entry["next_leg"] = upcoming[0] if upcoming else None
        tails.append(entry)

    # Real registrations first, busiest first; placeholder slots last.
    tails.sort(key=lambda t: (t["placeholder"], -len(t["legs"]), t["tail"]))
    return tails


# ── cache ────────────────────────────────────────────────────────────────────
_cache = {"data": None, "ts": 0.0, "error": None}
_lock = threading.Lock()


def get_timetable(force=False):
    with _lock:
        fresh = _cache["data"] is not None and (time.time() - _cache["ts"]) < CACHE_TTL
        if fresh and not force:
            return _cache["data"], _cache["error"], _cache["ts"]

        try:
            records = aviabit.fetch_plan(DAYS_BACK, DAYS_AHEAD)
        except Exception as exc:
            logger.error("fetch_plan crashed: %s", exc, exc_info=True)
            records = []

        if not records:
            error = aviabit.last_error or "Aviabit returned no flights"
            # Keep serving the last good copy rather than blanking the screen.
            if _cache["data"]:
                _cache["error"] = error
                return _cache["data"], error, _cache["ts"]
            _cache.update(data=[], ts=time.time(), error=error)
            return [], error, _cache["ts"]

        _cache.update(data=build_timetable(records), ts=time.time(), error=None)
        return _cache["data"], None, _cache["ts"]


# ── api ──────────────────────────────────────────────────────────────────────
@app.route("/api/timetable")
def api_timetable():
    tails, error, ts = get_timetable()
    return jsonify({
        "tails": tails,
        "error": error,
        "updated": ts,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "window": {"days_back": DAYS_BACK, "days_ahead": DAYS_AHEAD},
    })


@app.route("/api/refresh", methods=["POST", "GET"])
def api_refresh():
    tails, error, ts = get_timetable(force=True)
    return jsonify({"tails": tails, "error": error, "updated": ts})


@app.route("/api/health")
def api_health():
    return jsonify({
        "aviabit_logged_in": aviabit.logged_in,
        "last_error": aviabit.last_error,
        "cached_tails": len(_cache["data"] or []),
        "cache_age": round(time.time() - _cache["ts"]) if _cache["ts"] else None,
    })


@app.route("/manifest.webmanifest")
def manifest():
    """Lets iOS/Android treat the page as an installed app."""
    return jsonify({
        "name": "Skyguard Radar",
        "short_name": "Skyguard",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b1017",
        "theme_color": "#0b1017",
        "orientation": "any",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "any maskable"},
        ],
    })


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False)
