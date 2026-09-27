#!/usr/bin/env python3
"""
api-revolver v0.3.0 test -- the Phone A web server, end to end over real
HTTP on 127.0.0.1, with FAKE keys in a temp folder and a fake Groq.
No real API calls; your real ~/.revolver files are never touched.

Run from the api-revolver repo root:
    python test_v0_3_0.py
"""
import json
import os
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

os.environ["REVOLVER_HOME"] = tempfile.mkdtemp(prefix="revolver-test-")
sys.path.insert(0, os.getcwd())

from revolver import storage, client                       # noqa: E402
from revolver.rotator import Rotator                        # noqa: E402
from revolver.server import make_server, load_token, TOKEN_PATH   # noqa: E402

RESULTS = []
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never use a proxy for localhost
BASE = ""


def check(label, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if not cond else ""))


class FakeResp:
    status_code = 200
    headers = {}
    text = ""

    def json(self):
        return {"choices": [{"message": {"content": "hi"}}], "usage": {"total_tokens": 7}}


client.requests.post = lambda url, **kw: FakeResp()


def call(method, path, body=None, token=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with OPENER.open(req, timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def total_requests():
    return sum(k.get("requests_used", 0) for k in storage.load()["keys"])


def main():
    global BASE
    store = storage.load()
    for n in ("k1", "k2", "k3"):
        storage.add_key(store, "groq", n, "SECRET-" + n, "fake-model", 200000)

    token = load_token()
    httpd = make_server("127.0.0.1", 0, token, quiet=True)
    BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"temp store: {storage.STORE_PATH}\nserver: {BASE}\n")

    print("1. pages and health (no token needed)")
    code, body = call("GET", "/api/health")
    check("health 200", code == 200 and json.loads(body).get("ok") is True, (code, body))
    code, body = call("GET", "/")
    check("chat page served", code == 200 and "Revolver Chat" in body, code)
    code, body = call("GET", "/dashboard")
    check("dashboard page served", code == 200 and "Revolver Dashboard" in body, code)

    print("2. token is enforced")
    code, _ = call("POST", "/api/chat", {"prompt": "x"})
    check("no token -> 401", code == 401, code)
    code, _ = call("POST", "/api/chat", {"prompt": "x"}, token="wrong")
    check("wrong token -> 401", code == 401, code)
    code, _ = call("GET", "/api/status")
    check("status without token -> 401", code == 401, code)

    print("3. chat works through the revolver")
    code, body = call("POST", "/api/chat", {"prompt": "hello"}, token=token)
    j = json.loads(body) if code == 200 else {}
    check("200 with text 'hi'", code == 200 and j.get("text") == "hi", (code, body[:120]))
    check("reports key used + tokens", j.get("key_used") in ("k1", "k2", "k3") and j.get("tokens") == 7, j)

    print("4. status never leaks API keys")
    code, body = call("GET", "/api/status", token=token)
    check("status 200 with 3 keys", code == 200 and len(json.loads(body)["keys"]) == 3, code)
    check("no 'SECRET' / 'api_key' in response", "SECRET" not in body and "api_key" not in body)

    print("5. bad requests are rejected cleanly")
    code, _ = call("POST", "/api/chat", raw=b"{not json", token=token)
    check("bad JSON -> 400", code == 400, code)
    code, _ = call("POST", "/api/chat", {"prompt": "   "}, token=token)
    check("empty prompt -> 400", code == 400, code)
    code, _ = call("POST", "/api/chat", {"prompt": "x", "max_tokens": "lots"}, token=token)
    check("non-numeric max_tokens -> 400", code == 400, code)
    code, _ = call("GET", "/nope")
    check("unknown path -> 404", code == 404, code)

    print("6. no usable key -> 503 with the reason")
    with storage.transaction() as s:
        for k in s["keys"]:
            k["cooldown_until"] = time.time() + 60
            k["last_error"] = "429 test"
    code, body = call("POST", "/api/chat", {"prompt": "x"}, token=token)
    check("503 'Next key free in'", code == 503 and "Next key free in" in body, (code, body[:120]))
    Rotator().enable()

    print("7. two phones at once: 12 parallel chats")
    before = total_requests()
    codes = []
    lock = threading.Lock()

    def worker():
        c, _ = call("POST", "/api/chat", {"prompt": "parallel"}, token=token)
        with lock:
            codes.append(c)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("all 12 got 200", codes.count(200) == 12, codes)
    check("all 12 counted (no lost updates)", total_requests() - before == 12,
          total_requests() - before)

    print("8. token file")
    mode = stat.S_IMODE(os.stat(TOKEN_PATH).st_mode)
    check("server_token is 600", mode == 0o600, oct(mode))
    check("same token on next load", load_token() == token)
    new = load_token(regenerate=True)
    check("'revolver token new' changes it", new != token and len(new) >= 12, new)

    httpd.shutdown()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")


if __name__ == "__main__":
    main()
