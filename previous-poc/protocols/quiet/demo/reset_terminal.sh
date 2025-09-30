#!/bin/bash
# Reset terminal mouse controls and settings

# Disable all mouse reporting modes
printf '\033[?1000l'  # Disable X10 mouse reporting
printf '\033[?1002l'  # Disable cell motion mouse tracking
printf '\033[?1003l'  # Disable all motion mouse tracking
printf '\033[?1006l'  # Disable SGR mouse mode

# Reset cursor visibility
printf '\033[?25h'     # Show cursor

# Clear scrollback and reset terminal
tput reset 2>/dev/null || reset

echo "Terminal mouse controls have been reset."
echo "Your scrollwheel should work normally now."