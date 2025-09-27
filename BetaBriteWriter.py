# base_code.py

import serial
import time
import requests
from datetime import datetime, timedelta, timezone
import os
import json
import atexit
from collections import defaultdict, Counter
import traceback
import sys
import shutil
from serial.tools import list_ports
import threading  # For NHC polling

# -----------------------------
# Settings file handling (JSON)
# -----------------------------
SETTINGS_FILE = "BetaBriteWriter.json"
LOG_FILE = "BetaBriteWriter.log"
MAX_LOG_DAYS = 5
MAX_LOG_SIZE_KB = 2048  # 2 MB

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
        size_kb = os.path.getsize(LOG_FILE) / 1024
        if size_kb >= MAX_LOG_SIZE_KB:
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
        return response.status_code != 401
    except Exception:
        return False

def validate_zip(zip_code, api_key):
    if not (zip_code.isdigit() and len(zip_code) == 5):
        return False
    url = f"http://api.openweathermap.org/data/2.5/weather?zip={zip_code},US&appid={api_key}"
    try:
        response = requests.get(url, timeout=5)
        return response.status_code == 200
    except Exception:
        return False

def validate_forecast_zone(zone):
    zone = zone.upper()
    url = f"https://api.weather.gov/zones/forecast/{zone}"
    try:
        response = requests.get(url, timeout=5)
        return response.status_code == 200
    except Exception:
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
    return [p.device for p in ports]

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
            continue
        for port in available_ports:
            if port.lower() == port_input.lower():
                return port

def choose_on_off_times(current_on, current_off):
    while True:
        on_time = input(f"Enter ON_TIME (HH:MM 24h) [current {current_on}]: ").strip() or current_on
        off_time = input(f"Enter OFF_TIME (HH:MM 24h) [current {current_off}]: ").strip() or current_off
        if validate_time_format(on_time) and validate_time_format(off_time):
            return on_time, off_time

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
            key = settings.get("API_KEY","")
            if key:
                val = input(f"Enter ZIP Code [current {current}]: ").strip() or current
                if validate_zip(val, key):
                    settings["ZIP_CODE"] = val
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
        elif choice == "6":
            current = settings.get("API_KEY","")
            key = input(f"Enter API Key [current {current}]: ").strip() or current
            if validate_api_key(key):
                settings["API_KEY"] = key
        elif choice == "7":
            current = settings.get("FORECAST_ZONE","")
            val = input(f"Enter Forecast Zone [current {current}]: ").strip() or current
            if validate_forecast_zone(val):
                settings["FORECAST_ZONE"] = val
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
    log(f"Sent to BetaBrite: {packet}", settings={"LOGGING_ON": True})

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

def build_forecast_string(entry, dt, api_type):
    if api_type=="OpenWeather":
        desc = entry["weather"][0]["main"]
        t_min = int(entry["main"]["temp_min"])
        t_max = int(entry["main"]["temp_max"])
    else:
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
# NWS Alerts
# -----------------------------
last_alert_id = None
last_nws_pull = None
NWS_NORMAL_INTERVAL_MINUTES = 5
NWS_ACTIVE_INTERVAL_SECONDS = 120

def check_nws_alerts(ser, zone, settings, force=False):
    global last_alert_id, last_nws_pull
    now = datetime.now()
    if last_nws_pull is None:
        last_nws_pull = datetime.min
    poll_now = False
    if force:
        poll_now = True
    elif last_alert_id is not None and (datetime.now() - last_nws_pull).total_seconds() >= NWS_ACTIVE_INTERVAL_SECONDS:
        poll_now = True
    elif now.minute % NWS_NORMAL_INTERVAL_MINUTES == 0 and (datetime.now() - last_nws_pull).total_seconds() >= 60:
        poll_now = True
    if not poll_now:
        return
    last_nws_pull = datetime.now()
    try:
        url = f"https://api.weather.gov/alerts/active?zone={zone}"
        response = requests.get(url, timeout=5)
        log(f"NWS pull status: {response.status_code}", settings)
        log(f"NWS pull response data: {json.dumps(response.json())}", settings)
        data = response.json()
        alerts = data.get("features", [])
        if alerts:
            latest = alerts[0]["id"]
            if latest != last_alert_id:
                last_alert_id = latest
                headline = alerts[0]["properties"]["headline"]
                desc = alerts[0]["properties"]["description"]
                alert_text = f"NWS: {headline} || {desc}"
                log(f"NWS alert active: {headline} || {desc}", settings)
        else:
            last_alert_id = None
    except Exception as e:
        log(f"Error fetching NWS alerts: {e}", settings)
        traceback.print_exc()

# -----------------------------
# Forecast sender
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
            log(f"Full API response OpenWeather: {data}", settings)
            for entry in data.get("list",[]):
                dt = datetime.fromtimestamp(entry["dt"])
                daily_forecast[dt.date()].append(entry)
        else:
            url = f"https://api.tomorrow.io/v4/timelines?location={zip_code}&fields=temperature,weatherCode&units=imperial&timesteps=1h&apikey={api_key}"
            data = requests.get(url).json()
            log(f"Full API response Tomorrow.io: {data}", settings)
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
        start_time = time.time()
        max_retry_seconds = 300
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
# NHC Hurricane Polling
# -----------------------------
last_nhc_pull = None
NHC_UTC_HOURS = [3, 9, 15, 21]
NHC_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"

def check_nhc_storms(settings, force=False):
    global last_nhc_pull
    now = datetime.now(timezone.utc)
    if last_nhc_pull is None:
        last_nhc_pull = datetime.min
    poll_now = False
    if force:
        poll_now = True
    else:
        next_poll = last_nhc_pull + timedelta(hours=6)
        if now >= next_poll and now.hour in NHC_UTC_HOURS and now.minute == 0:
            poll_now = True
    if not poll_now:
        return
    last_nhc_pull = now
    try:
        response = requests.get(NHC_URL, timeout=10)
        if response.status_code == 200:
            data = response.json()
            hurricanes = [s for s in data.get("activeStorms", []) if s.get("classification","") == "HU"]
            if hurricanes:
                log(f"NHC Active Hurricanes: {json.dumps(hurricanes)}", settings)
            else:
                log("NHC No active hurricanes", settings)
        else:
            log(f"NHC pull failed with status {response.status_code}", settings)
    except Exception as e:
        log(f"Error fetching NHC storms: {e}", settings)
        traceback.print_exc()

def nhc_poll_thread(settings):
    while True:
        now = datetime.now(timezone.utc)
        if now.hour in NHC_UTC_HOURS and now.minute == 0:
            check_nhc_storms(settings)
            time.sleep(60)
        else:
            time.sleep(30)

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

    # Initial forced polls
    check_nws_alerts(ser, settings.get("FORECAST_ZONE",""), settings, force=True)
    check_nhc_storms(settings, force=True)

    # Start NHC polling thread
    nhc_thread = threading.Thread(target=nhc_poll_thread, args=(settings,), daemon=True)
    nhc_thread.start()

    try:
        now = datetime.now()
        send_forecast(ser, settings, now)

        while True:
            now = datetime.now()
            check_nws_alerts(ser, settings.get("FORECAST_ZONE",""), settings)
            if now.minute == 0 and now.hour in SCHEDULED_HOURS:
                send_forecast(ser, settings, now)
            time.sleep(60)
    except KeyboardInterrupt:
        print("Exiting program...")
        sys.exit(0)

if __name__=="__main__":
    main()
