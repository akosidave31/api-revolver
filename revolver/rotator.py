"""
The revolver's cylinder. Decides which key is "up" and rotates
BEFORE a key hits its limit, not after it fails.

revolver v0.2.0: every method runs inside storage.transaction(), so
concurrent requests never lose usage updates, and usage is charged to
the key that actually served the request.

revolver v0.2.1: key health. A key is usable only if it is not disabled,
not cooling down, and under the rotation threshold. Health fields live in
keys.json next to the usage counters:
    cooldown_until  unix time; key is skipped until then (429 / 5xx / timeout)
    disabled        true after 401/403 until `revolver enable`
    last_error      short reason, shown on the dashboard
"""
import time
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


def _cooldown_left(key, now):
    return max(0.0, key.get("cooldown_until", 0) - now)


def _eligible(key, threshold_pct, now):
    return (not key.get("disabled", False)
            and _cooldown_left(key, now) <= 0
            and not _over_threshold(key, threshold_pct))


def _why_not(key, threshold_pct, now):
    if key.get("disabled", False):
        return f"disabled ({key.get('last_error', '')})"
    left = _cooldown_left(key, now)
    if left > 0:
        return f"cooling down {left:.0f}s ({key.get('last_error', '')})"
    if _over_threshold(key, threshold_pct):
        return f"{_worst_pct(key):.1f}% used"
    return "ok"


def _find(store, key_id):
    for i, k in enumerate(store["keys"]):
        if k["id"] == key_id:
            return i, k
    raise KeyError(f"No key with id {key_id}")


def _exhausted_message(store, now):
    keys = store["keys"]
    threshold = store["rotate_threshold_pct"]
    live = [k for k in keys if not k.get("disabled", False)]
    if not live:
        return ("All keys are disabled. See why with: revolver dashboard  "
                "-- fix, then: revolver enable all")
    waits = [_cooldown_left(k, now) for k in live
             if _cooldown_left(k, now) > 0 and not _over_threshold(k, threshold)]
    if waits:
        return (f"All usable keys are cooling down after rate limits/errors. "
                f"Next key free in {min(waits):.0f}s.")
    return ("All configured keys are past the rotation threshold. "
            "Add more keys with: revolver setup")


def _advance(store, start_idx, now=None):
    """Walk the cylinder to the next usable key. Mutates
    store["active_index"]; the caller's transaction saves it.
    Raises AllKeysExhausted if no key is usable."""
    now = time.time() if now is None else now
    keys = store["keys"]
    threshold = store["rotate_threshold_pct"]
    n = len(keys)
    for step in range(1, n + 1):
        candidate = (start_idx + step) % n
        _maybe_reset(keys[candidate])
        if _eligible(keys[candidate], threshold, now):
            store["active_index"] = candidate
            if candidate != start_idx:
                prev = keys[start_idx]
                print(f"[revolver] switched to key '{keys[candidate]['name']}' "
                      f"(previous '{prev['name']}': {_why_not(prev, threshold, now)})")
            return candidate
    raise AllKeysExhausted(_exhausted_message(store, now))


class Rotator:
    def __init__(self):
        with storage.transaction() as store:
            if not store["keys"]:
                raise NoKeysAvailable("No API keys configured. Run: revolver setup")
            # reset any keys whose period rolled over
            for k in store["keys"]:
                _maybe_reset(k)
            self.store = store  # snapshot, for display only

    def key_count(self):
        return len(self.store["keys"])

    def acquire_key(self, auto_rotate=True):
        """Return a copy of the key that should serve the next request,
        moving on if the current one is disabled, cooling down, or past
        the threshold. Pass its ["id"] back to record_usage() / mark_*()."""
        with storage.transaction() as store:
            keys = store["keys"]
            idx = store["active_index"] % len(keys)
            _maybe_reset(keys[idx])
            now = time.time()
            if auto_rotate and not _eligible(keys[idx], store["rotate_threshold_pct"], now):
                idx = _advance(store, idx, now)
            self.store = store
            return dict(keys[idx])

    # v0.1 name, kept so older callers keep working
    active_key = acquire_key

    def record_usage(self, tokens_used, key_id=None):
        """Call after every successful API response with the real token
        count (prompt + completion). Also counts as one request.
        key_id: the key that served the call (recommended)."""
        with storage.transaction() as store:
            keys = store["keys"]
            active_idx = store["active_index"] % len(keys)
            idx = active_idx if key_id is None else _find(store, key_id)[0]
            key = keys[idx]
            key["tokens_used"] += tokens_used
            key["requests_used"] = key.get("requests_used", 0) + 1

            # Proactively rotate so the *next* call already uses a fresh key,
            # only if this key is still the active one. Never fail THIS
            # (successful) call -- the next acquire_key() raises instead.
            if idx == active_idx and _over_threshold(key, store["rotate_threshold_pct"]):
                try:
                    _advance(store, idx)
                except AllKeysExhausted:
                    pass
            self.store = store

    def _mark(self, key_id, reason, cooldown_s=None, disable=False):
        with storage.transaction() as store:
            keys = store["keys"]
            idx, key = _find(store, key_id)
            now = time.time()
            key["last_error"] = reason[:160]
            if disable:
                key["disabled"] = True
            if cooldown_s is not None:
                key["cooldown_until"] = now + cooldown_s
            # move the cylinder off this key if it was the active one
            if idx == store["active_index"] % len(keys):
                try:
                    _advance(store, idx, now)
                except AllKeysExhausted:
                    pass
            self.store = store

    def mark_cooldown(self, key_id, seconds, reason):
        """Skip this key for `seconds` (rate limit, server error, timeout)."""
        self._mark(key_id, reason, cooldown_s=seconds)

    def mark_disabled(self, key_id, reason):
        """Take this key out of rotation until `revolver enable`."""
        self._mark(key_id, reason, disable=True)

    def enable(self, key_id=None):
        """Clear disabled/cooldown/last_error for one key, or all if None.
        Returns the names of the keys touched."""
        with storage.transaction() as store:
            touched = []
            for k in store["keys"]:
                if key_id is None or k["id"] == key_id:
                    k["disabled"] = False
                    k["cooldown_until"] = 0
                    k["last_error"] = ""
                    touched.append(k["name"])
            self.store = store
            return touched

    def force_rotate(self, from_key_id=None):
        """Rotate away from a key the provider rejected.
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
            threshold = store["rotate_threshold_pct"]
            now = time.time()
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
                    "model": k.get("model", ""),
                    "used": k["tokens_used"],
                    "limit": k["token_limit"],
                    "remaining": max(k["token_limit"] - k["tokens_used"], 0),
                    "pct": _pct_used(k),
                    "req_used": req_used,
                    "req_limit": req_limit,
                    "req_remaining": max(req_limit - req_used, 0),
                    "req_pct": _request_pct_used(k),
                    "period": k["period"],
                    "status": _why_not(k, threshold, now),
                })
            self.store = store
            return rows
