#!/bin/bash
if pgrep -x rofi > /dev/null; then
    pkill rofi
    python3 ~/holder/Projects/hofi/hofi.py &
elif pgrep -f hofi.py > /dev/null; then
    pkill -f hofi.py
    rofi -show drun
else
    rofi -show drun
fi
