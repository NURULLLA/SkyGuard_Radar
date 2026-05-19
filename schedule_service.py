import requests
import json
import urllib3
import logging
from datetime import datetime, timedelta, timezone

# Отключаем предупреждения о небезопасном SSL (для локальной работы)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

class SkyguardScheduleService:
    def __init__(self, username, password):
        self.base_url = "https://ab-web.aviastartu.ru"
        self.username = username
        self.password = password
        self.session = requests.Session()
        # Имитируем браузер для обхода CORS/Origin проверок
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/login"
        })
        self.logged_in = False
        self.last_error = None

    def login(self):
        """Выполняет вход в АвиаБит."""
        url = f"{self.base_url}/api/auth"
        payload = {
            "rememberMe": False,
            "version": {
                "date": "2025-02-03T08:30:00.000Z",
                "company": "ООО \"АвиаБит\"",
                "number": "9.3.3"
            },
            "eng": False,
            "username": self.username,
            "password": self.password
        }
        try:
            response = self.session.post(url, json=payload, timeout=10, verify=False)
            if response.status_code == 200:
                self.logged_in = True
                logger.info("✅ АвиаБит: Успешный вход")
                return True
            else:
                logger.error(f"❌ АвиаБит: Ошибка входа {response.status_code}: {response.text}")
                return False
        except Exception as e:
            self.last_error = f"Login Exception: {str(e)}"
            logger.error(f"❌ АвиаБит: Исключение при входе: {e}")
            return False

    def get_flight_plan(self, search_regs=None, days_around=14):
        """Запрашивает план полетов."""
        if not self.logged_in and not self.login():
            return []

        url = f"{self.base_url}/api/plan-flight"
        headers = {"Referer": f"{self.base_url}/plan-flight"}
        
        now = datetime.now()
        begin = int((now - timedelta(days=1)).timestamp() * 1000)
        end = int((now + timedelta(days=days_around)).timestamp() * 1000)

        params = {
            "dateBegin": begin,
            "dateEnd": end,
            "eng": "false",
            "apCode": "3",
            "apId": "0",
            "template": "0",
            "showCancel": "false"
        }

        try:
            response = self.session.get(url, params=params, timeout=15, verify=False, headers=headers)
            if response.status_code == 401:
                logger.warning("⚠️ Сессия АвиаБит истекла, перелогиниваемся...")
                if self.login():
                    response = self.session.get(url, params=params, timeout=15, verify=False, headers=headers)
                else:
                    return []

            if response.status_code == 200:
                data = response.json()
                if search_regs:
                    # Нормализуем список искомых бортов
                    search_norm = [r.replace("-", "").upper() for r in search_regs]
                    filtered = [f for f in data if str(f.get("pln", "")).replace("-", "").upper() in search_norm]
                    logger.info(f"📅 АвиаБит: Получено {len(filtered)} рейсов из расписания")
                    return filtered
                return data
            else:
                self.last_error = f"Plan error {response.status_code}: {response.text[:200]}"
                logger.error(f"❌ Ошибка получения плана: {response.status_code}")
                return []
        except Exception as e:
            self.last_error = f"Plan Exception: {str(e)}"
            logger.error(f"❌ Исключение при получении плана: {e}")
            return []

    def get_current_and_next_flights(self, registration):
        """Возвращает текущий и будущие рейсы для конкретного борта."""
        plan = self.get_flight_plan(search_regs=[registration])
        reg_norm = registration.replace("-", "").upper()
        
        flights = [f for f in plan if str(f.get("pln", "")).replace("-", "").upper() == reg_norm]
        flights.sort(key=lambda x: x.get("dateTakeoff", ""))

        now_iso = datetime.now(timezone.utc).isoformat()
        
        current = None
        upcoming = []
        
        for f in flights:
            takeoff = f.get("dateTakeoff", "")
            landing = f.get("dateLanding", "")
            
            if takeoff <= now_iso <= landing or f.get("status") == 1:
                current = f
            elif takeoff > now_iso:
                upcoming.append(f)
                
        return current, upcoming[:5]

    def get_crew_for_flight(self, flight_num, date_str=None):
        """Получает список экипажа для рейса из АвиаБит."""
        if not self.logged_in and not self.login():
            return [], "Не авторизован в АвиаБит"

        if date_str:
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                begin = int((dt - timedelta(hours=2)).timestamp() * 1000)
                end   = int((dt + timedelta(hours=26)).timestamp() * 1000)
            except Exception:
                now = datetime.now()
                begin = int((now - timedelta(days=1)).timestamp() * 1000)
                end   = int((now + timedelta(days=2)).timestamp() * 1000)
        else:
            now = datetime.now()
            begin = int((now - timedelta(days=1)).timestamp() * 1000)
            end   = int((now + timedelta(days=2)).timestamp() * 1000)

        params = {
            "dateBegin":  begin,
            "dateEnd":    end,
            "flight":     flight_num,
            "template":   "0",
            "showCancel": "false",
            "eng":        "false",
        }
        headers = {"Referer": f"{self.base_url}/plan-crew"}

        for endpoint in ["/api/plan-crew", "/api/crew-schedule", "/api/crew"]:
            try:
                url  = f"{self.base_url}{endpoint}"
                resp = self.session.get(url, params=params, timeout=10, verify=False, headers=headers)
                if resp.status_code == 401:
                    if self.login():
                        resp = self.session.get(url, params=params, timeout=10, verify=False, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and data:
                        logger.info(f"✅ Экипаж для {flight_num} получен через {endpoint}")
                        return data, None
                    elif isinstance(data, dict) and data:
                        return [data], None
                elif resp.status_code == 404:
                    continue
            except Exception as e:
                logger.warning(f"Crew {endpoint}: {e}")
                continue

        return [], "Данные экипажа недоступны в АвиаБит"

    def get_preliminary_crew_comment(self, pf_record_id):
        """Возвращает текстовый список экипажа из поля comments.MarkFlightNumber."""
        if not self.logged_in and not self.login():
            return None
        url = f"{self.base_url}/api/preliminary-crew-load"
        params = {"planFlightId": pf_record_id, "eng": "false"}
        headers = {"Referer": f"{self.base_url}/flight-card?planFlightId={pf_record_id}"}
        try:
            resp = self.session.get(url, params=params, timeout=10, verify=False, headers=headers)
            if resp.status_code == 401:
                if self.login():
                    resp = self.session.get(url, params=params, timeout=10, verify=False, headers=headers)
            if resp.status_code == 200 and 'application/json' in resp.headers.get('content-type', ''):
                data = resp.json()
                comments = data.get('comments') or {}
                mark = comments.get('MarkFlightNumber') or ''
                if mark.strip():
                    logger.info(f"✅ Комментарий экипажа получен для planFlightId={pf_record_id}")
                    return mark.strip()
        except Exception as e:
            logger.warning(f"preliminary-crew-load error: {e}")
        return None

    def get_past_flights(self, search_regs=None, days_back=30):
        """Возвращает завершённые рейсы за последние N дней."""
        if not self.logged_in and not self.login():
            return []

        url     = f"{self.base_url}/api/plan-flight"
        headers = {"Referer": f"{self.base_url}/plan-flight"}
        now     = datetime.now()
        begin   = int((now - timedelta(days=days_back)).timestamp() * 1000)
        end     = int(now.timestamp() * 1000)

        params = {
            "dateBegin":  begin,
            "dateEnd":    end,
            "eng":        "false",
            "apCode":     "3",
            "apId":       "0",
            "template":   "0",
            "showCancel": "false",
        }

        try:
            resp = self.session.get(url, params=params, timeout=20, verify=False, headers=headers)
            if resp.status_code == 401:
                if self.login():
                    resp = self.session.get(url, params=params, timeout=20, verify=False, headers=headers)
            if resp.status_code == 200:
                data    = resp.json()
                now_iso = datetime.now(timezone.utc).isoformat()
                past    = [f for f in data if f.get("dateLanding", "9") < now_iso]
                if search_regs:
                    norms = [r.replace("-", "").upper() for r in search_regs]
                    past  = [f for f in past if str(f.get("pln", "")).replace("-", "").upper() in norms]
                return sorted(past, key=lambda x: x.get("dateLanding", ""), reverse=True)
        except Exception as e:
            logger.error(f"Past flights error: {e}")
        return []
