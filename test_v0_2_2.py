#!/usr/bin/env python3
"""
api-revolver v0.2.2 test -- rate-limit header sync, with FAKE keys in a
temp folder and a fake network. No real API calls; your real
~/.revolver/keys.json is never touched.

Run from the api-revolver repo root:
    python test_v0_2_2.py
"""
import json
import os
import sys
import tempfile
import time

os.environ["REVOLVER_HOME"] = tempfile.mkdtemp(prefix="revolver-test-")
sys.path.insert(0, os.getcwd())

from revolver import storage, client                     # noqa: E402
from revolver.client import chat, _parse_duration         # noqa: E402

RESULTS = []


def check(label, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if not cond else ""))


class FakeResp:
    def __init__(self, code, body=None, headers=None, text=None):
        self.status_code = code
        self._body = body or {}
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(self._body)

    def json(self):
        return self._body


def groq_headers(req_rem, tok_rem, tok_reset="134ms"):
    return {
        "x-ratelimit-limit-requests": "1000",
        "x-ratelimit-remaining-requests": str(req_rem),
        "x-ratelimit-limit-tokens": "8000",
        "x-ratelimit-remaining-tokens": str(tok_rem),
        "x-ratelimit-reset-requests": "1m26.4s",
        "x-ratelimit-reset-tokens": tok_reset,
    }


def ok(headers=None, tokens=50):
    return FakeResp(200, {"choices": [{"message": {"content": "hi"}}],
                          "usage": {"total_tokens": tokens}}, headers=headers)


SCRIPT = {}


def fake_post(url, headers=None, json=None, timeout=None, params=None):
    item = SCRIPT[headers["Authorization"].split()[-1]].pop(0)
    if isinstance(item, Exception):
        raise item
    return item


client.requests.post = fake_post


def key(name):
    return next(k for k in storage.load()["keys"] if k["name"] == name)


def cooldown(name):
    return max(0.0, key(name).get("cooldown_until", 0) - time.time())


def main():
    store = storage.load()
    for n in ("k1", "k2"):
        # deliberately wrong local request_limit, header should fix it
        storage.add_key(store, "groq", n, n.upper(), "fake-model", 100000, request_limit=5000)
    print(f"temp store: {storage.STORE_PATH}\n")
    with storage.transaction() as _s:   # these cases assume k1 is tried first
        _s["selector"] = "sequential"

    print("1. duration parser")
    for raw, want in [("1m26.4s", 86.4), ("134ms", 0.134), ("7.66s", 7.66),
                      ("2h", 7200.0), ("5", 5.0), ("1h2m3s", 3723.0)]:
        got = _parse_duration(raw)
        check(f"{raw!r} -> {want}", got is not None and abs(got - want) < 1e-9, got)
    check("None -> None", _parse_duration(None) is None)
    check("junk -> None", _parse_duration("soon") is None)

    print("2. success syncs real request count + TPM")
    SCRIPT.update(K1=[ok(groq_headers(req_rem=990, tok_rem=7000))])
    r = chat("x")                       # default max_tokens=1024 < 7000 left
    k1 = key("k1")
    check("served by k1", r["key_used"] == "k1", r["key_used"])
    check("request_limit synced to 1000", k1["request_limit"] == 1000, k1["request_limit"])
    check("requests_used synced to 10", k1["requests_used"] == 10, k1["requests_used"])
    check("tpm_limit stored", k1.get("tpm_limit") == 8000, k1.get("tpm_limit"))
    check("tpm_remaining stored", k1.get("tpm_remaining") == 7000, k1.get("tpm_remaining"))
    check("k1 not cooling (enough TPM left)", cooldown("k1") == 0)

    print("3. TPM below max_tokens -> pre-emptive cooldown, switch BEFORE a 429")
    SCRIPT.update(K1=[ok(groq_headers(req_rem=989, tok_rem=500, tok_reset="20s"))])
    r = chat("x", max_tokens=1024)
    check("call itself still succeeds", r["key_used"] == "k1", r["key_used"])
    check("k1 cooling ~20s", 15 < cooldown("k1") <= 20.5, f"{cooldown('k1'):.1f}s")
    check("reason says TPM low", "TPM low" in key("k1").get("last_error", ""),
          key("k1").get("last_error"))

    print("4. next call goes to k2 without touching k1")
    SCRIPT.update(K2=[ok(groq_headers(req_rem=999, tok_rem=7950))])
    r = chat("x")
    check("served by k2", r["key_used"] == "k2", r["key_used"])
    check("k1 script untouched", SCRIPT["K1"] == [])

    print("5. no headers (e.g. Gemini) -> nothing synced, no crash")
    before = key("k2")["requests_used"]
    SCRIPT.update(K2=[ok(headers={})])
    r = chat("x")
    check("served by k2", r["key_used"] == "k2", r["key_used"])
    check("local count +1 only", key("k2")["requests_used"] == before + 1,
          key("k2")["requests_used"])

    print("6. 429 also syncs headers")
    h = groq_headers(req_rem=0, tok_rem=8000)
    h["retry-after"] = "86"
    SCRIPT.update(K2=[FakeResp(429, headers=h)], K1=[])
    try:
        chat("x")
    except Exception:
        pass                             # k1 is cooling too, exhaustion is expected
    check("k2 requests_used synced to 1000", key("k2")["requests_used"] == 1000,
          key("k2")["requests_used"])
    check("k2 cooling ~86s", 80 < cooldown("k2") <= 86.5, f"{cooldown('k2'):.1f}s")

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")


if __name__ == "__main__":
    main()
