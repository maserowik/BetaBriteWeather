import serial
import time
import requests
from datetime import datetime, timedelta
import os
import json
import atexit
from collections import defaultdict, Counter
import traceback
import sys
import shutil
import feedparser  # for NHC advisories
from serial.tools import list_ports

# -----------------------------
# Settings file handling (JSON)
# -----------------------------
SETTINGS_FILE = "BetaBriteWriter.json"
LOG_FILE = "BetaBriteWriter.log"
MAX_LOG_DAYS = 5

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "COM_PORT": "",
        "API_TYPE": "OpenWeather",
        "API_KEY": "",
        "ZIP_CODE": "",
        "FORECAST_ZONE": "",
        "ON_TIME": "06:00",
        "OFF_TIME": "22:00",
        "DISPLAY_ON_START": False,
        "LOGGING_ON": False
    }

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)

# -----------------------------
# Logging
# -----------------------------
def rotate_logs():
    if os.path.exists(LOG_FILE):
        for i in reversed(range(1, MAX_LOG_DAYS)):
            src = f"{LOG_FILE}.{i}"
            dst = f"{LOG_FILE}.{i+1}"
            if os.path.exists(src):
                os.replace(src, dst)
        shutil.move(LOG_FILE, f"{LOG_FILE}.1")

def log(msg, settings=None):
    if settings and settings.get("LOGGING_ON"):
        timestamp = datetime.now().strftime("%m/%d/%y %I:%M %p")
        with open(LOG_FILE, "a") as f:
            f.write(f"[{timestamp}] {msg}\n")

# -----------------------------
# Data validation
# -----------------------------
def validate_api_key(api_key):
    test_zip = "10001"
    url = f"http://api.openweathermap.org/data/2.5/weather?zip={test_zip},US&appid={api_key}"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 401:
            print("❌ Invalid API key.")
            return False
        return True
    except Exception as e:
        print(f"❌ API validation error: {e}")
        return False

def validate_zip(zip_code, api_key):
    if not (zip_code.isdigit() and len(zip_code) == 5):
        print("❌ Invalid ZIP format. Must be 5 digits.")
        return False
    url = f"http://api.openweathermap.org/data/2.5/weather?zip={zip_code},US&appid={api_key}"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code != 200:
            print(f"❌ ZIP not recognized: {response.json().get('message', 'Unknown error')}")
            return False
        return True
    except Exception as e:
        print(f"❌ ZIP validation error: {e}")
        return False

def validate_forecast_zone(zone):
    zone = zone.upper()
    url = f"https://api.weather.gov/zones/forecast/{zone}"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            return True
        print(f"❌ Forecast zone not valid: {zone}")
        return False
    except Exception as e:
        print(f"❌ Error validating forecast zone: {e}")
        return False

def validate_time_format(timestr):
    try:
        datetime.strptime(timestr, "%H:%M")
        return True
    except ValueError:
        return False

# -----------------------------
# COM Port Validation / Selection
# -----------------------------
def list_available_com_ports():
    ports = list_ports.comports()
    available = [p.device for p in ports]
    if not available:
        print("❌ No serial ports found on this system.")
    else:
        print("Available COM/serial ports:")
        for i, port in enumerate(available, 1):
            print(f"{i}. {port}")
    return available

def choose_com_port(current=""):
    while True:
        available_ports = list_available_com_ports()
        if not available_ports:
            input("No ports detected. Connect a device and press Enter to retry...")
            continue

        port_input = input(f"Enter COM port or number [current {current}]: ").strip()

        if not port_input and current:
            return current

        if port_input.isdigit():
            idx = int(port_input) - 1
            if 0 <= idx < len(available_ports):
                return available_ports[idx]
            else:
                print("❌ Invalid number. Try again.")
                continue

        for port in available_ports:
            if port.lower() == port_input.lower():
                return port

        print("❌ Invalid port. Please choose from the list or enter a valid number.")

def choose_on_off_times(current_on, current_off):
    while True:
        on_time = input(f"Enter ON_TIME (HH:MM 24h) [current {current_on}]: ").strip() or current_on
        off_time = input(f"Enter OFF_TIME (HH:MM 24h) [current {current_off}]: ").strip() or current_off
        if validate_time_format(on_time) and validate_time_format(off_time):
            return on_time, off_time
        print("❌ Invalid time format. Please use HH:MM in 24-hour format.")

# -----------------------------
# Menu
# -----------------------------
def review_settings(settings):
    valid_choices = {"1","2","3","4","5","6","7","S","L","0"}

    while True:
        print("\n==================================================")
        print("       BETABRITE WEATHER DISPLAY SYSTEM")
        print("==================================================")
        print("1. View Current Settings")
        print("2. Update COM Port")
        print("3. Update ZIP Code")
        print("4. Update ON/OFF Times")
        print("5. Select Weather API")
        print("6. Update API Key")
        print("7. Update Forecast Zone")
        print("S. Start Weather Display")
        print("L. Toggle Logging ON/OFF")
        print("0. Exit Program")
        print("==================================================")

        choice = ""
        while choice not in valid_choices:
            choice = input("Select an option (0-7, S, L): ").strip().upper()
            if choice not in valid_choices:
                print("❌ Invalid choice, try again.")

        if choice == "1":
            print(json.dumps(settings, indent=4))
        elif choice == "2":
            current = settings.get("COM_PORT","")
            settings["COM_PORT"] = choose_com_port(current)
        elif choice == "3":
            current = settings.get("ZIP_CODE","")
            for _ in range(3):
                key = settings.get("API_KEY","")
                if not key:
                    print("❌ Enter API key first!")
                    break
                val = input(f"Enter ZIP Code [current {current}]: ").strip() or current
                if validate_zip(val, key):
                    settings["ZIP_CODE"] = val
                    break
            else:
                print("❌ Failed ZIP validation. Keeping previous value.")
        elif choice == "4":
            current_on = settings.get("ON_TIME","06:00")
            current_off = settings.get("OFF_TIME","22:00")
            on_time, off_time = choose_on_off_times(current_on, current_off)
            settings["ON_TIME"] = on_time
            settings["OFF_TIME"] = off_time
        elif choice == "5":
            while True:
                api_type = input("Select Weather API (1-OpenWeather, 2-Tomorrow.io): ").strip()
                if api_type == "1":
                    settings["API_TYPE"] = "OpenWeather"
                    break
                elif api_type == "2":
                    settings["API_TYPE"] = "Tomorrow.io"
                    break
                else:
                    print("❌ Invalid choice. Enter 1 for OpenWeather or 2 for Tomorrow.io.")
        elif choice == "6":
            current = settings.get("API_KEY","")
            while True:
                key = input(f"Enter API Key [current {current}]: ").strip() or current
                if validate_api_key(key):
                    settings["API_KEY"] = key
                    break
                print("❌ Invalid API key. Please enter a valid key.")
        elif choice == "7":
            current = settings.get("FORECAST_ZONE","")
            for _ in range(3):
                val = input(f"Enter Forecast Zone [current {current}]: ").strip() or current
                if validate_forecast_zone(val):
                    settings["FORECAST_ZONE"] = val
                    break
            else:
                print("❌ Failed zone validation. Keeping previous value.")
        elif choice == "S":
            break
        elif choice == "L":
            settings["LOGGING_ON"] = not settings.get("LOGGING_ON", False)
            print(f"Logging is now {'ON' if settings['LOGGING_ON'] else 'OFF'}")
        elif choice == "0":
            sys.exit(0)

        save_settings(settings)

    return settings

# -----------------------------
# BetaBrite init
# -----------------------------
def init_serial(port, baud=9600):
    try:
        return serial.Serial(port, baud, bytesize=7, parity=serial.PARITY_EVEN, stopbits=1)
    except Exception as e:
        print(f"❌ Could not open COM port: {e}")
        exit(1)

FS = "\x1C"
colors_today = ["3"]  # green
colors_future = ["1","4","5","6","7","8"]
alert_color = "2"  # red

def send_message(ser, text, mode="a"):
    NUL = b'\x00'
    SOH = b'\x01'
    STX = b'\x02'
    EOT = b'\x04'
    ESC = b'\x1B'
    SP = b'\x20'
    packet = b''
    packet += NUL*10 + SOH + b"Z00" + STX + b"AA" + ESC+SP + mode.encode() + text.encode("ascii","ignore") + EOT
    ser.write(packet)
    ser.flush()
    time.sleep(0.2)

# -----------------------------
# Forecast schedule
# -----------------------------
SCHEDULED_HOURS = [0,3,6,9,12,15,18,21]

def get_next_forecast_times(now=None):
    if now is None: now = datetime.now()
    forecast_times = [now]
    candidate = now
    while len(forecast_times)<3:
        next_hour = min([h for h in SCHEDULED_HOURS if h>candidate.hour], default=None)
        if next_hour is None:
            candidate = candidate.replace(hour=SCHEDULED_HOURS[0], minute=0, second=0, microsecond=0) + timedelta(days=1)
        else:
            candidate = candidate.replace(hour=next_hour, minute=0, second=0, microsecond=0)
        forecast_times.append(candidate)
    return forecast_times

# -----------------------------
# Forecast string builder
# -----------------------------
def build_forecast_string(entry, dt, api_type):
    if api_type=="OpenWeather":
        desc = entry["weather"][0]["main"]
        t_min = int(entry["main"]["temp_min"])
        t_max = int(entry["main"]["temp_max"])
    else:  # Tomorrow.io
        desc = entry.get("weatherCode","N/A")
        t = entry.get("temperature",0)
        t_min = t_max = int(t)
    return f"{dt.strftime('%I:%M %p %a %m/%d/%y ')} {desc} {t_min}F/{t_max}F"

def build_colored_blocks(blocks, mode="future"):
    color_seq = colors_today if mode=="today" else colors_future
    result = ""
    for i, block in enumerate(blocks):
        color = color_seq[i%len(color_seq)]
        result += FS+color+block+"  "
    return result

# -----------------------------
# NWS and NHC Alerts
# -----------------------------
last_alert_id = None
last_nhc_time = None
def check_alerts(ser, zone, settings):
    global last_alert_id, last_nhc_time
    alert_texts = []

    # NWS
    try:
        url = f"https://api.weather.gov/alerts/active?zone={zone}"
        data = requests.get(url, timeout=5).json()
        alerts = data.get("features",[])
        if alerts:
            latest = alerts[0]["id"]
            if latest != last_alert_id:
                last_alert_id = latest
                headline = alerts[0]["properties"]["headline"]
                desc = alerts[0]["properties"]["description"]
                alert_texts.append(f"NWS: {headline} || {desc}")
    except Exception:
        traceback.print_exc()
        log("Error fetching NWS alerts", settings)

    # NHC Atlantic
    try:
        feed_url = "https://www.nhc.noaa.gov/text/refresh/MIATCPAT1+shtml/"
        feed = feedparser.parse(feed_url)
        if feed.entries:
            latest_entry = feed.entries[0]
            published_time = latest_entry.get("published_parsed")
            if not last_nhc_time or published_time > last_nhc_time:
                last_nhc_time = published_time
                alert_texts.append(f"NHC: {latest_entry.title} || {latest_entry.summary}")
    except Exception:
        traceback.print_exc()
        log("Error fetching NHC feed", settings)

    if alert_texts:
        colored_alerts = FS+alert_color+" || ".join(alert_texts)
        send_message(ser,colored_alerts)
        log(f"Alerts sent: {colored_alerts}", settings)

# -----------------------------
# Forecast sender with retry
# -----------------------------
def send_forecast(ser, settings, now=None):
    if now is None:
        now = datetime.now()
    api_type = settings.get("API_TYPE","OpenWeather")
    api_key = settings.get("API_KEY","")
    zip_code = settings.get("ZIP_CODE","")
    forecast_times = get_next_forecast_times(now)

    try:
        daily_forecast = defaultdict(list)
        if api_type=="OpenWeather":
            url = f"http://api.openweathermap.org/data/2.5/forecast?zip={zip_code},us&units=imperial&appid={api_key}"
            data = requests.get(url).json()
            for entry in data.get("list",[]):
                dt = datetime.fromtimestamp(entry["dt"])
                daily_forecast[dt.date()].append(entry)
        else:
            url = f"https://api.tomorrow.io/v4/timelines?location={zip_code}&fields=temperature,weatherCode&units=imperial&timesteps=1h&apikey={api_key}"
            data = requests.get(url).json()
            for timeline in data.get("data",{}).get("timelines",[]):
                for entry in timeline.get("intervals",[]):
                    dt = datetime.fromisoformat(entry.get("startTime"))
                    daily_forecast[dt.date()].append(entry.get("values",{}))

        today_blocks = []
        for i, f_time in enumerate(forecast_times):
            entries = daily_forecast.get(f_time.date(), [])
            if not entries: continue
            if api_type=="OpenWeather":
                entry = min(entries, key=lambda x: abs(datetime.fromtimestamp(x["dt"]) - f_time))
            else:
                entry = min(entries, key=lambda x: abs(datetime.fromisoformat(x.get("startTime","1970-01-01T00:00:00")) - f_time))
            today_blocks.append(build_forecast_string(entry, f_time, api_type))

        future_blocks = []
        future_days = sorted(daily_forecast.keys())
        future_days = [d for d in future_days if d>now.date()][:5]
        for day in future_days:
            temps_min, temps_max, conditions = [], [], []
            for entry in daily_forecast[day]:
                if api_type=="OpenWeather":
                    temps_min.append(int(entry["main"]["temp_min"]))
                    temps_max.append(int(entry["main"]["temp_max"]))
                    conditions.append(entry["weather"][0]["main"])
                else:
                    t = entry.get("temperature",0)
                    temps_min.append(t)
                    temps_max.append(t)
                    conditions.append("N/A")
            most_common = Counter(conditions).most_common(1)[0][0]
            future_blocks.append(f"{day.strftime('%a %m/%d/%y')} {most_common} {min(temps_min)}F/{max(temps_max)}F")

        colored_text = build_colored_blocks(today_blocks,"today")+build_colored_blocks(future_blocks,"future")
        next_update_candidates = [t for t in forecast_times[1:]]
        if next_update_candidates:
            next_update = next_update_candidates[0]
            colored_text += f" || Next update: {next_update.strftime('%m/%d/%y %I:%M %p').lstrip('0')}"

        # --- Retry logic ---
        start_time = time.time()
        max_retry_seconds = 300  # 5 minutes
        while True:
            try:
                send_message(ser, colored_text)
                log(f"Forecast sent: {colored_text}", settings)
                break
            except Exception as e:
                elapsed = time.time() - start_time
                print(f"❌ COM/USB send failed: {e}. Retrying...")
                if elapsed > max_retry_seconds:
                    print("❌ Failed to send forecast after 5 minutes. Waiting until next scheduled update.")
                    break
                time.sleep(10)

    except Exception:
        print("Error sending forecast:")
        traceback.print_exc()
        log("Error sending forecast", settings)

# -----------------------------
# Exit cleanup
# -----------------------------
def show_exit_message(ser):
    dt = datetime.now().strftime("%m/%d/%y %I:%M %p").lstrip("0")
    send_message(ser,f"Check Program || {dt}")

# -----------------------------
# Main Loop
# -----------------------------
def main():
    rotate_logs()
    settings = load_settings()
    settings = review_settings(settings)
    save_settings(settings)

    ser = init_serial(settings["COM_PORT"])
    atexit.register(show_exit_message, ser)

    try:
        # Immediately show forecast at startup
        now = datetime.now()
        check_alerts(ser, settings.get("FORECAST_ZONE",""), settings)
        send_forecast(ser, settings, now)

        while True:
            now = datetime.now()
            # Alerts
            check_alerts(ser, settings.get("FORECAST_ZONE",""), settings)
            # Forecast updates at scheduled hours
            if now.minute==0 and now.hour in SCHEDULED_HOURS:
                send_forecast(ser, settings, now)
            time.sleep(60)
    except KeyboardInterrupt:
        print("Exiting program...")
        sys.exit(0)

if __name__=="__main__":
    main()
