#!/usr/bin/env python3
"""
api-revolver v0.2.1 test -- failover per error type, with FAKE keys in a
temp folder and a fake network. Makes no real API calls and never touches
your real ~/.revolver/keys.json.

Run from the api-revolver repo root:
    python test_v0_2_1.py
"""
import json
import os
import sys
import tempfile
import time

os.environ["REVOLVER_HOME"] = tempfile.mkdtemp(prefix="revolver-test-")
sys.path.insert(0, os.getcwd())

import requests                                              # noqa: E402
from revolver import storage, client                         # noqa: E402
from revolver.client import chat, RevolverError              # noqa: E402
from revolver.rotator import Rotator, AllKeysExhausted       # noqa: E402


class FakeResp:
    def __init__(self, code, body=None, headers=None, text=None):
        self.status_code = code
        self._body = body or {}
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(self._body)

    def json(self):
        return self._body


def ok():
    return FakeResp(200, {"choices": [{"message": {"content": "hi"}}],
                          "usage": {"total_tokens": 7}})


SCRIPT = {}   # api_key -> list of responses / exceptions, consumed in order


def fake_post(url, headers=None, json=None, timeout=None, params=None):
    api_key = headers["Authorization"].split()[-1]
    item = SCRIPT[api_key].pop(0)
    if isinstance(item, Exception):
        raise item
    return item


client.requests.post = fake_post


def reset(**script):
    SCRIPT.clear()
    SCRIPT.update(script)
    with storage.transaction() as s:
        s["active_index"] = 0
        for k in s["keys"]:
            k.update(disabled=False, cooldown_until=0, last_error="",
                     tokens_used=0, requests_used=0)


def key(name):
    return next(k for k in storage.load()["keys"] if k["name"] == name)


def cooldown(name):
    return max(0.0, key(name).get("cooldown_until", 0) - time.time())


RESULTS = []


def check(label, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if not cond else ""))


def expect_raise(exc_type, fn):
    try:
        fn()
    except exc_type as e:
        return str(e)
    except Exception as e:
        return f"WRONG EXCEPTION {type(e).__name__}: {e}"
    return "NO EXCEPTION"


def main():
    store = storage.load()
    for n in ("k1", "k2", "k3"):
        storage.add_key(store, "groq", n, n.upper(), "fake-model", 100000)
    print(f"temp store: {storage.STORE_PATH}\n")
    with storage.transaction() as _s:   # these cases assume k1 is tried first
        _s["selector"] = "sequential"

    print("1. 429 -> cooldown by Retry-After, next key serves")
    reset(K1=[FakeResp(429, headers={"retry-after": "5"})], K2=[ok()])
    r = chat("x")
    check("served by k2", r["key_used"] == "k2", r["key_used"])
    check("k1 cooling ~5s", 1 < cooldown("k1") <= 5.5, f"{cooldown('k1'):.1f}s")
    check("k1 not disabled", not key("k1").get("disabled"))

    print("2. 401 -> key disabled, next key serves")
    reset(K1=[FakeResp(401, text="invalid api key")], K2=[ok()])
    r = chat("x")
    check("served by k2", r["key_used"] == "k2", r["key_used"])
    check("k1 disabled", key("k1").get("disabled") is True)

    print("3. 500 -> short cooldown, next key serves")
    reset(K1=[FakeResp(503, text="overloaded")], K2=[ok()])
    r = chat("x")
    check("served by k2", r["key_used"] == "k2", r["key_used"])
    check("k1 cooling ~30s", 25 < cooldown("k1") <= 30.5, f"{cooldown('k1'):.1f}s")

    print("4. timeout -> cooldown, next key serves")
    reset(K1=[requests.ReadTimeout("slow")], K2=[ok()])
    r = chat("x")
    check("served by k2", r["key_used"] == "k2", r["key_used"])
    check("k1 cooling", cooldown("k1") > 0)

    print("5. 404 retired model -> clear error, nothing disabled")
    reset(K1=[FakeResp(404, text='{"error":{"code":"model_not_found"}}')])
    msg = expect_raise(RevolverError, lambda: chat("x"))
    check("RevolverError mentions model", "fake-model" in msg, msg[:120])
    check("k1 not disabled, not cooling", not key("k1").get("disabled") and cooldown("k1") == 0)

    print("6. no network -> clear error, key not punished")
    reset(K1=[requests.ConnectionError("no route")])
    msg = expect_raise(RevolverError, lambda: chat("x"))
    check("RevolverError says network", "Network unreachable" in msg, msg[:120])
    check("k1 not cooling", cooldown("k1") == 0)

    print("7. every key 429 -> AllKeysExhausted with wait time")
    reset(**{k: [FakeResp(429, headers={"retry-after": "30"})] for k in ("K1", "K2", "K3")})
    msg = expect_raise(AllKeysExhausted, lambda: chat("x"))
    check("says next key free in Ns", "Next key free in" in msg, msg[:120])

    print("8. enable all -> keys usable again")
    Rotator().enable()
    SCRIPT.update(K1=[ok()], K2=[ok()], K3=[ok()])
    r = chat("x")
    check("call succeeds after enable", r["text"] == "hi")
    check("no key disabled or cooling",
          all(not k.get("disabled") and k.get("cooldown_until", 0) == 0
              for k in storage.load()["keys"]))

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")


if __name__ == "__main__":
    main()
