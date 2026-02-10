#!/bin/sh
# Wrapper for tuya_monitor.py with lock and logging
# Works on FreeBSD (daemon) and Linux (nohup)
# Works relative to script location, not $HOME

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

LOCKFILE="$SCRIPT_DIR/tuya_monitor.lock"
LOGFILE="$SCRIPT_DIR/tuya_monitor.log"

# Source environment from same directory
. "$SCRIPT_DIR/tuya_monitor.txt"

# Detect OS and use appropriate daemon method
if command -v daemon >/dev/null 2>&1; then
    # FreeBSD: use daemon command
    /usr/sbin/daemon \
        -p "$LOCKFILE" \
        -o "$LOGFILE" \
        -m 3 \
        /usr/bin/env python "$SCRIPT_DIR/tuya_monitor.py"
else
    # Linux: use nohup and background
    nohup /usr/bin/env python "$SCRIPT_DIR/tuya_monitor.py" >> "$LOGFILE" 2>&1 &
    echo $! > "$LOCKFILE"
fi

