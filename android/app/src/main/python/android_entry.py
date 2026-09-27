"""
Bridge between the Android app (Kotlin, via Chaquopy) and the revolver.
revolver v0.4.0. The revolver package itself is copied in unchanged by CI.
"""
import json
import os
import threading

_httpd = None
_lock = threading.Lock()


def init(home):
    """Point the revolver at the app's private folder. Must run before any
    `import revolver...` (storage reads REVOLVER_HOME at import time)."""
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.environ["REVOLVER_HOME"] = home


def start(port=8080):
    global _httpd
    with _lock:
        if _httpd is not None:
            return "already running"
        from revolver.server import make_server
        httpd = make_server("0.0.0.0", int(port))   # raises if the port is busy
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        _httpd = httpd
        return "ok"


def stop():
    global _httpd
    with _lock:
        if _httpd is None:
            return "not running"
        _httpd.shutdown()
        _httpd.server_close()
        _httpd = None
        return "stopped"


def info():
    from revolver import storage
    from revolver.server import VERSION, lan_ip, load_token
    store = storage.load()
    return json.dumps({
        "keys": len(store["keys"]),
        "token": load_token(),
        "ip": lan_ip(),
        "running": _httpd is not None,
        "version": VERSION,
    })


def add_keys(text, provider, model, token_limit, request_limit):
    """Add pasted keys (one per line), skipping blanks and duplicates.
    Returns how many were added. Names continue as 001, 002, ..."""
    from revolver import storage
    store = storage.load()
    existing = {k["api_key"] for k in store["keys"]}
    added = 0
    for line in str(text).splitlines():
        key = line.strip()
        if not key or key in existing:
            continue
        name = f"{len(store['keys']) + 1:03d}"
        storage.add_key(store, provider, name, key, model,
                        int(token_limit), "daily", int(request_limit))
        existing.add(key)
        added += 1
    return added
