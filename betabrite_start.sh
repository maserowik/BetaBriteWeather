#!/bin/bash
# betabrite_start.sh
# BetaBrite Weather Display — startup wrapper
#
# Interactive mode:  prompts user to configure or start
# Service mode:      starts headless directly (no terminal attached)

SCRIPT_DIR="/home/beta-blink/BetaBriteWeather"
PYTHON_BIN="$SCRIPT_DIR/.beta/bin/python"
WRITER="$SCRIPT_DIR/BetaBriteWriter.py"
CONFIGURE="$SCRIPT_DIR/BetaBriteConfigure.py"
JSON_FILE="$SCRIPT_DIR/BetaBriteWriter.json"

cd "$SCRIPT_DIR" || { echo "ERROR: Cannot cd to $SCRIPT_DIR"; exit 1; }

# ── INTERACTIVE MODE (manual terminal start) ──────────────────────────────────
if [ -t 0 ]; then
    echo "========================================"
    echo "  BetaBrite Weather Display Startup"
    echo "========================================"
    echo ""

    if [ -f "$JSON_FILE" ]; then
        echo "Current configuration:"
        cat "$JSON_FILE"
    else
        echo "  No configuration file found."
    fi

    echo ""
    echo "Options:"
    echo "  1. Start with current settings"
    echo "  2. Configure settings"
    echo "  3. Exit"
    echo ""
    read -rp "Enter choice [1]: " choice
    choice="${choice:-1}"

    case "$choice" in
        2)
            echo "Launching configuration tool..."
            exec "$PYTHON_BIN" "$CONFIGURE"
            ;;
        3)
            echo "Exiting."
            exit 0
            ;;
        *)
            echo "Starting with current settings..."
            ;;
    esac
fi

# ── SERVICE / HEADLESS MODE ───────────────────────────────────────────────────
if [ ! -f "$JSON_FILE" ]; then
    echo "ERROR: Configuration file not found: $JSON_FILE"
    echo "Run the configuration tool first:"
    echo "  $PYTHON_BIN $CONFIGURE"
    exit 1
fi

echo "Starting BetaBrite Weather Display (headless)..."
echo "Config: $JSON_FILE"
echo ""

# --headless tells the writer to read settings from JSON.
# --skip-validation avoids live API calls on every service restart.
exec "$PYTHON_BIN" "$WRITER" --headless --skip-validation
