"""
revolver v0.3.0: tiny web server for Phone A. Stdlib only.

    revolver serve            # 0.0.0.0:8080
    revolver serve 9000

Serves a chat page (/) and a dashboard (/dashboard) to any browser on the
same Wi-Fi, and passes prompts through the revolver.

API (everything except /api/health needs  Authorization: Bearer <token>):
  GET  /api/health  -> {"ok": true, "version": ...}
  GET  /api/status  -> {"selector", "threshold", "keys": [...]}  (no API keys)
  POST /api/chat    {"prompt": "...", "system": "...", "max_tokens": 1024,
                     "temperature": 0.7}
                    -> {"text", "key_used", "tokens", "seconds"}
Errors: 400 bad request, 401 token, 502 provider problem, 503 no usable key.
"""
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import storage
from .client import chat, RevolverError
from .rotator import Rotator, AllKeysExhausted, NoKeysAvailable

VERSION = "0.3.0"
MAX_BODY = 64 * 1024
MAX_TOKENS_CAP = 4096
TOKEN_PATH = os.path.join(storage.STORE_DIR, "server_token")


# ---------------------------------------------------------------- helpers

def load_token(regenerate=False):
    """Read the server token, creating it (mode 600) on first use."""
    storage._ensure_dir()
    if not regenerate and os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as f:
            tok = f.read().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(9)          # 12 chars, easy to type on a phone
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok


def lan_ip():
    """This phone's Wi-Fi address (no packet is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _wake_lock(on):
    exe = "termux-wake-lock" if on else "termux-wake-unlock"
    if not shutil.which(exe):
        return False
    try:
        subprocess.run([exe], timeout=15, check=False)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "revolver/" + VERSION
    token = ""          # set per server by make_server()
    quiet = False

    def log_message(self, fmt, *args):
        if not self.quiet:
            sys.stderr.write(f"[serve] {self.client_address[0]} {fmt % args}\n")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _authed(self):
        got = self.headers.get("Authorization", "")
        want = "Bearer " + self.token
        if hmac.compare_digest(got.encode("utf-8"), want.encode("utf-8")):
            return True
        self._json(401, {"error": "Missing or wrong token. Get it on Phone A: revolver token"})
        return False

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, CHAT_HTML, "text/html; charset=utf-8")
        if path == "/dashboard":
            return self._send(200, DASH_HTML, "text/html; charset=utf-8")
        if path == "/api/health":
            return self._json(200, {"ok": True, "version": VERSION})
        if path == "/api/status":
            if not self._authed():
                return
            try:
                r = Rotator()
                rows = r.dashboard_rows()
            except NoKeysAvailable as e:
                return self._json(503, {"error": str(e)})
            return self._json(200, {"selector": r.store.get("selector", "weighted"),
                                    "threshold": r.store["rotate_threshold_pct"],
                                    "keys": rows})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/chat":
            return self._json(404, {"error": "not found"})
        if not self._authed():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return self._json(400, {"error": "empty body"})
        if length > MAX_BODY:
            return self._json(413, {"error": f"body over {MAX_BODY} bytes"})
        try:
            req = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "body must be JSON"})
        if not isinstance(req, dict):
            return self._json(400, {"error": "body must be a JSON object"})

        prompt = req.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return self._json(400, {"error": "prompt is required"})
        system = req.get("system") if isinstance(req.get("system"), str) else None
        try:
            max_tokens = max(1, min(int(req.get("max_tokens", 1024)), MAX_TOKENS_CAP))
            temperature = max(0.0, min(float(req.get("temperature", 0.7)), 2.0))
        except (TypeError, ValueError):
            return self._json(400, {"error": "max_tokens / temperature must be numbers"})

        t0 = time.time()
        try:
            res = chat(prompt, system=system, max_tokens=max_tokens, temperature=temperature)
        except (AllKeysExhausted, NoKeysAvailable) as e:
            return self._json(503, {"error": str(e)})
        except RevolverError as e:
            return self._json(502, {"error": str(e)})
        except Exception as e:     # details stay in Phone A's log, not the browser
            traceback.print_exc()
            return self._json(500, {"error": f"internal error ({type(e).__name__}), see Phone A log"})
        self._json(200, {"text": res["text"], "key_used": res["key_used"],
                         "tokens": res["tokens_used_this_call"],
                         "seconds": round(time.time() - t0, 2)})


def make_server(host="0.0.0.0", port=8080, token=None, quiet=False):
    handler = type("BoundHandler", (Handler,),
                   {"token": token or load_token(), "quiet": quiet})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def _stop(signum, frame):
    raise KeyboardInterrupt


def serve(host="0.0.0.0", port=8080):
    try:
        Rotator()
    except NoKeysAvailable as e:
        print(e)
        return 1
    token = load_token()
    try:
        httpd = make_server(host, port, token)
    except OSError as e:
        print(f"Can't open port {port}: {e}. Is the server already running?")
        return 1
    signal.signal(signal.SIGTERM, _stop)      # pkill -> clean Server OFF
    locked = _wake_lock(True)
    ip = lan_ip()
    print(f"\n=== Revolver server ON (v{VERSION}) ===")
    print(f"  this phone : http://localhost:{port}")
    print(f"  Phone B    : http://{ip}:{port}" if ip else "  Phone B    : (no Wi-Fi address found)")
    print(f"  dashboard  : /dashboard")
    print(f"  token      : {token}")
    print(f"  wake-lock  : {'on' if locked else 'not available (install termux-api?)'}")
    print("  stop       : Ctrl+C  (or: pkill -f 'revolver serve')\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if locked:
            _wake_lock(False)
        print("\n=== Revolver server OFF ===")
    return 0


# ---------------------------------------------------------------- pages

_STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--muted:#667085;--accent:#2563eb;
--me:#e8f0fe;--err:#b42318;--ok:#067647;--warn:#b54708;--border:#e4e7ec;--track:#eaecf0}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--text:#e6e8eb;
--muted:#98a2b3;--accent:#60a5fa;--me:#1e2a44;--err:#f97066;--ok:#47cd89;--warn:#fdb022;
--border:#2a2f3a;--track:#2a2f3a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 system-ui,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;gap:8px;
padding:10px 16px;border-bottom:1px solid var(--border);background:var(--card)}
header a{color:var(--accent);text-decoration:none;font-size:14px}
.sub{font-size:13px;color:var(--muted)}
"""

CHAT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Revolver Chat</title>
<style>""" + _STYLE + """
body{display:flex;flex-direction:column;height:100dvh}
#log{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:88%;padding:10px 12px;border-radius:12px;white-space:pre-wrap;
overflow-wrap:anywhere;background:var(--card);border:1px solid var(--border)}
.me{align-self:flex-end;background:var(--me)}
.meta{font-size:12px;color:var(--muted);margin-top:4px}
.err{color:var(--err)}
form,#tokenbox{display:flex;gap:8px;padding:10px 16px;background:var(--card)}
form{border-top:1px solid var(--border)}
#tokenbox{display:none;border-bottom:1px solid var(--border)}
textarea,input{flex:1;padding:10px;border-radius:10px;border:1px solid var(--border);
background:var(--bg);color:var(--text);font:inherit}
textarea{resize:none;height:52px}
button{padding:0 18px;border:0;border-radius:10px;background:var(--accent);color:#fff;
font:inherit;font-weight:600}
button:disabled{opacity:.5}
</style></head><body>
<header><strong>Revolver Chat</strong>
<span><a href="/dashboard">Dashboard</a> &middot; <a href="#" id="forget">Token</a></span></header>
<div id="tokenbox"><input id="tok" placeholder="Server token (shown on Phone A)" autocomplete="off">
<button id="savetok" type="button">Save</button></div>
<div id="log"></div>
<form id="f"><textarea id="p" placeholder="Ask something..."></textarea>
<button id="send">Send</button></form>
<script>
const $ = id => document.getElementById(id);
let memTok = "";
function getTok(){ try { return localStorage.getItem("revolver_token") || memTok } catch(e) { return memTok } }
function setTok(t){ memTok = t; try { localStorage.setItem("revolver_token", t) } catch(e) {} }
function add(text, cls, meta){
  const d = document.createElement("div");
  d.className = "msg " + (cls || "");
  d.textContent = text;
  if (meta) { const m = document.createElement("div"); m.className = "meta"; m.textContent = meta; d.appendChild(m); }
  $("log").appendChild(d); $("log").scrollTop = $("log").scrollHeight;
  return d;
}
function tokenBox(show){ $("tokenbox").style.display = show ? "flex" : "none"; if (show) $("tok").focus(); }
$("savetok").onclick = () => { const t = $("tok").value.trim(); if (t) { setTok(t); tokenBox(false); add("Token saved on this browser."); } };
$("forget").onclick = e => { e.preventDefault(); tokenBox(true); };
if (!getTok()) tokenBox(true);
$("f").onsubmit = async e => {
  e.preventDefault();
  const prompt = $("p").value.trim();
  if (!prompt) return;
  if (!getTok()) { tokenBox(true); return; }
  add(prompt, "me"); $("p").value = ""; $("send").disabled = true;
  const wait = add("...");
  try {
    const r = await fetch("/api/chat", { method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + getTok() },
      body: JSON.stringify({ prompt }) });
    const j = await r.json().catch(() => ({ error: "bad response (" + r.status + ")" }));
    wait.remove();
    if (r.ok) add(j.text, "", "key " + j.key_used + " \\u00b7 " + j.tokens + " tokens \\u00b7 " + j.seconds + "s");
    else { add(j.error || ("error " + r.status), "err"); if (r.status === 401) tokenBox(true); }
  } catch (err) { wait.remove(); add("Can't reach Phone A: " + err, "err"); }
  finally { $("send").disabled = false; $("p").focus(); }
};
</script></body></html>"""

DASH_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Revolver Dashboard</title>
<style>""" + _STYLE + """
main{padding:16px;display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
#top{padding:12px 16px 0}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px}
.card.active{border-color:var(--accent)}
.name{font-weight:600}
.row{margin-top:8px;font-size:13px}
.bar{height:6px;border-radius:3px;background:var(--track);overflow:hidden;margin:3px 0}
.bar span{display:block;height:100%;background:var(--accent)}
.status{margin-top:8px;font-size:13px}
.ok{color:var(--ok)} .warn{color:var(--warn)}
</style></head><body>
<header><strong>Revolver Dashboard</strong><a href="/">Chat</a></header>
<div id="top"><div id="sum" class="sub"></div><div id="msg" class="sub"></div></div>
<main id="keys"></main>
<script>
const $ = id => document.getElementById(id);
function tok(){ try { return localStorage.getItem("revolver_token") || "" } catch(e) { return "" } }
function el(tag, cls, text){ const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
function row(label, pct, detail){
  const r = el("div", "row"); r.append(el("div", "", label + "  " + detail + "  (" + pct.toFixed(1) + "%)"));
  const b = el("div", "bar"), s = el("span"); s.style.width = Math.min(100, pct) + "%"; b.append(s); r.append(b);
  return r;
}
async function load(){
  if (!tok()) { $("msg").textContent = "No token on this browser yet: open Chat and enter it."; return; }
  try {
    const r = await fetch("/api/status", { headers: { "Authorization": "Bearer " + tok() } });
    const j = await r.json();
    if (!r.ok) { $("msg").textContent = j.error || ("error " + r.status); return; }
    $("sum").textContent = "selector: " + j.selector + " \\u00b7 rotate at " + j.threshold + "% \\u00b7 " + j.keys.length + " keys";
    const box = $("keys"); box.replaceChildren();
    for (const k of j.keys) {
      const c = el("div", "card" + (k.active ? " active" : ""));
      c.append(el("div", "name", "#" + k.id + " " + k.name + (k.active ? "  (last used)" : "")));
      c.append(el("div", "sub", k.provider + " \\u00b7 " + (k.model || "")));
      c.append(row("tokens/day", k.pct, k.used + "/" + k.limit));
      c.append(row("requests/day", k.req_pct, k.req_used + "/" + k.req_limit));
      if (k.tpm_limit) c.append(el("div", "row sub", "tpm limit " + k.tpm_limit + "/min \\u00b7 last seen " + (k.tpm_remaining ?? "?") + " left"));
      c.append(el("div", "status " + (k.status === "ok" ? "ok" : "warn"), k.status));
      box.append(c);
    }
    $("msg").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) { $("msg").textContent = "Can't reach Phone A: " + e; }
}
load(); setInterval(load, 5000);
</script></body></html>"""
