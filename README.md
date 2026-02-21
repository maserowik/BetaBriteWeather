# BetaBrite Weather Display System

A Python-based weather display for BetaBrite LED signs. Pulls live weather forecasts, National Weather Service (NWS) alerts, and National Hurricane Center (NHC) storm data, and scrolls them continuously on your sign. The display automatically turns on and off on a schedule you define.

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Requirements](#requirements)
3. [File Overview](#file-overview)
4. [Installation](#installation)
5. [Getting Your API Keys and Zone Code](#getting-your-api-keys-and-zone-code)
6. [Configuration](#configuration)
7. [Running the Program](#running-the-program)
8. [Setting Up Autostart on Linux](#setting-up-autostart-on-linux)
9. [Updating Settings After Installation](#updating-settings-after-installation)
10. [Command-Line Reference](#command-line-reference)
11. [Log Files](#log-files)
12. [Settings Reference](#settings-reference)
13. [Troubleshooting](#troubleshooting)

---

## How It Works

Once running, the program enters a continuous monitoring loop that:

- **Checks the display schedule** every second. Outside your ON/OFF window the sign is cleared. When the ON time arrives, it automatically fetches fresh data and starts displaying.
- **Updates the weather forecast** at midnight and every 3 hours (00:00, 03:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00). Each update shows conditions for the current period plus the next two 3-hour windows, followed by a 5-day outlook.
- **Checks NWS alerts** every 5 minutes. If an active alert is found, checks increase to every 2 minutes. Alerts scroll at the end of the forecast message in a distinct color. When an alert expires the check cadence returns to 5 minutes.
- **Checks NHC Atlantic hurricanes** four times a day (05:00, 11:00, 17:00, 23:00). Active Atlantic hurricanes are appended to the display alongside NWS alerts.
- **Recovers automatically** from serial disconnections, attempting to reconnect up to 10 times with a 30-second gap between each try. If reconnected while the display should be on, the forecast is immediately re-sent so the sign is never left blank.
- **Sends an exit message** to the sign whenever the program stops, showing "Check Program" and the current date/time so you know the software is not running.

---

## Requirements

### Hardware

- A BetaBrite LED sign (Classic or Prism series)
- A USB-to-serial adapter **or** a direct RS-232 serial connection to the sign
- A computer or single-board computer (Raspberry Pi works well) running Linux, macOS, or Windows

### Software

- Python 3.8 or later
- pip (Python package manager)

### Python Packages

All required packages are listed in `requirements.txt`:

```
pyserial==3.5
requests==2.32.5
python-dateutil==2.9.0.post0
pytz==2025.2
```

The optional `tzlocal` package is recommended for accurate local timezone detection. If it is not installed the program falls back to reading `/etc/timezone` (Linux) or the `/etc/localtime` symlink (macOS), and then defaults to `America/New_York` if neither is available.

```
tzlocal
```

### External Accounts

You need **one** of the following weather API accounts (both are free tiers):

- **OpenWeather** (recommended) — https://openweathermap.org/api
- **Tomorrow.io** — https://www.tomorrow.io

---

## File Overview

| File | Purpose |
|------|---------|
| `BetaBriteWriter.py` | Main runtime — connects to the sign and runs the display loop |
| `BetaBriteConfigure.py` | Standalone configuration tool — run this to set up or change settings |
| `betabrite_start.sh` | Shell wrapper used by the systemd service and for manual starts |
| `betabrite.service` | systemd service unit for autostart on Linux |
| `BetaBriteWriter.json` | Settings file — created by the configuration tool, read by the writer |
| `BetaBriteWriter.log` | Current log file (only created when logging is enabled) |
| `BetaBriteWriter.1.log` | Previous log file (rotated on each startup) |
| `requirements.txt` | Python package dependencies |

> **Note:** `BetaBriteWriter.json` is created automatically when you save settings in the configuration tool. You never need to edit it by hand.

---

## Installation

### Step 1 — Choose an install location

All files should live in the same directory. The default path used by the service file is:

```
/home/beta-blink/BetaBriteWeather/
```

You can use any path you like, but you will need to update `betabrite_start.sh` and `betabrite.service` if you choose a different one. The instructions below use the default path throughout.

### Step 2 — Create the directory and copy the files

```bash
mkdir -p /home/beta-blink/BetaBriteWeather
cd /home/beta-blink/BetaBriteWeather

# Copy all project files into this directory:
#   BetaBriteWriter.py
#   BetaBriteConfigure.py
#   betabrite_start.sh
#   betabrite.service
#   requirements.txt
```

### Step 3 — Create a Python virtual environment

A virtual environment keeps these packages isolated from the rest of your system. The service file and start script expect the virtual environment to be at `.beta/` inside the project directory.

```bash
cd /home/beta-blink/BetaBriteWeather
python3 -m venv .beta
```

### Step 4 — Install Python packages

```bash
.beta/bin/pip install --upgrade pip
.beta/bin/pip install -r requirements.txt

# Recommended: install tzlocal for accurate timezone detection
.beta/bin/pip install tzlocal
```

### Step 5 — Make the start script executable

```bash
chmod +x /home/beta-blink/BetaBriteWeather/betabrite_start.sh
```

### Step 6 — Verify your serial port

Plug in your USB-to-serial adapter (or connect the sign's RS-232 cable) and check which port it appears on:

```bash
ls /dev/ttyUSB* /dev/ttyACM* /dev/ttyS* 2>/dev/null
```

On Linux, a USB-to-serial adapter typically appears as `/dev/ttyUSB0`. On macOS it may appear as `/dev/cu.usbserial-XXXXXX`. On Windows it will be a COM port such as `COM3`.

If you are on Linux and get a "Permission denied" error when the program tries to open the port, add your user to the `dialout` group:

```bash
sudo usermod -aG dialout $USER
# Log out and back in for this to take effect
```

---

## Getting Your API Keys and Zone Code

You need these three pieces of information before you can configure the program.

### OpenWeather API Key

1. Go to https://openweathermap.org and create a free account.
2. After logging in, go to **API Keys** in your account dashboard.
3. Copy the default key, or click **Generate** to create a new one.
4. New keys can take up to 2 hours to activate. If validation fails immediately after signing up, wait an hour and try again.
5. The free tier supports the 5-day/3-hour forecast API used by this program with no usage restrictions for personal use.

### Tomorrow.io API Key (alternative)

1. Go to https://app.tomorrow.io and create a free account.
2. After logging in, your API key is displayed directly on the home dashboard under **Your API Key**.
3. Copy the key from there, or click **Manage API Keys** to create or regenerate keys.
4. The free tier allows 500 calls per day and 25 calls per hour. This program makes calls every 3 hours plus occasional validation calls, so the free tier is sufficient for normal use.
5. Tomorrow.io keys are active immediately — there is no waiting period after account creation.

### NWS Forecast Zone Code

The National Weather Service divides the US into forecast zones. You need the zone code for your location.

1. Go to https://www.weather.gov
2. Enter your city or ZIP code in the search box and go to your local forecast page.
3. Look at the URL — it will contain your forecast zone. For example:
   ```
   https://forecast.weather.gov/MapClick.php?CityName=Pittsburgh&state=PA&site=PBZ&textField1=40.4406&textField2=-79.9959
   ```
4. Alternatively, go directly to https://api.weather.gov/zones/forecast and search for your state.
5. Zone codes follow the pattern `XXZnnn` where `XX` is the two-letter state abbreviation and `nnn` is a three-digit number. For example, `PAZ021` is a Pittsburgh-area zone in Pennsylvania.
6. You can verify a zone code by visiting `https://api.weather.gov/zones/forecast/PAZ021` (replace with your zone) — if you see JSON data, the zone is valid.

---

## Configuration

Configuration is handled entirely by `BetaBriteConfigure.py`. You must run this before starting the writer for the first time. All settings are saved to `BetaBriteWriter.json` in the project directory.

### Running the configuration tool

```bash
cd /home/beta-blink/BetaBriteWeather
.beta/bin/python BetaBriteConfigure.py
```

You will see this menu:

```
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
==================================================
```

### First-time setup — recommended order

Work through the options in this order. Each validated entry is held in memory and saved all at once when you press **S**.

#### Option 5 — Select Weather API

Choose your weather data provider:

- **1. OpenWeather** — recommended, uses the free 5-day/3-hour forecast endpoint
- **2. Tomorrow.io** — alternative provider

You must select the API type before entering your API key, because the key format differs between providers. If you select Tomorrow.io and no coordinates are stored yet, the program will immediately prompt you to enter them (see option 8).

#### Option 6 — Update API Key

Enter your API key. The program immediately makes a test request to the correct endpoint for your selected API type to verify the key is active before accepting it. If validation fails, double-check the key and try again. Newly created OpenWeather keys may need up to 2 hours before they activate — if validation fails immediately after signing up, wait and retry. Tomorrow.io keys activate immediately.

#### Option 2 — Update COM Port

The program lists all detected serial ports automatically:

```
Available serial ports:
  1. /dev/ttyUSB0 - USB Serial
  M. Manual entry
```

Select the number next to your port. If your sign is not detected, choose **M** to enter the port path manually (e.g. `/dev/ttyUSB0` or `COM3`). The port is not validated against the sign at this stage — connection is attempted when the writer starts.

#### Option 3 — Update ZIP Code

Enter your 5-digit US ZIP code. The program validates it against the weather API using your API key, so make sure you set the API key first (option 6). If the ZIP code validation fails, check that your API key is correct and that the ZIP code is a valid US postal code.

#### Option 7 — Update Forecast Zone

Enter your NWS forecast zone code (e.g. `PAZ021`). The program contacts `api.weather.gov` to verify the zone exists. Zone codes are case-insensitive — they are automatically converted to uppercase.

#### Option 8 — Update Coordinates (Tomorrow.io only)

Tomorrow.io does not accept ZIP codes directly — it requires decimal latitude and longitude coordinates. Enter these when using Tomorrow.io as your weather provider.

To find your coordinates:
- Go to [latlong.net](https://www.latlong.net) and enter your city or ZIP code
- Or open Google Maps, right-click your location, and the coordinates appear at the top of the menu

For Bethel Park PA (ZIP 15102) the values are:
- **Latitude:** `40.3279`
- **Longitude:** `-80.0393`

Longitude is negative for locations west of the prime meridian (i.e. all of the continental US).

If you switch to OpenWeather later, the stored coordinates are ignored — OpenWeather uses the ZIP code directly.

#### Option 4 — Update ON/OFF Times

Set the hours during which the sign should be active. Times use 24-hour format (HH:MM).

- **ON_TIME** — the sign turns on and starts displaying at this time each day (default: `06:00`)
- **OFF_TIME** — the sign is cleared and goes blank at this time each day (default: `22:00`)

If ON_TIME is later than OFF_TIME (for example ON=`22:00`, OFF=`06:00`) the program correctly interprets this as overnight operation — active from 10 PM to 6 AM.

#### Logging options

Logging is off by default. Enable it with **L** if you want a record of program activity.

| Option | What it logs |
|--------|-------------|
| **L** | General activity: forecasts sent, alerts received, ON/OFF transitions |
| **9** | Full raw JSON responses from the weather API |
| **10** | Full raw JSON response from the NHC storms feed |
| **11** | Full raw JSON response from the NWS alerts API |
| **12** | Full hex and text of every packet sent to the BetaBrite sign |

Full logging options generate large log files quickly and are intended for debugging only. Leave them off during normal operation.

#### Option S — Save and Exit

Press **S** when all required settings are complete (COM Port, API Key, ZIP Code, Forecast Zone, and Coordinates if using Tomorrow.io). The program checks that nothing is missing before saving. If anything is missing, it tells you what is still needed.

Settings are saved atomically (written to a temporary file and then renamed) so a crash during save cannot corrupt your existing configuration.

#### Option 1 — View Current Settings

Shows the current in-memory settings at any time. The API key is masked with asterisks for security.

#### Option D — Delete Settings File

Deletes `BetaBriteWriter.json` and resets all settings to defaults in memory. You will need to re-enter everything and save again.

---

## Running the Program

### Interactive mode (normal desktop use)

With a terminal open, run:

```bash
cd /home/beta-blink/BetaBriteWeather
.beta/bin/python BetaBriteWriter.py
```

When started this way, the writer automatically launches the configuration tool first. If your settings file is already complete and you do not need to change anything, press **S** at the config menu to save and proceed, or **0** to skip directly to the display loop using the existing saved settings.

Once running, the program prints status messages to the terminal:

```
BetaBrite Weather Display System
==================================================
Running in headless mode...
Connected to BetaBrite

Display Schedule:  ON=06:00  OFF=22:00
Timezone: America/New_York
Display currently: ON

==================================================
FRESH POLL (Startup at 09:00 AM)
==================================================
Checking NWS alerts...
Checking NHC storms...
Fetching forecast...
Next forecast update: 12:00 PM
==================================================

Monitoring display — press Ctrl+C to exit
```

To stop the program press **Ctrl+C**. The program will send a "Check Program" message to the sign showing the date and time, then disconnect cleanly.

### Using the start script manually

The start script provides a simple menu when run interactively:

```bash
cd /home/beta-blink/BetaBriteWeather
./betabrite_start.sh
```

```
========================================
  BetaBrite Weather Display Startup
========================================

Current configuration:
{
    "COM_PORT": "/dev/ttyUSB0",
    ...
}

Options:
  1. Start with current settings
  2. Configure settings
  3. Exit

Enter choice [1]:
```

Pressing Enter or typing `1` starts the display. Typing `2` launches the configuration tool. This is the recommended way to start the program manually after initial setup is complete.

### Headless mode (for scripts and services)

```bash
.beta/bin/python BetaBriteWriter.py --headless
```

In headless mode the writer reads all settings from `BetaBriteWriter.json` without showing any menus. It validates the settings against the live APIs on startup. Use `--skip-validation` to bypass that check (recommended for services, where startup should not depend on network availability at boot):

```bash
.beta/bin/python BetaBriteWriter.py --headless --skip-validation
```

---

## Setting Up Autostart on Linux

These instructions use systemd, which is the init system on Raspberry Pi OS, Ubuntu, Debian, and most other modern Linux distributions.

### Step 1 — Create the system user (optional but recommended)

The service file runs the program as a dedicated user `beta-blink` rather than root. If you are installing in a different user's home directory you can skip this step, but update the `User=` and path fields in the service file accordingly.

```bash
sudo useradd --system --create-home --home-dir /home/beta-blink --shell /bin/bash beta-blink
```

Add the user to the `dialout` group so it can access serial ports:

```bash
sudo usermod -aG dialout beta-blink
```

### Step 2 — Verify paths in the service file

Open `betabrite.service` and confirm these lines match your actual installation:

```ini
[Unit]
Description=BetaBrite Weather Display
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=beta-blink
WorkingDirectory=/home/beta-blink/BetaBriteWeather
Environment=HOME=/home/beta-blink
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/beta-blink/BetaBriteWeather/betabrite_start.sh
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
KillMode=mixed
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
```

Key fields to check:

- `User=` — the Linux user the service runs as. If you are not using `beta-blink`, change this.
- `WorkingDirectory=` — must be the full path to your project directory.
- `ExecStart=` — must be the full path to `betabrite_start.sh`.
- `Environment=HOME=` — should match the home directory of the user in `User=`.
- `After=network-online.target` and `Wants=network-online.target` — these ensure the service waits for a network connection before starting, which is important because the program needs internet access to fetch weather data.

If you need to find your user's UID for `XDG_RUNTIME_DIR`, run:

```bash
id beta-blink
```

Replace `1000` in `XDG_RUNTIME_DIR=/run/user/1000` with the actual UID shown.

### Step 3 — Install the service file

```bash
sudo cp /home/beta-blink/BetaBriteWeather/betabrite.service /etc/systemd/system/betabrite.service
```

### Step 4 — Reload systemd and enable the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable betabrite.service
```

`enable` tells systemd to start this service automatically on every boot.

### Step 5 — Start the service now

```bash
sudo systemctl start betabrite.service
```

### Step 6 — Verify it is running

```bash
sudo systemctl status betabrite.service
```

You should see output like:

```
● betabrite.service - BetaBrite Weather Display
     Loaded: loaded (/etc/systemd/system/betabrite.service; enabled; vendor preset: enabled)
     Active: active (running) since Sat 2026-02-21 09:00:00 EST; 5s ago
   Main PID: 1234 (betabrite_start)
```

If the status shows `failed` or `inactive`, see [Troubleshooting](#troubleshooting).

### Useful service management commands

```bash
# Check current status
sudo systemctl status betabrite.service

# Stop the service
sudo systemctl stop betabrite.service

# Restart the service
sudo systemctl restart betabrite.service

# Disable autostart (service will no longer start on boot)
sudo systemctl disable betabrite.service

# View live log output from the service
sudo journalctl -u betabrite.service -f

# View the last 100 lines of service output
sudo journalctl -u betabrite.service -n 100
```

---

## Updating Settings After Installation

To change any setting after the initial setup:

### If the service is running

Stop the service first, make your changes, then restart:

```bash
sudo systemctl stop betabrite.service
cd /home/beta-blink/BetaBriteWeather
.beta/bin/python BetaBriteConfigure.py
# Make your changes, then press S to save
sudo systemctl start betabrite.service
```

### If running manually

Stop the program with **Ctrl+C**, then run the configuration tool:

```bash
.beta/bin/python BetaBriteConfigure.py
```

Make your changes, press **S** to save, then restart the writer.

### Quick edits via the start script

If you start the program manually using `betabrite_start.sh`, option **2** at the startup menu launches the configuration tool directly without needing to navigate to the directory and run Python manually.

---

## Command-Line Reference

`BetaBriteWriter.py` accepts these command-line arguments. All are optional — without any arguments the program runs in interactive mode and reads settings from the JSON file.

| Argument | Description |
|----------|-------------|
| `--headless` | Skip all interactive menus. Read settings from `BetaBriteWriter.json`. Required for service operation. |
| `--skip-validation` | When used with `--headless`, skip live API and port validation on startup. Recommended for the systemd service so a brief network outage at boot does not prevent the program from starting. |
| `--com PORT` | Override the COM port from the JSON file (e.g. `--com /dev/ttyUSB0`). |
| `--api-key KEY` | Override the API key from the JSON file. |
| `--zip ZIPCODE` | Override the ZIP code from the JSON file. |
| `--zone ZONE` | Override the forecast zone from the JSON file. |
| `--api-type TYPE` | Override the API type. Accepts `OpenWeather` or `Tomorrow.io`. |
| `--logging` | Enable logging, overriding the JSON file setting. |

CLI arguments take precedence over values in `BetaBriteWriter.json`. Any setting not provided on the command line is read from the JSON file.

---

## Log Files

Log files are written to the project directory when logging is enabled (option **L** in the configuration tool).

| File | Contents |
|------|---------|
| `BetaBriteWriter.log` | Current session log |
| `BetaBriteWriter.1.log` | Previous session (rotated on startup) |
| `BetaBriteWriter.2.log` through `BetaBriteWriter.5.log` | Older sessions |

Log entries are timestamped in 12-hour format, for example:

```
[02/21/26 09:00 AM] Program started
[02/21/26 09:00 AM] Forecast sent
[02/21/26 09:05 AM] NWS alert: WINTER STORM WARNING IN EFFECT FROM 6 PM THIS EVENING...
[02/21/26 12:00 PM] Scheduled forecast at 12:00 PM
```

Each time the program starts, the existing `BetaBriteWriter.log` is renamed to `BetaBriteWriter.1.log`, and older files are shifted up by one number. When the oldest backup (`BetaBriteWriter.5.log`) would be overwritten, it is deleted. Each log file is also capped at 1 MB by the rotating file handler.

---

## Settings Reference

This table describes every field in `BetaBriteWriter.json`. You should not need to edit this file directly — use the configuration tool instead.

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `COM_PORT` | string | `""` | Serial port path, e.g. `/dev/ttyUSB0` or `COM3`. **Required.** |
| `API_TYPE` | string | `"OpenWeather"` | Weather data provider. Must be `"OpenWeather"` or `"Tomorrow.io"`. |
| `API_KEY` | string | `""` | API key for the selected weather provider. **Required.** |
| `ZIP_CODE` | string | `""` | Your 5-digit US ZIP code. **Required.** |
| `LAT` | number | — | Decimal latitude for your location. **Required for Tomorrow.io.** Set via option 8 in the configure tool. |
| `LON` | number | — | Decimal longitude for your location. **Required for Tomorrow.io.** Negative for US locations. Set via option 8 in the configure tool. |
| `FORECAST_ZONE` | string | `""` | Your NWS forecast zone code, e.g. `"PAZ021"`. **Required.** |
| `ON_TIME` | string | `"06:00"` | Time the display turns on each day (24-hour HH:MM). |
| `OFF_TIME` | string | `"22:00"` | Time the display turns off and clears each day (24-hour HH:MM). |
| `LOGGING_ON` | boolean | `false` | Enable general activity logging to `BetaBriteWriter.log`. |
| `FULL_API_LOGGING` | boolean | `false` | Log full raw JSON from the weather API. High volume — debug only. |
| `FULL_NHC_LOGGING` | boolean | `false` | Log full raw JSON from the NHC feed. Debug only. |
| `FULL_NWS_LOGGING` | boolean | `false` | Log full raw JSON from the NWS alerts API. Debug only. |
| `FULL_BETABRITE_LOGGING` | boolean | `false` | Log every packet sent to the sign in hex and text. Debug only. |

---

## Troubleshooting

### The service fails to start

Check the systemd journal for the exact error:

```bash
sudo journalctl -u betabrite.service -n 50
```

Common causes:

- **Path mismatch** — the `WorkingDirectory=` or `ExecStart=` in the service file does not match where the files are actually installed. Verify the paths and run `sudo systemctl daemon-reload` after any edits to the service file.
- **Permission denied on serial port** — the service user is not in the `dialout` group. Run `sudo usermod -aG dialout beta-blink` and reboot.
- **Virtual environment not found** — the `.beta/` directory does not exist or the packages are not installed. Re-run the installation steps.

### The sign shows "Check Program" and does not update

This message is sent whenever the program exits. It means the program stopped — it is not a normal display state. Check whether the service is still running:

```bash
sudo systemctl status betabrite.service
```

If it is not running, check the journal for why it exited and restart it.

### The forecast never updates / display stays blank

- Confirm the COM port is correct (option **2** in the configuration tool). Unplug and replug the USB-to-serial adapter and check `ls /dev/ttyUSB*` to see if the port name has changed.
- Confirm the API key is valid by running the configuration tool and re-entering the key (option **6**) — it will be validated on entry.
- Enable logging (option **L**) and check `BetaBriteWriter.log` for error messages.

### NWS alerts are not appearing

- Confirm your forecast zone code is correct. Visit `https://api.weather.gov/zones/forecast/YOURZONEHERE` in a browser and verify it returns data.
- Re-enter the zone in the configuration tool (option **7**) to re-validate it.
- Check whether alerts are actually active for your zone at https://alerts.weather.gov.

### The timezone shown at startup is wrong

Install `tzlocal`:

```bash
.beta/bin/pip install tzlocal
```

Then restart the program. Without `tzlocal` the program reads the system timezone from `/etc/timezone`. If that file is missing or incorrect, fix it:

```bash
sudo timedatectl set-timezone America/New_York
```

Replace `America/New_York` with your actual timezone. A full list of valid timezone names is available by running `timedatectl list-timezones`.

### Settings keep getting reset

Check that the project directory is writable by the user running the program:

```bash
ls -la /home/beta-blink/BetaBriteWeather/
```

The `BetaBriteWriter.json` file should be owned by the same user as `User=` in the service file. If it is owned by root, fix it:

```bash
sudo chown beta-blink:beta-blink /home/beta-blink/BetaBriteWeather/BetaBriteWriter.json
```

### I need to completely start over

Delete the settings file and run the configuration tool again:

```bash
cd /home/beta-blink/BetaBriteWeather
.beta/bin/python BetaBriteConfigure.py
# Choose option D, confirm deletion, then re-enter all settings and press S
```
