"""
Storage layer. Everything lives in ~/.revolver/keys.json
Plain JSON, no DB, no external deps required to just read/write state.
"""
import json
import os
from datetime import date

STORE_DIR = os.path.expanduser("~/.revolver")
STORE_PATH = os.path.join(STORE_DIR, "keys.json")

DEFAULT_STORE = {
    "keys": [],           # list of key records, see add_key() for shape
    "active_index": 0,    # index into keys[] currently in use
    "rotate_threshold_pct": 90,  # proactively rotate at this % of limit used
}


def _ensure_dir():
    os.makedirs(STORE_DIR, exist_ok=True)


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
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, STORE_PATH)  # atomic write, no half-corrupted files


def add_key(store, provider, name, api_key, model, token_limit, period="daily",
            request_limit=1000):
    next_id = (max((k["id"] for k in store["keys"]), default=0)) + 1
    store["keys"].append({
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
    save(store)
    return store
