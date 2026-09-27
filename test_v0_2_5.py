#!/usr/bin/env python3
"""
api-revolver v0.2.5 test -- 429 cooldown choice + reason on the dashboard.
FAKE keys in a temp folder, fake network; your real ~/.revolver/keys.json
is never touched.

Run from the api-revolver repo root:
    python test_v0_2_5.py
"""
import json
import os
import sys
import tempfile
import time

os.environ["REVOLVER_HOME"] = tempfile.mkdtemp(prefix="revolver-test-")
sys.path.insert(0, os.getcwd())

from revolver import storage, client                     # noqa: E402
from revolver.client import chat, _cooldown_for_429       # noqa: E402
from revolver.rotator import Rotator                      # noqa: E402

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


def hdrs(req_rem=999, tok_rem=7982, req_reset="1m26.4s", tok_reset="134ms"):
    return {"x-ratelimit-limit-requests": "1000",
            "x-ratelimit-remaining-requests": str(req_rem),
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": str(tok_rem),
            "x-ratelimit-reset-requests": req_reset,
            "x-ratelimit-reset-tokens": tok_reset}


TPD_BODY = ('{"error":{"message":"Rate limit reached for model `qwen/qwen3.8-27b` '
            'in organization `org_x` on tokens per day (TPD): Limit 200000, '
            'Used 199990, Requested 50. Please try again in 7m30.5s.",'
            '"type":"tokens","code":"rate_limit_exceeded"}}')


def near(a, b):
    return abs(a - b) < 0.01


def main():
    print("1. cooldown choice")
    cases = [
        ("Retry-After header wins", FakeResp(429, headers={"retry-after": "5", **hdrs()}, text=TPD_BODY), 5.0),
        ("'try again in 7m30.5s' in body", FakeResp(429, text=TPD_BODY), 450.5),
        ("headers show quota left -> 10s", FakeResp(429, headers=hdrs(), text="{}"), 10.0),
        ("requests at 0 -> reset-requests", FakeResp(429, headers=hdrs(req_rem=0), text="{}"), 86.4),
        ("TPM nearly 0 -> reset-tokens", FakeResp(429, headers=hdrs(tok_rem=100, tok_reset="20s"), text="{}"), 20.0),
        ("both low -> longer wait", FakeResp(429, headers=hdrs(req_rem=0, tok_rem=100, tok_reset="20s"), text="{}"), 86.4),
        ("no information -> 60s", FakeResp(429, text="{}"), 60.0),
        ("absurd wait clamped to 1h", FakeResp(429, text="try again in 5h"), 3600.0),
    ]
    for label, resp, want in cases:
        got = _cooldown_for_429(resp)
        check(f"{label}: {want}s", near(got, want), got)

    print("2. live path: reason stored, next key serves")
    store = storage.load()
    for n in ("k1", "k2"):
        storage.add_key(store, "groq", n, n.upper(), "fake-model", 200000)
    Rotator().set_selector("sequential")        # deterministic: k1 first
    script = {"K1": [FakeResp(429, text=TPD_BODY)],
              "K2": [FakeResp(200, {"choices": [{"message": {"content": "hi"}}],
                                    "usage": {"total_tokens": 9}})]}
    client.requests.post = lambda url, headers=None, **kw: script[headers["Authorization"].split()[-1]].pop(0)
    r = chat("x")
    k1 = next(k for k in storage.load()["keys"] if k["name"] == "k1")
    left = k1["cooldown_until"] - time.time()
    check("served by k2", r["key_used"] == "k2", r["key_used"])
    check("k1 cooling ~450s (from body)", 440 < left <= 451, f"{left:.0f}s")
    check("last_error keeps the reason", "tokens per day" in k1.get("last_error", ""),
          k1.get("last_error"))
    status = next(row["status"] for row in Rotator().dashboard_rows() if row["name"] == "k1")
    check("dashboard status shows it", "tokens per day" in status, status)

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")


if __name__ == "__main__":
    main()
