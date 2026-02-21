"""
BetaBriteConfigure.py
Standalone configuration tool for the BetaBrite Weather Display System.
Run this to set up or modify settings before starting BetaBriteWriter.py.
"""

import json
import os
import sys
import time
import requests
from datetime import datetime
from typing import Dict, Optional
from serial.tools import list_ports

# ==================== CONSTANTS ====================
SETTINGS_FILE = "BetaBriteWriter.json"
MAX_API_RETRIES = 3
API_RETRY_DELAY = 5


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


# ==================== VALIDATION ====================
def _retry_request(func, *args, **kwargs):
    """Simple retry with fixed delay for validation calls."""
    for attempt in range(MAX_API_RETRIES):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt < MAX_API_RETRIES - 1:
                print(f"  Retrying ({attempt + 1}/{MAX_API_RETRIES - 1})...")
                time.sleep(API_RETRY_DELAY)
            else:
                raise e


class Validator:
    @staticmethod
    def com_port(port: str) -> bool:
        if not port:
            return False
        ports = list_ports.comports()
        return any(port.lower() in p.device.lower() for p in ports)

    @staticmethod
    def api_key(api_key: str, api_type: str = "OpenWeather") -> bool:
        if not api_key:
            return False
        print(f"  Validating {api_type} API key...", end=" ", flush=True)

        if api_type == "Tomorrow.io":
            # Tomorrow.io: hit the timelines endpoint with a known location
            url = (
                f"https://api.tomorrow.io/v4/weather/forecast"
                f"?location=40.7128,-74.0060&units=imperial&apikey={api_key}"
            )
        else:
            # OpenWeather: hit the 5-day forecast endpoint with a known ZIP
            url = (
                f"http://api.openweathermap.org/data/2.5/forecast"
                f"?zip=10001,US&appid={api_key}"
            )

        try:
            response = _retry_request(requests.get, url, timeout=5)
            if response.status_code == 200:
                print("OK")
                return True
            try:
                data = response.json()
                # Tomorrow.io wraps errors differently from OpenWeather
                msg = (
                    data.get("message")
                    or data.get("type")
                    or response.text[:120]
                )
            except Exception:
                msg = response.text[:120]
            print(f"FAILED (HTTP {response.status_code}: {msg})")
            return False
        except Exception as e:
            print(f"FAILED ({e})")
            return False

    @staticmethod
    def zip_code(zip_code: str, api_key: str, api_type: str = "OpenWeather") -> bool:
        if not (zip_code.isdigit() and len(zip_code) == 5):
            print("  Invalid format — must be 5 digits.")
            return False
        if not api_key:
            print("  No API key set.")
            return False
        print(f"  Validating ZIP code with {api_type}...", end=" ", flush=True)

        if api_type == "Tomorrow.io":
            try:
                import requests as _req
                geo = _req.get(f"https://geocoding.geo.census.gov/geocoder/locations/address?zip={zip_code}&benchmark=2020&format=json", timeout=8).json()
                coords = geo["result"]["addressMatches"][0]["coordinates"]
                loc = f'{coords["y"]},{coords["x"]}'
            except Exception:
                loc = zip_code
            url = (
                f"https://api.tomorrow.io/v4/weather/forecast"
                f"?location={loc}&units=imperial&apikey={api_key}"
            )
        else:
            url = (
                f"http://api.openweathermap.org/data/2.5/forecast"
                f"?zip={zip_code},US&appid={api_key}"
            )

        try:
            response = _retry_request(requests.get, url, timeout=5)
            if response.status_code == 200:
                print("OK")
                return True
            try:
                data = response.json()
                msg = (
                    data.get("message")
                    or data.get("type")
                    or response.text[:120]
                )
            except Exception:
                msg = response.text[:120]
            print(f"FAILED (HTTP {response.status_code}: {msg})")
            return False
        except Exception as e:
            print(f"FAILED ({e})")
            return False

    @staticmethod
    def forecast_zone(zone: str) -> bool:
        if not zone:
            return False
        zone = zone.upper()
        print("  Validating forecast zone...", end=" ", flush=True)
        url = f"https://api.weather.gov/zones/forecast/{zone}"
        try:
            response = _retry_request(requests.get, url, timeout=5)
            result = response.status_code == 200
            print("OK" if result else "FAILED")
            return result
        except Exception as e:
            print(f"FAILED ({e})")
            return False

    @staticmethod
    def time_format(timestr: str) -> bool:
        try:
            datetime.strptime(timestr, "%H:%M")
            return True
        except ValueError:
            return False

    @staticmethod
    def settings_complete(settings: Dict) -> list:
        """Return list of missing required fields."""
        missing = []
        if not settings.get("COM_PORT"):
            missing.append("COM Port")
        if not settings.get("API_KEY"):
            missing.append("API Key")
        if settings.get("API_TYPE") == "Tomorrow.io":
            if not settings.get("LAT") or not settings.get("LON"):
                missing.append("Coordinates (LAT/LON) — use option 8")
        if not settings.get("ZIP_CODE"):
            missing.append("ZIP Code")
        if not settings.get("FORECAST_ZONE"):
            missing.append("Forecast Zone")
        return missing


# ==================== MENU ACTIONS ====================
def _view_settings(settings: Dict):
    print()
    # Mask the API key for display
    display = settings.copy()
    if display.get("API_KEY"):
        display["API_KEY"] = "*" * len(display["API_KEY"])
    print(json.dumps(display, indent=4))


def _update_com_port(settings: Dict):
    current = settings.get("COM_PORT", "")
    available_ports = list(list_ports.comports())

    if available_ports:
        print("\nAvailable serial ports:")
        for idx, port in enumerate(available_ports, 1):
            print(f"  {idx}. {port.device} - {port.description}")
        print(f"  M. Manual entry")
        print()
        choice = input(
            f"Select port number, M for manual, or Enter to keep [{current or 'none'}]: "
        ).strip().upper()

        if choice == "M":
            val = input("Enter COM port manually: ").strip()
            if val:
                settings["COM_PORT"] = val
        elif choice.isdigit() and 1 <= int(choice) <= len(available_ports):
            settings["COM_PORT"] = available_ports[int(choice) - 1].device
            print(f"Selected: {settings['COM_PORT']}")
        elif choice == "":
            pass
        else:
            print("Invalid selection, keeping current port.")
    else:
        print("\nNo serial ports detected.")
        val = input(f"Enter COM port manually [current: {current or 'none'}]: ").strip()
        if val:
            settings["COM_PORT"] = val


def _update_zip_code(settings: Dict):
    current = settings.get("ZIP_CODE", "")
    key = settings.get("API_KEY", "")
    api_type = settings.get("API_TYPE", "OpenWeather")
    if not key:
        print("  Please set an API Key first (option 6).")
        return
    val = input(f"Enter ZIP Code [current: {current or 'none'}]: ").strip()
    if not val:
        return
    if Validator.zip_code(val, key, api_type):
        settings["ZIP_CODE"] = val
        # Resolve lat/lon for Tomorrow.io using OpenWeather geocoding
        # (OpenWeather geo API works with any valid OW key)
        print("  Resolving coordinates...", end=" ", flush=True)
        try:
            if api_type == "OpenWeather":
                # Use the user's own OW key for geocoding
                geo_key = key
            else:
                # For Tomorrow.io we still need OW key for geocoding
                # Ask the user if they have one, otherwise skip
                print("")
                geo_key = input("  Enter an OpenWeather API key for coordinate lookup (or press Enter to skip): ").strip()
                if not geo_key:
                    print("  Coordinates not resolved. Tomorrow.io requires LAT/LON in settings.")
                    print("  Add them manually to BetaBriteWriter.json: \"LAT\": 40.3279, \"LON\": -80.0393")
                    print("  ZIP Code accepted.")
                    return
            import requests as _req
            geo_url = f"http://api.openweathermap.org/geo/1.0/zip?zip={val},US&appid={geo_key}"
            r = _req.get(geo_url, timeout=8)
            data = r.json()
            if "lat" in data and "lon" in data:
                settings["LAT"] = round(float(data["lat"]), 6)
                settings["LON"] = round(float(data["lon"]), 6)
                print(f"OK ({settings['LAT']}, {settings['LON']})")
            else:
                print(f"FAILED ({data.get('message', 'unknown error')})")
        except Exception as e:
            print(f"FAILED ({e})")
        print("  ZIP Code accepted.")
    else:
        print("  ZIP Code rejected — check the value and try again.")


def _update_on_off_times(settings: Dict):
    current_on = settings.get("ON_TIME", "06:00")
    current_off = settings.get("OFF_TIME", "22:00")
    on_time = input(f"Enter ON_TIME  (HH:MM 24h) [current: {current_on}]: ").strip() or current_on
    off_time = input(f"Enter OFF_TIME (HH:MM 24h) [current: {current_off}]: ").strip() or current_off
    if Validator.time_format(on_time) and Validator.time_format(off_time):
        settings["ON_TIME"] = on_time
        settings["OFF_TIME"] = off_time
        print("  Times updated.")
    else:
        print("  Invalid time format — use HH:MM (24-hour). No changes made.")


def _update_coordinates(settings: Dict):
    """Prompt user to enter lat/lon coordinates for Tomorrow.io."""
    current_lat = settings.get("LAT", "")
    current_lon = settings.get("LON", "")
    print("  Tomorrow.io requires coordinates (latitude/longitude) instead of ZIP code.")
    print("  Find yours at: https://www.latlong.net or Google Maps (right-click your location).")
    lat_str = input(f"  Enter Latitude  [current: {current_lat or 'none'}]: ").strip()
    lon_str = input(f"  Enter Longitude [current: {current_lon or 'none'}]: ").strip()
    if not lat_str and not lon_str:
        return
    try:
        if lat_str:
            settings["LAT"] = round(float(lat_str), 6)
        if lon_str:
            settings["LON"] = round(float(lon_str), 6)
        print(f"  Coordinates set to ({settings.get('LAT')}, {settings.get('LON')}).")
    except ValueError:
        print("  Invalid coordinates — must be decimal numbers like 40.3279 and -80.0393.")


def _select_api(settings: Dict):
    print("\nSelect Weather API:")
    print("  1. OpenWeather")
    print("  2. Tomorrow.io")
    choice = input("Enter choice: ").strip()
    if choice == "1":
        settings["API_TYPE"] = "OpenWeather"
        print("  API type set to OpenWeather.")
    elif choice == "2":
        settings["API_TYPE"] = "Tomorrow.io"
        print("  API type set to Tomorrow.io.")
        if not settings.get("LAT") or not settings.get("LON"):
            print("  Tomorrow.io requires coordinates — please enter them now.")
            _update_coordinates(settings)
    else:
        print("  Invalid choice — no changes made.")


def _update_api_key(settings: Dict):
    current = settings.get("API_KEY", "")
    masked = "*" * len(current) if current else "none"
    key = input(f"Enter API Key [current: {masked}]: ").strip()
    if not key:
        return
    api_type = settings.get("API_TYPE", "OpenWeather")
    if Validator.api_key(key, api_type):
        settings["API_KEY"] = key
        print("  API Key accepted.")
    else:
        print("  API Key rejected — check the key and try again.")


def _update_forecast_zone(settings: Dict):
    current = settings.get("FORECAST_ZONE", "")
    val = input(f"Enter Forecast Zone [current: {current or 'none'}]: ").strip()
    if not val:
        return
    val = val.upper()
    if Validator.forecast_zone(val):
        settings["FORECAST_ZONE"] = val
        print("  Forecast Zone accepted.")
    else:
        print("  Forecast Zone rejected — check https://www.weather.gov for valid zone codes.")


def _toggle_flag(settings: Dict, key: str, label: str):
    settings[key] = not settings.get(key, False)
    print(f"  {label} is now {'ON' if settings[key] else 'OFF'}.")


def _delete_settings(settings: Dict) -> Dict:
    confirm = input("Are you sure you want to delete all settings? [N/y]: ").strip().lower()
    if confirm == "y":
        if Settings.delete():
            print("  Settings file deleted.")
            return Settings.load()
        else:
            print("  Could not delete settings file.")
    else:
        print("  Cancelled.")
    return settings


def _check_ready(settings: Dict) -> bool:
    missing = Validator.settings_complete(settings)
    if missing:
        print(f"\n  Cannot start — missing required settings: {', '.join(missing)}")
        input("  Press Enter to continue...")
        return False
    return True


# ==================== MAIN MENU ====================
MENU = """
==================================================
       BETABRITE WEATHER DISPLAY — CONFIGURE
==================================================
 1.  View Current Settings
 2.  Update COM Port
 3.  Update ZIP Code
 4.  Update ON/OFF Times
 5.  Select Weather API
 6.  Update API Key
 7.  Update Forecast Zone
 8.  Update Coordinates (required for Tomorrow.io)
---
 L.  Toggle Logging ON/OFF
 9.  Toggle Full API Logging
10.  Toggle Full NHC Logging
11.  Toggle Full NWS Logging
12.  Toggle Full BetaBrite Logging
---
 D.  Delete Settings File
 S.  Save and Exit
 0.  Exit Without Saving
=================================================="""

VALID_CHOICES = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12",
                 "D", "S", "L", "0"}


def run_configure(initial_settings: Optional[Dict] = None) -> Optional[Dict]:
    """
    Run the interactive configuration menu.
    Returns the final settings dict if the user chose Save, else None.
    """
    settings = initial_settings if initial_settings is not None else Settings.load()
    saved = False

    while True:
        print(MENU)
        choice = input("Select an option: ").strip().upper()
        if choice not in VALID_CHOICES:
            continue

        if choice == "1":
            _view_settings(settings)
        elif choice == "2":
            _update_com_port(settings)
        elif choice == "3":
            _update_zip_code(settings)
        elif choice == "4":
            _update_on_off_times(settings)
        elif choice == "5":
            _select_api(settings)
        elif choice == "6":
            _update_api_key(settings)
        elif choice == "7":
            _update_forecast_zone(settings)
        elif choice == "8":
            _update_coordinates(settings)
        elif choice == "9":
            _toggle_flag(settings, "FULL_API_LOGGING", "Full API Logging")
        elif choice == "10":
            _toggle_flag(settings, "FULL_NHC_LOGGING", "Full NHC Logging")
        elif choice == "11":
            _toggle_flag(settings, "FULL_NWS_LOGGING", "Full NWS Logging")
        elif choice == "12":
            _toggle_flag(settings, "FULL_BETABRITE_LOGGING", "Full BetaBrite Logging")
        elif choice == "L":
            _toggle_flag(settings, "LOGGING_ON", "Logging")
        elif choice == "D":
            settings = _delete_settings(settings)
        elif choice == "S":
            if not _check_ready(settings):
                continue
            if Settings.save(settings):
                print(f"\n  Settings saved to {SETTINGS_FILE}.")
                saved = True
            else:
                print("\n  ERROR: Could not save settings file.")
            break
        elif choice == "0":
            print("\n  Exiting without saving.")
            break

    return settings if saved else None


# ==================== ENTRY POINT ====================
if __name__ == "__main__":
    print("BetaBrite Weather Display — Configuration Tool")
    result = run_configure()
    if result:
        print("\nConfiguration complete. You can now run BetaBriteWriter.py.")
    else:
        print("\nNo changes were saved.")
    sys.exit(0)
