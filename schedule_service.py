"""Aviabit (АвиаБит) schedule client — timetable data only.

Only two things happen here: authenticate, and pull the flight plan.
No FlightRadar24, no weather, no notifications.
"""

import logging
from datetime import datetime, timedelta, timezone

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

BASE_URL = "https://ab-web.aviastartu.ru"

# /api/plan-flight answers with at most ~400 records per call and returns them
# OLDEST FIRST. A wide window therefore silently drops the most recent flights —
# exactly the ones a timetable cares about. We slice the window into short
# chunks and merge the results instead of asking for everything at once.
SLICE_DAYS = 7
PAGE_CAP = 400

CLIENT_VERSION = {
    "date": "2025-02-03T08:30:00.000Z",
    "company": 'ООО "АвиаБит"',
    "number": "9.3.3",
}


def _ms(dt):
    return int(dt.timestamp() * 1000)


class AviabitSchedule:
    def __init__(self, username, password, base_url=BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.logged_in = False
        self.last_error = None

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/122.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/login",
        })

    # ── auth ─────────────────────────────────────────────────────────────────
    def login(self):
        payload = {
            "rememberMe": False,
            "version": CLIENT_VERSION,
            "eng": False,
            "username": self.username,
            "password": self.password,
        }
        try:
            r = self.session.post(f"{self.base_url}/api/auth",
                                  json=payload, timeout=15, verify=False)
            if r.status_code == 200:
                self.logged_in = True
                self.last_error = None
                logger.info("Aviabit: signed in as %s", self.username)
                return True
            self.logged_in = False
            self.last_error = f"login {r.status_code}: {r.text[:160]}"
            logger.error("Aviabit: %s", self.last_error)
        except Exception as exc:
            self.logged_in = False
            self.last_error = f"login failed: {exc}"
            logger.error("Aviabit: %s", self.last_error)
        return False

    def _get(self, path, params, referer=None):
        """GET with one transparent re-login on 401."""
        if not self.logged_in and not self.login():
            return None

        url = f"{self.base_url}{path}"
        headers = {"Referer": f"{self.base_url}{referer}"} if referer else {}

        for attempt in (1, 2):
            try:
                r = self.session.get(url, params=params, headers=headers,
                                     timeout=25, verify=False)
            except Exception as exc:
                self.last_error = f"{path}: {exc}"
                logger.warning("Aviabit %s", self.last_error)
                return None

            if r.status_code == 401 and attempt == 1:
                logger.info("Aviabit: session expired, signing in again")
                self.logged_in = False
                if not self.login():
                    return None
                continue

            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    self.last_error = f"{path}: response was not JSON"
                    return None

            self.last_error = f"{path}: HTTP {r.status_code}"
            logger.warning("Aviabit %s", self.last_error)
            return None
        return None

    # ── flight plan ──────────────────────────────────────────────────────────
    def _plan_slice(self, begin, end):
        params = {
            "dateBegin": _ms(begin),
            "dateEnd": _ms(end),
            "eng": "false",
            "apCode": "3",
            "apId": "0",
            "template": "0",
            "showCancel": "false",
        }
        data = self._get("/api/plan-flight", params, referer="/plan-flight")
        if not isinstance(data, list):
            return []
        if len(data) >= PAGE_CAP:
            logger.warning(
                "Aviabit returned %d records for %s..%s — at the page cap, "
                "some flights in this slice may be missing",
                len(data), begin.date(), end.date())
        return data

    def fetch_plan(self, days_back=2, days_ahead=21):
        """Every flight in the window, for every tail, de-duplicated."""
        now = datetime.now(timezone.utc)
        begin = now - timedelta(days=days_back)
        end = now + timedelta(days=days_ahead)

        merged = {}
        cursor = begin
        while cursor < end:
            stop = min(cursor + timedelta(days=SLICE_DAYS), end)
            for rec in self._plan_slice(cursor, stop):
                key = rec.get("recordID") or (
                    rec.get("pln"), rec.get("flight"), rec.get("dateTakeoff"))
                merged[key] = rec
            cursor = stop

        logger.info("Aviabit: %d flights between %s and %s",
                    len(merged), begin.date(), end.date())
        return list(merged.values())
