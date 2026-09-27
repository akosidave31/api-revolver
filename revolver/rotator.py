"""
The revolver's cylinder. Decides which key is "up" and rotates
BEFORE a key hits its limit, not after it fails.

revolver v0.2.0: every method runs inside storage.transaction(), so
concurrent requests never lose usage updates, and usage is charged to
the key that actually served the request.
"""
from datetime import date
from . import storage


class NoKeysAvailable(Exception):
    pass


class AllKeysExhausted(Exception):
    pass


def _period_expired(key):
    start = date.fromisoformat(key["period_start"])
    today = date.today()
    if key["period"] == "daily":
        return today > start
    if key["period"] == "monthly":
        return (today.year, today.month) != (start.year, start.month)
    return False


def _maybe_reset(key):
    if _period_expired(key):
        key["tokens_used"] = 0
        key["requests_used"] = 0
        key["period_start"] = str(date.today())


def _pct_used(key):
    if key["token_limit"] <= 0:
        return 0.0
    return 100.0 * key["tokens_used"] / key["token_limit"]


def _request_pct_used(key):
    limit = key.get("request_limit", 0)
    if limit <= 0:
        return 0.0
    return 100.0 * key.get("requests_used", 0) / limit


def _worst_pct(key):
    """Whichever dimension -- tokens or requests -- is closer to its
    cap is what actually determines when this key runs out."""
    return max(_pct_used(key), _request_pct_used(key))


def _over_threshold(key, threshold_pct):
    return _worst_pct(key) >= threshold_pct


def _find(store, key_id):
    for i, k in enumerate(store["keys"]):
        if k["id"] == key_id:
            return i, k
    raise KeyError(f"No key with id {key_id}")


def _advance(store, start_idx):
    """Walk the cylinder looking for a key under threshold. Mutates
    store["active_index"]; the caller's transaction saves it.
    Raises AllKeysExhausted if every key is past threshold."""
    keys = store["keys"]
    threshold = store["rotate_threshold_pct"]
    n = len(keys)
    for step in range(1, n + 1):
        candidate = (start_idx + step) % n
        _maybe_reset(keys[candidate])
        if not _over_threshold(keys[candidate], threshold):
            store["active_index"] = candidate
            prev = keys[start_idx]
            print(f"[revolver] switched to key '{keys[candidate]['name']}' "
                  f"(was at {_worst_pct(prev):.1f}% on previous key "
                  f"-- tokens {_pct_used(prev):.1f}%, "
                  f"requests {_request_pct_used(prev):.1f}%)")
            return candidate
    raise AllKeysExhausted(
        "All configured keys are past the rotation threshold. "
        "Add more keys with: revolver setup"
    )


class Rotator:
    def __init__(self):
        with storage.transaction() as store:
            if not store["keys"]:
                raise NoKeysAvailable("No API keys configured. Run: revolver setup")
            # reset any keys whose period rolled over
            for k in store["keys"]:
                _maybe_reset(k)
            self.store = store  # snapshot, for display only

    def acquire_key(self, auto_rotate=True):
        """Return a copy of the key that should serve the next request,
        rotating proactively if the current one is past the threshold.
        Pass its ["id"] back to record_usage() / force_rotate()."""
        with storage.transaction() as store:
            keys = store["keys"]
            idx = store["active_index"] % len(keys)
            _maybe_reset(keys[idx])
            if auto_rotate and _over_threshold(keys[idx], store["rotate_threshold_pct"]):
                idx = _advance(store, idx)
            self.store = store
            return dict(keys[idx])

    # v0.1 name, kept so older callers keep working
    active_key = acquire_key

    def record_usage(self, tokens_used, key_id=None):
        """Call after every API response with the real token count
        (prompt + completion). Also counts as one request.
        key_id: the key that served the call (recommended). If omitted,
        charges the active key, like v0.1."""
        with storage.transaction() as store:
            keys = store["keys"]
            active_idx = store["active_index"] % len(keys)
            idx = active_idx if key_id is None else _find(store, key_id)[0]
            key = keys[idx]
            key["tokens_used"] += tokens_used
            key["requests_used"] = key.get("requests_used", 0) + 1

            # Proactively rotate so the *next* call already uses a fresh key.
            # Only if this key is still the active one: another request may
            # have rotated already. If every key is spent, don't fail THIS
            # (successful) call -- the next acquire_key() raises instead.
            if idx == active_idx and _over_threshold(key, store["rotate_threshold_pct"]):
                try:
                    _advance(store, idx)
                except AllKeysExhausted:
                    pass
            self.store = store

    def force_rotate(self, from_key_id=None):
        """Rotate away from a key the provider rejected (e.g. 429).
        from_key_id: the key that failed. If the active key is already a
        different one (another request rotated first), nothing happens."""
        with storage.transaction() as store:
            keys = store["keys"]
            idx = store["active_index"] % len(keys)
            if from_key_id is not None and keys[idx]["id"] != from_key_id:
                self.store = store
                return idx
            new_idx = _advance(store, idx)
            self.store = store
            return new_idx

    def set_threshold(self, pct):
        with storage.transaction() as store:
            store["rotate_threshold_pct"] = pct
            self.store = store

    def dashboard_rows(self):
        with storage.transaction() as store:
            keys = store["keys"]
            active_idx = store["active_index"] % len(keys)
            rows = []
            for i, k in enumerate(keys):
                _maybe_reset(k)
                req_limit = k.get("request_limit", 0)
                req_used = k.get("requests_used", 0)
                rows.append({
                    "active": i == active_idx,
                    "id": k["id"],
                    "name": k["name"],
                    "provider": k["provider"],
                    "used": k["tokens_used"],
                    "limit": k["token_limit"],
                    "remaining": max(k["token_limit"] - k["tokens_used"], 0),
                    "pct": _pct_used(k),
                    "req_used": req_used,
                    "req_limit": req_limit,
                    "req_remaining": max(req_limit - req_used, 0),
                    "req_pct": _request_pct_used(k),
                    "period": k["period"],
                })
            self.store = store
            return rows
