#!/bin/bash
HOFI_PID=$(pgrep -f "hofi.py")

if pgrep -x rofi > /dev/null; then
    pkill rofi
    if [ -n "$HOFI_PID" ]; then
        kill -USR1 "$HOFI_PID"
    else
        python3 ~/holder/Projects/hofi/hofi.py &
    fi
elif [ -f /tmp/hofi.visible ]; then
    kill -USR1 "$HOFI_PID"
    rofi -show drun
else
    rofi -show drun
fi
