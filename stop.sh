#!/usr/bin/env bash
# stop.sh — stop a running session started by run.sh
set -u
BASE="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$BASE/run"
LAST="$RUN_DIR/last.conf"
STATE_FILE="$RUN_DIR/authorized_macs"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo ./stop.sh"
  exit 1
fi

AP_IFACE=""; UPSTREAM=""; GATEWAY="192.168.4.1"
# shellcheck disable=SC1090
[ -f "$LAST" ] && . "$LAST" 2>/dev/null || true

for p in "$RUN_DIR/portal.pid" "$RUN_DIR/dnsmasq.pid" "$RUN_DIR/hostapd.pid" "$RUN_DIR/spy.pid"; do
  if [ -f "$p" ]; then kill "$(cat "$p")" 2>/dev/null || true; rm -f "$p"; fi
done
pkill -f "portal_server.py.*$RUN_DIR" 2>/dev/null || true
pkill -f "spy_view.py.*8765" 2>/dev/null || true
pkill -f "hostapd.*$RUN_DIR/hostapd.conf" 2>/dev/null || true
pkill -f "hostapd.*/tmp/ap_hostapd.conf" 2>/dev/null || true
for p in "$RUN_DIR/traffic.pid"; do
  if [ -f "$p" ]; then kill "$(cat "$p")" 2>/dev/null || true; rm -f "$p"; fi
done

if [ -n "$AP_IFACE" ]; then
  if [ -f "$STATE_FILE" ]; then
    while read -r mac; do
      [ -z "$mac" ] && continue
      iptables -D FORWARD -i "$AP_IFACE" -m mac --mac-source "$mac" -j ACCEPT 2>/dev/null || true
      iptables -t nat -D CAPTIVE_HTTP -m mac --mac-source "$mac" -j RETURN 2>/dev/null || true
    done < "$STATE_FILE"
  fi
  iptables -t nat -D PREROUTING -i "$AP_IFACE" -p tcp --dport 80 -j CAPTIVE_HTTP 2>/dev/null || true
  iptables -t nat -F CAPTIVE_HTTP 2>/dev/null || true
  iptables -t nat -X CAPTIVE_HTTP 2>/dev/null || true
  for _ch in CAPTIVE_AUTH CAPTIVE_PORTAL; do
    iptables -D FORWARD -j "$_ch" 2>/dev/null || true
    iptables -t nat -D PREROUTING -j "$_ch" 2>/dev/null || true
    iptables -F "$_ch" 2>/dev/null || true
    iptables -X "$_ch" 2>/dev/null || true
    iptables -t nat -F "$_ch" 2>/dev/null || true
    iptables -t nat -X "$_ch" 2>/dev/null || true
  done
  iptables -D FORWARD -i "$AP_IFACE" -p tcp --dport 443 -j REJECT --reject-with tcp-reset 2>/dev/null || true
  iptables -D FORWARD -i "$AP_IFACE" -j DROP 2>/dev/null || true
  iptables -D INPUT -i "$AP_IFACE" -p udp --dport 67 -j ACCEPT 2>/dev/null || true
  iptables -D INPUT -i "$AP_IFACE" -p udp --dport 68 -j ACCEPT 2>/dev/null || true
  iptables -D INPUT -i "$AP_IFACE" -p udp --dport 53 -j ACCEPT 2>/dev/null || true
  iptables -D INPUT -i "$AP_IFACE" -p tcp --dport 53 -j ACCEPT 2>/dev/null || true
  iptables -D INPUT -i "$AP_IFACE" -p tcp --dport 80 -j ACCEPT 2>/dev/null || true
  [ -n "$UPSTREAM" ] && iptables -t nat -D POSTROUTING -o "$UPSTREAM" -j MASQUERADE 2>/dev/null || true
  ip addr del "$GATEWAY/24" dev "$AP_IFACE" 2>/dev/null || true
  ip link set "$AP_IFACE" down 2>/dev/null || true
  if [ -f "$RUN_DIR/orig_reg" ]; then
    _or="$(cat "$RUN_DIR/orig_reg" 2>/dev/null)"
    if [ -n "$_or" ]; then iw reg set "$_or" 2>/dev/null || true; fi
  fi
  if command -v nmcli >/dev/null 2>&1; then
    nmcli dev set "$AP_IFACE" managed yes 2>/dev/null || true
  fi
fi
echo "[*] Stopped."
