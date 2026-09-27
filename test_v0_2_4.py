#!/usr/bin/env python3
"""
api-revolver v0.2.4 test -- weighted key selection.
FAKE keys in a temp folder, no network; your real ~/.revolver/keys.json
is never touched.

Run from the api-revolver repo root:
    python test_v0_2_4.py
"""
import os
import random
import sys
import tempfile
import time
from collections import Counter

os.environ["REVOLVER_HOME"] = tempfile.mkdtemp(prefix="revolver-test-")
sys.path.insert(0, os.getcwd())

from revolver import storage                                  # noqa: E402
from revolver.rotator import Rotator, AllKeysExhausted        # noqa: E402

RESULTS = []
N = 400


def check(label, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if not cond else ""))


def reset_all():
    with storage.transaction() as s:
        s["active_index"] = 0
        s.pop("selector", None)
        for k in s["keys"]:
            k.update(disabled=False, cooldown_until=0, last_error="",
                     tokens_used=0, requests_used=0, request_limit=1000)
            for f in ("tpm_limit", "tpm_remaining", "tpm_reset_at", "req_seen_at"):
                k.pop(f, None)


def setkey(name, **fields):
    with storage.transaction() as s:
        for k in s["keys"]:
            if k["name"] == name:
                k.update(fields)


def picks(n=N):
    r = Rotator()
    return Counter(r.acquire_key()["name"] for _ in range(n))


def main():
    random.seed(7)
    store = storage.load()
    for n in ("k1", "k2", "k3"):
        storage.add_key(store, "groq", n, n.upper(), "fake-model", 200000)
    print(f"temp store: {storage.STORE_PATH}\n")

    print("1. default selector is weighted")
    reset_all()
    check("no selector saved -> weighted",
          storage.load().get("selector", "weighted") == "weighted")

    print(f"2. fresh keys share load ({N} picks)")
    c = picks()
    shares = {k: c[k] / N for k in ("k1", "k2", "k3")}
    check("each key gets 20-47%", all(0.20 <= s <= 0.47 for s in shares.values()),
          {k: f"{v:.0%}" for k, v in shares.items()})

    print("3. nearly-empty key is protected (85% of daily requests used)")
    reset_all()
    setkey("k1", requests_used=850)
    c = picks()
    check("k1 picked < 5%", c["k1"] / N < 0.05, f"{c['k1'] / N:.1%}")

    print("4. cooling and disabled keys are never picked")
    reset_all()
    setkey("k1", cooldown_until=time.time() + 60, last_error="429 rate limited")
    setkey("k2", disabled=True, last_error="401")
    c = picks(100)
    check("only k3 picked", set(c) == {"k3"}, dict(c))

    print("5. low TPM (500/8000 until reset) is skipped while others exist")
    reset_all()
    setkey("k1", tpm_limit=8000, tpm_remaining=500, tpm_reset_at=time.time() + 30)
    c = picks()
    check("k1 never picked", c["k1"] == 0, dict(c))

    print("6. TPM info expires after reset time")
    reset_all()
    setkey("k1", tpm_limit=8000, tpm_remaining=500, tpm_reset_at=time.time() - 1)
    c = picks()
    check("k1 back in normal share (>20%)", c["k1"] / N > 0.20, f"{c['k1'] / N:.1%}")

    print("7. all keys low -> still serves the best one instead of failing")
    reset_all()
    for name, used in (("k1", 880), ("k2", 895), ("k3", 899)):
        setkey(name, requests_used=used)   # still under the 90% threshold
    # ...but low TPM pushes every key's headroom under the 10% cutoff:
    # k1 7.5%, k2 8.75%, k3 9.4% -> fallback picks the best, k3
    setkey("k1", tpm_limit=8000, tpm_remaining=600, tpm_reset_at=time.time() + 30)
    setkey("k2", tpm_limit=8000, tpm_remaining=700, tpm_reset_at=time.time() + 30)
    setkey("k3", tpm_limit=8000, tpm_remaining=750, tpm_reset_at=time.time() + 30)
    try:
        name = Rotator().acquire_key()["name"]
        check("returns best key k3", name == "k3", name)
    except AllKeysExhausted as e:
        check("returns best key k3", False, f"raised: {e}")

    print("8. nothing usable -> AllKeysExhausted")
    reset_all()
    for name in ("k1", "k2", "k3"):
        setkey(name, cooldown_until=time.time() + 60, last_error="429 rate limited")
    try:
        Rotator().acquire_key()
        check("raises AllKeysExhausted", False, "no exception")
    except AllKeysExhausted as e:
        check("raises AllKeysExhausted", "Next key free in" in str(e), str(e))

    print("9. sequential selector restores v0.2.3 behavior")
    reset_all()
    Rotator().set_selector("sequential")
    c = picks(50)
    check("sticks to k1", c == Counter({"k1": 50}), dict(c))
    try:
        Rotator().set_selector("bogus")
        check("rejects unknown selector", False, "accepted")
    except ValueError:
        check("rejects unknown selector", True)

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")


if __name__ == "__main__":
    main()
