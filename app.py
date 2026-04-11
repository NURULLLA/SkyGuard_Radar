import json
import threading
import time
import logging
import math
import requests
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template
from flask_cors import CORS
from FlightRadar24 import FlightRadar24API
from schedule_service import SkyguardScheduleService
import sqlite3
import logging.handlers
import os

# ─── LOGGING ─────────────────────────────────────────────────────────────────
if not os.path.exists('logs'): os.makedirs('logs')
formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
file_handler = logging.handlers.RotatingFileHandler(
    'logs/skyguard.log', maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    logger.info("✅ config.json загружен")
except Exception as e:
    logger.warning(f"⚠️ config.json не найден ({e}), читаем из ENV переменных")
    config = None

# Поддержка Environment Variables (для Render.com и других облаков)
if config:
    AVIABIT_CREDENTIALS = config["aviabit"]
    TELEGRAM_CONFIG     = config.get("telegram", {})
    AIRCRAFT_CONFIG     = config["aircraft"]
    AIRPORTS            = config["airports"]
    POLL_INTERVAL       = config.get("poll_interval", 30)
    MAX_TRACK_POINTS    = config.get("max_track_points", 100)
else:
    # Читаем из переменных окружения (Render Dashboard → Environment)
    AVIABIT_CREDENTIALS = {
        "username": os.environ.get("AVIABIT_USERNAME", ""),
        "password": os.environ.get("AVIABIT_PASSWORD", ""),
        "base_url": os.environ.get("AVIABIT_URL", "https://ab-web.aviastartu.ru")
    }
    TELEGRAM_CONFIG = {
        "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "chat_id":   os.environ.get("TELEGRAM_CHAT_ID", "")
    }
    AIRCRAFT_CONFIG = {
        "UK75057": {"name": "UK-75057", "color": "#00d4ff", "icao": "UK75057"},
        "UK75058": {"name": "UK-75058", "color": "#ff6b35", "icao": "UK75058"}
    }
    AIRPORTS = {
        "SHJ": {"name": "Sharjah",           "country": "UAE",         "lat": 25.3283, "lon": 55.5172},
        "DXB": {"name": "Dubai",             "country": "UAE",         "lat": 25.2532, "lon": 55.3657},
        "ASM": {"name": "Asmara",            "country": "Eritrea",     "lat": 15.3311, "lon": 38.9103},
        "BOM": {"name": "Mumbai",            "country": "India",       "lat": 19.0896, "lon": 72.8656},
        "URC": {"name": "Urumqi",            "country": "China",       "lat": 43.9071, "lon": 87.4742},
        "SKD": {"name": "Samarkand",         "country": "Uzbekistan",  "lat": 39.7005, "lon": 66.9839},
        "TAS": {"name": "Tashkent",          "country": "Uzbekistan",  "lat": 41.2575, "lon": 69.2812},
        "KBL": {"name": "Kabul",             "country": "Afghanistan", "lat": 34.5658, "lon": 69.2123},
        "NBO": {"name": "Nairobi",           "country": "Kenya",       "lat": -1.3192, "lon": 36.9275},
        "IST": {"name": "Istanbul",          "country": "Turkey",      "lat": 41.2753, "lon": 28.7519},
        "DWC": {"name": "Dubai Al Maktoum",  "country": "UAE",         "lat": 24.8962, "lon": 55.1612},
        "BEY": {"name": "Beirut",            "country": "Lebanon",     "lat": 33.8209, "lon": 35.4884},
        "AMM": {"name": "Amman",             "country": "Jordan",      "lat": 31.7225, "lon": 35.9932},
        "FRU": {"name": "Бишкек (Манас)",   "country": "Кыргызстан",  "lat": 42.8474, "lon": 74.4776},
        "BSZ": {"name": "Бишкек (Манас)",   "country": "Кыргызстан",  "lat": 42.8474, "lon": 74.4776}
    }
    POLL_INTERVAL    = 30
    MAX_TRACK_POINTS = 100

if not AVIABIT_CREDENTIALS.get("username"):
    logger.error("❌ AVIABIT_USERNAME не задан! Установите env переменную на Render.")

AIRCRAFT_REGISTRATIONS = list(AIRCRAFT_CONFIG.keys())
logger.info(f"🛩 Борты: {AIRCRAFT_REGISTRATIONS}")
logger.info(f"🔑 Aviabit user: {AVIABIT_CREDENTIALS.get('username', '???')}")

app = Flask(__name__)
CORS(app)
fr_api = FlightRadar24API()
schedule_service = SkyguardScheduleService(
    username=AVIABIT_CREDENTIALS["username"],
    password=AVIABIT_CREDENTIALS["password"])

# ─── DATABASE ─────────────────────────────────────────────────────────────────
DB_PATH = 'skyguard.db'

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        text TEXT NOT NULL, ts REAL NOT NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS last_positions (
                        reg TEXT PRIMARY KEY,
                        lat REAL NOT NULL, lon REAL NOT NULL,
                        ts  REAL NOT NULL, callsign TEXT)''')
        conn.commit()
    logger.info("🗄 БД инициализирована")

def add_alert(text):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO alerts (text, ts) VALUES (?, ?)", (text, time.time()))
    except Exception as e: logger.error(f"DB add_alert: {e}")

def get_alerts(limit=50):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute("SELECT text, ts FROM alerts ORDER BY ts DESC LIMIT ?", (limit,))
            return [{"text": r[0], "ts": r[1]} for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"DB get_alerts: {e}"); return []

def save_last_position(reg, lat, lon, callsign=None):
    """Сохраняет последнюю известную позицию борта."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO last_positions (reg, lat, lon, ts, callsign) VALUES (?,?,?,?,?)",
                (reg, lat, lon, time.time(), callsign))
    except Exception as e: logger.error(f"DB save_pos: {e}")

def get_last_position(reg):
    """Возвращает последнюю известную позицию борта из БД."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "SELECT lat, lon, ts, callsign FROM last_positions WHERE reg=?", (reg,))
            row = cur.fetchone()
            if row:
                return {"lat": row[0], "lon": row[1], "ts": row[2], "callsign": row[3]}
    except Exception as e: logger.error(f"DB get_pos: {e}")
    return None

init_db()

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def send_telegram(message):
    token = TELEGRAM_CONFIG.get("bot_token")
    chat_id = TELEGRAM_CONFIG.get("chat_id")
    if not token or not chat_id: return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": message}, timeout=5)
    except Exception as e: logger.error(f"Telegram: {e}")

def get_airport_info(iata):
    info = AIRPORTS.get(iata)
    if info:
        return f"{info['name']}, {info['country']} ({iata})"
    return iata

def calculate_bearing(lat1, lon1, lat2, lon2):
    """Азимут из точки 1 в точку 2 (градусы)."""
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dλ = math.radians(lon2 - lon1)
    x = math.sin(dλ) * math.cos(φ2)
    y = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(dλ)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def calculate_route_position(origin_iata, dest_iata, takeoff_iso, landing_iso):
    """Расчётная позиция самолёта по линейному прогрессу рейса (для карты)."""
    if origin_iata not in AIRPORTS or dest_iata not in AIRPORTS:
        return None
    o, d = AIRPORTS[origin_iata], AIRPORTS[dest_iata]
    try:
        t0 = datetime.fromisoformat(takeoff_iso.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(landing_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        total = (t1 - t0).total_seconds()
        if total <= 0: return None
        p = max(0.0, min(1.0, (now - t0).total_seconds() / total))
        return {"lat": o['lat'] + (d['lat'] - o['lat']) * p,
                "lon": o['lon'] + (d['lon'] - o['lon']) * p,
                "progress": p}
    except Exception as e:
        logger.error(f"route_pos error: {e}"); return None

def get_airport_weather(iata, lat, lon):
    """Получает температуру в аэропорту через бесплатный API open-meteo."""
    if not lat or not lon: return None
    now = time.time()
    if iata in weather_cache and (now - weather_cache[iata]["ts"]) < 1800:
        return weather_cache[iata]["temp"]
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            temp = round(resp.json()["current_weather"]["temperature"])
            weather_cache[iata] = {"temp": temp, "ts": now}
            return temp
    except: pass
    return weather_cache.get(iata, {}).get("temp")

# ─── CACHE ────────────────────────────────────────────────────────────────────
data_lock          = threading.Lock()
flight_cache       = {}
weather_cache      = {}
schedule_cache     = {r: {"current": None, "upcoming": []} for r in AIRCRAFT_REGISTRATIONS}
track_history      = {r: [] for r in AIRCRAFT_REGISTRATIONS}
last_schedule_update = 0
notified_delays    = set()

# ─── MAIN POLL ────────────────────────────────────────────────────────────────
def fetch_data():
    global flight_cache, schedule_cache, last_schedule_update, notified_delays
    now_ts = time.time()

    # 1. Расписание — раз в 10 минут
    if now_ts - last_schedule_update > 600:
        try:
            all_plan = schedule_service.get_flight_plan(search_regs=AIRCRAFT_REGISTRATIONS)
            if not all_plan:
                last_schedule_update = now_ts - 540
                logger.warning("🕒 АвиаБит вернул пустоту — продолжаем с FR24")
                # НЕ делаем return — FR24 должен работать независимо!
            now_iso = datetime.now(timezone.utc).isoformat()
            with data_lock:
                for reg in AIRCRAFT_REGISTRATIONS:
                    reg_n = reg.replace("-", "").upper()
                    plane = sorted(
                        [f for f in all_plan if str(f.get("pln","")).replace("-","").upper() == reg_n],
                        key=lambda x: x.get("dateTakeoff", ""))
                    cur, upc = None, []
                    for f in plane:
                        if f.get("dateTakeoff","") <= now_iso <= f.get("dateLanding","") or f.get("status") == 1:
                            cur = f
                        elif f.get("dateTakeoff","") > now_iso:
                            upc.append(f)
                    schedule_cache[reg] = {"current": cur, "upcoming": upc[:5]}
                last_schedule_update = now_ts
                logger.info("✨ Расписание обновлено")
        except Exception as e:
            logger.error(f"Schedule error: {e}")
            last_schedule_update = now_ts - 540

    # 2. Для каждого борта — FR24 + позиция
    found = {}
    for reg in AIRCRAFT_REGISTRATIONS:
        with data_lock:
            sched    = schedule_cache.get(reg, {}).get("current")
            upcoming = schedule_cache.get(reg, {}).get("upcoming", [])

        # FR24 — только для проверки факта полёта
        fr24_found = False
        fr24_alt   = 0
        fr24_callsign = None
        try:
            flights = fr_api.get_flights(registration=reg)
            if flights:
                fl = flights[0]
                fr24_alt      = fl.altitude or 0
                fr24_callsign = fl.callsign
                fr24_found    = True
                # Сохраняем позицию в БД (последняя известная точка)
                if fl.latitude and fl.longitude:
                    save_last_position(reg, fl.latitude, fl.longitude, fl.callsign)
                    with data_lock:
                        track_history[reg].append(
                            {"lat": fl.latitude, "lng": fl.longitude, "ts": time.time()})
                        track_history[reg] = track_history[reg][-MAX_TRACK_POINTS:]
        except Exception as e:
            logger.warning(f"FR24 {reg}: {e}")

        # Аэропорты из расписания
        origin_iata = (sched.get("airPortTOCode") if sched else None) or "—"
        dest_iata   = (sched.get("airPortLACode")  if sched else None) or "—"
        origin_full = get_airport_info(origin_iata)
        dest_full   = get_airport_info(dest_iata)

        # Координаты аэропортов для карты
        origin_coords = ({"lat": AIRPORTS[origin_iata]["lat"], "lon": AIRPORTS[origin_iata]["lon"]}
                         if origin_iata in AIRPORTS else None)
        dest_coords   = ({"lat": AIRPORTS[dest_iata]["lat"],   "lon": AIRPORTS[dest_iata]["lon"]}
                         if dest_iata in AIRPORTS else None)

        # Маршрутный азимут
        route_heading = 0
        if origin_coords and dest_coords:
            route_heading = calculate_bearing(
                origin_coords["lat"], origin_coords["lon"],
                dest_coords["lat"],   dest_coords["lon"])

        # ── Определяем статус и позицию ──────────────────────────────────────
        lat = lon = None
        status        = "offline"
        position_type = None
        route_progress = None
        callsign = fr24_callsign or (sched.get("flight") if sched else "N/A") or "N/A"

        now_iso = datetime.now(timezone.utc).isoformat()

        if fr24_found:
            # FR24 подтвердил борт
            status        = "airborne" if fr24_alt > 100 else "ground"
            position_type = "live"
            # Позиция — расчётная по маршруту (FR24 GPS не используем для отображения)
            if sched and sched.get("dateTakeoff") and sched.get("dateLanding"):
                est = calculate_route_position(origin_iata, dest_iata,
                                               sched["dateTakeoff"], sched["dateLanding"])
                if est:
                    lat, lon = est["lat"], est["lon"]
                    route_progress = est["progress"]
            if lat is None and origin_coords:
                lat, lon = origin_coords["lat"], origin_coords["lon"]

        elif sched:
            t0 = sched.get("dateTakeoff", "")
            t1 = sched.get("dateLanding", "")
            if t0 and t1 and t0 <= now_iso <= t1:
                # По расписанию должен быть в воздухе, но FR24 не нашёл
                est = calculate_route_position(origin_iata, dest_iata, t0, t1)
                if est:
                    lat, lon = est["lat"], est["lon"]
                    route_progress = est["progress"]
                    status        = "airborne"
                    position_type = "estimated"
                else:
                    last_pos = get_last_position(reg)
                    if last_pos: lat, lon = last_pos["lat"], last_pos["lon"]
                    status        = "airborne"
                    position_type = "last_known"
            else:
                # На земле по расписанию
                last_pos = get_last_position(reg)
                if last_pos: lat, lon = last_pos["lat"], last_pos["lon"]
                status        = "ground"
                position_type = "last_known" if last_pos else None
        else:
            last_pos = get_last_position(reg)
            if last_pos: lat, lon = last_pos["lat"], last_pos["lon"]
            status        = "offline"
            position_type = "last_known" if last_pos else None

        # Прогресс маршрута (если ещё не задан)
        if route_progress is None and sched and sched.get("dateTakeoff") and sched.get("dateLanding"):
            est = calculate_route_position(origin_iata, dest_iata,
                                           sched["dateTakeoff"], sched["dateLanding"])
            if est: route_progress = est["progress"]

        # ETA — из расписания
        eta_minutes = None
        if sched and sched.get("dateLanding"):
            try:
                t1 = datetime.fromisoformat(sched["dateLanding"].replace("Z", "+00:00"))
                m  = int((t1 - datetime.now(timezone.utc)).total_seconds() / 60)
                if m > 0: eta_minutes = m
            except: pass

        # Время в воздухе
        duration_mins = 0
        if sched and sched.get("dateTakeoff"):
            try:
                t0 = datetime.fromisoformat(sched["dateTakeoff"].replace("Z", "+00:00"))
                d  = int((datetime.now(timezone.utc) - t0).total_seconds() / 60)
                if d > 0: duration_mins = d
            except: pass

        # Задержка
        delay_minutes = 0
        if status != "airborne" and upcoming:
            nf = upcoming[0]
            try:
                tp = datetime.fromisoformat(nf.get("dateTakeoff","").replace("Z","+00:00"))
                delay = (datetime.now(timezone.utc) - tp).total_seconds() / 60
                if delay > 15:
                    delay_minutes = int(delay)
                    fid = f"{reg}_{nf.get('flight')}_{nf.get('dateTakeoff')}"
                    if fid not in notified_delays:
                        send_telegram(
                            f"⚠️ ЗАДЕРЖКА ВЫЛЕТА\n━━━━━━━━━━━━━━━━━━━━\n"
                            f"✈️ Борт: {AIRCRAFT_CONFIG[reg]['name']}\n"
                            f"🎫 Рейс: {nf.get('flight')}\n"
                            f"⏰ План: {tp.strftime('%H:%M UTC')}\n"
                            f"⏳ Опаздывает на: {delay_minutes} мин\n━━━━━━━━━━━━━━━━━━━━")
                        notified_delays.add(fid)
            except: pass

        # Уведомление о смене статуса (только при live данных)
        with data_lock:
            prev = flight_cache.get(reg)
        status_prev = prev.get("status") if prev else None
        if status_prev and status_prev != status and position_type == "live":
            name    = AIRCRAFT_CONFIG[reg]['name']
            now_dt  = datetime.now(timezone.utc)
            title   = "✈️ ВЗЛЁТ БОРТА" if status == "airborne" else "🛬 ПОСАДКА БОРТА"
            footer  = "🛫 Удачного полета!" if status == "airborne" else "✅ Борт успешно завершил рейс."
            msg = (f"{title}\n━━━━━━━━━━━━━━━━━━━━\n"
                   f"✈️  Борт:       {name}\n"
                   f"🎫  Рейс:       {callsign}\n"
                   f"🛫  Вылет:      {origin_full}\n"
                   f"🛬  Прибытие:   {dest_full}\n"
                   f"📅  Дата:       {now_dt.strftime('%d.%m.%Y')}\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n"
                   f"🕐  Время:      {now_dt.strftime('%H:%M UTC')}\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n{footer}")
            add_alert(f"{title}: {name} ({callsign})")
            send_telegram(msg)
            logger.info(f"Alert: {title} {name}")

        # Следующий вылет: timestamp и рейс (для фронтенд-таймера)
        next_dep_ts  = None
        next_dep_flight = None
        if upcoming:
            try:
                t0 = datetime.fromisoformat(upcoming[0]["dateTakeoff"].replace("Z","+00:00"))
                next_dep_ts     = t0.timestamp()
                next_dep_flight = upcoming[0].get("flight")
            except: pass

        origin_temp = get_airport_weather(origin_iata, origin_coords["lat"], origin_coords["lon"]) if origin_coords else None
        dest_temp   = get_airport_weather(dest_iata, dest_coords["lat"], dest_coords["lon"]) if dest_coords else None

        found[reg] = {
            "registration": reg, "callsign": callsign,
            "latitude": lat, "longitude": lon,
            "altitude": (fr24_alt if fr24_found else 0),
            "speed": 0, "heading": route_heading,
            "vertical_speed": 0,
            "origin": origin_full, "destination": dest_full,
            "origin_iata": origin_iata, "dest_iata": dest_iata,
            "origin_temp": origin_temp, "dest_temp": dest_temp,
            "aircraft_model": "B757",
            "status": status, "position_type": position_type,
            "eta": eta_minutes, "duration": duration_mins,
            "timestamp": time.time(), "delay": delay_minutes,
            "origin_coords": origin_coords, "dest_coords": dest_coords,
            "route_progress": route_progress,
            "next_dep_ts": next_dep_ts, "next_dep_flight": next_dep_flight,
        }

    with data_lock:
        for reg in AIRCRAFT_REGISTRATIONS:
            flight_cache[reg] = found.get(reg)

def get_adaptive_interval():
    """Адаптивный интервал поллинга в зависимости от времени до вылета."""
    min_until = float('inf')
    now = datetime.now(timezone.utc)
    with data_lock:
        for reg in AIRCRAFT_REGISTRATIONS:
            # Проверяем текущий рейс и предстоящие
            cur = schedule_cache.get(reg, {}).get("current")
            upc = schedule_cache.get(reg, {}).get("upcoming", [])
            flights_to_check = ([cur] if cur else []) + upc
            for f in flights_to_check:
                if not f: continue
                t0_str = f.get("dateTakeoff", "")
                if not t0_str: continue
                try:
                    t0 = datetime.fromisoformat(t0_str.replace("Z", "+00:00"))
                    mins = (t0 - now).total_seconds() / 60
                    # Активная зона: за 60 мин до и 10 мин после вылета
                    if -10 <= mins <= 60:
                        min_until = min(min_until, mins)
                except: pass
    if min_until <= 10:   return 5   # каждые 5 сек
    if min_until <= 30:   return 10  # каждые 10 сек
    if min_until <= 60:   return 15  # каждые 15 сек
    return POLL_INTERVAL             # штатный интервал

def background_poll():
    while True:
        fetch_data()
        interval = get_adaptive_interval()
        time.sleep(interval)

# ─── API ──────────────────────────────────────────────────────────────────────
@app.route("/api/flights")
def api_flights():
    result = []
    with data_lock:
        for reg in AIRCRAFT_REGISTRATIONS:
            data, sched = flight_cache.get(reg), schedule_cache.get(reg, {})
            sched_mapped = {"current": None, "upcoming": []}
            if sched.get("current"):
                c = dict(sched["current"])
                c["origin_full"] = get_airport_info(c.get("airPortTOCode"))
                c["dest_full"]   = get_airport_info(c.get("airPortLACode"))
                sched_mapped["current"] = c
            for u in sched.get("upcoming", []):
                item = dict(u)
                item["origin_full"] = get_airport_info(item.get("airPortTOCode"))
                item["dest_full"]   = get_airport_info(item.get("airPortLACode"))
                sched_mapped["upcoming"].append(item)

            if data:
                entry = dict(data)
            else:
                last_pos = get_last_position(reg)
                # Берём ближайший предстоящий рейс для отображения маршрута
                next_f = sched_mapped["upcoming"][0] if sched_mapped.get("upcoming") else None
                entry = {
                    "registration": reg, "status": "offline",
                    "callsign": (sched.get("current") or {}).get("flight") or (next_f or {}).get("flight") or "N/A",
                    "latitude":  last_pos["lat"] if last_pos else None,
                    "longitude": last_pos["lon"] if last_pos else None,
                    "heading": 0, "altitude": 0, "speed": 0, "vertical_speed": 0,
                    "origin":      (next_f or {}).get("origin_full") or "—",
                    "destination": (next_f or {}).get("dest_full")   or "—",
                    "delay": 0, "eta": None, "duration": 0,
                    "position_type": "last_known" if last_pos else None,
                    "timestamp": last_pos["ts"] if last_pos else time.time(),
                    "origin_coords": None, "dest_coords": None, "route_progress": None,
                    "origin_temp": None, "dest_temp": None,
                    "origin_iata": "—", "dest_iata": "—",
                    "next_dep_ts": None, "next_dep_flight": None,
                }
            entry.update({"display": AIRCRAFT_CONFIG[reg],
                          "track": track_history.get(reg, []),
                          "schedule": sched_mapped})
            result.append(entry)
    return jsonify(result)

@app.route("/api/alerts")
def api_alerts():
    return jsonify(get_alerts(50))

@app.route("/api/status")
def api_status():
    """Диагностический endpoint — проверяет подключения."""
    aviabit_ok = False
    aviabit_msg = ""
    try:
        r = requests.get(
            f"{AVIABIT_CREDENTIALS.get('base_url','https://ab-web.aviastartu.ru')}/api/auth",
            timeout=5, verify=False)
        aviabit_msg = f"HTTP {r.status_code}"
        aviabit_ok = r.status_code in (200, 401, 403)
    except Exception as e:
        aviabit_msg = str(e)

    fr24_ok = False
    fr24_msg = ""
    try:
        test = fr_api.get_flights(registration="UK75057")
        fr24_ok = True
        fr24_msg = f"OK, нашёл {len(test)} рейс(ов)"
    except Exception as e:
        fr24_msg = str(e)

    return jsonify({
        "aviabit": {
            "url": AVIABIT_CREDENTIALS.get("base_url"),
            "username": AVIABIT_CREDENTIALS.get("username") or "❌ НЕ ЗАДАН",
            "reachable": aviabit_ok,
            "message": aviabit_msg,
            "logged_in": schedule_service.logged_in
        },
        "fr24": {
            "reachable": fr24_ok,
            "message": fr24_msg
        },
        "schedule_last_update": last_schedule_update,
        "aircraft": AIRCRAFT_REGISTRATIONS,
        "config_source": "config.json" if (config is not None) else "environment_variables"
    })

@app.route("/")
def index():
    return render_template("index.html")

# ─── ЗАПУСК ФОНОВЫХ ПОТОКОВ ───────────────────────────────────────────────────
# КРИТИЧНО: запускаем на уровне модуля — иначе gunicorn их не выполнит!
# (gunicorn импортирует app как модуль и не выполняет if __name__ == "__main__")
_initial_thread = threading.Thread(target=fetch_data, daemon=True)
_initial_thread.start()
_poll_thread = threading.Thread(target=background_poll, daemon=True)
_poll_thread.start()
logger.info("🚀 Фоновые потоки запущены (gunicorn-совместимо)")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, port=port, host="0.0.0.0")
