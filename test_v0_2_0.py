#!/usr/bin/env python3
"""
api-revolver v0.2.0 test -- hammers the store from several processes and
threads at once, using FAKE keys in a temp folder. Makes no API calls and
never touches your real ~/.revolver/keys.json.

Run from the api-revolver repo root:
    python test_v0_2_0.py
"""
import os
import sys
import stat
import tempfile
import threading
import multiprocessing as mp

# Must be set BEFORE importing revolver, so storage points at the temp dir.
# Python 3.14 starts child processes with "forkserver", which re-runs this
# file's top level in each child -- so create the temp dir only once and
# hand it to the children through an env var they inherit.
if "REVOLVER_TEST_HOME" not in os.environ:
    os.environ["REVOLVER_TEST_HOME"] = tempfile.mkdtemp(prefix="revolver-test-")
os.environ["REVOLVER_HOME"] = os.environ["REVOLVER_TEST_HOME"]
sys.path.insert(0, os.getcwd())

from revolver import storage            # noqa: E402
from revolver.rotator import Rotator    # noqa: E402

PROCS, THREADS, CALLS, TOK = 4, 8, 50, 10


def worker(n):
    r = Rotator()
    for _ in range(n):
        r.record_usage(TOK, key_id=1)


def check(label, got, expected):
    ok = got == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got}, expected {expected}")
    return ok


def main():
    print(f"temp store: {storage.STORE_PATH}\n")
    store = storage.load()
    storage.add_key(store, "groq", "fake-a", "not-a-real-key", "m", 10**9)
    storage.add_key(store, "groq", "fake-b", "not-a-real-key", "m", 10**9)

    # Start processes BEFORE threads (forking while a thread holds a lock
    # can deadlock the child).
    procs = [mp.Process(target=worker, args=(CALLS,)) for _ in range(PROCS)]
    for p in procs:
        p.start()
    threads = [threading.Thread(target=worker, args=(CALLS,)) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for x in procs + threads:
        x.join()

    # Charge key 2 while key 1 is active -> must land on key 2
    Rotator().record_usage(5, key_id=2)

    keys = storage.load()["keys"]
    calls = (PROCS + THREADS) * CALLS
    results = [
        check("key 1 requests (no lost updates)", keys[0]["requests_used"], calls),
        check("key 1 tokens", keys[0]["tokens_used"], calls * TOK),
        check("key 2 tokens (charged to right key)", keys[1]["tokens_used"], 5),
        check("keys.json permissions",
              oct(stat.S_IMODE(os.stat(storage.STORE_PATH).st_mode)), "0o600"),
    ]
    print(f"\n{sum(results)}/{len(results)} passed")


if __name__ == "__main__":
    main()
