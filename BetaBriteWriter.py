"""
BetaBriteWriter.py
BetaBrite Weather Display System — main runtime.
Run BetaBriteConfigure.py first to set up settings.
"""

import serial
import time
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
import os
import json
from collections import defaultdict, Counter
import traceback
import sys
from serial.tools import list_ports
import threading
from abc import ABC, abstractmethod
import argparse
import logging
from logging.handlers import RotatingFileHandler
from dateutil.parser import isoparse
import pytz

# ==================== CONSTANTS ====================
SETTINGS_FILE = "BetaBriteWriter.json"
LOG_FILE = "BetaBriteWriter.log"
MAX_LOG_BACKUPS = 5
MAX_LOG_BYTES = 1024 * 1024  # 1 MB

# Serial Protocol
NUL = b'\x00'
SOH = b'\x01'
STX = b'\x02'
EOT = b'\x04'
ESC = b'\x1B'
SP  = b'\x20'

# Timing
SERIAL_WRITE_DELAY   = 0.2
MAX_SEND_RETRY_TIME  = 300
MAX_API_RETRIES      = 3
API_RETRY_BASE_DELAY = 2   # seconds; doubles each attempt (2 → 4 → 8)
SERIAL_RECONNECT_ATTEMPTS = 10
SERIAL_RECONNECT_DELAY    = 30  # seconds between reconnect attempts

# Display
FS             = "\x1C"
COLORS_TODAY   = ["3"]
COLORS_FUTURE  = ["4", "5", "6", "7", "8"]
ALERT_COLOR    = "1"

# Scheduling
SCHEDULED_HOURS       = [0, 3, 6, 9, 12, 15, 18, 21]
NWS_SCHEDULED_MINUTES = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
NHC_SCHEDULED_HOURS   = [5, 11, 17, 23]

# URLs
NHC_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"

# Weather codes (Tomorrow.io)
TOMORROW_WEATHER_CODES = {
    0: "Unknown", 1000: "Clear", 1100: "Mostly Clear", 1101: "Partly Cloudy",
    1102: "Mostly Cloudy", 1001: "Cloudy", 2000: "Fog", 2100: "Light Fog",
    4000: "Drizzle", 4001: "Rain", 4200: "Light Rain", 4201: "Heavy Rain",
    5000: "Snow", 5001: "Flurries", 5100: "Light Snow", 5101: "Heavy Snow",
    6000: "Freezing Drizzle", 6001: "Freezing Rain", 6200: "Light Freezing Rain",
    6201: "Heavy Freezing Rain", 7000: "Ice Pellets", 7101: "Heavy Ice Pellets",
    7102: "Light Ice Pellets", 8000: "Thunderstorm"
}

MAX_DISPLAY_MESSAGE_SIZE = 2048

# ==================== TIMEZONE ====================
def _detect_local_timezone() -> pytz.BaseTzInfo:
    """
    Detect the system timezone reliably.
    Falls back to America/New_York if detection fails.
    """
    # Prefer tzlocal when available
    try:
        from tzlocal import get_localzone
        return get_localzone()
    except ImportError:
        pass

    # Try /etc/timezone (Linux)
    try:
        with open("/etc/timezone") as f:
            tz_name = f.read().strip()
        return pytz.timezone(tz_name)
    except Exception:
        pass

    # Try /etc/localtime symlink (macOS / some Linux)
    try:
        link = os.readlink("/etc/localtime")
        # .../zoneinfo/America/New_York
        tz_name = "/".join(link.split("/")[-2:])
        return pytz.timezone(tz_name)
    except Exception:
        pass

    return pytz.timezone("America/New_York")


LOCAL_TZ = _detect_local_timezone()


def now_local() -> datetime:
    """Return the current time as a timezone-aware datetime in LOCAL_TZ."""
    return datetime.now(LOCAL_TZ)


# ==================== HELPER FUNCTIONS ====================
def aggregate_temperatures(entries: List[Dict]) -> Tuple[int, int]:
    if not entries:
        return 0, 0
    temps_min = [int(entry["main"]["temp_min"]) for entry in entries]
    temps_max = [int(entry["main"]["temp_max"]) for entry in entries]
    return min(temps_min), max(temps_max)


def build_alert_suffix(nhc_names: List[str], nws_headlines: List[str]) -> str:
    """Build the alert portion of the display message (NHC + NWS)."""
    parts = []
    if nhc_names:
        parts.append(f" || {FS}{ALERT_COLOR}NHC Atlantic Hurricane(s): {', '.join(nhc_names)}")
    for headline in nws_headlines:
        parts.append(f" || {FS}{ALERT_COLOR}NWS Alert: {headline}")
    return "".join(parts)


def truncate_message(message: str, limit: int = MAX_DISPLAY_MESSAGE_SIZE) -> str:
    """
    Hard-truncate a display message to *limit* bytes, avoiding cuts
    mid-escape-sequence by stepping back to the last safe boundary.
    """
    if len(message) <= limit:
        return message
    # Reserve 3 chars for "..."
    cut = message[: limit - 3]
    # Step back past any trailing ESC or FS character to avoid broken sequences
    while cut and cut[-1] in ("\x1B", "\x1C"):
        cut = cut[:-1]
    return cut + "..."


# ==================== RETRY LOGIC ====================
def retry_request(func, *args, **kwargs):
    """
    Retry *func* up to MAX_API_RETRIES times with exponential backoff.
    Raises the last exception if all attempts fail.
    """
    delay = API_RETRY_BASE_DELAY
    for attempt in range(MAX_API_RETRIES):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt < MAX_API_RETRIES - 1:
                jitter = delay * 0.1 * (0.5 - (attempt % 2))  # small jitter
                time.sleep(delay + jitter)
                delay *= 2
            else:
                raise e


# ==================== GLOBAL STATE ====================
class ThreadSafeState:
    """
    Central runtime state, grouped into three logical areas:
      - Forecast    : last update time, last hour updated, stored message
      - Alerts      : NWS headlines, NHC names, last NWS alert ID
      - Display     : was_active flag
      - Timestamps  : last NWS pull, last NHC pull
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.shutdown_event = threading.Event()

        # Forecast
        self._last_forecast_update: Optional[datetime] = None
        self._last_forecast_hour: Optional[int] = None
        self._last_forecast_message: str = ""

        # Alerts
        self._last_alert_id: Optional[str] = None
        self._nws_active_headlines: List[str] = []
        self._nhc_active_names: List[str] = []

        # Display
        self._display_was_active: Optional[bool] = None

        # Pull timestamps
        self._last_nws_pull: datetime = datetime.min
        self._last_nhc_pull: datetime = datetime.min

    # ---- context manager shorthand ----
    def _get(self, attr):
        with self._lock:
            return getattr(self, attr)

    def _set(self, attr, value):
        with self._lock:
            setattr(self, attr, value)

    # ---- forecast ----
    @property
    def last_forecast_update(self) -> Optional[datetime]:
        return self._get("_last_forecast_update")

    @last_forecast_update.setter
    def last_forecast_update(self, v):
        self._set("_last_forecast_update", v)

    @property
    def last_forecast_hour(self) -> Optional[int]:
        return self._get("_last_forecast_hour")

    @last_forecast_hour.setter
    def last_forecast_hour(self, v):
        self._set("_last_forecast_hour", v)

    @property
    def last_forecast_message(self) -> str:
        return self._get("_last_forecast_message")

    @last_forecast_message.setter
    def last_forecast_message(self, v):
        self._set("_last_forecast_message", v)

    # ---- alerts ----
    @property
    def last_alert_id(self) -> Optional[str]:
        return self._get("_last_alert_id")

    @last_alert_id.setter
    def last_alert_id(self, v):
        self._set("_last_alert_id", v)

    @property
    def nws_active_headlines(self) -> List[str]:
        with self._lock:
            return self._nws_active_headlines.copy()

    @nws_active_headlines.setter
    def nws_active_headlines(self, v: List[str]):
        with self._lock:
            self._nws_active_headlines = v.copy()

    @property
    def nhc_active_names(self) -> List[str]:
        with self._lock:
            return self._nhc_active_names.copy()

    @nhc_active_names.setter
    def nhc_active_names(self, v: List[str]):
        with self._lock:
            self._nhc_active_names = v.copy()

    # ---- display ----
    @property
    def display_was_active(self) -> Optional[bool]:
        return self._get("_display_was_active")

    @display_was_active.setter
    def display_was_active(self, v):
        self._set("_display_was_active", v)

    # ---- pull timestamps ----
    @property
    def last_nws_pull(self) -> datetime:
        return self._get("_last_nws_pull")

    def mark_nws_pull(self):
        self._set("_last_nws_pull", datetime.now())

    @property
    def last_nhc_pull(self) -> datetime:
        return self._get("_last_nhc_pull")

    def mark_nhc_pull(self):
        self._set("_last_nhc_pull", datetime.now())

    # ---- shutdown ----
    def shutdown(self):
        self.shutdown_event.set()

    def should_shutdown(self) -> bool:
        return self.shutdown_event.is_set()


state = ThreadSafeState()


# ==================== SETTINGS ====================
class Settings:
    DEFAULT_SETTINGS = {
        "COM_PORT": "",
        "API_TYPE": "OpenWeather",
        "API_KEY": "",
        "ZIP_CODE": "",
        "FORECAST_ZONE": "",
        "ON_TIME": "06:00",
        "OFF_TIME": "22:00",
        "LOGGING_ON": False,
        "FULL_API_LOGGING": False,
        "FULL_NHC_LOGGING": False,
        "FULL_NWS_LOGGING": False,
        "FULL_BETABRITE_LOGGING": False
    }

    @staticmethod
    def load() -> Dict:
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    loaded = json.load(f)
                    settings = Settings.DEFAULT_SETTINGS.copy()
                    settings.update(loaded)
                    return settings
            except Exception as e:
                print(f"Error loading settings: {e}. Using defaults.")
        return Settings.DEFAULT_SETTINGS.copy()


# ==================== LOGGING ====================
def setup_logger(settings: Dict) -> logging.Logger:
    logger = logging.getLogger("BetaBrite")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    if settings.get("LOGGING_ON"):
        # Rotate existing logs on startup
        for i in range(MAX_LOG_BACKUPS, 0, -1):
            old_log = f"BetaBriteWriter.{i}.log"
            new_log = f"BetaBriteWriter.{i + 1}.log"
            if os.path.exists(old_log):
                if i == MAX_LOG_BACKUPS:
                    os.remove(old_log)
                else:
                    os.rename(old_log, new_log)
        if os.path.exists(LOG_FILE):
            os.rename(LOG_FILE, "BetaBriteWriter.1.log")

        handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_LOG_BYTES, backupCount=MAX_LOG_BACKUPS)
        formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%m/%d/%y %I:%M %p')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


class Logger:
    _logger: Optional[logging.Logger] = None

    @classmethod
    def initialize(cls, settings: Dict):
        cls._logger = setup_logger(settings)

    @classmethod
    def log(cls, msg: str, settings: Optional[Dict] = None):
        if cls._logger and settings and settings.get("LOGGING_ON"):
            cls._logger.info(msg)


# ==================== TIME MANAGEMENT ====================
def is_display_active(settings: Dict, now: Optional[datetime] = None) -> bool:
    if now is None:
        now = now_local()
    current_time = now.time()
    on_time  = datetime.strptime(settings["ON_TIME"],  "%H:%M").time()
    off_time = datetime.strptime(settings["OFF_TIME"], "%H:%M").time()
    if on_time < off_time:
        return on_time <= current_time < off_time
    return current_time >= on_time or current_time < off_time


def get_forecast_times(now: datetime) -> List[datetime]:
    """Return [now_or_current_scheduled_hour, +next_2_scheduled_hours]."""
    if now.tzinfo is None:
        now = LOCAL_TZ.localize(now)

    times = []
    if now.hour in SCHEDULED_HOURS and now.minute < 5:
        current = now.replace(minute=0, second=0, microsecond=0)
    else:
        current = now

    times.append(current)
    current_hour = current.hour

    for _ in range(2):
        next_hours = [h for h in SCHEDULED_HOURS if h > current_hour]
        if next_hours:
            next_hour = next_hours[0]
            next_time = current.replace(hour=next_hour, minute=0, second=0, microsecond=0)
        else:
            next_hour = SCHEDULED_HOURS[0]
            next_time = (current + timedelta(days=1)).replace(
                hour=next_hour, minute=0, second=0, microsecond=0
            )
        times.append(next_time)
        current = next_time
        current_hour = next_hour

    return times


def get_next_forecast_update(now: datetime) -> datetime:
    current_hour = now.hour
    next_hours = [h for h in SCHEDULED_HOURS if h > current_hour]
    if next_hours:
        return now.replace(hour=next_hours[0], minute=0, second=0, microsecond=0)
    return (now + timedelta(days=1)).replace(
        hour=SCHEDULED_HOURS[0], minute=0, second=0, microsecond=0
    )


def get_next_nws_check(now: datetime, alert_active: bool) -> datetime:
    if alert_active:
        return now + timedelta(minutes=2)
    for m in NWS_SCHEDULED_MINUTES:
        if now.minute < m:
            return now.replace(minute=m, second=0, microsecond=0)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def get_nearest_5min_mark(now: datetime) -> datetime:
    for m in NWS_SCHEDULED_MINUTES:
        if now.minute <= m:
            return now.replace(minute=m, second=0, microsecond=0)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def should_check_nhc(now: datetime, last_check: datetime) -> bool:
    if now.hour not in NHC_SCHEDULED_HOURS:
        return False
    if now.minute >= 5:
        return False
    if last_check.hour == now.hour and last_check.date() == now.date():
        return False
    return True


# ==================== WEATHER API ====================
class WeatherAPI(ABC):
    def __init__(self, api_key: str, zip_code: str):
        self.api_key  = api_key
        self.zip_code = zip_code
        self.headers  = {"User-Agent": "BetaBriteWeather/1.0"}

    @abstractmethod
    def get_forecast_data(self) -> Dict:
        pass

    @abstractmethod
    def parse_forecast(self, data: Dict, forecast_times: List[datetime],
                       settings: Dict) -> Tuple[List[str], List[str]]:
        pass


class OpenWeatherAPI(WeatherAPI):
    def get_forecast_data(self) -> Dict:
        url = (
            f"http://api.openweathermap.org/data/2.5/forecast"
            f"?zip={self.zip_code},us&units=imperial&appid={self.api_key}"
        )
        return retry_request(requests.get, url, headers=self.headers, timeout=10).json()

    def parse_forecast(self, data: Dict, forecast_times: List[datetime],
                       settings: Dict) -> Tuple[List[str], List[str]]:
        daily_forecast: Dict = defaultdict(list)
        for entry in data.get("list", []):
            dt = datetime.fromtimestamp(entry["dt"], tz=pytz.UTC).astimezone(LOCAL_TZ)
            daily_forecast[dt.date()].append(entry)

        today_blocks = []
        for f_time in forecast_times:
            entries = daily_forecast.get(f_time.date(), [])
            if not entries:
                continue
            entry = min(
                entries,
                key=lambda x: abs(
                    datetime.fromtimestamp(x["dt"], tz=pytz.UTC).astimezone(LOCAL_TZ) - f_time
                )
            )
            desc = entry["weather"][0]["main"]
            t_min, t_max = aggregate_temperatures(entries)
            today_blocks.append(
                f"{f_time.strftime('%I:%M %p %a %m/%d/%y')} {desc} {t_min}F/{t_max}F"
            )

        Logger.log(f"Parsed Today Blocks: {today_blocks}", settings)

        now = now_local()
        future_blocks = []
        for day in sorted(d for d in daily_forecast if d > now.date())[:5]:
            temps_min, temps_max, conditions = [], [], []
            for entry in daily_forecast[day]:
                temps_min.append(int(entry["main"]["temp_min"]))
                temps_max.append(int(entry["main"]["temp_max"]))
                conditions.append(entry["weather"][0]["main"])
            most_common = Counter(conditions).most_common(1)[0][0]
            future_blocks.append(
                f"{day.strftime('%a %m/%d/%y')} {most_common} {min(temps_min)}F/{max(temps_max)}F"
            )

        return today_blocks, future_blocks


class TomorrowAPI(WeatherAPI):
    def __init__(self, api_key: str, zip_code: str, lat: float = None, lon: float = None):
        super().__init__(api_key, zip_code)
        if lat is None or lon is None:
            raise RuntimeError(
                "TomorrowAPI requires LAT and LON in settings. "
                "Run: python3 BetaBriteConfigure.py, option 3 to re-save your ZIP code."
            )
        self.lat = lat
        self.lon = lon

    def get_forecast_data(self) -> Dict:
        url = (
            f"https://api.tomorrow.io/v4/weather/forecast"
            f"?location={self.lat},{self.lon}"
            f"&units=imperial"
            f"&apikey={self.api_key}"
        )
        return retry_request(requests.get, url, headers=self.headers, timeout=10).json()

    def _get_weather_description(self, code: int) -> str:
        return TOMORROW_WEATHER_CODES.get(code, "Unknown")

    def parse_forecast(self, data: Dict, forecast_times: List[datetime],
                       settings: Dict) -> Tuple[List[str], List[str]]:
        # Response: { "timelines": { "minutely": [...], "hourly": [...], "daily": [...] } }
        timelines = data.get("timelines", {})
        hourly    = timelines.get("hourly", [])
        daily     = timelines.get("daily",  [])

        Logger.log(f"Tomorrow.io: {len(hourly)} hourly, {len(daily)} daily entries", settings)

        # --- Build hourly lookup keyed by LOCAL date ---
        hourly_by_date: Dict = defaultdict(list)
        for entry in hourly:
            dt_str = entry.get("time", "")
            if not dt_str:
                continue
            # Parse UTC timestamp and convert to local time
            dt_local = isoparse(dt_str).astimezone(LOCAL_TZ)
            hourly_by_date[dt_local.date()].append(
                {"dt": dt_local, "values": entry.get("values", {})}
            )

        # --- today_blocks: find the hourly entry closest to each forecast time ---
        today_blocks = []
        for f_time in forecast_times:
            # Ensure f_time is timezone-aware
            if f_time.tzinfo is None:
                f_time = LOCAL_TZ.localize(f_time)
            entries = hourly_by_date.get(f_time.date(), [])
            if not entries:
                Logger.log(f"No hourly entries for {f_time.date()}", settings)
                continue
            entry = min(entries, key=lambda x: abs(x["dt"] - f_time))
            values = entry["values"]
            desc = self._get_weather_description(int(values.get("weatherCode", 0)))
            temp = int(values.get("temperature", 0))
            today_blocks.append(
                f"{f_time.strftime('%I:%M %p %a %m/%d/%y')} {desc} {temp}F/{temp}F"
            )

        Logger.log(f"Tomorrow.io today_blocks: {today_blocks}", settings)

        # --- future_blocks: one entry per future day from the daily timeline ---
        # Daily entry "time" is the START of that day in UTC (e.g. 11:00Z = 06:00 EST)
        # Use the UTC date to avoid off-by-one from timezone conversion
        now = now_local()
        future_blocks = []
        for entry in daily:
            dt_str = entry.get("time", "")
            if not dt_str:
                continue
            # Keep as UTC date to match the day the entry represents
            dt_utc = isoparse(dt_str)
            dt_local = dt_utc.astimezone(LOCAL_TZ)
            day = dt_local.date()
            if day <= now.date():
                continue
            values = entry.get("values", {})
            t_min = int(values.get("temperatureMin", 0))
            t_max = int(values.get("temperatureMax", 0))
            code  = int(values.get("weatherCodeMax", values.get("weatherCodeMin", 0)))
            desc  = self._get_weather_description(code)
            future_blocks.append(
                f"{day.strftime('%a %m/%d/%y')} {desc} {t_min}F/{t_max}F"
            )
            if len(future_blocks) >= 5:
                break

        Logger.log(f"Tomorrow.io future_blocks: {future_blocks}", settings)
        return today_blocks, future_blocks


class BetaBrite:
    def __init__(self, port: str, baud: int = 9600):
        self.port = port
        self.baud = baud
        self.ser: Optional[serial.Serial] = None

    def connect(self) -> bool:
        for bytesize, parity in [(7, serial.PARITY_EVEN), (8, serial.PARITY_NONE)]:
            try:
                self.ser = serial.Serial(
                    self.port, self.baud,
                    bytesize=bytesize, parity=parity, stopbits=1, timeout=1
                )
                if bytesize == 8:
                    print("Connected with 8N1 configuration")
                return True
            except serial.SerialException:
                continue
        print(f"Could not open COM port {self.port}")
        return False

    def disconnect(self):
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception as e:
                print(f"Error closing serial port: {e}")

    def send_message(self, text: str, mode: str = "a",
                     settings: Optional[Dict] = None) -> bool:
        if not self.ser or not self.ser.is_open:
            print("Serial port not open")
            return False

        packet = (
            NUL * 10 + SOH + b"Z00" + STX + b"AA"
            + ESC + SP + mode.encode()
            + text.encode("ascii", "ignore") + EOT
        )

        if settings and settings.get("FULL_BETABRITE_LOGGING"):
            hex_repr = ' '.join(f'{b:02X}' for b in packet)
            Logger.log(f"BetaBrite HEX: {hex_repr}", settings)
            Logger.log(f"BetaBrite TXT: {text}", settings)
        else:
            Logger.log(f"Sent to BetaBrite: {text}", settings)

        deadline = time.time() + MAX_SEND_RETRY_TIME
        while True:
            try:
                self.ser.write(packet)
                self.ser.flush()
                time.sleep(SERIAL_WRITE_DELAY)
                return True
            except (serial.SerialException, OSError) as e:
                if time.time() > deadline:
                    Logger.log("Send failed after max retry time", settings)
                    return False
                time.sleep(10)

    def is_connected(self) -> bool:
        return self.ser is not None and self.ser.is_open


# ==================== FORECAST ====================
def _build_colored_blocks(blocks: List[str], mode: str = "future") -> str:
    color_seq = COLORS_TODAY if mode == "today" else COLORS_FUTURE
    return "".join(
        f"{FS}{color_seq[i % len(color_seq)]}{block}  "
        for i, block in enumerate(blocks)
    )


def fetch_forecast_data(settings: Dict, now: datetime) -> Tuple[List[str], List[str]]:
    """Fetch and parse forecast data. Returns (today_blocks, future_blocks)."""
    if settings.get("API_TYPE") == "OpenWeather":
        api = OpenWeatherAPI(settings.get("API_KEY", ""), settings.get("ZIP_CODE", ""))
    else:
        api = TomorrowAPI(settings.get("API_KEY", ""), settings.get("ZIP_CODE", ""),
                          lat=settings.get("LAT"), lon=settings.get("LON"))

    data = api.get_forecast_data()

    if settings.get("FULL_API_LOGGING"):
        Logger.log(f"{api.__class__.__name__} response: {json.dumps(data)}", settings)

    forecast_times = get_forecast_times(now)
    return api.parse_forecast(data, forecast_times, settings)


def format_forecast_message(today_blocks: List[str], future_blocks: List[str],
                             now: datetime) -> str:
    """Build the base forecast string (no alerts appended yet)."""
    colored_text = (
        _build_colored_blocks(today_blocks, "today")
        + _build_colored_blocks(future_blocks, "future")
    )
    next_update = get_next_forecast_update(now)
    next_str = next_update.strftime("%m/%d/%y %I:%M %p").lstrip("0").replace(" 0", " ")
    return colored_text + f" || Next Update: {next_str}"


def assemble_full_message(forecast_message: str, today_blocks: List[str],
                          future_blocks: List[str], now: datetime) -> str:
    """
    Combine forecast with current alert state, truncating if needed.
    Falls back to a shorter version before hard-truncating.
    """
    alert_suffix = build_alert_suffix(state.nhc_active_names, state.nws_active_headlines)
    full = forecast_message + alert_suffix

    if len(full) <= MAX_DISPLAY_MESSAGE_SIZE:
        return full

    # First fallback: trim future days to 3
    shorter_forecast = (
        _build_colored_blocks(today_blocks, "today")
        + _build_colored_blocks(future_blocks[:3], "future")
        + f" || Next Update: {get_next_forecast_update(now).strftime('%m/%d/%y %I:%M %p').lstrip('0').replace(' 0', ' ')}"
    )
    full = shorter_forecast + alert_suffix

    return truncate_message(full)


def send_to_display(betabrite: BetaBrite, message: str, settings: Dict):
    betabrite.send_message(message, settings=settings)


def send_forecast(betabrite: BetaBrite, settings: Dict, now: Optional[datetime] = None):
    """
    Orchestrate a full forecast update:
      fetch → format → store → append alerts → send.
    Called only on scheduled intervals or display-on transitions.
    """
    if now is None:
        now = now_local()

    # Debounce: skip if we updated less than 5 minutes ago
    last_update = state.last_forecast_update
    if last_update and (now - last_update).total_seconds() < 300:
        print("Skipping duplicate forecast update.")
        return

    # Claim the slot immediately so concurrent/re-entrant calls are blocked
    state.last_forecast_update = now

    try:
        today_blocks, future_blocks = fetch_forecast_data(settings, now)

        forecast_message = format_forecast_message(today_blocks, future_blocks, now)
        state.last_forecast_message = forecast_message

        full_message = assemble_full_message(forecast_message, today_blocks, future_blocks, now)
        send_to_display(betabrite, full_message, settings)

        Logger.log("Forecast sent", settings)
        print(f"Forecast updated at {now.strftime('%I:%M %p')}")

    except Exception as e:
        # Clear the slot so the next scheduled attempt will retry
        state.last_forecast_update = None
        Logger.log(f"Forecast error: {e}", settings)
        print(f"Error sending forecast: {e}")
        traceback.print_exc()


def append_alerts_to_display(betabrite: BetaBrite, settings: Dict):
    """Re-send the stored forecast with the current alert state appended."""
    forecast_message = state.last_forecast_message
    if not forecast_message:
        Logger.log("No stored forecast to append alerts to", settings)
        return

    try:
        # We don't have today/future blocks here, so use a conservative truncation
        alert_suffix = build_alert_suffix(state.nhc_active_names, state.nws_active_headlines)
        full = truncate_message(forecast_message + alert_suffix)
        send_to_display(betabrite, full, settings)
        Logger.log("Alerts appended to display", settings)
        print("Alerts appended to display")
    except Exception as e:
        Logger.log(f"Error appending alerts: {e}", settings)
        print(f"Error appending alerts: {e}")


def clear_display(betabrite: BetaBrite, settings: Dict):
    try:
        betabrite.send_message(" ", settings=settings)
        Logger.log("Display cleared (OFF period)", settings)
        print(f"Display cleared — OFF until {settings['ON_TIME']}")
    except Exception as e:
        Logger.log(f"Error clearing display: {e}", settings)


# ==================== ALERTS ====================
class NWSAlerts:
    @staticmethod
    def check_alerts(zone: str, settings: Dict, betabrite: BetaBrite):
        state.mark_nws_pull()
        try:
            url = f"https://api.weather.gov/alerts/active?zone={zone}"
            headers = {"User-Agent": "BetaBriteWeather/1.0"}
            response = requests.get(url, headers=headers, timeout=10)
            if settings.get("FULL_NWS_LOGGING"):
                Logger.log(f"NWS full response: {response.text}", settings)
            response.raise_for_status()
            alerts = response.json().get("features", [])

            if alerts:
                latest_id = alerts[0]["id"]
                headlines = []
                for a in alerts:
                    desc = a.get("properties", {}).get("description", "")
                    if "\n\n" in desc:
                        desc = desc.split("\n\n")[0]
                    desc = desc.replace("\n", " ").strip()
                    if desc:
                        headlines.append(desc)

                state.nws_active_headlines = headlines

                if latest_id != state.last_alert_id:
                    state.last_alert_id = latest_id
                    Logger.log(f"NWS alert: {headlines[0] if headlines else '(no text)'}", settings)
                    print(f"NWS Alert: {headlines[0] if headlines else '(no text)'}")
                    append_alerts_to_display(betabrite, settings)
            else:
                state.last_alert_id = None
                state.nws_active_headlines = []

        except Exception as e:
            Logger.log(f"NWS error: {e}", settings)
            print(f"NWS check failed: {e}")


class NHCMonitor:
    @staticmethod
    def check_storms(settings: Dict, betabrite: BetaBrite):
        """Atlantic basin hurricanes only."""
        state.mark_nhc_pull()
        try:
            headers = {"User-Agent": "BetaBriteWeather/1.0"}
            response = requests.get(NHC_URL, headers=headers, timeout=10)
            if settings.get("FULL_NHC_LOGGING"):
                Logger.log(f"NHC full response: {response.text}", settings)
            response.raise_for_status()
            data = response.json()

            hurricanes = [
                s for s in data.get("activeStorms", [])
                if s.get("classification", "") == "HU"
                and s.get("id", "").lower().startswith("al")
            ]

            if hurricanes:
                names = [h.get("name") for h in hurricanes if h.get("name")]
                state.nhc_active_names = names
                Logger.log(f"NHC Atlantic Hurricane(s): {', '.join(names)}", settings)
                print(f"NHC Atlantic Hurricane(s): {', '.join(names)}")
                append_alerts_to_display(betabrite, settings)
            else:
                state.nhc_active_names = []

        except Exception as e:
            Logger.log(f"NHC error: {e}", settings)
            print(f"NHC check failed: {e}")


# ==================== FRESH POLL ====================
def do_fresh_poll(betabrite: BetaBrite, settings: Dict, reason: str = ""):
    """Sequentially poll NWS → NHC → forecast and update the display."""
    now = now_local()
    print(f"\n{'=' * 50}")
    print(f"FRESH POLL {reason}")
    print(f"{'=' * 50}")

    zone = settings.get("FORECAST_ZONE", "")
    if zone:
        print("Checking NWS alerts...")
        NWSAlerts.check_alerts(zone, settings, betabrite)

    print("Checking NHC storms...")
    NHCMonitor.check_storms(settings, betabrite)

    print("Fetching forecast...")
    send_forecast(betabrite, settings, now)

    next_update = get_next_forecast_update(now)
    print(f"Next forecast update: {next_update.strftime('%I:%M %p')}")
    print(f"{'=' * 50}\n")


# ==================== SERIAL RECONNECT ====================
def reconnect_with_retry(betabrite: BetaBrite, settings: Dict) -> bool:
    """
    Attempt to reconnect up to SERIAL_RECONNECT_ATTEMPTS times,
    waiting SERIAL_RECONNECT_DELAY seconds between each try.
    Returns True if reconnected, False if all attempts failed.
    """
    for attempt in range(1, SERIAL_RECONNECT_ATTEMPTS + 1):
        print(f"\nSerial reconnect attempt {attempt}/{SERIAL_RECONNECT_ATTEMPTS}...")
        Logger.log(f"Serial reconnect attempt {attempt}", settings)
        if betabrite.connect():
            print("Reconnected to BetaBrite")
            Logger.log("Reconnected to BetaBrite", settings)
            return True
        if attempt < SERIAL_RECONNECT_ATTEMPTS:
            print(f"Reconnect failed — retrying in {SERIAL_RECONNECT_DELAY}s")
            time.sleep(SERIAL_RECONNECT_DELAY)
    Logger.log("All serial reconnect attempts failed", settings)
    return False


# ==================== CLI ====================
def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BetaBrite Weather Display System")
    parser.add_argument("--headless",  action="store_true",
                        help="Run without interactive menu (reads from JSON)")
    parser.add_argument("--com",       type=str)
    parser.add_argument("--api-key",   type=str)
    parser.add_argument("--zip",       type=str)
    parser.add_argument("--zone",      type=str)
    parser.add_argument("--api-type",  type=str,
                        choices=["OpenWeather", "Tomorrow.io"], default=None)
    parser.add_argument("--logging",   action="store_true")
    parser.add_argument("--skip-validation", action="store_true",
                        help="Skip live API/port validation on headless start")
    return parser.parse_args()


def build_headless_settings(args: argparse.Namespace) -> Dict:
    """
    Build a settings dict for headless mode.
    CLI args override JSON values; JSON values fill anything not specified on CLI.
    """
    from BetaBriteConfigure import Validator

    settings = Settings.load()

    # CLI overrides
    if args.com:
        settings["COM_PORT"] = args.com
    if args.api_key:
        settings["API_KEY"] = args.api_key
    if args.zip:
        settings["ZIP_CODE"] = args.zip
    if args.zone:
        settings["FORECAST_ZONE"] = args.zone
    if args.api_type:
        settings["API_TYPE"] = args.api_type
    if args.logging:
        settings["LOGGING_ON"] = True

    if not args.skip_validation:
        errors = []
        if not Validator.com_port(settings.get("COM_PORT", "")):
            errors.append("COM port not found or not accessible.")
        api_type = settings.get("API_TYPE", "OpenWeather")
        if not Validator.api_key(settings.get("API_KEY", ""), api_type):
            errors.append("API key invalid or API unreachable.")
        if not Validator.zip_code(settings.get("ZIP_CODE", ""),
                                  settings.get("API_KEY", ""), api_type):
            errors.append("ZIP code invalid.")
        if not Validator.forecast_zone(settings.get("FORECAST_ZONE", "")):
            errors.append("Forecast zone invalid.")
        if errors:
            for e in errors:
                print(f"ERROR: {e}")
            sys.exit(1)

    return settings


def show_exit_message(betabrite: BetaBrite, settings: Dict):
    if not betabrite.is_connected():
        return
    try:
        now = datetime.now()
        formatted_dt = now.strftime("%m/%d/%y %I:%M %p").lstrip("0").replace(" 0", " ")
        message = f"{FS}1Check Program || {formatted_dt}"
        print(f"Sending exit message: Check Program || {formatted_dt}")
        betabrite.send_message(message, settings=settings)
        Logger.log(f"Exit message sent: {formatted_dt}", settings)
        time.sleep(2)
    except Exception as e:
        print(f"Exit message error: {e}")
        traceback.print_exc()


# ==================== MAIN ====================
def main():
    args = parse_arguments()
    print("BetaBrite Weather Display System")
    print("=" * 50)

    if args.headless:
        print("Running in headless mode...")
        settings = build_headless_settings(args)
    else:
        # Interactive mode: launch the configurator first, then run
        from BetaBriteConfigure import run_configure
        settings = Settings.load()
        result = run_configure(settings)
        if result is None:
            # User exited config without saving — still use loaded settings if complete
            from BetaBriteConfigure import Validator
            missing = Validator.settings_complete(settings)
            if missing:
                print(f"Cannot start — missing: {', '.join(missing)}")
                sys.exit(1)
        else:
            settings = result

    Logger.initialize(settings)
    betabrite = BetaBrite(settings.get("COM_PORT", ""))

    if not betabrite.connect():
        print("Failed to connect to BetaBrite. Exiting.")
        Logger.log("Failed to connect to BetaBrite", settings)
        sys.exit(1)

    print("Connected to BetaBrite")
    Logger.log("Program started", settings)
    print(f"\nDisplay Schedule:  ON={settings['ON_TIME']}  OFF={settings['OFF_TIME']}")
    print(f"Timezone: {LOCAL_TZ}")

    # Initialise display state using a consistent `now`
    now = now_local()
    current_active = is_display_active(settings, now)
    state.display_was_active = current_active
    print(f"Display currently: {'ON' if current_active else 'OFF'}")

    if current_active:
        do_fresh_poll(betabrite, settings, f"(Startup at {now.strftime('%I:%M %p')})")
        state.last_forecast_hour = now.hour
    else:
        print(f"Display OFF — clearing display. Will activate at {settings['ON_TIME']}\n")
        clear_display(betabrite, settings)

    next_nws_check = get_next_nws_check(now, False)

    try:
        print("\nMonitoring display — press Ctrl+C to exit\n")

        while True:
            now = now_local()
            display_active = is_display_active(settings, now)
            was_active = state.display_was_active

            # ── DISPLAY STATE TRANSITIONS ─────────────────────────────────
            if display_active and not was_active:
                print(f"\n[{now.strftime('%I:%M:%S %p')}] Display turning ON")
                Logger.log("Display turned ON", settings)
                state.display_was_active = True
                do_fresh_poll(betabrite, settings,
                              f"(ON transition at {now.strftime('%I:%M %p')})")
                state.last_forecast_hour = now.hour
                next_nws_check = get_next_nws_check(now, False)

            elif not display_active and was_active:
                print(f"\n[{now.strftime('%I:%M:%S %p')}] Display turning OFF")
                Logger.log("Display turned OFF", settings)
                state.display_was_active = False
                clear_display(betabrite, settings)

            elif display_active:
                # ── SCHEDULED FORECAST (0, 3, 6, 9, 12, 15, 18, 21) ──────
                # Guard: use a timestamp rather than just hour to avoid
                # re-triggering if the sleep overshoots the exact minute.
                if now.hour in SCHEDULED_HOURS and now.minute == 0 and now.second < 5:
                    if state.last_forecast_hour != now.hour:
                        print(f"\n[{now.strftime('%I:%M:%S %p')}] Scheduled forecast update")
                        Logger.log(f"Scheduled forecast at {now.strftime('%I:%M %p')}", settings)
                        do_fresh_poll(betabrite, settings,
                                      f"(Scheduled at {now.strftime('%I:%M %p')})")
                        state.last_forecast_hour = now.hour
                        next_nws_check = get_next_nws_check(now, False)

                # ── NWS CHECKS ────────────────────────────────────────────
                zone = settings.get("FORECAST_ZONE", "")
                if zone and now >= next_nws_check:
                    was_alert_active = state.last_alert_id is not None
                    NWSAlerts.check_alerts(zone, settings, betabrite)
                    is_alert_active = state.last_alert_id is not None

                    if was_alert_active != is_alert_active:
                        print("Alert status changed — updating display")
                        append_alerts_to_display(betabrite, settings)

                    if is_alert_active:
                        next_nws_check = get_next_nws_check(now, True)
                    elif was_alert_active and not is_alert_active:
                        next_nws_check = get_nearest_5min_mark(now)
                        print(f"Alert expired — next NWS check at {next_nws_check.strftime('%I:%M %p')}")
                    else:
                        next_nws_check = get_next_nws_check(now, False)

                # ── NHC CHECKS ────────────────────────────────────────────
                if should_check_nhc(now, state.last_nhc_pull):
                    NHCMonitor.check_storms(settings, betabrite)

            # ── SERIAL RECONNECT ──────────────────────────────────────────
            if not betabrite.is_connected():
                print("\nSerial port disconnected.")
                Logger.log("Serial port disconnected", settings)
                if not reconnect_with_retry(betabrite, settings):
                    print("Could not reconnect after all attempts. Exiting.")
                    break
                # Re-send forecast after reconnect so the display is not blank
                if display_active:
                    send_forecast(betabrite, settings, now_local())

            if state.should_shutdown():
                break

            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\nShutdown signal received...")
        Logger.log("Shutdown initiated by user", settings)
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        Logger.log(f"Fatal error: {e}", settings)
        traceback.print_exc()
    finally:
        print("Cleaning up...")
        state.shutdown()
        time.sleep(1)
        show_exit_message(betabrite, settings)
        betabrite.disconnect()
        Logger.log("Program stopped", settings)
        print("Shutdown complete")


if __name__ == "__main__":
    main()
