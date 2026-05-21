import json
import threading
import time
import logging
import math
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template, request
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
except Exception:
    config = {}
    logger.info("⚠️ config.json не найден — используем переменные окружения")

_av = config.get("aviabit", {})
AVIABIT_CREDENTIALS = {
    "username": os.environ.get("AVIABIT_USERNAME") or _av.get("username", ""),
    "password": os.environ.get("AVIABIT_PASSWORD") or _av.get("password", ""),
}

TELEGRAM_CONFIG = {
    "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN") or config.get("telegram", {}).get("bot_token"),
    "chat_id":   os.environ.get("TELEGRAM_CHAT_ID")   or config.get("telegram", {}).get("chat_id"),
}

_default_aircraft = {
    "UK75057": {"name": "UK-75057", "color": "#00d4ff", "icao": "UK75057"},
    "UK75058": {"name": "UK-75058", "color": "#ff6b35", "icao": "UK75058"},
}
_default_airports = {
    "SHJ": {"name": "Sharjah",        "country": "UAE",         "lat": 25.3283, "lon": 55.5172},
    "DXB": {"name": "Dubai",           "country": "UAE",         "lat": 25.2532, "lon": 55.3657},
    "DWC": {"name": "Dubai Al Maktoum","country": "UAE",         "lat": 24.8962, "lon": 55.1612},
    "TAS": {"name": "Tashkent",        "country": "Uzbekistan",  "lat": 41.2575, "lon": 69.2812},
    "SKD": {"name": "Samarkand",       "country": "Uzbekistan",  "lat": 39.7005, "lon": 66.9839},
    "BSZ": {"name": "Бишкек (Манас)",  "country": "Кыргызстан", "lat": 42.8474, "lon": 74.4776},
    "KBL": {"name": "Kabul",           "country": "Afghanistan", "lat": 34.5658, "lon": 69.2123},
    "IST": {"name": "Istanbul",        "country": "Turkey",      "lat": 41.2753, "lon": 28.7519},
    "NBO": {"name": "Nairobi",         "country": "Kenya",       "lat": -1.3192, "lon": 36.9275},
    "BOM": {"name": "Mumbai",          "country": "India",       "lat": 19.0896, "lon": 72.8656},
    "ASM": {"name": "Asmara",          "country": "Eritrea",     "lat": 15.3311, "lon": 38.9103},
}

AIRCRAFT_CONFIG        = config.get("aircraft") or _default_aircraft
AIRCRAFT_REGISTRATIONS = list(AIRCRAFT_CONFIG.keys())
AIRPORTS               = config.get("airports") or _default_airports
POLL_INTERVAL          = config.get("poll_interval", 30)
MAX_TRACK_POINTS       = config.get("max_track_points", 100)

if not AVIABIT_CREDENTIALS["username"]:
    logger.error("❌ AVIABIT_USERNAME не задан"); exit(1)

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
        conn.execute('''CREATE TABLE IF NOT EXISTS notified_delays (
                        flight_id TEXT PRIMARY KEY, ts REAL NOT NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS flight_status_log (
                        reg TEXT PRIMARY KEY, status TEXT NOT NULL, ts REAL NOT NULL)''')
        conn.commit()
    logger.info("🗄 БД инициализирована")

def db_load_notified_delays():
    """Загружает уже отправленные уведомления о задержках из БД."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute("SELECT flight_id FROM notified_delays")
            return {row[0] for row in cur.fetchall()}
    except Exception as e:
        logger.error(f"DB load_notified_delays: {e}")
        return set()

def db_save_notified_delay(flight_id):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR IGNORE INTO notified_delays (flight_id, ts) VALUES (?, ?)",
                         (flight_id, time.time()))
    except Exception as e:
        logger.error(f"DB save_notified_delay: {e}")

def db_load_flight_status():
    """Загружает последний известный статус бортов из БД."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute("SELECT reg, status FROM flight_status_log")
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        logger.error(f"DB load_flight_status: {e}")
        return {}

def db_save_flight_status(reg, status):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO flight_status_log (reg, status, ts) VALUES (?, ?, ?)",
                         (reg, status, time.time()))
    except Exception as e:
        logger.error(f"DB save_flight_status: {e}")

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
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO last_positions (reg, lat, lon, ts, callsign) VALUES (?,?,?,?,?)",
                (reg, lat, lon, time.time(), callsign))
    except Exception as e: logger.error(f"DB save_pos: {e}")

def get_last_position(reg):
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

# ─── CACHE ─── (загружаем персистентное состояние из БД) ──────────────────────
notified_delays      = db_load_notified_delays()
last_notified_status = db_load_flight_status()
logger.info(f"📋 Загружено из БД: {len(notified_delays)} задержек, "
            f"{len(last_notified_status)} статусов бортов")

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram(message):
    token   = TELEGRAM_CONFIG.get("bot_token")
    chat_id = TELEGRAM_CONFIG.get("chat_id")
    if not token or not chat_id: return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": message}, timeout=5)
    except Exception as e: logger.error(f"Telegram: {e}")

# ─── AIRPORT HELPERS ─────────────────────────────────────────────────────────
# Fallback для аэропортов не в config.json
_AIRPORT_FALLBACK = {
    # Азия — Южная/Юго-Восточная
    'DAC': ('Дакка',              'Bangladesh'),
    'CGP': ('Читтагонг',          'Bangladesh'),
    'ZYL': ('Силхет',             'Bangladesh'),
    'CMB': ('Коломбо',            'Sri Lanka'),
    'BOM': ('Мумбаи',             'India'),
    'DEL': ('Дели',               'India'),
    'CCU': ('Калькутта',          'India'),
    'MAA': ('Ченнаи',             'India'),
    'HYD': ('Хайдарабад',         'India'),
    'BLR': ('Бангалор',           'India'),
    'AMD': ('Ахмедабад',          'India'),
    'COK': ('Кочи',               'India'),
    'KHI': ('Карачи',             'Pakistan'),
    'LHE': ('Лахор',              'Pakistan'),
    'ISB': ('Исламабад',          'Pakistan'),
    'PEW': ('Пешавар',            'Pakistan'),
    'KBL': ('Кабул',              'Afghanistan'),
    'HKG': ('Гонконг',            'Hong Kong'),
    'SIN': ('Сингапур',           'Singapore'),
    'KUL': ('Куала-Лумпур',       'Malaysia'),
    'BKK': ('Бангкок',            'Thailand'),
    'CGK': ('Джакарта',           'Indonesia'),
    'MNL': ('Манила',             'Philippines'),
    # Восточная Азия
    'ICN': ('Сеул',               'South Korea'),
    'NRT': ('Токио',              'Japan'),
    'HND': ('Токио (Ханеда)',      'Japan'),
    'PEK': ('Пекин',              'China'),
    'PVG': ('Шанхай',             'China'),
    'CAN': ('Гуанчжоу',           'China'),
    'CTU': ('Чэнду',              'China'),
    'URC': ('Урумчи',             'China'),
    # Ближний Восток
    'DXB': ('Дубай',              'UAE'),
    'DWC': ('Дубай (Аль-Мактум)', 'UAE'),
    'AUH': ('Абу-Даби',           'UAE'),
    'SHJ': ('Шарджа',             'UAE'),
    'RKT': ('Рас-эль-Хайма',      'UAE'),
    'FJR': ('Фуджейра',           'UAE'),
    'MCT': ('Маскат',             'Oman'),
    'SLL': ('Салала',             'Oman'),
    'BAH': ('Манама',             'Bahrain'),
    'DOH': ('Доха',               'Qatar'),
    'KWI': ('Кувейт',             'Kuwait'),
    'RUH': ('Эр-Рияд',            'Saudi Arabia'),
    'JED': ('Джидда',             'Saudi Arabia'),
    'DMM': ('Даммам',             'Saudi Arabia'),
    'MED': ('Медина',             'Saudi Arabia'),
    'ADE': ('Аден',               'Yemen'),
    'SAH': ('Сана',               'Yemen'),
    'BGW': ('Багдад',             'Iraq'),
    'BSR': ('Басра',              'Iraq'),
    'ERB': ('Эрбиль',             'Iraq'),
    'AMM': ('Амман',              'Jordan'),
    'BEY': ('Бейрут',             'Lebanon'),
    'DAM': ('Дамаск',             'Syria'),
    'CAI': ('Каир',               'Egypt'),
    'HRG': ('Хургада',            'Egypt'),
    'SSH': ('Шарм-эш-Шейх',       'Egypt'),
    'IST': ('Стамбул',            'Turkey'),
    'SAW': ('Стамбул (Сабиха)',    'Turkey'),
    'ESB': ('Анкара',             'Turkey'),
    'AYT': ('Анталья',            'Turkey'),
    # Центральная Азия / СНГ
    'TAS': ('Ташкент',            'Uzbekistan'),
    'SKD': ('Самарканд',          'Uzbekistan'),
    'BHK': ('Бухара',             'Uzbekistan'),
    'FEG': ('Фергана',            'Uzbekistan'),
    'NVI': ('Навои',              'Uzbekistan'),
    'UGC': ('Ургенч',             'Uzbekistan'),
    'ALA': ('Алматы',             'Kazakhstan'),
    'TSE': ('Астана',             'Kazakhstan'),
    'OSS': ('Ош',                 'Kyrgyzstan'),
    'FRU': ('Бишкек',             'Kyrgyzstan'),
    'DYU': ('Душанбе',            'Tajikistan'),
    'ASB': ('Ашхабад',            'Turkmenistan'),
    'GYD': ('Баку',               'Azerbaijan'),
    'EVN': ('Ереван',             'Armenia'),
    'LWN': ('Гюмри',              'Armenia'),
    'TBS': ('Тбилиси',            'Georgia'),
    # Африка
    'NBO': ('Найроби',            'Kenya'),
    'MBA': ('Момбаса',            'Kenya'),
    'EBB': ('Энтеббе',            'Uganda'),
    'ADD': ('Аддис-Абеба',        'Ethiopia'),
    'DAR': ('Дар-эс-Салам',       'Tanzania'),
    'JNB': ('Йоханнесбург',       'South Africa'),
    'CPT': ('Кейптаун',           'South Africa'),
    'LOS': ('Лагос',              'Nigeria'),
    'ABV': ('Абуджа',             'Nigeria'),
    'ACC': ('Аккра',              'Ghana'),
    'CMN': ('Касабланка',         'Morocco'),
    'CAI': ('Каир',               'Egypt'),
    'ALG': ('Алжир',              'Algeria'),
    'TUN': ('Тунис',              'Tunisia'),
    'TIP': ('Триполи',            'Libya'),
    # Россия / Европа
    'SVO': ('Москва (Шереметьево)','Russia'),
    'DME': ('Москва (Домодедово)', 'Russia'),
    'VKO': ('Москва (Внуково)',    'Russia'),
    'LED': ('Санкт-Петербург',    'Russia'),
    'SVX': ('Екатеринбург',       'Russia'),
    'OVB': ('Новосибирск',        'Russia'),
    'KJA': ('Красноярск',         'Russia'),
    'IKT': ('Иркутск',            'Russia'),
    'FCO': ('Рим',                'Italy'),
    'MXP': ('Милан',              'Italy'),
    'CDG': ('Париж',              'France'),
    'LHR': ('Лондон',             'UK'),
    'AMS': ('Амстердам',          'Netherlands'),
    'FRA': ('Франкфурт',          'Germany'),
    'MUC': ('Мюнхен',             'Germany'),
    'VIE': ('Вена',               'Austria'),
    'ZRH': ('Цюрих',              'Switzerland'),
    'WAW': ('Варшава',            'Poland'),
    'PRG': ('Прага',              'Czech Republic'),
    'BUD': ('Будапешт',           'Hungary'),
    'KIV': ('Кишинёв',            'Moldova'),
    'RIX': ('Рига',               'Latvia'),
    'TLL': ('Таллин',             'Estonia'),
    'VNO': ('Вильнюс',            'Lithuania'),
}

def get_airport_info(iata):
    if not iata or iata in ('—', ''):
        return '—'
    info = AIRPORTS.get(iata)
    if info:
        return f"{info['name']}, {info.get('country', '')} ({iata})".strip(", ")
    fb = _AIRPORT_FALLBACK.get(iata.upper())
    if fb:
        return f"{fb[0]}, {fb[1]} ({iata})"
    return iata

def calculate_bearing(lat1, lon1, lat2, lon2):
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dλ = math.radians(lon2 - lon1)
    x = math.sin(dλ) * math.cos(φ2)
    y = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(dλ)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def calculate_route_position(origin_iata, dest_iata, takeoff_iso, landing_iso):
    if origin_iata not in AIRPORTS or dest_iata not in AIRPORTS:
        return None
    o, d = AIRPORTS[origin_iata], AIRPORTS[dest_iata]
    try:
        t0    = datetime.fromisoformat(takeoff_iso.replace("Z", "+00:00"))
        t1    = datetime.fromisoformat(landing_iso.replace("Z", "+00:00"))
        now   = datetime.now(timezone.utc)
        total = (t1 - t0).total_seconds()
        if total <= 0: return None
        p = max(0.0, min(1.0, (now - t0).total_seconds() / total))
        return {"lat": o['lat'] + (d['lat'] - o['lat']) * p,
                "lon": o['lon'] + (d['lon'] - o['lon']) * p,
                "progress": p}
    except Exception as e:
        logger.error(f"route_pos error: {e}"); return None

# ─── METAR / NOTAM ────────────────────────────────────────────────────────────
# Маппинг IATA → ICAO для aviationweather.gov
IATA_TO_ICAO = {
    # ОАЭ
    'DXB': 'OMDB', 'AUH': 'OMAA', 'SHJ': 'OMSJ', 'AAN': 'OMAL',
    'FJR': 'OMFJ', 'DWC': 'OMDW', 'RKT': 'OMRK',
    # Узбекистан
    'TAS': 'UTTT', 'SKD': 'UTSS', 'BHK': 'UTSB', 'UGC': 'UTNU',
    'NVI': 'UTSA', 'KSQ': 'UTSL', 'FEG': 'UTKF', 'NCU': 'UTNN',
    'TMZ': 'UTAT', 'AFS': 'UTSF',
    # Казахстан
    'ALA': 'UAAA', 'TSE': 'UACC', 'GUW': 'UARG', 'AKX': 'UATT',
    'SCO': 'UATE', 'URA': 'UARR',
    # Азербайджан
    'GYD': 'UBBB',
    # Пакистан
    'KHI': 'OPKC', 'LHE': 'OPLA', 'ISB': 'OPIS', 'MUX': 'OPMT',
    'SKT': 'OPST', 'PEW': 'OPPS', 'PZH': 'OPPZ', 'RYK': 'OPRK',
    # Россия
    'SVO': 'UUEE', 'DME': 'UUDD', 'VKO': 'UUWW', 'LED': 'ULLI',
    'SVX': 'USSS', 'OVB': 'UNNT', 'KJA': 'UNKL', 'IKT': 'UIII',
    # Европа
    'LHR': 'EGLL', 'CDG': 'LFPG', 'AMS': 'EHAM', 'FRA': 'EDDF',
    'IST': 'LTFM', 'SAW': 'LTFJ', 'ESB': 'LTAC',
    # СНГ
    'MSQ': 'UMMS', 'KBP': 'UKBB', 'IEV': 'UKKK',
    # Индия
    'DEL': 'VIDP', 'BOM': 'VABB', 'HYD': 'VOHS', 'MAA': 'VOMM', 'CCU': 'VECC',
    # Мальдивы
    'MLE': 'VRMM',
    # Турция
    'ANK': 'LTAC', 'AYT': 'LTAI',
}

metar_cache   = {}
notam_cache   = {}
history_cache = {}
fr24_last_ok  = 0

def get_icao(iata):
    if not iata or iata in ('—', ''):
        return None
    icao = AIRPORTS.get(iata, {}).get('icao')
    return icao or IATA_TO_ICAO.get(iata.upper())

def get_metar(iata):
    """METAR с кешем 30 мин. Возвращает dict или None."""
    icao = get_icao(iata)
    if not icao:
        return None
    now = time.time()
    cached = metar_cache.get(icao)
    if cached and (now - cached['ts']) < 1800:
        return cached['data']
    try:
        resp = requests.get(
            f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json",
            timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                m = data[0]
                result = {
                    'raw':        m.get('rawOb', ''),
                    'temp':       m.get('temp'),
                    'dewp':       m.get('dewp'),
                    'wind_dir':   m.get('wdir'),
                    'wind_speed': m.get('wspd'),
                    'wind_gust':  m.get('wgst'),
                    'visibility': m.get('visib'),
                    'wx':         m.get('wxString', '') or m.get('presentWx', '') or '',
                    'category':   m.get('flightCategory', ''),
                    'clouds':     m.get('clouds', []),
                    'time':       m.get('reportTime', ''),
                    'altimeter':  m.get('altim'),
                }
                metar_cache[icao] = {'data': result, 'ts': now}
                return result
    except Exception as e:
        logger.warning(f"METAR {icao}: {e}")
    return (cached or {}).get('data')

def get_notams(iata):
    """NOTAMы с кешем 1 час."""
    icao = get_icao(iata)
    if not icao:
        return []
    now = time.time()
    cached = notam_cache.get(icao)
    if cached and (now - cached['ts']) < 3600:
        return cached['data']
    try:
        resp = requests.get(
            f"https://aviationweather.gov/api/data/notam?ids={icao}&format=json",
            timeout=8)
        if resp.status_code == 200:
            data   = resp.json()
            notams = []
            for n in data[:20]:
                text = (n.get('traditionalMessage', '') or
                        n.get('icaoMessage', '') or '')[:400]
                notams.append({
                    'id':    n.get('notamID', ''),
                    'text':  text,
                    'type':  n.get('classification', ''),
                    'start': n.get('startDate', ''),
                    'end':   n.get('endDate', ''),
                })
            notam_cache[icao] = {'data': notams, 'ts': now}
            return notams
    except Exception as e:
        logger.warning(f"NOTAM {icao}: {e}")
    return (cached or {}).get('data', [])

# ─── WEATHER FALLBACK (open-meteo) ────────────────────────────────────────────
weather_cache = {}

def get_airport_weather(iata, lat, lon):
    if not lat or not lon: return None
    now = time.time()
    if iata in weather_cache and (now - weather_cache[iata]["ts"]) < 1800:
        return weather_cache[iata]["temp"]
    try:
        url  = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            temp = round(resp.json()["current_weather"]["temperature"])
            weather_cache[iata] = {"temp": temp, "ts": now}
            return temp
    except: pass
    return weather_cache.get(iata, {}).get("temp")

# ─── CACHE ────────────────────────────────────────────────────────────────────
data_lock             = threading.Lock()
flight_cache          = {}
schedule_cache        = {r: {"current": None, "upcoming": []} for r in AIRCRAFT_REGISTRATIONS}
track_history         = {r: [] for r in AIRCRAFT_REGISTRATIONS}
last_schedule_update  = 0
# notified_delays и last_notified_status загружаются из БД выше при init_db()

# ─── MAIN POLL ────────────────────────────────────────────────────────────────
def fetch_data():
    global flight_cache, schedule_cache, last_schedule_update, fr24_last_ok
    now_ts = time.time()

    # 1. Расписание — раз в 10 минут
    if now_ts - last_schedule_update > 600:
        try:
            all_plan = schedule_service.get_flight_plan(search_regs=AIRCRAFT_REGISTRATIONS)
            if not all_plan:
                last_schedule_update = now_ts - 540
                logger.warning("🕒 АвиаБит вернул пустоту")
            now_iso = datetime.now(timezone.utc).isoformat()
            with data_lock:
                for reg in AIRCRAFT_REGISTRATIONS:
                    reg_n = reg.replace("-", "").upper()
                    plane = sorted(
                        [f for f in all_plan if str(f.get("pln", "")).replace("-", "").upper() == reg_n],
                        key=lambda x: x.get("dateTakeoff", ""))
                    cur, upc = None, []
                    for f in plane:
                        if f.get("dateTakeoff", "") <= now_iso <= f.get("dateLanding", "") or f.get("status") == 1:
                            cur = f
                        elif f.get("dateTakeoff", "") > now_iso:
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

        fr24_found = fr24_alt = fr24_gs = False
        fr24_on_ground = False
        fr24_callsign  = None
        try:
            flights = fr_api.get_flights(registration=reg)
            if flights:
                fl             = flights[0]
                fr24_alt       = fl.altitude or 0
                fr24_gs        = fl.ground_speed or 0
                fr24_on_ground = getattr(fl, 'on_ground', 0) == 1
                fr24_callsign  = fl.callsign
                fr24_found     = True
                fr24_last_ok   = time.time()
                if fl.latitude and fl.longitude:
                    save_last_position(reg, fl.latitude, fl.longitude, fl.callsign)
                    with data_lock:
                        track_history[reg].append({"lat": fl.latitude, "lng": fl.longitude, "ts": time.time()})
                        track_history[reg] = track_history[reg][-MAX_TRACK_POINTS:]
        except Exception as e:
            logger.warning(f"FR24 {reg}: {e}")

        origin_iata = (sched.get("airPortTOCode") if sched else None) or "—"
        dest_iata   = (sched.get("airPortLACode")  if sched else None) or "—"
        origin_full = get_airport_info(origin_iata)
        dest_full   = get_airport_info(dest_iata)

        origin_coords = ({"lat": AIRPORTS[origin_iata]["lat"], "lon": AIRPORTS[origin_iata]["lon"]}
                         if origin_iata in AIRPORTS else None)
        dest_coords   = ({"lat": AIRPORTS[dest_iata]["lat"],   "lon": AIRPORTS[dest_iata]["lon"]}
                         if dest_iata in AIRPORTS else None)

        route_heading = 0
        if origin_coords and dest_coords:
            route_heading = calculate_bearing(
                origin_coords["lat"], origin_coords["lon"],
                dest_coords["lat"],   dest_coords["lon"])

        lat = lon = None
        status        = "offline"
        position_type = None
        route_progress = None
        callsign = fr24_callsign or (sched.get("flight") if sched else "N/A") or "N/A"
        now_iso  = datetime.now(timezone.utc).isoformat()

        if fr24_found:
            status        = "ground" if fr24_on_ground else "airborne"
            if status == "ground" and fr24_gs > 50: status = "airborne"
            position_type = "live"
            if sched and sched.get("dateTakeoff") and sched.get("dateLanding"):
                est = calculate_route_position(origin_iata, dest_iata,
                                               sched["dateTakeoff"], sched["dateLanding"])
                if est:
                    lat, lon       = est["lat"], est["lon"]
                    route_progress = est["progress"]
            if lat is None and origin_coords:
                lat, lon = origin_coords["lat"], origin_coords["lon"]
        elif sched:
            t0 = sched.get("dateTakeoff", "")
            t1 = sched.get("dateLanding", "")
            if t0 and t1 and t0 <= now_iso <= t1:
                est = calculate_route_position(origin_iata, dest_iata, t0, t1)
                if est:
                    lat, lon       = est["lat"], est["lon"]
                    route_progress = est["progress"]
                    status         = "airborne"
                    position_type  = "estimated"
                else:
                    last_pos = get_last_position(reg)
                    if last_pos: lat, lon = last_pos["lat"], last_pos["lon"]
                    status        = "airborne"
                    position_type = "last_known"
            else:
                last_pos = get_last_position(reg)
                if last_pos: lat, lon = last_pos["lat"], last_pos["lon"]
                status        = "ground"
                position_type = "last_known" if last_pos else None
        else:
            last_pos = get_last_position(reg)
            if last_pos: lat, lon = last_pos["lat"], last_pos["lon"]
            status        = "offline"
            position_type = "last_known" if last_pos else None

        if route_progress is None and sched and sched.get("dateTakeoff") and sched.get("dateLanding"):
            est = calculate_route_position(origin_iata, dest_iata,
                                           sched["dateTakeoff"], sched["dateLanding"])
            if est: route_progress = est["progress"]

        eta_minutes = None
        if sched and sched.get("dateLanding"):
            try:
                t1 = datetime.fromisoformat(sched["dateLanding"].replace("Z", "+00:00"))
                m  = int((t1 - datetime.now(timezone.utc)).total_seconds() / 60)
                if m > 0: eta_minutes = m
            except: pass

        duration_mins = 0
        if sched and sched.get("dateTakeoff"):
            try:
                t0 = datetime.fromisoformat(sched["dateTakeoff"].replace("Z", "+00:00"))
                d  = int((datetime.now(timezone.utc) - t0).total_seconds() / 60)
                if d > 0: duration_mins = d
            except: pass

        delay_minutes = 0
        if status != "airborne" and upcoming:
            nf = upcoming[0]
            try:
                tp    = datetime.fromisoformat(nf.get("dateTakeoff", "").replace("Z", "+00:00"))
                delay = (datetime.now(timezone.utc) - tp).total_seconds() / 60
                if delay > 15:
                    delay_minutes = int(delay)
                    fid = f"{reg}_{nf.get('flight')}_{nf.get('dateTakeoff')}"
                    if fid not in notified_delays:
                        # Плановая продолжительность рейса для сообщения о задержке
                        d_dur = ""
                        d_eta = ""
                        try:
                            t1n = datetime.fromisoformat(nf.get("dateLanding","").replace("Z","+00:00"))
                            dm = int((t1n - tp).total_seconds() / 60)
                            dh, dmin = divmod(dm, 60)
                            d_dur = f"{dh}ч {dmin:02d}мин"
                            d_eta = t1n.strftime("%H:%M UTC")
                        except: pass
                        dur_line_d = f"⏱  Длит. рейса: {d_dur}\n" if d_dur else ""
                        eta_line_d = f"🏁  Прибытие:   {d_eta}\n" if d_eta else ""
                        nf_orig = get_airport_info(nf.get("airPortTOCode") or "—")
                        nf_dest = get_airport_info(nf.get("airPortLACode") or "—")
                        send_telegram(
                            f"⚠️ ЗАДЕРЖКА ВЫЛЕТА\n━━━━━━━━━━━━━━━━━━━━\n"
                            f"✈️  Борт:       {AIRCRAFT_CONFIG[reg]['name']}\n"
                            f"🎫  Рейс:       {nf.get('flight')}\n"
                            f"🛫  Вылет:      {nf_orig}\n"
                            f"🛬  Прибытие:   {nf_dest}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏰  План вылета: {tp.strftime('%H:%M UTC')}\n"
                            f"⏳  Опаздывает: {delay_minutes} мин\n"
                            f"{dur_line_d}{eta_line_d}"
                            f"━━━━━━━━━━━━━━━━━━━━")
                        with data_lock:
                            notified_delays.add(fid)
                        db_save_notified_delay(fid)
            except: pass

        with data_lock:
            prev_notified = last_notified_status.get(reg)
        # Уведомляем при любом источнике данных (live FR24 или estimated Aviabit),
        # но не при offline — чтобы не было ложных срабатываний при потере связи
        can_notify = status in ("airborne", "ground") and position_type in ("live", "estimated")
        if can_notify and prev_notified and prev_notified != status:
            name   = AIRCRAFT_CONFIG[reg]['name']
            now_dt = datetime.now(timezone.utc)
            title  = "✈️ ВЗЛЁТ БОРТА" if status == "airborne" else "🛬 ПОСАДКА БОРТА"
            footer = "🛫 Удачного полета!" if status == "airborne" else "✅ Борт успешно завершил рейс."

            # Вычисляем плановую продолжительность рейса
            planned_dur_str = ""
            eta_str = ""
            actual_dur_str = ""
            if sched:
                try:
                    t0_plan = datetime.fromisoformat(sched["dateTakeoff"].replace("Z", "+00:00"))
                    t1_plan = datetime.fromisoformat(sched["dateLanding"].replace("Z", "+00:00"))
                    plan_dur = int((t1_plan - t0_plan).total_seconds() / 60)
                    ph, pm = divmod(plan_dur, 60)
                    planned_dur_str = f"{ph}ч {pm:02d}мин"
                except: pass
                # Расчётное время прибытия (берём dateLandingCalculation если есть)
                try:
                    eta_raw = sched.get("dateLandingCalculation") or sched.get("dateLanding")
                    if eta_raw:
                        eta_dt = datetime.fromisoformat(eta_raw.replace("Z", "+00:00"))
                        eta_str = eta_dt.strftime("%H:%M UTC")
                except: pass
                # Фактическое время в полёте (для посадки)
                try:
                    t0_real = sched.get("dateTakeoffReal") or sched.get("dateTakeoff")
                    if t0_real:
                        real_dt = datetime.fromisoformat(t0_real.replace("Z", "+00:00"))
                        actual_m = int((now_dt - real_dt).total_seconds() / 60)
                        if 0 < actual_m < 1440:
                            ah, am = divmod(actual_m, 60)
                            actual_dur_str = f"{ah}ч {am:02d}мин"
                except: pass

            if status == "airborne":
                dur_line  = f"⏱  Длит. рейса: {planned_dur_str}\n" if planned_dur_str else ""
                eta_line  = f"🏁  Прибытие:   {eta_str}\n"         if eta_str        else ""
            else:
                dur_line  = f"⏱  В полёте:   {actual_dur_str}\n"   if actual_dur_str else ""
                eta_line  = ""

            msg = (f"{title}\n━━━━━━━━━━━━━━━━━━━━\n"
                   f"✈️  Борт:       {name}\n"
                   f"🎫  Рейс:       {callsign}\n"
                   f"🛫  Вылет:      {origin_full}\n"
                   f"🛬  Прибытие:   {dest_full}\n"
                   f"📅  Дата:       {now_dt.strftime('%d.%m.%Y')}\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n"
                   f"🕐  Время:      {now_dt.strftime('%H:%M UTC')}\n"
                   f"{dur_line}{eta_line}"
                   f"━━━━━━━━━━━━━━━━━━━━\n{footer}")
            add_alert(f"{title}: {name} ({callsign})")
            send_telegram(msg)
            logger.info(f"🔔 ALERT: {title} {name} ({prev_notified} → {status})")

        if status in ("airborne", "ground"):
            with data_lock:
                if last_notified_status.get(reg) != status:
                    last_notified_status[reg] = status
                    db_save_flight_status(reg, status)

        next_dep_ts = next_dep_flight = None
        if upcoming:
            try:
                t0              = datetime.fromisoformat(upcoming[0]["dateTakeoff"].replace("Z", "+00:00"))
                next_dep_ts     = t0.timestamp()
                next_dep_flight = upcoming[0].get("flight")
            except: pass

        # METAR → температура, если недоступно — open-meteo
        origin_metar = get_metar(origin_iata) if origin_iata not in ('—', '') else None
        dest_metar   = get_metar(dest_iata)   if dest_iata   not in ('—', '') else None

        if origin_metar and origin_metar.get('temp') is not None:
            origin_temp = round(origin_metar['temp'])
        elif origin_coords:
            origin_temp = get_airport_weather(origin_iata, origin_coords['lat'], origin_coords['lon'])
        else:
            origin_temp = None

        if dest_metar and dest_metar.get('temp') is not None:
            dest_temp = round(dest_metar['temp'])
        elif dest_coords:
            dest_temp = get_airport_weather(dest_iata, dest_coords['lat'], dest_coords['lon'])
        else:
            dest_temp = None

        found[reg] = {
            "registration": reg, "callsign": callsign,
            "latitude": lat, "longitude": lon,
            "altitude": (fr24_alt if fr24_found else 0),
            "speed": 0, "heading": route_heading, "vertical_speed": 0,
            "origin": origin_full, "destination": dest_full,
            "origin_iata": origin_iata, "dest_iata": dest_iata,
            "origin_temp": origin_temp, "dest_temp": dest_temp,
            "origin_metar": origin_metar, "dest_metar": dest_metar,
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
    min_until = float('inf')
    now = datetime.now(timezone.utc)
    with data_lock:
        for reg in AIRCRAFT_REGISTRATIONS:
            cur = schedule_cache.get(reg, {}).get("current")
            upc = schedule_cache.get(reg, {}).get("upcoming", [])
            for f in ([cur] if cur else []) + upc:
                if not f: continue
                t0_str = f.get("dateTakeoff", "")
                if not t0_str: continue
                try:
                    t0   = datetime.fromisoformat(t0_str.replace("Z", "+00:00"))
                    mins = (t0 - now).total_seconds() / 60
                    if -10 <= mins <= 60:
                        min_until = min(min_until, mins)
                except: pass
    if min_until <= 10:  return 5
    if min_until <= 30:  return 10
    if min_until <= 60:  return 15
    return POLL_INTERVAL

def background_poll():
    logger.info("🚀 Background poll thread started")
    while True:
        try:
            fetch_data()
        except Exception as e:
            logger.error(f"❌ fetch_data crashed: {e}", exc_info=True)
        interval = get_adaptive_interval()
        time.sleep(interval)

# ─── Background thread — lazy start in worker process (survives Gunicorn fork) ─
_bg_thread = None
_bg_thread_lock = threading.Lock()

@app.before_request
def ensure_bg_thread():
    global _bg_thread
    if _bg_thread is None or not _bg_thread.is_alive():
        with _bg_thread_lock:
            if _bg_thread is None or not _bg_thread.is_alive():
                logger.info(f"▶ Starting background thread in worker pid={os.getpid()}")
                _bg_thread = threading.Thread(target=background_poll, daemon=True)
                _bg_thread.start()

# ─── API ──────────────────────────────────────────────────────────────────────
@app.after_request
def no_keepalive(response):
    response.headers["Connection"] = "close"
    return response

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
                next_f   = sched_mapped["upcoming"][0] if sched_mapped.get("upcoming") else None
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
                    "origin_metar": None, "dest_metar": None,
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
    with data_lock:
        has_flight_data = any(flight_cache.get(r) for r in AIRCRAFT_REGISTRATIONS)
    fr24_ok = (time.time() - fr24_last_ok) < 300
    return jsonify({
        'aviabit': {
            'reachable': schedule_service.logged_in,
            'error':     schedule_service.last_error
        },
        'fr24': {
            'reachable': fr24_ok or has_flight_data
        },
        'last_update': last_schedule_update,
        'pid': os.getpid(),
        'thread_alive': _bg_thread.is_alive(),
    })

CREW_ROLE_NAMES = {
    'КВ': 'Командир ВС', 'КВС': 'Командир ВС',
    '2П': 'Второй пилот', 'ВП': 'Второй пилот',
    'БП': 'Бортпроводник', 'БИ': 'Бортинженер',
    'ШТ': 'Штурман', 'ДПП': 'Доп. пилот', 'ПИ': 'Пилот-инструктор',
}

def parse_crew_xml(xml_str):
    """Парсит XML поля crew из плана полётов Авиабит."""
    if not xml_str or xml_str.strip() in ('<crew/>', ''):
        return []
    try:
        root   = ET.fromstring(xml_str)
        result = []
        for emp in root.findall('employee'):
            arm_chair = emp.get('armChair', '—')
            result.append({
                'name':         (emp.text or '—').strip(),
                'role':         arm_chair,
                'role_full':    CREW_ROLE_NAMES.get(arm_chair, arm_chair),
                'personnel_id': emp.get('personnelId', ''),
                'order':        int(emp.get('orderNumber', 99)),
            })
        return sorted(result, key=lambda x: x['order'])
    except Exception as e:
        logger.warning(f"Crew XML parse: {e}")
        return []

def parse_crew_comment(comment_text):
    """Парсит текстовый список экипажа из поля MarkFlightNumber.
    Формат: '1.ФАМИЛИЯ ИМЯ\\n2.ФАМИЛИЯ ИМЯ...'
    """
    if not comment_text:
        return []
    import re
    result = []
    for i, line in enumerate(re.split(r'[\r\n]+', comment_text.strip())):
        line = line.strip().strip('\t')
        if not line:
            continue
        # Remove leading number like "1." or "№ рейса:"
        name = re.sub(r'^(?:№\s*рейса\s*:\s*\d+\.|№\s*рейса\s*:|\d+\.)\s*', '', line).strip()
        if name:
            result.append({
                'name':         name,
                'role':         '—',
                'role_full':    'Член экипажа',
                'personnel_id': '',
                'order':        i + 1,
                'source':       'comment',
            })
    return result

def merge_crew(xml_crew, comment_crew):
    """Объединяет XML экипаж (пилоты) с экипажем из комментариев, исключая дублей по имени."""
    if not comment_crew:
        return xml_crew
    if not xml_crew:
        return comment_crew
    existing = {m['name'].upper().replace(' ', '') for m in xml_crew}
    merged = list(xml_crew)
    offset = max((m['order'] for m in xml_crew), default=0) + 1
    for i, member in enumerate(comment_crew):
        if member['name'].upper().replace(' ', '') not in existing:
            m = dict(member)
            m['order'] = offset + i
            merged.append(m)
    return merged

@app.route("/api/crew")
def api_crew():
    flight = request.args.get('flight', '').strip()
    date   = request.args.get('date', '').strip()
    if not flight:
        return jsonify({'error': 'Требуется параметр flight', 'crew': []}), 400

    fn_norm = flight.replace("-", "").upper()

    def find_entry_in_plan(plan):
        """Ищет запись рейса в плане и возвращает (crew_parsed, pf_record_id)."""
        for entry in plan:
            if str(entry.get("flight", "")).replace("-", "").upper() == fn_norm:
                parsed = parse_crew_xml(entry.get("crew", ""))
                return parsed, entry.get("pfRecordId")
        return None, None

    # 1. Ищем в кеше расписания (текущий + предстоящие рейсы)
    cached_entries = []
    with data_lock:
        for r in AIRCRAFT_REGISTRATIONS:
            sched = schedule_cache.get(r, {})
            for entry in ([sched.get("current")] + sched.get("upcoming", [])):
                if entry:
                    cached_entries.append(entry)

    for entry in cached_entries:
        if str(entry.get("flight", "")).replace("-", "").upper() == fn_norm:
            parsed = parse_crew_xml(entry.get("crew", ""))
            pf_id  = entry.get("pfRecordId")
            comment_crew = []
            if pf_id:
                comment = schedule_service.get_preliminary_crew_comment(pf_id)
                comment_crew = parse_crew_comment(comment)
            merged = merge_crew(parsed, comment_crew)
            if merged:
                src = 'combined' if (parsed and comment_crew) else ('comment' if comment_crew else 'xml')
                return jsonify({'crew': merged, 'error': None, 'flight': flight, 'source': src})

    # 2. Запрашиваем расширенный план (30 дней) и ищем там
    try:
        all_plan = schedule_service.get_flight_plan(search_regs=AIRCRAFT_REGISTRATIONS, days_around=30)
        for entry in all_plan:
            if str(entry.get("flight", "")).replace("-", "").upper() == fn_norm:
                parsed = parse_crew_xml(entry.get("crew", ""))
                pf_id  = entry.get("pfRecordId")
                comment_crew = []
                if pf_id:
                    comment = schedule_service.get_preliminary_crew_comment(pf_id)
                    comment_crew = parse_crew_comment(comment)
                merged = merge_crew(parsed, comment_crew)
                if merged:
                    src = 'combined' if (parsed and comment_crew) else ('comment' if comment_crew else 'xml')
                    return jsonify({'crew': merged, 'error': None, 'flight': flight, 'source': src})
        return jsonify({'crew': [], 'error': 'Экипаж не назначен для этого рейса', 'flight': flight})
    except Exception as e:
        logger.error(f"Crew API {flight}: {e}")
        return jsonify({'error': str(e), 'crew': []}), 500

@app.route("/api/history/<reg>")
def api_history(reg):
    reg_norm = reg.upper().replace("-", "")
    now = time.time()
    cached = history_cache.get(reg_norm)
    if cached and (now - cached['ts']) < 600:
        return jsonify(cached['data'])
    try:
        # Ищем по нормализованному рег. номеру среди всех зарегистрированных
        search_reg = next((r for r in AIRCRAFT_REGISTRATIONS
                           if r.replace("-", "").upper() == reg_norm), reg)
        flights = schedule_service.get_past_flights(search_regs=[search_reg], days_back=30)
        result  = []
        for f in flights[:60]:
            result.append({
                'flight':      f.get('flight', ''),
                'origin':      f.get('airPortTOCode', ''),
                'dest':        f.get('airPortLACode', ''),
                'origin_full': get_airport_info(f.get('airPortTOCode', '')),
                'dest_full':   get_airport_info(f.get('airPortLACode', '')),
                'date_takeoff': f.get('dateTakeoff', ''),
                'date_landing': f.get('dateLanding', ''),
                'status':      f.get('status', 0),
            })
        response = {'reg': reg.upper(), 'flights': result}
        history_cache[reg_norm] = {'data': response, 'ts': now}
        return jsonify(response)
    except Exception as e:
        logger.error(f"History API {reg}: {e}")
        return jsonify({'error': str(e), 'flights': []}), 500

@app.route("/api/metar/<iata>")
def api_metar(iata):
    metar = get_metar(iata.upper())
    if metar:
        return jsonify(metar)
    return jsonify({'error': 'METAR недоступен'}), 404

@app.route("/api/notam/<iata>")
def api_notam(iata):
    notams = get_notams(iata.upper())
    return jsonify({'notams': notams, 'count': len(notams), 'iata': iata.upper()})

@app.route("/")
def index():
    return render_template("index.html")

logger.info(f"📦 Module loaded — pid={os.getpid()}")

if __name__ == "__main__":
    # Start thread immediately when running directly (not via Gunicorn)
    _bg_thread = threading.Thread(target=background_poll, daemon=True)
    _bg_thread.start()
    app.run(debug=False, port=5050, host="0.0.0.0")
