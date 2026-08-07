#!/bin/bash
# Keeps the JBL speaker connected, reconnecting automatically whenever
# it drops — bluetoothctl doesn't auto-reconnect a trusted device on
# its own after every disconnect/reboot on this setup (confirmed live,
# repeatedly, this session).
#
# Installed as a systemd service on the Pi:
#   sudo cp jbl-reconnect.sh /home/meni/jbl-reconnect.sh
#   sudo chmod +x /home/meni/jbl-reconnect.sh
#   (see jbl-reconnect.service in this directory for the unit file)
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now jbl-reconnect
JBL_MAC="B8:D5:0B:FB:ED:B2"
while true; do
  if ! bluetoothctl info "$JBL_MAC" 2>/dev/null | grep -q 'Connected: yes'; then
    echo "$(date): reconnecting JBL..."
    bluetoothctl connect "$JBL_MAC" >/dev/null 2>&1
  fi
  sleep 10
done
