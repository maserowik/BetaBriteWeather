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
import shutil
from serial.tools import list_ports
import threading
from abc import ABC, abstractmethod
import argparse
import logging
from logging.handlers import RotatingFileHandler
import tempfile

# ==================== CONSTANTS ====================
# Files
SETTINGS_FILE = "BetaBriteWriter.json"
LOG_FILE = "BetaBriteWriter.log"

# Logging
MAX_LOG_BACKUPS = 5
MAX_LOG_BYTES = 2 * 1024 * 1024  # 2 MB

# Serial Protocol
NUL = b'\x00'
SOH = b'\x01'
STX = b'\x02'
EOT = b'\x04'
ESC = b'\x1B'
SP = b'\x20'

# Timing
FORECAST_UPDATE_INTERVAL = 300  # seconds
SERIAL_WRITE_DELAY = 0.2  # seconds
MAX_SEND_RETRY_TIME = 300  # seconds
MAX_API_RETRIES = 3  # number of retry attempts for API calls
API_RETRY_DELAY = 5  # seconds between retries
NWS_NORMAL_INTERVAL_MINUTES = 5
NWS_ACTIVE_INTERVAL_SECONDS = 120
NHC_POLL_INTERVAL = 3600  # 1 hour in seconds
NHC_HOURS = [3, 9, 15, 21]

# Display
FS = "\x1C"
COLORS_TODAY = ["3"]  # green
COLORS_FUTURE = ["4", "5", "6", "7", "8"]
ALERT_COLOR = "1"  # red
SCHEDULED_HOURS = [0, 3, 6, 9, 12, 15, 18, 21]

# URLs
NHC_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"

# Tomorrow.io weather code mappings
TOMORROW_WEATHER_CODES = {
    0: "Unknown",
    1000: "Clear",
    1100: "Mostly Clear",
    1101: "Partly Cloudy",
    1102: "Mostly Cloudy",
    1001: "Cloudy",
    2000: "Fog",
    2100: "Light Fog",
    4000: "Drizzle",
    4001: "Rain",
    4200: "Light Rain",
    4201: "Heavy Rain",
    5000: "Snow",
    5001: "Flurries",
    5100: "Light Snow",
    5101: "Heavy Snow",
    6000: "Freezing Drizzle",
    6001: "Freezing Rain",
    6200: "Light Freezing Rain",
    6201: "Heavy Freezing Rain",
    7000: "Ice Pellets",
    7101: "Heavy Ice Pellets",
    7102: "Light Ice Pellets",
    8000: "Thunderstorm"
}


# ==================== GLOBAL STATE WITH THREAD SAFETY ====================
class ThreadSafeState:
    """Thread-safe container for global state"""

    def __init__(self):
        self._lock = threading.Lock()
        self.last_alert_id: Optional[str] = None
        self.last_nws_pull: datetime = datetime.min
        self.last_nhc_pull: datetime = datetime.min
        self.nhc_active_names: List[str] = []
        self.nws_active_headlines: List[str] = []  # <-- ADD THIS
        self.shutdown_event = threading.Event()

    def set_alert_id(self, alert_id: Optional[str]):
        with self._lock:
            self.last_alert_id = alert_id

    def get_alert_id(self) -> Optional[str]:
        with self._lock:
            return self.last_alert_id

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

    def set_nhc_names(self, names: List[str]):
        with self._lock:
            self.nhc_active_names = names.copy()

    def get_nhc_names(self) -> List[str]:
        with self._lock:
            return self.nhc_active_names.copy()

    def set_nws_headlines(self, headlines: List[str]):
        with self._lock:
            self.nws_active_headlines = headlines.copy()  # <-- ADD THIS

    def get_nws_headlines(self) -> List[str]:
        with self._lock:
            return self.nws_active_headlines.copy()  # <-- ADD THIS

    def shutdown(self):
        """Signal shutdown to all threads"""
        self.shutdown_event.set()

    def should_shutdown(self) -> bool:
        """Check if shutdown has been requested"""
        return self.shutdown_event.is_set()


state = ThreadSafeState()


# ==================== COM PORT UTILITIES ====================
def list_available_com_ports() -> List[str]:
    """Get list of available COM ports"""
    ports = list_ports.comports()
    return [p.device for p in ports]


# ==================== SETTINGS MANAGEMENT ====================
class Settings:
    """Settings manager with validation"""
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
        "FULL_NWS_LOGGING": False
    }

    @staticmethod
    def load() -> Dict:
        """Load settings from file or return defaults"""
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    loaded = json.load(f)
                    # Merge with defaults to handle new keys
                    settings = Settings.DEFAULT_SETTINGS.copy()
                    settings.update(loaded)
                    return settings
            except Exception as e:
                print(f"Error loading settings: {e}. Using defaults.")
        return Settings.DEFAULT_SETTINGS.copy()

    @staticmethod
    def save(settings: Dict) -> bool:
        """Save settings to file atomically"""
        try:
            # Write to temporary file first
            temp_file = SETTINGS_FILE + ".tmp"
            with open(temp_file, "w") as f:
                json.dump(settings, f, indent=4)
            # Atomic rename
            os.replace(temp_file, SETTINGS_FILE)
            return True
        except Exception as e:
            print(f"Error saving settings: {e}")
            Logger.log(f"Settings save error: {e}", settings)
            return False

    @staticmethod
    def delete() -> bool:
        """Delete settings file"""
        try:
            if os.path.exists(SETTINGS_FILE):
                os.remove(SETTINGS_FILE)
            return True
        except Exception as e:
            print(f"Could not delete settings file: {e}")
            return False


# ==================== LOGGING ====================
def setup_logger(settings: Dict) -> logging.Logger:
    """Setup logger with rotating file handler (ANSI only, no icons)"""
    logger = logging.getLogger("BetaBrite")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    if settings.get("LOGGING_ON"):
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_LOG_BYTES,
            backupCount=MAX_LOG_BACKUPS
        )
        # ANSI-only log format, no icons or emojis
        formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%m/%d/%y %I:%M %p')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


class Logger:
    """Logging utility wrapper"""
    _logger: Optional[logging.Logger] = None

    @classmethod
    def initialize(cls, settings: Dict):
        """Initialize the logger"""
        cls._logger = setup_logger(settings)

    @classmethod
    def log(cls, msg: str, settings: Optional[Dict] = None):
        """Write log entry if logging is enabled"""
        if cls._logger and settings and settings.get("LOGGING_ON"):
            # Remove icons/emojis from log messages
            clean_msg = msg.translate({ord(c): None for c in "✅❌⚠️🌤️🤖🔄📡⏳🧹"})
            cls._logger.info(clean_msg)


# ==================== VALIDATION ====================
class Validator:
    """Input validation utilities"""

    @staticmethod
    def com_port(port: str) -> bool:
        """Validate COM port string format"""
        if not port:
            return False

        # Check if port exists in available ports (flexible matching)
        ports = list_ports.comports()
        return any(port.lower() in p.device.lower() for p in ports)

    @staticmethod
    def api_key(api_key: str) -> bool:
        """Validate API key by testing with a known ZIP"""
        if not api_key:
            return False

        test_zip = "10001"
        url = f"http://api.openweathermap.org/data/2.5/weather?zip={test_zip},US&appid={api_key}"

        for attempt in range(MAX_API_RETRIES):
            try:
                response = requests.get(url, timeout=5)
                return response.status_code == 200
            except requests.RequestException as e:
                if attempt < MAX_API_RETRIES - 1:
                    print(f"API validation attempt {attempt + 1}/{MAX_API_RETRIES} failed, retrying...")
                    time.sleep(API_RETRY_DELAY)
                else:
                    Logger.log(f"API validation error after {MAX_API_RETRIES} attempts: {e}", None)
                    return False
        return False

    @staticmethod
    def zip_code(zip_code: str, api_key: str) -> bool:
        """Validate ZIP code format and existence"""
        if not (zip_code.isdigit() and len(zip_code) == 5):
            return False

        if not api_key:
            return False

        url = f"http://api.openweathermap.org/data/2.5/weather?zip={zip_code},US&appid={api_key}"

        for attempt in range(MAX_API_RETRIES):
            try:
                response = requests.get(url, timeout=5)
                return response.status_code == 200
            except requests.RequestException as e:
                if attempt < MAX_API_RETRIES - 1:
                    print(f"ZIP validation attempt {attempt + 1}/{MAX_API_RETRIES} failed, retrying...")
                    time.sleep(API_RETRY_DELAY)
                else:
                    Logger.log(f"ZIP validation error after {MAX_API_RETRIES} attempts: {e}", None)
                    return False
        return False

    @staticmethod
    def forecast_zone(zone: str) -> bool:
        """Validate NWS forecast zone"""
        if not zone:
            return False

        zone = zone.upper()
        url = f"https://api.weather.gov/zones/forecast/{zone}"

        for attempt in range(MAX_API_RETRIES):
            try:
                response = requests.get(url, timeout=5)
                return response.status_code == 200
            except requests.RequestException as e:
                if attempt < MAX_API_RETRIES - 1:
                    print(f"Zone validation attempt {attempt + 1}/{MAX_API_RETRIES} failed, retrying...")
                    time.sleep(API_RETRY_DELAY)
                else:
                    Logger.log(f"Zone validation error after {MAX_API_RETRIES} attempts: {e}", None)
                    return False
        return False

    @staticmethod
    def time_format(timestr: str) -> bool:
        """Validate HH:MM time format"""
        try:
            datetime.strptime(timestr, "%H:%M")
            return True
        except ValueError:
            return False


# ==================== WEATHER API ABSTRACTION ====================
class WeatherAPI(ABC):
    """Abstract base class for weather APIs"""

    def __init__(self, api_key: str, zip_code: str):
        self.api_key = api_key
        self.zip_code = zip_code

    @abstractmethod
    def get_forecast_data(self) -> Dict:
        """Fetch forecast data from API"""
        pass

    @abstractmethod
    def parse_forecast(self, data: Dict, forecast_times: List[datetime]) -> Tuple[List[str], List[str]]:
        """Parse forecast data into today and future blocks"""
        pass


class OpenWeatherAPI(WeatherAPI):
    """OpenWeather API implementation"""

    def get_forecast_data(self) -> Dict:
        url = f"http://api.openweathermap.org/data/2.5/forecast?zip={self.zip_code},us&units=imperial&appid={self.api_key}"

        for attempt in range(MAX_API_RETRIES):
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                return response.json()
            except requests.RequestException as e:
                if attempt < MAX_API_RETRIES - 1:
                    print(f"API request failed (attempt {attempt + 1}/{MAX_API_RETRIES}): {e}")
                    time.sleep(API_RETRY_DELAY)
                else:
                    raise

    def parse_forecast(self, data: Dict, forecast_times: List[datetime]) -> Tuple[List[str], List[str]]:
        daily_forecast = defaultdict(list)

        for entry in data.get("list", []):
            dt = datetime.fromtimestamp(entry["dt"])
            daily_forecast[dt.date()].append(entry)

        # Today blocks
        today_blocks = []
        for f_time in forecast_times:
            entries = daily_forecast.get(f_time.date(), [])
            if not entries:
                continue

            entry = min(entries, key=lambda x: abs(datetime.fromtimestamp(x["dt"]) - f_time))
            desc = entry["weather"][0]["main"]
            t_min = int(entry["main"]["temp_min"])
            t_max = int(entry["main"]["temp_max"])
            today_blocks.append(f"{f_time.strftime('%I:%M %p %a %m/%d/%y ')} {desc} {t_min}F/{t_max}F")

        # Future blocks
        future_blocks = []
        now = datetime.now()
        future_days = sorted([d for d in daily_forecast.keys() if d > now.date()])[:5]

        for day in future_days:
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
    """Tomorrow.io API implementation"""

    def get_forecast_data(self) -> Dict:
        url = f"https://api.tomorrow.io/v4/timelines?location={self.zip_code}&fields=temperature,weatherCode&units=imperial&timesteps=1h&apikey={self.api_key}"

        for attempt in range(MAX_API_RETRIES):
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                return response.json()
            except requests.RequestException as e:
                if attempt < MAX_API_RETRIES - 1:
                    print(f"API request failed (attempt {attempt + 1}/{MAX_API_RETRIES}): {e}")
                    time.sleep(API_RETRY_DELAY)
                else:
                    raise

    def _get_weather_description(self, code: int) -> str:
        """Convert Tomorrow.io weather code to description"""
        return TOMORROW_WEATHER_CODES.get(code, "Unknown")

    def parse_forecast(self, data: Dict, forecast_times: List[datetime]) -> Tuple[List[str], List[str]]:
        daily_forecast = defaultdict(list)

        for timeline in data.get("data", {}).get("timelines", []):
            for entry in timeline.get("intervals", []):
                dt_str = entry.get("startTime", "")
                if not dt_str:
                    continue
                dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
                daily_forecast[dt.date()].append({
                    "dt": dt,
                    "values": entry.get("values", {})
                })

        # Today blocks
        today_blocks = []
        for f_time in forecast_times:
            entries = daily_forecast.get(f_time.date(), [])
            if not entries:
                continue

            entry = min(entries, key=lambda x: abs(x["dt"] - f_time))
            values = entry["values"]
            weather_code = values.get("weatherCode", 0)
            desc = self._get_weather_description(weather_code)
            temp = int(values.get("temperature", 0))
            today_blocks.append(f"{f_time.strftime('%I:%M %p %a %m/%d/%y ')} {desc} {temp}F/{temp}F")

        # Future blocks
        future_blocks = []
        now = datetime.now()
        future_days = sorted([d for d in daily_forecast.keys() if d > now.date()])[:5]

        for day in future_days:
            temps = []
            weather_codes = []
            for entry in daily_forecast[day]:
                temps.append(int(entry["values"].get("temperature", 0)))
                weather_codes.append(entry["values"].get("weatherCode", 0))

            if temps and weather_codes:
                # Get most common weather condition
                most_common_code = Counter(weather_codes).most_common(1)[0][0]
                desc = self._get_weather_description(most_common_code)
                future_blocks.append(
                    f"{day.strftime('%a %m/%d/%y')} {desc} {min(temps)}F/{max(temps)}F"
                )

        return today_blocks, future_blocks


# ==================== BETABRITE COMMUNICATION ====================
class BetaBrite:
    """BetaBrite serial communication handler"""

    def __init__(self, port: str, baud: int = 9600):
        self.port = port
        self.baud = baud
        self.ser: Optional[serial.Serial] = None

    def connect(self) -> bool:
        """Open serial connection with fallback for different BetaBrite models"""
        try:
            # Try 7E1 (7 data bits, even parity, 1 stop bit)
            self.ser = serial.Serial(
                self.port,
                self.baud,
                bytesize=7,
                parity=serial.PARITY_EVEN,
                stopbits=1,
                timeout=1
            )
            return True
        except serial.SerialException as e:
            print(f"Failed with 7E1, trying 8N1: {e}")
            try:
                # Fallback to 8N1 (8 data bits, no parity, 1 stop bit)
                self.ser = serial.Serial(
                    self.port,
                    self.baud,
                    bytesize=8,
                    parity=serial.PARITY_NONE,
                    stopbits=1,
                    timeout=1
                )
                print("Connected with 8N1 configuration")
                return True
            except serial.SerialException as e2:
                print(f"Could not open COM port {self.port}: {e2}")
                return False

    def disconnect(self):
        """Close serial connection"""
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception as e:
                print(f"Error closing serial port: {e}")

    def send_message(self, text: str, mode: str = "a", settings: Optional[Dict] = None) -> bool:
        """Send message to BetaBrite with retry logic"""
        if not self.ser or not self.ser.is_open:
            print("Serial port not open")
            return False

        packet = (
                NUL * 10 + SOH + b"Z00" + STX + b"AA" +
                ESC + SP + mode.encode() +
                text.encode("ascii", "ignore") + EOT
        )

        start_time = time.time()

        while True:
            try:
                self.ser.write(packet)
                self.ser.flush()
                time.sleep(SERIAL_WRITE_DELAY)
                Logger.log(f"Sent to BetaBrite: {text}", settings)
                return True
            except (serial.SerialException, OSError) as e:
                elapsed = time.time() - start_time
                print(f"COM/USB send failed: {e}. Retrying...")
                Logger.log(f"Send failed: {e}", settings)

                if elapsed > MAX_SEND_RETRY_TIME:
                    print("Failed to send after 5 minutes. Giving up.")
                    Logger.log("Send failed after max retries", settings)
                    return False

                time.sleep(10)

    def is_connected(self) -> bool:
        """Check if serial port is open"""
        return self.ser is not None and self.ser.is_open


# ==================== FORECAST UTILITIES ====================
def get_next_forecast_times(now: Optional[datetime] = None) -> List[datetime]:
    """Calculate next 3 forecast times based on scheduled hours"""
    if now is None:
        now = datetime.now()

    forecast_times = [now]
    current_hour = now.hour

    # Find next two scheduled times
    for _ in range(2):
        # Find next scheduled hour after current_hour
        next_hours = [h for h in SCHEDULED_HOURS if h > current_hour]

        if next_hours:
            # Next scheduled hour is today
            next_hour = next_hours[0]
            next_time = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)
        else:
            # Next scheduled hour is tomorrow
            next_hour = SCHEDULED_HOURS[0]
            next_time = now.replace(hour=next_hour, minute=0, second=0, microsecond=0) + timedelta(days=1)

        forecast_times.append(next_time)
        now = next_time
        current_hour = next_hour

    return forecast_times


def build_colored_blocks(blocks: List[str], mode: str = "future") -> str:
    """Build colored text blocks for display"""
    color_seq = COLORS_TODAY if mode == "today" else COLORS_FUTURE
    result = ""

    for i, block in enumerate(blocks):
        color = color_seq[i % len(color_seq)]
        result += f"{FS}{color}{block}  "

    return result


# ==================== ALERT SYSTEMS ====================
class NWSAlerts:
    """NWS alert monitoring"""

    @staticmethod
    def check_alerts(zone: str, settings: Dict, force: bool = False):
        """Check for NWS alerts"""
        now = datetime.now()
        last_pull = state.get_nws_pull_time()
        last_alert = state.get_alert_id()

        # Determine if we should poll
        poll_now = False
        if force:
            poll_now = True
        elif last_alert and (now - last_pull).total_seconds() >= NWS_ACTIVE_INTERVAL_SECONDS:
            poll_now = True
        elif (now - last_pull).total_seconds() >= NWS_NORMAL_INTERVAL_MINUTES * 60:
            poll_now = True

        if not poll_now:
            return

        state.update_nws_pull()

        try:
            url = f"https://api.weather.gov/alerts/active?zone={zone}"
            response = requests.get(url, timeout=10)

            if settings.get("FULL_NWS_LOGGING"):
                Logger.log(f"NWS full response: {response.text}", settings)

            Logger.log(f"NWS pull status: {response.status_code}", settings)

            response.raise_for_status()
            data = response.json()
            alerts = data.get("features", [])

            if alerts:
                latest = alerts[0]["id"]
                headlines = [a["properties"]["headline"] for a in alerts if "headline" in a["properties"]]
                state.set_nws_headlines(headlines)  # <-- ADD THIS
                if latest != last_alert:
                    state.set_alert_id(latest)
                    headline = alerts[0]["properties"]["headline"]
                    desc = alerts[0]["properties"]["description"]
                    Logger.log(f"NWS alert active: {headline} || {desc}", settings)
            else:
                state.set_alert_id(None)
                state.set_nws_headlines([])  # <-- CLEAR WHEN NO ALERTS

        except Exception as e:
            Logger.log(f"Error fetching NWS alerts: {e}", settings)
            print(f"NWS alert check failed: {e}")


class NHCMonitor:
    """National Hurricane Center monitoring"""

    @staticmethod
    def check_storms(settings: Dict, force: bool = False):
        """Check for active hurricanes"""
        now = datetime.now()
        last_pull = state.get_nhc_pull_time()

        # Determine if we should poll
        poll_now = False
        if force:
            poll_now = True
        else:
            next_poll = last_pull + timedelta(hours=6)
            # Check if we're within 5 minutes of a scheduled hour
            if now >= next_poll and now.hour in NHC_HOURS and now.minute < 5:
                poll_now = True

        if not poll_now:
            return

        state.update_nhc_pull()

        try:
            response = requests.get(NHC_URL, timeout=10)

            if settings.get("FULL_NHC_LOGGING"):
                Logger.log(f"NHC full response: {response.text}", settings)

            response.raise_for_status()
            data = response.json()

            hurricanes = [
                s for s in data.get("activeStorms", [])
                if s.get("classification", "") == "HU"
            ]

            if hurricanes:
                names = [h.get("name") for h in hurricanes if h.get("name")]
                state.set_nhc_names(names)
                Logger.log(f"NHC Hurricane(s): {', '.join(names)}", settings)
            else:
                state.set_nhc_names([])
                Logger.log("NHC: No active hurricanes", settings)

        except Exception as e:
            Logger.log(f"Error fetching NHC storms: {e}", settings)
            print(f"NHC check failed: {e}")

    @staticmethod
    def poll_thread(settings: Dict):
        """Background thread for NHC polling"""
        while not state.should_shutdown():
            try:
                NHCMonitor.check_storms(settings)
            except Exception as e:
                Logger.log(f"NHC thread error: {e}", settings)

            # Use wait with shutdown check for responsive shutdown
            state.shutdown_event.wait(timeout=NHC_POLL_INTERVAL)


# ==================== FORECAST SENDER ====================
def send_forecast(betabrite: BetaBrite, settings: Dict, now: Optional[datetime] = None):
    """Fetch and send weather forecast to BetaBrite"""
    if now is None:
        now = datetime.now()

    api_type = settings.get("API_TYPE", "OpenWeather")
    api_key = settings.get("API_KEY", "")
    zip_code = settings.get("ZIP_CODE", "")

    if not api_key or not zip_code:
        print("API key or ZIP code not configured")
        return

    try:
        # Create appropriate API instance
        if api_type == "OpenWeather":
            api = OpenWeatherAPI(api_key, zip_code)
        else:
            api = TomorrowAPI(api_key, zip_code)

        # Fetch and parse forecast
        forecast_times = get_next_forecast_times(now)
        data = api.get_forecast_data()

        if settings.get("FULL_API_LOGGING"):
            Logger.log(f"{api_type} full response: {json.dumps(data)}", settings)

        today_blocks, future_blocks = api.parse_forecast(data, forecast_times)

        # Build colored display text
        colored_text = (
                build_colored_blocks(today_blocks, "today") +
                build_colored_blocks(future_blocks, "future")
        )

        # Add next update time
        if len(forecast_times) > 1:
            next_update = forecast_times[1]
            colored_text += f" || Next update: {next_update.strftime('%m/%d/%y %I:%M %p').lstrip('0')}"

        # NWS alerts (red)
        nws_headlines = state.get_nws_headlines()
        if nws_headlines:
            for headline in nws_headlines:
                colored_text += f" ||{FS}{ALERT_COLOR} NWS Alert: {headline}{FS}3"

        # NHC alerts (red)
        nhc_names = state.get_nhc_names()
        if nhc_names:
            hurricane_text = ", ".join(nhc_names)
            colored_text += f" ||{FS}{ALERT_COLOR} NHC Hurricane(s): {hurricane_text}{FS}3"

        # Send to display
        betabrite.send_message(colored_text, settings=settings)
        Logger.log(f"Forecast sent successfully", settings)

    except requests.RequestException as e:
        print(f"API request failed: {e}")
        Logger.log(f"API error: {e}", settings)
    except Exception as e:
        print(f"Error sending forecast: {e}")
        Logger.log(f"Forecast error: {e}", settings)
        traceback.print_exc()


# ==================== COMMAND LINE ARGUMENTS ====================
def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments for headless mode"""
    parser = argparse.ArgumentParser(
        description="BetaBrite Weather Display System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Interactive mode:
    python betabrite.py

  Headless mode:
    python betabrite.py --headless --com COM3 --api-key YOUR_KEY --zip 12345 --zone PAZ072

  With custom config and interval:
    python betabrite.py --headless --config custom.json --interval 600
        """
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run in headless mode without interactive menu"
    )
    parser.add_argument(
        "--com",
        type=str,
        help="COM port (e.g., COM3 or /dev/ttyUSB0)"
    )
    parser.add_argument(
        "--api-key",
        type=str,
        help="Weather API key"
    )
    parser.add_argument(
        "--zip",
        type=str,
        help="ZIP code"
    )
    parser.add_argument(
        "--zone",
        type=str,
        help="NWS forecast zone (e.g., PAZ072)"
    )
    parser.add_argument(
        "--api-type",
        type=str,
        choices=["OpenWeather", "Tomorrow.io"],
        default="OpenWeather",
        help="Weather API type (default: OpenWeather)"
    )
    parser.add_argument(
        "--logging",
        action="store_true",
        help="Enable logging"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=SETTINGS_FILE,
        help=f"Path to settings file (default: {SETTINGS_FILE})"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=FORECAST_UPDATE_INTERVAL,
        help=f"Update interval in seconds (default: {FORECAST_UPDATE_INTERVAL})"
    )

    return parser.parse_args()


def validate_headless_settings(args: argparse.Namespace) -> Dict:
    """Validate and create settings from command line arguments"""
    if not args.headless:
        return None

    errors = []

    print("Validating headless configuration...")

    # Validate required arguments
    if not args.com:
        errors.append("--com is required in headless mode")
    else:
        print(f"   Validating COM port {args.com}...")
        if not Validator.com_port(args.com):
            errors.append(f"Invalid COM port: {args.com}")
        else:
            print(f"   COM port valid")

    if not args.api_key:
        errors.append("--api-key is required in headless mode")
    else:
        print(f"   Validating API key...")
        if not Validator.api_key(args.api_key):
            errors.append("Invalid API key")
        else:
            print(f"   API key valid")

    if not args.zip:
        errors.append("--zip is required in headless mode")
    else:
        print(f"   Validating ZIP code {args.zip}...")
        if not Validator.zip_code(args.zip, args.api_key or ""):
            errors.append(f"Invalid ZIP code: {args.zip}")
        else:
            print(f"   ZIP code valid")

    if not args.zone:
        errors.append("--zone is required in headless mode")
    else:
        print(f"   Validating forecast zone {args.zone}...")
        if not Validator.forecast_zone(args.zone):
            errors.append(f"Invalid forecast zone: {args.zone}")
        else:
            print(f"   Forecast zone valid")

    if errors:
        print("\nHeadless mode validation errors:")
        for error in errors:
            print(f"   - {error}")
        sys.exit(1)

    print("All validations passed\n")

    # Create settings
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
        "FULL_NWS_LOGGING": False
    }


# ==================== INTERACTIVE SETTINGS MENU ====================
def review_settings(settings: Dict) -> Dict:
    valid_choices = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "D", "S", "L", "0"}
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
        print("D.  Delete Settings File")
        print("S.  Start Weather Display")
        print("L.  Toggle Logging ON/OFF")
        print("0.  Exit Program")
        print("=" * 50)
        choice = input("Select an option: ").strip().upper()
        if choice not in valid_choices:
            print("Invalid choice. Please try again.")
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
            while True:
                val = input(f"Enter ZIP Code [current: {current or 'none'}]: ").strip() or current
                if Validator.zip_code(val, key):
                    settings["ZIP_CODE"] = val
                    break
                print("Invalid ZIP Code. Please try again.")
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
            else:
                print("Invalid choice")
        elif choice == "6":
            current = settings.get("API_KEY", "")
            key = input(f"Enter API Key [current: {'*' * len(current) if current else 'none'}]: ").strip()
            if not key and current:
                continue
            if key and Validator.api_key(key):
                settings["API_KEY"] = key
                print("API Key validated")
            else:
                print("Invalid API Key")
        elif choice == "7":
            current = settings.get("FORECAST_ZONE", "")
            while True:
                val = input(f"Enter Forecast Zone [current: {current or 'none'}]: ").strip() or current
                if Validator.forecast_zone(val):
                    settings["FORECAST_ZONE"] = val
                    print("Forecast Zone validated")
                    break
                print("Invalid Forecast Zone. Please try again.")
        elif choice == "8":
            settings["FULL_API_LOGGING"] = not settings.get("FULL_API_LOGGING", False)
            print(f"Full API logging is now {'ON' if settings['FULL_API_LOGGING'] else 'OFF'}")
        elif choice == "9":
            settings["FULL_NHC_LOGGING"] = not settings.get("FULL_NHC_LOGGING", False)
            print(f"Full NHC logging is now {'ON' if settings['FULL_NHC_LOGGING'] else 'OFF'}")
        elif choice == "10":
            settings["FULL_NWS_LOGGING"] = not settings.get("FULL_NWS_LOGGING", False)
            print(f"Full NWS logging is now {'ON' if settings['FULL_NWS_LOGGING'] else 'OFF'}")
        elif choice == "D":
            confirm = input("Are you sure you want to delete settings? [N/y]: ").strip().lower()
            if confirm == "y":
                if Settings.delete():
                    print("Settings file deleted.")
                    settings = Settings.load()
            else:
                print("Delete canceled.")
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
                input("Press Enter to continue configuring...")
                continue
            break
        elif choice == "L":
            settings["LOGGING_ON"] = not settings.get("LOGGING_ON", False)
            print(f"Logging is now {'ON' if settings['LOGGING_ON'] else 'OFF'}")
        elif choice == "0":
            print("Exiting...")
            sys.exit(0)
        Settings.save(settings)
    return settings


# ==================== EXIT HANDLING ====================
def show_exit_message(betabrite: BetaBrite, settings: Dict):
    if not betabrite.is_connected():
        return
    try:
        dt = datetime.now().strftime("%m/%d/%y %I:%M %p")
        parts = dt.split(' ')
        parts[0] = parts[0].lstrip('0').replace('/0', '/')
        formatted_dt = ' '.join(parts)
        message = f"{FS}1Check Program || {formatted_dt}"
        betabrite.send_message(message, settings=settings)
        Logger.log(f"Exit message sent: {formatted_dt}", settings)
        print(f"Exit message sent to display")
        time.sleep(1)
    except Exception as e:
        print(f"Could not send exit message: {e}")
        Logger.log(f"Error sending exit message: {e}", settings)


# ==================== MAIN EXECUTION ====================
def main():
    args = parse_arguments()

    print("BetaBrite Weather Display System")
    print("=" * 50)

    # Handle headless mode
    if args.headless:
        print("Running in headless mode...")
        settings = validate_headless_settings(args)
    else:
        # Load and review settings interactively
        settings = Settings.load()
        settings = review_settings(settings)

    # Initialize logger
    Logger.initialize(settings)

    # Initialize BetaBrite
    betabrite = BetaBrite(settings.get("COM_PORT"))
    if not betabrite.connect():
        print("Failed to connect to BetaBrite. Exiting.")
        Logger.log("Failed to connect to BetaBrite", settings)
        sys.exit(1)

    print("Connected to BetaBrite")
    Logger.log("Program started", settings)

    # Start NHC monitoring thread
    nhc_thread = threading.Thread(
        target=NHCMonitor.poll_thread,
        args=(settings,),
        daemon=True
    )
    nhc_thread.start()
    Logger.log("NHC monitoring thread started", settings)

    # Initial NWS check time
    next_nws_check = get_next_nws_check(datetime.now(), False)

    update_interval = args.interval if hasattr(args, 'interval') else FORECAST_UPDATE_INTERVAL

    try:
        print("Starting weather display loop...")
        print(f"Update interval: {update_interval} seconds")
        print("Press Ctrl+C to exit\n")

        while True:
            # Calculate next update time for wall-clock synchronization
            start_time = datetime.now()
            next_update_time = start_time + timedelta(seconds=update_interval)

            # Send forecast update
            print(f"Updating forecast... [{start_time.strftime('%I:%M %p')}]")
            send_forecast(betabrite, settings)

            # NWS alert check scheduling
            zone = settings.get("FORECAST_ZONE", "")
            while datetime.now() < next_update_time and not state.should_shutdown():
                now = datetime.now()
                last_alert = state.get_alert_id()
                alert_active = last_alert is not None

                # If it's time for NWS check
                if now >= next_nws_check and zone:
                    NWSAlerts.check_alerts(zone, settings)
                    # Update next check time based on alert status
                    last_alert = state.get_alert_id()
                    alert_active = last_alert is not None
                    next_nws_check = get_next_nws_check(now, alert_active)

                time.sleep(1)

            if state.should_shutdown():
                break

    except KeyboardInterrupt:
        print("Shutdown signal received...")
        Logger.log("Shutdown initiated by user", settings)

    except Exception as e:
        print(f"Unexpected error: {e}")
        Logger.log(f"Fatal error: {e}", settings)
        traceback.print_exc()

    finally:
        # Cleanup
        print("Cleaning up...")

        # Signal threads to shutdown
        state.shutdown()

        # Wait briefly for threads to finish
        time.sleep(2)

        # Send exit message and close serial port
        show_exit_message(betabrite, settings)
        betabrite.disconnect()

        Logger.log("Program stopped", settings)
        print("Shutdown complete")


def get_next_nws_check(now: datetime, alert_active: bool) -> datetime:
    """
    Returns the next scheduled NWS check time.
    - If alert_active: every 2 minutes from now.
    - If not: at the next 0, 5, 10, ..., 55 minute mark.
    """
    if alert_active:
        # Next check is now + 2 minutes
        return now + timedelta(minutes=2)
    else:
        # Scheduled minutes
        schedule_minutes = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
        next_minute = None
        for m in schedule_minutes:
            if now.minute < m:
                next_minute = m
                break
        if next_minute is None:
            # Next hour, at 0 minutes
            next_time = now.replace(hour=(now.hour + 1) % 24, minute=0, second=0, microsecond=0)
            if now.hour == 23:
                next_time += timedelta(days=1)
            return next_time
        else:
            return now.replace(minute=next_minute, second=0, microsecond=0)


if __name__ == "__main__":
    main()