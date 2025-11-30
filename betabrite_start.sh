#!/bin/bash

# BetaBrite startup wrapper script
# Gives user option to configure settings before starting

SCRIPT_DIR="/home/beta-blink/BetaBriteWeather"
PYTHON_BIN="$SCRIPT_DIR/.beta/bin/python"
MAIN_SCRIPT="$SCRIPT_DIR/BetaBriteWriter.py"
JSON_FILE="$SCRIPT_DIR/BetaBriteWriter.json"

cd "$SCRIPT_DIR" || exit 1

# Check if this is a manual start (interactive terminal)
if [ -t 0 ]; then
    # Interactive mode - show menu
    echo "========================================"
    echo "  BetaBrite Weather Display Startup"
    echo "========================================"
    echo ""
    echo "Current configuration:"
    if [ -f "$JSON_FILE" ]; then
        cat "$JSON_FILE"
    else
        echo "  No configuration file found"
    fi
    echo ""
    echo "Options:"
    echo "  1. Start with current settings"
    echo "  2. Configure settings (interactive menu)"
    echo "  3. Exit"
    echo ""
    read -p "Enter choice [1]: " choice
    choice=${choice:-1}

    case $choice in
        2)
            echo "Starting configuration menu..."
            "$PYTHON_BIN" "$MAIN_SCRIPT"
            exit 0
            ;;
        3)
            echo "Exiting..."
            exit 0
            ;;
        *)
            echo "Starting with current settings..."
            ;;
    esac
fi

# Start in headless mode using settings from JSON
if [ ! -f "$JSON_FILE" ]; then
    echo "ERROR: Configuration file not found: $JSON_FILE"
    echo "Please run configuration first:"
    echo "  $PYTHON_BIN $MAIN_SCRIPT"
    exit 1
fi

# Extract settings from JSON
COM_PORT=$(jq -r '.COM_PORT' "$JSON_FILE")
API_KEY=$(jq -r '.API_KEY' "$JSON_FILE")
ZIP_CODE=$(jq -r '.ZIP_CODE' "$JSON_FILE")
FORECAST_ZONE=$(jq -r '.FORECAST_ZONE' "$JSON_FILE")
API_TYPE=$(jq -r '.API_TYPE // "OpenWeather"' "$JSON_FILE")
LOGGING_ON=$(jq -r '.LOGGING_ON // false' "$JSON_FILE")

# Build command
CMD="$PYTHON_BIN $MAIN_SCRIPT --headless"
CMD="$CMD --com \"$COM_PORT\""
CMD="$CMD --api-key \"$API_KEY\""
CMD="$CMD --zip \"$ZIP_CODE\""
CMD="$CMD --zone \"$FORECAST_ZONE\""
CMD="$CMD --api-type \"$API_TYPE\""

if [ "$LOGGING_ON" = "true" ]; then
    CMD="$CMD --logging"
fi

echo "Starting BetaBrite Weather Display..."
echo "Configuration: $JSON_FILE"
echo "COM Port: $COM_PORT"
echo "ZIP Code: $ZIP_CODE"
echo ""

# Execute
eval exec $CMD
