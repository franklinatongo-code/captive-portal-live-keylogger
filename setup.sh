#!/usr/bin/env bash
# setup.sh — one-time dependency install + AP capability check
set -u
if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo ./setup.sh"
  exit 1
fi
apt update && apt install -y hostapd dnsmasq iptables python3 iw iproute2 rfkill
rfkill unblock wifi || true
echo
echo "--- AP support ---"
if iw list 2>/dev/null | grep -q " AP$"; then
  echo "OK: at least one radio supports AP mode."
else
  echo "WARNING: no AP mode found in 'iw list'. You may need another adapter."
fi
echo "--- interfaces ---"
iw dev
