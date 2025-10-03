import serial
import time
import requests
from datetime import datetime, timedelta
import os
import json
from collections import defaultdict, Counter
import traceback
import sys
import shutil
from serial.tools import list_ports
import threading

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
        "LOGGING_ON": False,
        "FULL_API_LOGGING": False,
        "FULL_NHC_LOGGING": False,
        "FULL_NWS_LOGGING": False
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
                dst = f"{LOG_FILE}.{i + 1}"
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
        print("Available COM Ports:")
        for i, p in enumerate(available_ports, start=1):
            print(f"{i}: {p}")
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
    valid_choices = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "D", "S", "L", "0"}
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
        print("8. Toggle Full API Logging")
        print("9. Toggle Full NHC Logging")
        print("10. Toggle Full NWS Logging")
        print("D. Delete Settings File")
        print("S. Start Weather Display")
        print("L. Toggle Logging ON/OFF")
        print("0. Exit Program")
        print("==================================================")

        choice = ""
        while choice.upper() not in valid_choices:
            choice = input("Select an option (0-10, D, S, L): ").strip()
            if choice.upper() not in valid_choices:
                print("❌ Invalid choice, try again.")

        choice_upper = choice.upper()

        if choice_upper == "1":
            print(json.dumps(settings, indent=4))
        elif choice_upper == "2":
            current = settings.get("COM_PORT", "")
            settings["COM_PORT"] = choose_com_port(current)
        elif choice_upper == "3":
            current = settings.get("ZIP_CODE", "")
            key = settings.get("API_KEY", "")
            if key:
                while True:
                    val = input(f"Enter ZIP Code [current {current}]: ").strip() or current
                    if validate_zip(val, key):
                        settings["ZIP_CODE"] = val
                        break
                    else:
                        print("❌ Invalid ZIP Code. Please try again.")
        elif choice_upper == "4":
            current_on = settings.get("ON_TIME", "06:00")
            current_off = settings.get("OFF_TIME", "22:00")
            on_time, off_time = choose_on_off_times(current_on, current_off)
            settings["ON_TIME"] = on_time
            settings["OFF_TIME"] = off_time
        elif choice_upper == "5":
            while True:
                api_type = input("Select Weather API (1-OpenWeather, 2-Tomorrow.io): ").strip()
                if api_type == "1":
                    settings["API_TYPE"] = "OpenWeather"
                    break
                elif api_type == "2":
                    settings["API_TYPE"] = "Tomorrow.io"
                    break
        elif choice_upper == "6":
            current = settings.get("API_KEY", "")
            key = input(f"Enter API Key [current {current}]: ").strip() or current
            if validate_api_key(key):
                settings["API_KEY"] = key
        elif choice_upper == "7":
            current = settings.get("FORECAST_ZONE", "")
            while True:
                val = input(f"Enter Forecast Zone [current {current}]: ").strip() or current
                if validate_forecast_zone(val):
                    settings["FORECAST_ZONE"] = val
                    break
                else:
                    print("❌ Invalid Forecast Zone. Please try again.")
        elif choice_upper == "8":
            settings["FULL_API_LOGGING"] = not settings.get("FULL_API_LOGGING", False)
            print(f"Full API logging is now {'ON' if settings['FULL_API_LOGGING'] else 'OFF'}")
        elif choice_upper == "9":
            settings["FULL_NHC_LOGGING"] = not settings.get("FULL_NHC_LOGGING", False)
            print(f"Full NHC logging is now {'ON' if settings['FULL_NHC_LOGGING'] else 'OFF'}")
        elif choice_upper == "10":
            settings["FULL_NWS_LOGGING"] = not settings.get("FULL_NWS_LOGGING", False)
            print(f"Full NWS logging is now {'ON' if settings['FULL_NWS_LOGGING'] else 'OFF'}")
        elif choice_upper == "D":
            confirm = input("Are you sure you want to delete BetaBriteWriter.json? [N/y]: ").strip().lower() or "n"
            if confirm == "y":
                try:
                    os.remove(SETTINGS_FILE)
                    print("✅ Settings file deleted.")
                    settings = load_settings()
                except Exception as e:
                    print(f"❌ Could not delete settings file: {e}")
            else:
                print("❌ Delete canceled. Settings file kept.")
        elif choice_upper == "S":
            break
        elif choice_upper == "L":
            settings["LOGGING_ON"] = not settings.get("LOGGING_ON", False)
            print(f"Logging is now {'ON' if settings['LOGGING_ON'] else 'OFF'}")
        elif choice_upper == "0":
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
colors_future = ["4", "5", "6", "7", "8"]
alert_color = "1"  # red


def send_message(ser, text, mode="a", settings=None):
    NUL = b'\x00'
    SOH = b'\x01'
    STX = b'\x02'
    EOT = b'\x04'
    ESC = b'\x1B'
    SP = b'\x20'
    packet = b''
    packet += NUL * 10 + SOH + b"Z00" + STX + b"AA" + ESC + SP + mode.encode() + text.encode("ascii", "ignore") + EOT
    ser.write(packet)
    ser.flush()
    time.sleep(0.2)
    log(f"Sent to BetaBrite: {text}", settings)


# -----------------------------
# Forecast schedule
# -----------------------------
SCHEDULED_HOURS = [0, 3, 6, 9, 12, 15, 18, 21]


def get_next_forecast_times(now=None):
    if now is None: now = datetime.now()
    forecast_times = [now]
    candidate = now
    while len(forecast_times) < 3:
        next_hour = min([h for h in SCHEDULED_HOURS if h > candidate.hour], default=None)
        if next_hour is None:
            candidate = candidate.replace(hour=SCHEDULED_HOURS[0], minute=0, second=0, microsecond=0) + timedelta(
                days=1)
        else:
            candidate = candidate.replace(hour=next_hour, minute=0, second=0, microsecond=0)
        forecast_times.append(candidate)
    return forecast_times


def build_forecast_string(entry, dt, api_type):
    if api_type == "OpenWeather":
        desc = entry["weather"][0]["main"]
        t_min = int(entry["main"]["temp_min"])
        t_max = int(entry["main"]["temp_max"])
    else:
        desc = entry.get("weatherCode", "N/A")
        t = entry.get("temperature", 0)
        t_min = t_max = int(t)
    return f"{dt.strftime('%I:%M %p %a %m/%d/%y ')} {desc} {t_min}F/{t_max}F"


def build_colored_blocks(blocks, mode="future"):
    color_seq = colors_today if mode == "today" else colors_future
    result = ""
    for i, block in enumerate(blocks):
        color = color_seq[i % len(color_seq)]
        result += FS + color + block + "  "
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
        if settings.get("FULL_NWS_LOGGING"):
            log(f"NWS full response: {response.text}", settings)
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
# NHC Hurricane Polling
# -----------------------------
last_nhc_pull = datetime.min
NHC_HOURS = [3, 9, 15, 21]
NHC_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
nhc_active_names = []


def check_nhc_storms(settings, force=False):
    global last_nhc_pull, nhc_active_names
    now = datetime.now()
    poll_now = False
    if force:
        poll_now = True
    else:
        next_poll = last_nhc_pull + timedelta(hours=6)
        if now >= next_poll and now.hour in NHC_HOURS and now.minute == 0:
            poll_now = True
    if not poll_now:
        return
    last_nhc_pull = now
    try:
        response = requests.get(NHC_URL, timeout=10)
        if settings.get("FULL_NHC_LOGGING"):
            log(f"NHC full response: {response.text}", settings)
        if response.status_code == 200:
            data = response.json()
            hurricanes = [s for s in data.get("activeStorms", []) if s.get("classification", "") == "HU"]
            if hurricanes:
                nhc_active_names = [h.get("name") for h in hurricanes]
                log(f"NHC Hurricane(s): {', '.join(nhc_active_names)}", settings)
            else:
                nhc_active_names = []
                log("NHC No active hurricanes", settings)
        else:
            log(f"NHC pull failed with status {response.status_code}", settings)
    except Exception as e:
        log(f"Error fetching NHC storms: {e}", settings)
        traceback.print_exc()


def nhc_poll_thread(settings):
    while True:
        check_nhc_storms(settings)
        time.sleep(60)


# -----------------------------
# Forecast sender with NHC appended
# -----------------------------
def send_forecast(ser, settings, now=None):
    if now is None:
        now = datetime.now()
    api_type = settings.get("API_TYPE", "OpenWeather")
    api_key = settings.get("API_KEY", "")
    zip_code = settings.get("ZIP_CODE", "")
    forecast_times = get_next_forecast_times(now)
    try:
        daily_forecast = defaultdict(list)
        if api_type == "OpenWeather":
            url = f"http://api.openweathermap.org/data/2.5/forecast?zip={zip_code},us&units=imperial&appid={api_key}"
            data = requests.get(url).json()
            if settings.get("FULL_API_LOGGING"):
                log(f"OpenWeather full response: {json.dumps(data)}", settings)
            for entry in data.get("list", []):
                dt = datetime.fromtimestamp(entry["dt"])
                daily_forecast[dt.date()].append(entry)
        else:
            url = f"https://api.tomorrow.io/v4/timelines?location={zip_code}&fields=temperature,weatherCode&units=imperial&timesteps=1h&apikey={api_key}"
            data = requests.get(url).json()
            if settings.get("FULL_API_LOGGING"):
                log(f"Tomorrow.io full response: {json.dumps(data)}", settings)
            for timeline in data.get("data", {}).get("timelines", []):
                for entry in timeline.get("intervals", []):
                    dt = datetime.fromisoformat(entry.get("startTime"))
                    daily_forecast[dt.date()].append(entry.get("values", {}))
        today_blocks = []
        for i, f_time in enumerate(forecast_times):
            entries = daily_forecast.get(f_time.date(), [])
            if not entries: continue
            if api_type == "OpenWeather":
                entry = min(entries, key=lambda x: abs(datetime.fromtimestamp(x["dt"]) - f_time))
            else:
                entry = min(entries, key=lambda x: abs(
                    datetime.fromisoformat(x.get("startTime", "1970-01-01T00:00:00")) - f_time))
            today_blocks.append(build_forecast_string(entry, f_time, api_type))
        future_blocks = []
        future_days = sorted(daily_forecast.keys())
        future_days = [d for d in future_days if d > now.date()][:5]
        for day in future_days:
            temps_min, temps_max, conditions = [], [], []
            for entry in daily_forecast[day]:
                if api_type == "OpenWeather":
                    temps_min.append(int(entry["main"]["temp_min"]))
                    temps_max.append(int(entry["main"]["temp_max"]))
                    conditions.append(entry["weather"][0]["main"])
                else:
                    t = entry.get("temperature", 0)
                    temps_min.append(t)
                    temps_max.append(t)
                    conditions.append("N/A")
            most_common = Counter(conditions).most_common(1)[0][0]
            future_blocks.append(f"{day.strftime('%a %m/%d/%y')} {most_common} {min(temps_min)}F/{max(temps_max)}F")
        colored_text = build_colored_blocks(today_blocks, "today") + build_colored_blocks(future_blocks, "future")
        next_update_candidates = [t for t in forecast_times[1:]]
        if next_update_candidates:
            next_update = next_update_candidates[0]
            colored_text += f" || Next update: {next_update.strftime('%m/%d/%y %I:%M %p').lstrip('0')}"
        # Append NHC hurricanes in red if active
        if nhc_active_names:
            hurricane_text = ", ".join(nhc_active_names)
            colored_text += f" ||{FS}{alert_color} NHC Hurricane(s): {hurricane_text}{FS}3"
        start_time = time.time()
        max_retry_seconds = 300
        while True:
            try:
                send_message(ser, colored_text, settings=settings)
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
def show_exit_message(ser, settings):
    try:
        if ser and ser.is_open:
            dt = datetime.now().strftime("%m/%d/%y %I:%M %p")
            parts = dt.split(' ')
            parts[0] = parts[0].lstrip('0').replace('/0', '/')
            formatted_dt = ' '.join(parts)

            message = f"{FS}1Check Program || {formatted_dt}"
            send_message(ser, message, settings=settings)
            log(f"Exit message sent: Check Program || {formatted_dt}", settings)
            print(f"Exit message sent: Check Program || {formatted_dt}")
            time.sleep(1)
    except Exception as e:
        print(f"Could not send exit message: {e}")
        log(f"Error sending exit message: {e}", settings)


# -----------------------------
# Main Loop
# -----------------------------
def main():
    rotate_logs()
    settings = load_settings()
    settings = review_settings(settings)
    save_settings(settings)

    ser = init_serial(settings["COM_PORT"])

    # Initial forced polls
    check_nws_alerts(ser, settings.get("FORECAST_ZONE", ""), settings, force=True)
    check_nhc_storms(settings, force=True)

    # Start NHC polling thread
    nhc_thread = threading.Thread(target=lambda: nhc_poll_thread(settings), daemon=True)
    nhc_thread.start()

    try:
        now = datetime.now()
        send_forecast(ser, settings, now)

        while True:
            now = datetime.now()
            check_nws_alerts(ser, settings.get("FORECAST_ZONE", ""), settings)
            if now.minute == 0 and now.hour in SCHEDULED_HOURS:
                send_forecast(ser, settings, now)
            time.sleep(60)
    except KeyboardInterrupt:
        print("\nExiting program...")
        show_exit_message(ser, settings)
        if ser and ser.is_open:
            ser.close()
        sys.exit(0)
    except Exception as e:
        print(f"Error in main loop: {e}")
        traceback.print_exc()
        log(f"Error in main loop: {e}", settings)
        show_exit_message(ser, settings)
        if ser and ser.is_open:
            ser.close()
        sys.exit(1)
    finally:
        if ser and ser.is_open:
            show_exit_message(ser, settings)
            ser.close()


if __name__ == "__main__":
    main()