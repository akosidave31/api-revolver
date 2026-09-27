"""
Storage layer. Everything lives in ~/.revolver/keys.json
(override the folder with the REVOLVER_HOME environment variable).

revolver v0.2.0: all read-modify-write goes through transaction(), which
holds an in-process lock AND an OS file lock, so several threads (a server)
or several processes (server + CLI dashboard) never overwrite each
other's usage counts.
"""
import fcntl
import json
import os
import threading
from contextlib import contextmanager
from datetime import date

STORE_DIR = os.path.expanduser(os.environ.get("REVOLVER_HOME", "~/.revolver"))
STORE_PATH = os.path.join(STORE_DIR, "keys.json")
LOCK_PATH = os.path.join(STORE_DIR, "keys.lock")

DEFAULT_STORE = {
    "keys": [],           # list of key records, see add_key() for shape
    "active_index": 0,    # index into keys[] currently in use
    "rotate_threshold_pct": 90,  # proactively rotate at this % of limit used
}

# Serializes threads inside this process. flock() below serializes
# separate processes. Not re-entrant on purpose: never open a
# transaction inside another one.
_thread_lock = threading.Lock()


def _ensure_dir():
    os.makedirs(STORE_DIR, mode=0o700, exist_ok=True)


def load():
    _ensure_dir()
    if not os.path.exists(STORE_PATH):
        save(DEFAULT_STORE)
        return json.loads(json.dumps(DEFAULT_STORE))
    with open(STORE_PATH, "r") as f:
        return json.load(f)


def save(store):
    _ensure_dir()
    tmp = STORE_PATH + ".tmp"
    # 0600: the file holds API keys, only your user may read it
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(store, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STORE_PATH)  # atomic write, no half-corrupted files


@contextmanager
def transaction():
    """Exclusive load -> modify -> save of keys.json.

        with storage.transaction() as store:
            store["keys"][0]["tokens_used"] += 10

    Saved only if the block finishes without an exception.
    """
    _ensure_dir()
    with _thread_lock:
        with open(LOCK_PATH, "a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                store = load()
                yield store
                save(store)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def add_key(store, provider, name, api_key, model, token_limit, period="daily",
            request_limit=1000):
    """Append a key using the latest on-disk state. The passed-in `store`
    dict is refreshed in place so existing callers keep working."""
    with transaction() as fresh:
        next_id = (max((k["id"] for k in fresh["keys"]), default=0)) + 1
        fresh["keys"].append({
            "id": next_id,
            "provider": provider,
            "name": name,
            "api_key": api_key,
            "model": model,
            "token_limit": token_limit,
            "period": period,           # "daily" or "monthly"
            "tokens_used": 0,
            "request_limit": request_limit,
            "requests_used": 0,
            "period_start": str(date.today()),
        })
    store.clear()
    store.update(fresh)
    return store
