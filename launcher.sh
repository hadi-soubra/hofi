#!/bin/bash
# cycles rofi → hofi → aofi → rofi on every call (bound to a 3-finger swipe up)
HOFI_PID=$(pgrep -f "python3 .*hofi\.py")
AOFI_PID=$(pgrep -f "python3 .*aofi\.py")

if pgrep -x rofi > /dev/null; then
    pkill rofi
    if [ -n "$HOFI_PID" ]; then
        kill -USR1 "$HOFI_PID"
    else
        python3 ~/holder/Projects/hofi/hofi.py &
    fi
elif [ -f /tmp/hofi.visible ]; then
    kill -USR1 "$HOFI_PID"
    if [ -n "$AOFI_PID" ]; then
        kill -USR1 "$AOFI_PID"
    else
        python3 ~/holder/Projects/aofi/aofi.py &
    fi
elif [ -f /tmp/aofi.visible ]; then
    kill -USR1 "$AOFI_PID"
    rofi -show drun
else
    rofi -show drun
fi
