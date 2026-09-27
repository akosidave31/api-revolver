#!/usr/bin/env python3
"""
api-revolver v0.2.3 test -- refill-aware daily request counts.
FAKE keys in a temp folder, fake network, no real API calls; your real
~/.revolver/keys.json is never touched.

Run from the api-revolver repo root:
    python test_v0_2_3.py
"""
import json
import os
import sys
import tempfile
import time
from datetime import date, timedelta

os.environ["REVOLVER_HOME"] = tempfile.mkdtemp(prefix="revolver-test-")
sys.path.insert(0, os.getcwd())

from revolver import storage, client                          # noqa: E402
from revolver.client import chat                               # noqa: E402
from revolver.rotator import Rotator, _requests_used, _maybe_reset   # noqa: E402

RESULTS = []
REFILL_S = 86400 / 1000          # 86.4s per request at 1000/day


def check(label, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if not cond else ""))


class FakeResp:
    def __init__(self, code, body=None, headers=None):
        self.status_code = code
        self._body = body or {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


SCRIPT = {}


def fake_post(url, headers=None, json=None, timeout=None, params=None):
    return SCRIPT[headers["Authorization"].split()[-1]].pop(0)


client.requests.post = fake_post


def setkey(name, **fields):
    with storage.transaction() as s:
        for k in s["keys"]:
            if k["name"] == name:
                k.update(fields)


def key(name):
    return next(k for k in storage.load()["keys"] if k["name"] == name)


def main():
    store = storage.load()
    for n in ("k1", "k2"):
        storage.add_key(store, "groq", n, n.upper(), "fake-model", 100000)
    print(f"temp store: {storage.STORE_PATH}\n")
    with storage.transaction() as _s:   # these cases assume k1 is tried first
        _s["selector"] = "sequential"
    now = time.time()

    print("1. refill math")
    k = {"requests_used": 950, "request_limit": 1000, "period": "daily",
         "req_seen_at": now - 100 * REFILL_S}
    check("950 used, 100 refilled -> ~850", abs(_requests_used(k, now) - 850) < 0.01,
          _requests_used(k, now))
    k["req_seen_at"] = now - 5000 * REFILL_S
    check("never below 0", _requests_used(k, now) == 0)
    no_hdr = {"requests_used": 950, "request_limit": 1000, "period": "daily"}
    check("no header data -> unchanged (950)", _requests_used(no_hdr, now) == 950)

    print("2. benched key comes back once refilled")
    # k1 synced at 950 used (95% > 90% threshold) three hours ago -> ~825 now
    setkey("k1", requests_used=950, request_limit=1000, req_seen_at=now - 3 * 3600)
    with storage.transaction() as s:
        s["active_index"] = 0
    picked = Rotator().acquire_key()
    check("k1 usable again (refilled to ~82.5%)", picked["name"] == "k1", picked["name"])

    print("3. freshly synced key at 95% is still skipped")
    setkey("k1", requests_used=950, req_seen_at=time.time())
    picked = Rotator().acquire_key()
    check("switched to k2", picked["name"] == "k2", picked["name"])

    print("4. midnight reset leaves synced counts alone")
    yesterday = str(date.today() - timedelta(days=1))
    synced = {"tokens_used": 500, "requests_used": 400, "req_seen_at": now,
              "period": "daily", "period_start": yesterday}
    local = {"tokens_used": 500, "requests_used": 400,
             "period": "daily", "period_start": yesterday}
    _maybe_reset(synced)
    _maybe_reset(local)
    check("synced: requests kept (400)", synced["requests_used"] == 400)
    check("synced: daily tokens still reset", synced["tokens_used"] == 0)
    check("local-only: requests reset to 0", local["requests_used"] == 0)

    print("5. a real call stamps req_seen_at")
    setkey("k2", requests_used=0, req_seen_at=None)
    SCRIPT["K2"] = [FakeResp(200, {"choices": [{"message": {"content": "hi"}}],
                                    "usage": {"total_tokens": 9}},
                             headers={"x-ratelimit-limit-requests": "1000",
                                      "x-ratelimit-remaining-requests": "990"})]
    t0 = time.time()
    r = chat("x")
    k2 = key("k2")
    check("served by k2", r["key_used"] == "k2", r["key_used"])
    check("req_seen_at stamped", (k2.get("req_seen_at") or 0) >= t0, k2.get("req_seen_at"))
    check("count synced to 10", k2["requests_used"] == 10, k2["requests_used"])

    print("6. dashboard shows refill-aware count")
    setkey("k1", requests_used=950, req_seen_at=time.time() - 100 * REFILL_S)
    row = next(r for r in Rotator().dashboard_rows() if r["name"] == "k1")
    check("k1 shows ~850 used", abs(row["req_used"] - 850) <= 1, row["req_used"])

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")


if __name__ == "__main__":
    main()
