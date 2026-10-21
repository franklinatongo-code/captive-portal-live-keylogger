#!/usr/bin/env python3
"""Wireshark-lite: live sites/URLs per device from a tcpdump log (stdlib only).

run.sh starts: tcpdump -i <ap> -l -n -q -s 600 -tt 'udp port 53 or tcp port 80 or tcp port 443'
This viewer tails that log and shows, per client IP/MAC:
  - DNS lookups (what they asked for)
  - HTTP URLs (GET/POST path + Host: — plain http only)
  - HTTPS/TLS sites via DNS answers (ip -> hostname map, no decrypting)
  - plain IP connections when no hostname is known

Run:  python3 traffic_view.py --log run/traffic.log --iface wlan1 --gateway 192.168.5.1
      python3 traffic_view.py --once   (single snapshot, for scripts/tests)
"""
import argparse
import collections
import os
import re
import sys
import time

HDR = re.compile(r"^(\S+)\s+IP\s+(\S+)\s+>\s+(\S+):")
DNS_Q = re.compile(r"\d+\+\s+[A-Z]+\?\s+(\S+?)\.?\s*\(")
DNS_A = re.compile(r"(\S+?)\.\s+A\s+(\d+\.\d+\.\d+\.\d+)")
HTTP_REQ = re.compile(r"^\s*(GET|POST|HEAD|PUT|DELETE|OPTIONS)\s+(\S+)\s+HTTP/")
HTTP_HOST = re.compile(r"^\s*Host:\s*(\S+)", re.IGNORECASE)


def split_hostport(s):
    if "." not in s:
        return s, ""
    host, _, port = s.rpartition(".")
    return (host, port) if port.isdigit() and host.count(".") >= 1 else (s, "")


def fmt_ts(ts):
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "??:??:??"


def fmt_age(ts, now):
    s = max(0, int(now - float(ts)))
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s"


class Traffic:
    def __init__(self, gateway):
        self.gateway = gateway
        self.subnet = ".".join(gateway.split(".")[:3]) + "."
        self.devs = {}   # ip -> {"events": deque, "sites": set, "last": ts}
        self.ip2host = {}
        self.pending = {}  # src ip -> [method, path] (GET seen, waiting for Host:)
        self._ts = time.time()
        self._last_src = ""
        self._fh = None
        self._fkey = None

    def is_client(self, ip):
        return ip.startswith(self.subnet) and ip != self.gateway

    def dev(self, ip, ts):
        d = self.devs.get(ip)
        if d is None:
            d = self.devs[ip] = {"events": collections.deque(maxlen=30),
                                 "sites": set(), "last": ts}
        d["last"] = max(d["last"], ts)
        return d

    def add(self, ip, ts, kind, text, site=None):
        d = self.dev(ip, ts)
        d["events"].append((ts, kind, text))
        if site:
            d["sites"].add(site)

    def feed_line(self, line):
        m = HDR.match(line)
        if not m:
            # payload continuation line of the previous packet (HTTP GET/Host: etc.)
            mr = HTTP_REQ.match(line)
            if mr and self._last_src and self.is_client(self._last_src):
                self.pending[self._last_src] = [mr.group(1), mr.group(2), self._ts]
                return
            mh = HTTP_HOST.match(line)
            if mh:
                host = mh.group(1).strip().rstrip(".")
                for src, (method, path, pts) in list(self.pending.items()):
                    if self._ts - pts < 5:  # same burst only — no stale misattribution
                        self.add(src, self._ts, "http", f"{method} http://{host}{path}", site=host)
                self.pending.clear()
            return
        ts, src_raw, dst_raw = m.group(1), m.group(2), m.group(3)
        try:
            ts = float(ts)
        except ValueError:
            return
        self._ts = ts
        src, sport = split_hostport(src_raw)
        dst, dport = split_hostport(dst_raw)
        self._last_src = src

        # DNS query: client -> :53
        if dport == "53" and self.is_client(src):
            q = DNS_Q.search(line)
            if q:
                name = q.group(1).rstrip(".")
                self.add(src, ts, "dns", name, site=name)
            return
        # DNS answer: :53 -> client (learn ip -> hostname for HTTPS attribution)
        if sport == "53" and self.is_client(dst):
            for name, ip in DNS_A.findall(line):
                self.ip2host[ip] = name.rstrip(".")
            return
        if not self.is_client(src):
            self.pending.pop(src, None)
            return
        # HTTP request line inside client's packet
        mr = HTTP_REQ.match(line)
        if mr and dport == "80":
            self.pending[src] = [mr.group(1), mr.group(2), ts]
            return
        mh = HTTP_HOST.match(line)
        if mh and src in self.pending:
            method, path, pts = self.pending.pop(src)
            host = mh.group(1).strip().rstrip(".")
            self.add(src, ts, "http", f"{method} http://{host}{path}", site=host)
            return
        # TCP SYN: new connection (HTTPS site via DNS map, else raw ip:port)
        if "Flags [S]" in line and dport in ("80", "443", "8080"):
            host = self.ip2host.get(dst)
            if host:
                proto = "https" if dport == "443" else "http"
                self.add(src, ts, "tls" if dport == "443" else "tcp",
                         f"{proto}://{host}/", site=host)
            else:
                self.add(src, ts, "tcp", f"-> {dst}:{dport}", site=f"{dst}:{dport}")

    def feed_file(self, path, start_at_end=True):
        try:
            st = os.stat(path)
            key = (st.st_ino, st.st_size)
        except OSError:
            return None
        if self._fkey != key:  # new/rotated file
            try:
                if self._fh:
                    self._fh.close()
            except Exception:
                pass
            self._fh = open(path, errors="ignore")
            if start_at_end:
                self._fh.seek(0, os.SEEK_END)
            self._fkey = key
        while True:
            line = self._fh.readline()
            if not line:
                break
            self.feed_line(line.rstrip("\n"))
        return True


def read_arp(iface):
    out = {}
    try:
        with open("/proc/net/arp") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) >= 6 and p[5] == iface and p[3] != "00:00:00:00:00:00":
                    out[p[0]] = p[3]
    except OSError:
        pass
    return out


KIND_ICON = {"dns": "DNS ", "http": "HTTP", "tls": "TLS ", "tcp": "TCP "}


def render(t, arp, args):
    now = time.time()
    L = [f"TRAFFIC on {args.iface} — live sites/URLs per device (DNS + HTTP + HTTPS-via-DNS)   {fmt_ts(now)}"]
    if not t.devs:
        L.append("(nothing yet — connect a device and browse; HTTPS shows as site via DNS, plain HTTP shows full URL)")
        return "\n".join(L)
    for ip in sorted(t.devs, key=lambda i: t.devs[i]["last"], reverse=True):
        d = t.devs[ip]
        mac = arp.get(ip, "?")
        L.append(f"\n{ip} ({mac})  {len(d['sites'])} sites · {len(d['events'])} events  [active {fmt_age(d['last'], now)} ago]")
        for ts, kind, text in list(d["events"])[-12:]:
            icon = KIND_ICON.get(kind, kind)
            L.append(f"  {fmt_ts(ts)}  {icon}  {text[:100]}")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Wireshark-lite traffic viewer")
    ap.add_argument("--log", default="run/traffic.log")
    ap.add_argument("--iface", default="wlan1")
    ap.add_argument("--gateway", default="192.168.5.1")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--once", action="store_true", help="single snapshot (no clear loop)")
    args = ap.parse_args(argv)
    base = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isabs(args.log):
        for cand in (os.path.join(base, args.log), os.path.join(os.getcwd(), args.log)):
            if os.path.exists(cand):
                args.log = cand
                break
    t = Traffic(args.gateway)
    if args.once:
        t.feed_file(args.log, start_at_end=False)
        print(render(t, read_arp(args.iface), args))
        return
    t.feed_file(args.log, start_at_end=True)
    try:
        while True:
            t.feed_file(args.log, start_at_end=True)
            if sys.stdout.isatty():
                sys.stdout.write("\033[2J\033[H")
            print(render(t, read_arp(args.iface), args), flush=True)
            time.sleep(max(0.5, args.interval))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
