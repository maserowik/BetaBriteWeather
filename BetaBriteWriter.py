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
import time as time_module

# ==================== CONSTANTS ====================
SETTINGS_FILE = "BetaBriteWriter.json"
LOG_FILE = "BetaBriteWriter.log"
MAX_LOG_BACKUPS = 5
MAX_LOG_BYTES = 2 * 1024 * 1024

# Serial Protocol
NUL = b'\x00'
SOH = b'\x01'
STX = b'\x02'
EOT = b'\x04'
ESC = b'\x1B'
SP = b'\x20'

# Timing
SERIAL_WRITE_DELAY = 0.2
MAX_SEND_RETRY_TIME = 300
MAX_API_RETRIES = 3
API_RETRY_DELAY = 5

# Display
FS = "\x1C"
COLORS_TODAY = ["3"]
COLORS_FUTURE = ["4", "5", "6", "7", "8"]
ALERT_COLOR = "1"
SCHEDULED_HOURS = [0, 3, 6, 9, 12, 15, 18, 21]
NWS_SCHEDULED_MINUTES = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
NHC_SCHEDULED_HOURS = [5, 11, 17, 23]

# URLs
NHC_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"

# Weather codes
TOMORROW_WEATHER_CODES = {
    0: "Unknown", 1000: "Clear", 1100: "Mostly Clear", 1101: "Partly Cloudy",
    1102: "Mostly Cloudy", 1001: "Cloudy", 2000: "Fog", 2100: "Light Fog",
    4000: "Drizzle", 4001: "Rain", 4200: "Light Rain", 4201: "Heavy Rain",
    5000: "Snow", 5001: "Flurries", 5100: "Light Snow", 5101: "Heavy Snow",
    6000: "Freezing Drizzle", 6001: "Freezing Rain", 6200: "Light Freezing Rain",
    6201: "Heavy Freezing Rain", 7000: "Ice Pellets", 7101: "Heavy Ice Pellets",
    7102: "Light Ice Pellets", 8000: "Thunderstorm"
}

DEFAULT_TIMEZONE = pytz.timezone("America/New_York")

# Auto-detect timezone
try:
    if hasattr(time_module, 'tzname') and time_module.tzname[0]:
        local_tz_name = time_module.tzname[time_module.daylight]
        DEFAULT_TIMEZONE = pytz.timezone(local_tz_name)
    else:
        DEFAULT_TIMEZONE = pytz.timezone("America/New_York")
except:
    DEFAULT_TIMEZONE = pytz.timezone("America/New_York")

MAX_DISPLAY_MESSAGE_SIZE = 2048

# ==================== HELPER FUNCTIONS ====================
def aggregate_temperatures(entries: List[Dict]) -> Tuple[int, int]:
    if not entries:
        return 0, 0
    temps_min = [int(entry["main"]["temp_min"]) for entry in entries]
    temps_max = [int(entry["main"]["temp_max"]) for entry in entries]
    return min(temps_min), max(temps_max)

# ==================== GLOBAL STATE ====================
class ThreadSafeState:
    def __init__(self):
        self._lock = threading.Lock()
        self.last_forecast_update: Optional[datetime] = None
        self.last_alert_id: Optional[str] = None
        self.last_nws_pull: datetime = datetime.min
        self.last_nhc_pull: datetime = datetime.min
        self.nhc_active_names: List[str] = []
        self.nws_active_headlines: List[str] = []
        self.display_was_active: Optional[bool] = None
        self.last_forecast_hour: Optional[int] = None
        self.shutdown_event = threading.Event()

    def set_last_forecast_update(self, update_time: datetime):
        with self._lock:
            self.last_forecast_update = update_time

    def get_last_forecast_update(self) -> Optional[datetime]:
        with self._lock:
            return self.last_forecast_update

    def set_last_forecast_hour(self, hour: int):
        with self._lock:
            self.last_forecast_hour = hour

    def get_last_forecast_hour(self) -> Optional[int]:
        with self._lock:
            return self.last_forecast_hour

    def set_alert_id(self, alert_id: Optional[str]):
        with self._lock:
            self.last_alert_id = alert_id

    def get_alert_id(self) -> Optional[str]:
        with self._lock:
            return self.last_alert_id

    def set_nws_headlines(self, headlines: List[str]):
        with self._lock:
            self.nws_active_headlines = headlines.copy()

    def get_nws_headlines(self) -> List[str]:
        with self._lock:
            return self.nws_active_headlines.copy()

    def set_nhc_names(self, names: List[str]):
        with self._lock:
            self.nhc_active_names = names.copy()

    def get_nhc_names(self) -> List[str]:
        with self._lock:
            return self.nhc_active_names.copy()

    def update_nws_pull(self):
        with self._lock:
            self.last_nws_pull = datetime.now()

    def get_nws_pull_time(self) -> datetime:
        with self._lock:
            return self.last_nws_pull

    def update_nhc_pull(self):
        with self._lock:
            self.last_nhc_pull = datetime.now()

    def get_nhc_pull_time(self) -> datetime:
        with self._lock:
            return self.last_nhc_pull

    def set_display_state(self, was_active: bool):
        with self._lock:
            self.display_was_active = was_active

    def get_display_state(self) -> Optional[bool]:
        with self._lock:
            return self.display_was_active

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

    @staticmethod
    def save(settings: Dict) -> bool:
        try:
            temp_file = SETTINGS_FILE + ".tmp"
            with open(temp_file, "w") as f:
                json.dump(settings, f, indent=4)
            os.replace(temp_file, SETTINGS_FILE)
            return True
        except Exception as e:
            print(f"Error saving settings: {e}")
            return False

    @staticmethod
    def delete() -> bool:
        try:
            if os.path.exists(SETTINGS_FILE):
                os.remove(SETTINGS_FILE)
            return True
        except Exception as e:
            print(f"Could not delete settings file: {e}")
            return False


# ==================== LOGGING ====================
def setup_logger(settings: Dict) -> logging.Logger:
    logger = logging.getLogger("BetaBrite")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    if settings.get("LOGGING_ON"):
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


# ==================== VALIDATION ====================
class Validator:
    @staticmethod
    def com_port(port: str) -> bool:
        if not port:
            return False
        ports = list_ports.comports()
        return any(port.lower() in p.device.lower() for p in ports)

    @staticmethod
    def api_key(api_key: str) -> bool:
        if not api_key:
            return False
        test_zip = "10001"
        url = f"http://api.openweathermap.org/data/2.5/forecast?zip={test_zip},US&appid={api_key}"
        for attempt in range(MAX_API_RETRIES):
            try:
                response = requests.get(url, timeout=5)
                return response.status_code == 200
            except requests.RequestException:
                if attempt < MAX_API_RETRIES - 1:
                    time.sleep(API_RETRY_DELAY)
        return False

    @staticmethod
    def zip_code(zip_code: str, api_key: str) -> bool:
        if not (zip_code.isdigit() and len(zip_code) == 5):
            return False
        if not api_key:
            return False
        url = f"http://api.openweathermap.org/data/2.5/forecast?zip={zip_code},US&appid={api_key}"
        for attempt in range(MAX_API_RETRIES):
            try:
                response = requests.get(url, timeout=5)
                return response.status_code == 200
            except requests.RequestException:
                if attempt < MAX_API_RETRIES - 1:
                    time.sleep(API_RETRY_DELAY)
        return False

    @staticmethod
    def forecast_zone(zone: str) -> bool:
        if not zone:
            return False
        zone = zone.upper()
        url = f"https://api.weather.gov/zones/forecast/{zone}"
        for attempt in range(MAX_API_RETRIES):
            try:
                response = requests.get(url, timeout=5)
                return response.status_code == 200
            except requests.RequestException:
                if attempt < MAX_API_RETRIES - 1:
                    time.sleep(API_RETRY_DELAY)
        return False

    @staticmethod
    def time_format(timestr: str) -> bool:
        try:
            datetime.strptime(timestr, "%H:%M")
            return True
        except ValueError:
            return False


# ==================== TIME MANAGEMENT ====================
def is_display_active(settings: Dict, now: Optional[datetime] = None) -> bool:
    if now is None:
        now = datetime.now(DEFAULT_TIMEZONE)

    current_time = now.time()
    on_time = datetime.strptime(settings["ON_TIME"], "%H:%M").time()
    off_time = datetime.strptime(settings["OFF_TIME"], "%H:%M").time()

    if on_time < off_time:
        # Normal case: ON=06:00, OFF=22:00 -> active between 06:00 and 22:00
        return on_time <= current_time < off_time
    else:
        # Overnight case: ON=22:00, OFF=06:00 -> active from 22:00 to 06:00
        return current_time >= on_time or current_time < off_time


def clear_display(betabrite, settings: Dict):
    """Send blank message to clear display"""
    try:
        message = " "
        betabrite.send_message(message, settings=settings)
        Logger.log("Display cleared (OFF period)", settings)
        print(f"Display cleared - OFF until {settings['ON_TIME']}")
    except Exception as e:
        Logger.log(f"Error clearing display: {e}", settings)


def get_forecast_times(now: datetime) -> List[datetime]:
    """
    Get forecast times: current time + next 2 scheduled hours
    If at a scheduled hour, use that + next 2
    """
    # Ensure now is timezone-aware
    if now.tzinfo is None:
        now = DEFAULT_TIMEZONE.localize(now)

    times = []

    # Check if we're at a scheduled hour (within first 5 minutes)
    if now.hour in SCHEDULED_HOURS and now.minute < 5:
        current = now.replace(minute=0, second=0, microsecond=0)
    else:
        # Use actual current time
        current = now

    times.append(current)

    # Find next 2 scheduled hours
    current_hour = current.hour
    for _ in range(2):
        next_hours = [h for h in SCHEDULED_HOURS if h > current_hour]
        if next_hours:
            next_hour = next_hours[0]
            next_time = current.replace(hour=next_hour, minute=0, second=0, microsecond=0)
        else:
            next_hour = SCHEDULED_HOURS[0]
            next_time = current.replace(hour=next_hour, minute=0, second=0, microsecond=0) + timedelta(days=1)

        times.append(next_time)
        current = next_time
        current_hour = next_hour

    return times


def get_next_forecast_update(now: datetime) -> datetime:
    """Calculate next scheduled forecast update time"""
    current_hour = now.hour

    # Find next scheduled hour
    next_hours = [h for h in SCHEDULED_HOURS if h > current_hour]
    if next_hours:
        next_hour = next_hours[0]
        return now.replace(hour=next_hour, minute=0, second=0, microsecond=0)
    else:
        # Next day at first scheduled hour
        next_hour = SCHEDULED_HOURS[0]
        return (now + timedelta(days=1)).replace(hour=next_hour, minute=0, second=0, microsecond=0)


def get_next_nws_check(now: datetime, alert_active: bool) -> datetime:
    """Calculate next NWS check time"""
    if alert_active:
        # Every 2 minutes when alert active
        return now + timedelta(minutes=2)
    else:
        # Next 5-minute mark
        for m in NWS_SCHEDULED_MINUTES:
            if now.minute < m:
                return now.replace(minute=m, second=0, microsecond=0)
        # Next hour at :00
        next_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return next_time


def get_nearest_5min_mark(now: datetime) -> datetime:
    """Get nearest 5-minute mark on or after now"""
    for m in NWS_SCHEDULED_MINUTES:
        if now.minute <= m:
            return now.replace(minute=m, second=0, microsecond=0)
    # Next hour at :00
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def should_check_nhc(now: datetime, last_check: datetime) -> bool:
    """Check if it's time for NHC update (5, 11, 17, 23)"""
    if now.hour not in NHC_SCHEDULED_HOURS:
        return False

    # Only check once per hour (within first 5 minutes)
    if now.minute >= 5:
        return False

    # Don't check if we already checked this hour
    if last_check.hour == now.hour and last_check.date() == now.date():
        return False

    return True


# ==================== RETRY LOGIC ====================
def retry_request(func, *args, **kwargs):
    for attempt in range(MAX_API_RETRIES):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt < MAX_API_RETRIES - 1:
                time.sleep(API_RETRY_DELAY)
            else:
                raise e


# ==================== WEATHER API ====================
class WeatherAPI(ABC):
    def __init__(self, api_key: str, zip_code: str):
        self.api_key = api_key
        self.zip_code = zip_code
        self.headers = {"User-Agent": "BetaBriteWeather/1.0"}

    @abstractmethod
    def get_forecast_data(self) -> Dict:
        pass

    @abstractmethod
    def parse_forecast(self, data: Dict, forecast_times: List[datetime], settings: Dict) -> Tuple[List[str], List[str]]:
        pass


class OpenWeatherAPI(WeatherAPI):
    def get_forecast_data(self) -> Dict:
        url = f"http://api.openweathermap.org/data/2.5/forecast?zip={self.zip_code},us&units=imperial&appid={self.api_key}"
        return retry_request(requests.get, url, headers=self.headers, timeout=10).json()

    def parse_forecast(self, data: Dict, forecast_times: List[datetime], settings: Dict) -> Tuple[List[str], List[str]]:
        daily_forecast = defaultdict(list)
        for entry in data.get("list", []):
            # FIXED: Convert to local timezone
            dt = datetime.fromtimestamp(entry["dt"], tz=pytz.UTC).astimezone(DEFAULT_TIMEZONE)
            daily_forecast[dt.date()].append(entry)

        today_blocks = []
        for f_time in forecast_times:
            entries = daily_forecast.get(f_time.date(), [])
            if not entries:
                continue
            entry = min(entries, key=lambda x: abs(datetime.fromtimestamp(x["dt"], tz=pytz.UTC).astimezone(DEFAULT_TIMEZONE) - f_time))
            desc = entry["weather"][0]["main"]
            t_min, t_max = aggregate_temperatures(entries)
            today_blocks.append(f"{f_time.strftime('%I:%M %p %a %m/%d/%y')} {desc} {t_min}F/{t_max}F")

        Logger.log(f"Parsed Today Blocks: {today_blocks}", settings)

        future_blocks = []
        now = datetime.now(DEFAULT_TIMEZONE)
        future_days = sorted([d for d in daily_forecast.keys() if d > now.date()])[:5]
        for day in future_days:
            temps_min, temps_max, conditions = [], [], []
            for entry in daily_forecast[day]:
                temps_min.append(int(entry["main"]["temp_min"]))
                temps_max.append(int(entry["main"]["temp_max"]))
                conditions.append(entry["weather"][0]["main"])
            most_common = Counter(conditions).most_common(1)[0][0]
            future_blocks.append(f"{day.strftime('%a %m/%d/%y')} {most_common} {min(temps_min)}F/{max(temps_max)}F")

        return today_blocks, future_blocks


class TomorrowAPI(WeatherAPI):
    def get_forecast_data(self) -> Dict:
        url = f"https://api.tomorrow.io/v4/timelines?location={self.zip_code}&fields=temperature,weatherCode&units=imperial&timesteps=1h&apikey={self.api_key}"
        return retry_request(requests.get, url, headers=self.headers, timeout=10).json()

    def _get_weather_description(self, code: int) -> str:
        return TOMORROW_WEATHER_CODES.get(code, "Unknown")

    def parse_forecast(self, data: Dict, forecast_times: List[datetime], settings: Dict) -> Tuple[List[str], List[str]]:
        daily_forecast = defaultdict(list)
        for timeline in data.get("data", {}).get("timelines", []):
            for entry in timeline.get("intervals", []):
                dt_str = entry.get("startTime", "")
                if not dt_str:
                    continue
                dt = isoparse(dt_str)
                daily_forecast[dt.date()].append({"dt": dt, "values": entry.get("values", {})})

        today_blocks = []
        for f_time in forecast_times:
            entries = daily_forecast.get(f_time.date(), [])
            if not entries:
                continue
            entry = min(entries, key=lambda x: abs(x["dt"] - f_time))
            values = entry["values"]
            desc = self._get_weather_description(values.get("weatherCode", 0))
            temp = int(values.get("temperature", 0))
            today_blocks.append(f"{f_time.strftime('%I:%M %p %a %m/%d/%y')} {desc} {temp}F/{temp}F")

        future_blocks = []
        now = datetime.now(DEFAULT_TIMEZONE)
        future_days = sorted([d for d in daily_forecast.keys() if d > now.date()])[:5]
        for day in future_days:
            temps, weather_codes = [], []
            for entry in daily_forecast[day]:
                temps.append(int(entry["values"].get("temperature", 0)))
                weather_codes.append(entry["values"].get("weatherCode", 0))
            if temps and weather_codes:
                most_common_code = Counter(weather_codes).most_common(1)[0][0]
                desc = self._get_weather_description(most_common_code)
                future_blocks.append(f"{day.strftime('%a %m/%d/%y')} {desc} {min(temps)}F/{max(temps)}F")

        return today_blocks, future_blocks


# ==================== BETABRITE ====================
class BetaBrite:
    def __init__(self, port: str, baud: int = 9600):
        self.port = port
        self.baud = baud
        self.ser: Optional[serial.Serial] = None

    def connect(self) -> bool:
        try:
            self.ser = serial.Serial(self.port, self.baud, bytesize=7, parity=serial.PARITY_EVEN, stopbits=1, timeout=1)
            return True
        except serial.SerialException:
            try:
                self.ser = serial.Serial(self.port, self.baud, bytesize=8, parity=serial.PARITY_NONE, stopbits=1,
                                         timeout=1)
                print("Connected with 8N1 configuration")
                return True
            except serial.SerialException as e:
                print(f"Could not open COM port {self.port}: {e}")
                return False

    def disconnect(self):
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception as e:
                print(f"Error closing serial port: {e}")

    def send_message(self, text: str, mode: str = "a", settings: Optional[Dict] = None) -> bool:
        if not self.ser or not self.ser.is_open:
            print("Serial port not open")
            return False

        packet = (NUL * 10 + SOH + b"Z00" + STX + b"AA" + ESC + SP + mode.encode() + text.encode("ascii",
                                                                                                 "ignore") + EOT)

        # Full BetaBrite logging - log hex and full text
        if settings and settings.get("FULL_BETABRITE_LOGGING"):
            hex_repr = ' '.join(f'{b:02X}' for b in packet)
            Logger.log(f"BetaBrite FULL HEX: {hex_repr}", settings)
            Logger.log(f"BetaBrite FULL TEXT: {text}", settings)
        else:
            # Normal logging - log complete text without truncation
            Logger.log(f"Sent to BetaBrite: {text}", settings)

        start_time = time.time()

        while True:
            try:
                self.ser.write(packet)
                self.ser.flush()
                time.sleep(SERIAL_WRITE_DELAY)
                return True
            except (serial.SerialException, OSError) as e:
                elapsed = time.time() - start_time
                if elapsed > MAX_SEND_RETRY_TIME:
                    Logger.log("Send failed after max retries", settings)
                    return False
                time.sleep(10)

    def is_connected(self) -> bool:
        return self.ser is not None and self.ser.is_open


# ==================== FORECAST ====================
def build_colored_blocks(blocks: List[str], mode: str = "future") -> str:
    color_seq = COLORS_TODAY if mode == "today" else COLORS_FUTURE
    result = ""
    for i, block in enumerate(blocks):
        color = color_seq[i % len(color_seq)]
        result += f"{FS}{color}{block}  "
    return result


# ==================== ALERTS ====================
class NWSAlerts:
    @staticmethod
    def check_alerts(zone: str, settings: Dict):
        """Check NWS alerts - only called when display is ON"""
        state.update_nws_pull()
        try:
            url = f"https://api.weather.gov/alerts/active?zone={zone}"
            headers = {"User-Agent": "BetaBriteWeather/1.0"}
            response = requests.get(url, headers=headers, timeout=10)
            if settings.get("FULL_NWS_LOGGING"):
                Logger.log(f"NWS full response: {response.text}", settings)
            response.raise_for_status()
            data = response.json()
            alerts = data.get("features", [])

            if alerts:
                latest = alerts[0]["id"]
                headlines = [a["properties"]["headline"] for a in alerts if "headline" in a["properties"]]
                state.set_nws_headlines(headlines)
                if latest != state.get_alert_id():
                    state.set_alert_id(latest)
                    Logger.log(f"NWS alert: {headlines[0]}", settings)
                    print(f"NWS Alert: {headlines[0]}")
            else:
                state.set_alert_id(None)
                state.set_nws_headlines([])
        except Exception as e:
            Logger.log(f"NWS error: {e}", settings)
            print(f"NWS check failed: {e}")


class NHCMonitor:
    @staticmethod
    def check_storms(settings: Dict):
        """Check NHC storms - ATLANTIC BASIN ONLY"""
        state.update_nhc_pull()
        try:
            headers = {"User-Agent": "BetaBriteWeather/1.0"}
            response = requests.get(NHC_URL, headers=headers, timeout=10)
            if settings.get("FULL_NHC_LOGGING"):
                Logger.log(f"NHC full response: {response.text}", settings)
            response.raise_for_status()
            data = response.json()

            # Filter for Atlantic basin hurricanes only
            hurricanes = [
                s for s in data.get("activeStorms", [])
                if s.get("classification", "") == "HU" and s.get("basin", "").upper() == "AL"
            ]

            if hurricanes:
                names = [h.get("name") for h in hurricanes if h.get("name")]
                state.set_nhc_names(names)
                Logger.log(f"NHC Atlantic Hurricane(s): {', '.join(names)}", settings)
                print(f"NHC Atlantic Hurricane(s): {', '.join(names)}")
            else:
                state.set_nhc_names([])
        except Exception as e:
            Logger.log(f"NHC error: {e}", settings)
            print(f"NHC check failed: {e}")


# ==================== FORECAST SENDER ====================
def send_forecast(betabrite: BetaBrite, settings: Dict, now: Optional[datetime] = None):
    """Send complete forecast with alerts"""
    if now is None:
        now = datetime.now(DEFAULT_TIMEZONE)

    last_update = state.get_last_forecast_update()
    if last_update and (now - last_update).total_seconds() < 300:
        print("Skipping duplicate forecast update.")
        return

    try:
        state.set_last_forecast_update(now)

        # Get forecast times
        forecast_times = get_forecast_times(now)

        # Fetch weather data
        if settings.get("API_TYPE") == "OpenWeather":
            api = OpenWeatherAPI(settings.get("API_KEY"), settings.get("ZIP_CODE"))
        else:
            api = TomorrowAPI(settings.get("API_KEY"), settings.get("ZIP_CODE"))

        data = api.get_forecast_data()

        if settings.get("FULL_API_LOGGING"):
            Logger.log(f"{api.__class__.__name__} response: {json.dumps(data)}", settings)

        today_blocks, future_blocks = api.parse_forecast(data, forecast_times, settings)

        # Build display text
        colored_text = build_colored_blocks(today_blocks, "today") + build_colored_blocks(future_blocks, "future")
        next_update = get_next_forecast_update(now)
        next_update_str = next_update.strftime("%m/%d/%y %I:%M %p").lstrip('0').replace(' 0', ' ')
        update_text = f" || Next Update: {next_update_str}"

        # Add NHC hurricanes
        nhc_names = state.get_nhc_names()
        nhc_text = f" || {FS}{ALERT_COLOR}NHC Atlantic Hurricane(s): {', '.join(nhc_names)}" if nhc_names else ""

        # Add NWS alerts at the END
        nws_headlines = state.get_nws_headlines()
        nws_text = "".join([f" || {FS}{ALERT_COLOR}NWS Alert: {headline}" for headline in nws_headlines])

        # Build complete message first
        full_message = colored_text + update_text + nhc_text + nws_text

        # Then truncate if needed, prioritizing today_blocks
        if len(full_message) > MAX_DISPLAY_MESSAGE_SIZE:
            truncated_future = build_colored_blocks(future_blocks[:3], "future")  # Limit future blocks
            full_message = build_colored_blocks(today_blocks, "today") + truncated_future + update_text + nhc_text + nws_text
            if len(full_message) > MAX_DISPLAY_MESSAGE_SIZE:
                full_message = full_message[:MAX_DISPLAY_MESSAGE_SIZE - 3] + "..."

        # Send to display
        betabrite.send_message(full_message, settings=settings)
        Logger.log("Forecast sent", settings)
        print(f"Forecast updated at {now.strftime('%I:%M %p')}")

    except Exception as e:
        state.set_last_forecast_update(None)
        Logger.log(f"Forecast error: {e}", settings)
        print(f"Error sending forecast: {e}")
        traceback.print_exc()


# ==================== CLI ====================
def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BetaBrite Weather Display System")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--com", type=str)
    parser.add_argument("--api-key", type=str)
    parser.add_argument("--zip", type=str)
    parser.add_argument("--zone", type=str)
    parser.add_argument("--api-type", type=str, choices=["OpenWeather", "Tomorrow.io"], default="OpenWeather")
    parser.add_argument("--logging", action="store_true")
    return parser.parse_args()


def validate_headless_settings(args: argparse.Namespace) -> Dict:
    if not args.headless:
        return None

    errors = []
    if not args.com or not Validator.com_port(args.com):
        errors.append("Invalid COM port. Ensure the device is connected and the port is correct.")
    if not args.api_key or not Validator.api_key(args.api_key):
        errors.append("Invalid API key. Check your API provider for the correct key.")
    if not args.zip or not Validator.zip_code(args.zip, args.api_key or ""):
        errors.append("Invalid ZIP code. Ensure it is a valid 5-digit US ZIP code.")
    if not args.zone or not Validator.forecast_zone(args.zone):
        errors.append("Invalid forecast zone. Check the National Weather Service for valid zones.")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        sys.exit(1)

    return {
        "COM_PORT": args.com,
        "API_TYPE": args.api_type,
        "API_KEY": args.api_key,
        "ZIP_CODE": args.zip,
        "FORECAST_ZONE": args.zone,
        "ON_TIME": "06:00",
        "OFF_TIME": "22:00",
        "LOGGING_ON": args.logging,
        "FULL_API_LOGGING": False,
        "FULL_NHC_LOGGING": False,
        "FULL_NWS_LOGGING": False,
        "FULL_BETABRITE_LOGGING": False
    }


def review_settings(settings: Dict) -> Dict:
    valid_choices = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "D", "S", "L", "0"}
    while True:
        print("\n" + "=" * 50)
        print("       BETABRITE WEATHER DISPLAY SYSTEM")
        print("=" * 50)
        print("1.  View Current Settings")
        print("2.  Update COM Port")
        print("3.  Update ZIP Code")
        print("4.  Update ON/OFF Times")
        print("5.  Select Weather API")
        print("6.  Update API Key")
        print("7.  Update Forecast Zone")
        print("8.  Toggle Full API Logging")
        print("9.  Toggle Full NHC Logging")
        print("10. Toggle Full NWS Logging")
        print("11. Toggle Full BetaBrite Logging")
        print("D.  Delete Settings File")
        print("S.  Start Weather Display")
        print("L.  Toggle Logging ON/OFF")
        print("0.  Exit Program")
        print("=" * 50)
        choice = input("Select an option: ").strip().upper()
        if choice not in valid_choices:
            continue

        if choice == "1":
            print("\n" + json.dumps(settings, indent=4))
        elif choice == "2":
            current = settings.get("COM_PORT", "")
            settings["COM_PORT"] = input(f"Enter COM port [current: {current or 'none'}]: ").strip() or current
        elif choice == "3":
            current = settings.get("ZIP_CODE", "")
            key = settings.get("API_KEY", "")
            if not key:
                print("Please set API Key first (option 6)")
                continue
            val = input(f"Enter ZIP Code [current: {current or 'none'}]: ").strip() or current
            if Validator.zip_code(val, key):
                settings["ZIP_CODE"] = val
        elif choice == "4":
            current_on = settings.get("ON_TIME", "06:00")
            current_off = settings.get("OFF_TIME", "22:00")
            on_time = input(f"Enter ON_TIME (HH:MM 24h) [current: {current_on}]: ").strip() or current_on
            off_time = input(f"Enter OFF_TIME (HH:MM 24h) [current: {current_off}]: ").strip() or current_off
            if Validator.time_format(on_time) and Validator.time_format(off_time):
                settings["ON_TIME"] = on_time
                settings["OFF_TIME"] = off_time
            else:
                print("Invalid time format. Use HH:MM (24-hour)")
        elif choice == "5":
            print("\nSelect Weather API:")
            print("  1. OpenWeather")
            print("  2. Tomorrow.io")
            api_choice = input("Enter choice: ").strip()
            if api_choice == "1":
                settings["API_TYPE"] = "OpenWeather"
            elif api_choice == "2":
                settings["API_TYPE"] = "Tomorrow.io"
        elif choice == "6":
            current = settings.get("API_KEY", "")
            key = input(f"Enter API Key [current: {'*' * len(current) if current else 'none'}]: ").strip()
            if key and Validator.api_key(key):
                settings["API_KEY"] = key
                print("API Key validated")
            elif not key and current:
                pass
            else:
                print("Invalid API Key")
        elif choice == "7":
            current = settings.get("FORECAST_ZONE", "")
            val = input(f"Enter Forecast Zone [current: {current or 'none'}]: ").strip() or current
            if Validator.forecast_zone(val):
                settings["FORECAST_ZONE"] = val
                print("Forecast Zone validated")
        elif choice == "8":
            settings["FULL_API_LOGGING"] = not settings.get("FULL_API_LOGGING", False)
            print(f"Full API logging is now {'ON' if settings['FULL_API_LOGGING'] else 'OFF'}")
        elif choice == "9":
            settings["FULL_NHC_LOGGING"] = not settings.get("FULL_NHC_LOGGING", False)
            print(f"Full NHC logging is now {'ON' if settings['FULL_NHC_LOGGING'] else 'OFF'}")
        elif choice == "10":
            settings["FULL_NWS_LOGGING"] = not settings.get("FULL_NWS_LOGGING", False)
            print(f"Full NWS logging is now {'ON' if settings['FULL_NWS_LOGGING'] else 'OFF'}")
        elif choice == "11":
            settings["FULL_BETABRITE_LOGGING"] = not settings.get("FULL_BETABRITE_LOGGING", False)
            print(f"Full BetaBrite logging is now {'ON' if settings['FULL_BETABRITE_LOGGING'] else 'OFF'}")
        elif choice == "D":
            confirm = input("Are you sure you want to delete settings? [N/y]: ").strip().lower()
            if confirm == "y":
                if Settings.delete():
                    print("Settings file deleted.")
                    settings = Settings.load()
        elif choice == "S":
            required = []
            if not settings.get("COM_PORT"):
                required.append("COM Port")
            if not settings.get("API_KEY"):
                required.append("API Key")
            if not settings.get("ZIP_CODE"):
                required.append("ZIP Code")
            if not settings.get("FORECAST_ZONE"):
                required.append("Forecast Zone")
            if required:
                print(f"Missing required settings: {', '.join(required)}")
                input("Press Enter to continue...")
                continue
            break
        elif choice == "L":
            settings["LOGGING_ON"] = not settings.get("LOGGING_ON", False)
            print(f"Logging is now {'ON' if settings['LOGGING_ON'] else 'OFF'}")
        elif choice == "0":
            sys.exit(0)

        Settings.save(settings)
    return settings


def show_exit_message(betabrite: BetaBrite, settings: Dict):
    """Always show exit message regardless of ON/OFF schedule"""
    if not betabrite.is_connected():
        return
    try:
        now = datetime.now()
        formatted_dt = now.strftime("%m/%d/%y %I:%M %p")
        formatted_dt = formatted_dt.lstrip('0').replace(' 0', ' ')
        message = f"{FS}1Check Program || {formatted_dt}"

        print(f"Sending exit message: Check Program || {formatted_dt}")
        betabrite.send_message(message, settings=settings)
        Logger.log(f"Exit message sent: {formatted_dt}", settings)
        time.sleep(2)
    except Exception as e:
        print(f"Exit message error: {e}")
        traceback.print_exc()


def do_fresh_poll(betabrite: BetaBrite, settings: Dict, reason: str = ""):
    """Do complete fresh poll of all APIs"""
    now = datetime.now()
    print(f"\n{'=' * 50}")
    print(f"FRESH POLL {reason}")
    print(f"{'=' * 50}")

    # Poll NWS
    zone = settings.get("FORECAST_ZONE", "")
    if zone:
        print("Checking NWS alerts...")
        NWSAlerts.check_alerts(zone, settings)

    # Poll NHC
    print("Checking NHC storms...")
    NHCMonitor.check_storms(settings)

    # Send forecast
    print("Fetching forecast...")
    send_forecast(betabrite, settings, now)

    # Show next update time
    next_update = get_next_forecast_update(now)
    print(f"Next forecast update: {next_update.strftime('%I:%M %p')}")
    print(f"{'=' * 50}\n")


def main():
    args = parse_arguments()
    print("BetaBrite Weather Display System")
    print("=" * 50)

    if args.headless:
        print("Running in headless mode...")
        settings = validate_headless_settings(args)
    else:
        settings = Settings.load()
        settings = review_settings(settings)

    Logger.initialize(settings)
    betabrite = BetaBrite(settings.get("COM_PORT"))

    if not betabrite.connect():
        print("Failed to connect to BetaBrite. Exiting.")
        Logger.log("Failed to connect to BetaBrite", settings)
        sys.exit(1)

    print("Connected to BetaBrite")
    Logger.log("Program started", settings)

    print(f"\nDisplay Schedule:")
    print(f"  ON:  {settings['ON_TIME']}")
    print(f"  OFF: {settings['OFF_TIME']}")

    # Initialize display state
    now = datetime.now()
    current_state = is_display_active(settings, now)
    state.set_display_state(current_state)
    print(f"  Current: {'ON' if current_state else 'OFF'}")

    # If starting in ON period, do fresh poll
    if current_state:
        do_fresh_poll(betabrite, settings, f"(Startup at {now.strftime('%I:%M %p')})")
        state.set_last_forecast_hour(now.hour)
    else:
        # Display is OFF - clear any existing message
        print(f"  Display OFF - clearing display\n")
        clear_display(betabrite, settings)
        print(f"  Will activate at {settings['ON_TIME']}\n")

    next_nws_check = get_next_nws_check(now, False)

    try:
        print("\nMonitoring display...")
        print("Press Ctrl+C to exit\n")

        while True:
            now = datetime.now()
            display_active = is_display_active(settings, now)
            was_active = state.get_display_state()

            # === DISPLAY STATE TRANSITIONS ===
            if display_active and not was_active:
                # Display turning ON
                print(f"\n[{now.strftime('%I:%M:%S %p')}] Display turning ON")
                Logger.log("Display turned ON", settings)
                state.set_display_state(True)
                do_fresh_poll(betabrite, settings, f"(ON transition at {now.strftime('%I:%M %p')})")
                state.set_last_forecast_hour(now.hour)
                next_nws_check = get_next_nws_check(now, False)

            elif not display_active and was_active:
                # Display turning OFF
                print(f"\n[{now.strftime('%I:%M:%S %p')}] Display turning OFF")
                Logger.log("Display turned OFF", settings)
                state.set_display_state(False)
                clear_display(betabrite, settings)

            elif display_active:
                # === DISPLAY IS ON - CHECK FOR UPDATES ===

                # Check if we hit a scheduled forecast hour (0, 3, 6, 9, 12, 15, 18, 21)
                last_forecast_hour = state.get_last_forecast_hour()

                if now.hour in SCHEDULED_HOURS and now.minute == 0 and now.second < 5:
                    # At a scheduled hour
                    if last_forecast_hour != now.hour:
                        # Haven't updated this hour yet
                        print(f"\n[{now.strftime('%I:%M:%S %p')}] Scheduled forecast update")
                        Logger.log(f"Scheduled forecast update at {now.strftime('%I:%M %p')}", settings)
                        do_fresh_poll(betabrite, settings, f"(Scheduled at {now.strftime('%I:%M %p')})")
                        state.set_last_forecast_hour(now.hour)
                        next_nws_check = get_next_nws_check(now, False)

                # === NWS CHECKS (every 5 min or 2 min if alert active) ===
                zone = settings.get("FORECAST_ZONE", "")
                if zone and now >= next_nws_check:
                    was_alert_active = state.get_alert_id() is not None

                    NWSAlerts.check_alerts(zone, settings)

                    is_alert_active = state.get_alert_id() is not None

                    # Update forecast if alert status changed
                    if was_alert_active != is_alert_active:
                        print(f"Alert status changed - updating forecast")
                        send_forecast(betabrite, settings, now)

                    # Calculate next check
                    if is_alert_active:
                        # Continue checking every 2 minutes
                        next_nws_check = get_next_nws_check(now, True)
                    elif was_alert_active and not is_alert_active:
                        # Alert just expired - go to nearest 5-minute mark
                        next_nws_check = get_nearest_5min_mark(now)
                        print(f"Alert expired - next check at {next_nws_check.strftime('%I:%M %p')}")
                    else:
                        # Normal schedule
                        next_nws_check = get_next_nws_check(now, False)

                # === NHC CHECKS (5, 11, 17, 23) ===
                last_nhc = state.get_nhc_pull_time()
                if should_check_nhc(now, last_nhc):
                    NHCMonitor.check_storms(settings)
                    # Update forecast to show hurricanes
                    send_forecast(betabrite, settings, now)

            # === SERIAL RECONNECT ===
            if not betabrite.is_connected():
                print("\nSerial port disconnected. Reconnecting...")
                Logger.log("Serial reconnect attempt", settings)
                if not betabrite.connect():
                    print("Reconnect failed. Exiting.")
                    Logger.log("Reconnect failed", settings)
                    break
                print("Reconnected to BetaBrite")
                Logger.log("Reconnected", settings)

            # === SHUTDOWN CHECK ===
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